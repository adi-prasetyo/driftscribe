"""Unit tests for the pure resource-map graph builder (Phase 1).

build_graph is pure (no network): it takes the dict workers.infra_reader returns
and reshapes it into the node-only graph DTO the Svelte InfraDiagram renders. The
load-bearing properties: type-aware grouping + friendly labels, per-node
managed/drift flags, COUNTS-ONLY secret groups (no name ever), the degraded
pass-through, sample-cap truncation surfacing, and total-ness (a malformed
inventory degrades instead of raising).
"""
import json

from driftscribe_lib.infra_graph import build_graph

RUN_TYPE = "run.googleapis.com/Service"
BUCKET_TYPE = "storage.googleapis.com/Bucket"
SECRET_TYPE = "secretmanager.googleapis.com/Secret"


def _inventory(**overrides) -> dict:
    """A realistic build_inventory-shaped dict: 2 Cloud Run (1 managed, 1 drift),
    1 drift bucket, and a counts-only secret group."""
    inv = {
        "project": "p",
        "generated_at": "2026-06-03T00:00:00+00:00",
        "inventory_source": "cloud_asset_inventory",
        "freshness_caveat": "CAI is eventually consistent…",
        "iac_snapshot_sha": "deadbeef",
        "total_resources": 4,
        "declared_in_iac": 1,
        "not_in_iac": 3,
        "by_type": {
            RUN_TYPE: {
                "count": 2,
                "declared_in_iac": 1,
                "not_in_iac": 1,
                "sensitive": False,
                "sample": [
                    {"name": "payment-demo", "location": "asia-northeast1", "iac": True, "match_confidence": "high"},
                    {"name": "storefront", "location": "asia-northeast1", "iac": False, "match_confidence": None},
                ],
            },
            BUCKET_TYPE: {
                "count": 1,
                "declared_in_iac": 0,
                "not_in_iac": 1,
                "sensitive": False,
                "sample": [
                    {"name": "assets", "location": "ASIA-NORTHEAST1", "iac": False, "match_confidence": None},
                ],
            },
            SECRET_TYPE: {
                "count": 1,
                "declared_in_iac": 0,
                "not_in_iac": 1,
                "sensitive": True,
            },
        },
        "declared_not_found": [],
        "truncated": {"per_type_sample": 10},
    }
    inv.update(overrides)
    return inv


# --------------------------------------------------------------------------- #
# Shape + grouping + labels
# --------------------------------------------------------------------------- #


def test_top_level_shape_and_passthrough():
    g = build_graph(_inventory())
    assert g["degraded"] is False
    assert g["degraded_reason"] is None
    assert g["edges"] == []  # Phase 1 is node-only
    assert g["project"] == "p"
    assert g["generated_at"] == "2026-06-03T00:00:00+00:00"
    assert g["iac_snapshot_sha"] == "deadbeef"
    assert g["caveat"] == "CAI is eventually consistent…"
    assert g["truncated"] == {"per_type_sample": 10}


def test_totals_come_from_inventory_top_level():
    g = build_graph(_inventory())
    assert g["totals"] == {"resources": 4, "managed": 1, "drift": 3}


def test_groups_sorted_by_asset_type_with_friendly_labels():
    g = build_graph(_inventory())
    labels = [(grp["asset_type"], grp["label"]) for grp in g["groups"]]
    # sorted() over the three asset types: run < secretmanager < storage
    assert labels == [
        (RUN_TYPE, "Cloud Run service"),
        (SECRET_TYPE, "Secret"),
        (BUCKET_TYPE, "Storage bucket"),
    ]


def test_unknown_asset_type_label_is_humanized():
    inv = _inventory(
        by_type={
            "secretmanager.googleapis.com/SecretVersion": {
                "count": 2, "declared_in_iac": 0, "not_in_iac": 2, "sensitive": True,
            },
            "example.googleapis.com/WidgetThing": {
                "count": 1, "declared_in_iac": 0, "not_in_iac": 1, "sensitive": False,
                "sample": [{"name": "w1", "location": "g", "iac": False, "match_confidence": None}],
            },
        }
    )
    g = build_graph(inv)
    by_atype = {grp["asset_type"]: grp["label"] for grp in g["groups"]}
    # known sensitive type keeps its curated label; unknown type is CamelCase-spaced
    assert by_atype["secretmanager.googleapis.com/SecretVersion"] == "Secret version"
    assert by_atype["example.googleapis.com/WidgetThing"] == "Widget Thing"


# --------------------------------------------------------------------------- #
# Node managed/drift flags + ids
# --------------------------------------------------------------------------- #


def test_nodes_carry_managed_flag_and_stable_ids():
    g = build_graph(_inventory())
    run = next(grp for grp in g["groups"] if grp["asset_type"] == RUN_TYPE)
    assert [n["label"] for n in run["nodes"]] == ["payment-demo", "storefront"]
    assert [n["managed"] for n in run["nodes"]] == [True, False]
    assert [n["id"] for n in run["nodes"]] == ["g0n0", "g0n1"]
    assert run["nodes"][0]["location"] == "asia-northeast1"
    assert run["nodes"][0]["asset_type"] == RUN_TYPE


def test_group_rollup_counts():
    g = build_graph(_inventory())
    run = next(grp for grp in g["groups"] if grp["asset_type"] == RUN_TYPE)
    assert (run["count"], run["managed"], run["drift"]) == (2, 1, 1)
    assert run["sensitive"] is False


# --------------------------------------------------------------------------- #
# Secret / sensitive groups: COUNTS-ONLY, never a node, never a name
# --------------------------------------------------------------------------- #


def test_sensitive_group_is_counts_only_no_nodes():
    g = build_graph(_inventory())
    secret = next(grp for grp in g["groups"] if grp["asset_type"] == SECRET_TYPE)
    assert secret["sensitive"] is True
    assert secret["nodes"] == []
    assert secret["count"] == 1


def test_sensitive_group_drops_nodes_even_if_inventory_leaks_a_sample():
    """Defense in depth: build_inventory never emits `sample` for a sensitive
    type, but if a malformed inventory did, build_graph must STILL emit no node
    and the planted secret name must not appear anywhere in the payload."""
    leaked = "ghp_supersecret_planted_value"
    inv = _inventory(
        by_type={
            SECRET_TYPE: {
                "count": 1, "declared_in_iac": 0, "not_in_iac": 1,
                # `sensitive` flag MISSING + a planted sample → still must be dropped.
                "sample": [{"name": leaked, "location": "g", "iac": False, "match_confidence": None}],
            },
        }
    )
    g = build_graph(inv)
    secret = g["groups"][0]
    assert secret["sensitive"] is True  # forced by the asset-type denylist
    assert secret["nodes"] == []
    assert leaked not in json.dumps(g)


# --------------------------------------------------------------------------- #
# Sample-cap truncation
# --------------------------------------------------------------------------- #


def test_truncated_in_group_when_count_exceeds_sample():
    inv = _inventory(
        by_type={
            RUN_TYPE: {
                "count": 12,  # 12 live, only 2 sampled
                "declared_in_iac": 0,
                "not_in_iac": 12,
                "sensitive": False,
                "sample": [
                    {"name": "a", "location": "g", "iac": False, "match_confidence": None},
                    {"name": "b", "location": "g", "iac": False, "match_confidence": None},
                ],
            },
        }
    )
    g = build_graph(inv)
    grp = g["groups"][0]
    assert len(grp["nodes"]) == 2
    assert grp["truncated_in_group"] == 10


def test_no_truncation_key_when_all_shown():
    g = build_graph(_inventory())
    run = next(grp for grp in g["groups"] if grp["asset_type"] == RUN_TYPE)
    assert "truncated_in_group" not in run


# --------------------------------------------------------------------------- #
# Degraded + malformed (total-ness)
# --------------------------------------------------------------------------- #


def test_degraded_passthrough_on_cloud_asset_unavailable():
    g = build_graph({"error": "cloud_asset_unavailable", "detail": "no perms", "project": "p"})
    assert g["degraded"] is True
    assert g["degraded_reason"] == "cloud_asset_unavailable"
    assert g["detail"] == "no perms"
    assert g["project"] == "p"
    assert g["groups"] == []
    assert g["edges"] == []
    assert g["totals"] == {"resources": 0, "managed": 0, "drift": 0}


def test_non_dict_inventory_degrades_without_raising():
    for bad in (None, [], "nope", 42):
        g = build_graph(bad)  # type: ignore[arg-type]
        assert g["degraded"] is True
        assert g["degraded_reason"] == "malformed_inventory"
        assert g["groups"] == []


def test_missing_by_type_yields_empty_groups_not_error():
    g = build_graph({"project": "p", "total_resources": 0})
    assert g["degraded"] is False
    assert g["groups"] == []
    assert g["totals"]["resources"] == 0


def test_garbage_entries_are_skipped_not_fatal():
    inv = {
        "by_type": {
            RUN_TYPE: "not-a-dict",  # skipped
            BUCKET_TYPE: {"count": "oops", "sample": "also-bad"},  # coerced/empty
        }
    }
    g = build_graph(inv)
    assert g["degraded"] is False
    # the non-dict entry is skipped; the bad-count bucket survives with count 0
    atypes = {grp["asset_type"] for grp in g["groups"]}
    assert atypes == {BUCKET_TYPE}
    assert g["groups"][0]["count"] == 0
    assert g["groups"][0]["nodes"] == []


def test_null_sample_name_renders_empty_not_literal_none():
    """A present-but-None name (malformed payload) must become '' — never the
    literal string 'None' (dict.get's default only fires on a MISSING key)."""
    inv = _inventory(
        by_type={
            RUN_TYPE: {
                "count": 1, "declared_in_iac": 0, "not_in_iac": 1, "sensitive": False,
                "sample": [{"name": None, "location": "g", "iac": False, "match_confidence": None}],
            },
        }
    )
    g = build_graph(inv)
    assert g["groups"][0]["nodes"][0]["label"] == ""


def test_non_string_by_type_keys_do_not_raise():
    """Direct (non-HTTP) callers could pass a non-string asset_type key; the
    'never raises' contract requires sorted()/labeling to tolerate it."""
    g = build_graph({"by_type": {123: {"count": 1, "sensitive": False}}})
    assert g["degraded"] is False
    assert g["groups"][0]["asset_type"] == "123"


def test_declared_set_status_passthrough():
    g = build_graph(_inventory(declared_set_status="parse_error"))
    assert g["declared_set_status"] == "parse_error"


def test_caveat_falls_back_when_absent():
    inv = _inventory()
    del inv["freshness_caveat"]
    g = build_graph(inv)
    assert "Cloud Asset Inventory" in g["caveat"]
