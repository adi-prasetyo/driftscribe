"""Unit tests for the pure resource-map graph builder (Phase 1).

build_graph is pure (no network): it takes the dict workers.infra_reader returns
and reshapes it into the node-only graph DTO the Svelte InfraDiagram renders. The
load-bearing properties: type-aware grouping + friendly labels, per-node
managed/drift flags, COUNTS-ONLY secret groups (no name ever), the degraded
pass-through, sample-cap truncation surfacing, and total-ness (a malformed
inventory degrades instead of raising).
"""
import json
from pathlib import Path

from driftscribe_lib.iac_plan_denylist import DenylistInput, evaluate
from driftscribe_lib.infra_graph import _CONTROL_PLANE_NODE_MATCHERS, build_graph

FIXTURES_DENYLIST = (
    Path(__file__).resolve().parents[1] / "fixtures" / "iac_plan_denylist"
)

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
# drift_adoptable — actionable drift (adoptable, non-control-plane) per group
# --------------------------------------------------------------------------- #


def _by_type_only(entry_atype: str, entry: dict) -> dict:
    """An inventory with a single by_type entry (top-level totals derived)."""
    return _inventory(
        by_type={entry_atype: entry},
        total_resources=entry["count"],
        declared_in_iac=entry["declared_in_iac"],
        not_in_iac=entry["not_in_iac"],
    )


def test_drift_adoptable_excludes_control_plane_on_adoptable_type():
    # 11 unmanaged Cloud Run services, 10 of them DriftScribe's own control plane.
    g = build_graph(_by_type_only(RUN_TYPE, {
        "count": 12, "declared_in_iac": 1, "not_in_iac": 11,
        "not_in_iac_control_plane": 10, "sensitive": False,
        "sample": [{"name": "adopt-probe-svc", "location": "l", "iac": False, "match_confidence": None}],
    }))
    grp = g["groups"][0]
    assert grp["drift"] == 11            # raw not_in_iac unchanged
    assert grp["drift_adoptable"] == 1   # only the one non-control-plane service


def test_drift_adoptable_zero_when_all_drift_is_control_plane():
    g = build_graph(_by_type_only(BUCKET_TYPE, {
        "count": 3, "declared_in_iac": 0, "not_in_iac": 3,
        "not_in_iac_control_plane": 3, "sensitive": False,
        "sample": [{"name": "x-tofu-state", "location": "US", "iac": False, "match_confidence": None}],
    }))
    grp = g["groups"][0]
    assert grp["drift"] == 3
    assert grp["drift_adoptable"] == 0


def test_drift_adoptable_zero_for_non_adoptable_type():
    g = build_graph(_by_type_only("example.googleapis.com/WidgetThing", {
        "count": 5, "declared_in_iac": 0, "not_in_iac": 5,
        "not_in_iac_control_plane": 0, "sensitive": False,
        "sample": [{"name": "w1", "location": "g", "iac": False, "match_confidence": None}],
    }))
    grp = g["groups"][0]
    assert grp["adoptable"] is False
    assert grp["drift"] == 5
    assert grp["drift_adoptable"] == 0   # a non-adoptable type has no actionable drift


def test_drift_adoptable_zero_for_sensitive_type():
    g = build_graph(_inventory())
    secret = next(grp for grp in g["groups"] if grp["asset_type"] == SECRET_TYPE)
    assert secret["drift_adoptable"] == 0


def test_drift_adoptable_falls_back_to_raw_drift_when_field_missing():
    # Stale inventory (pre-field) → over-report (show all as drift), never under-report.
    g = build_graph(_by_type_only(RUN_TYPE, {
        "count": 2, "declared_in_iac": 0, "not_in_iac": 2, "sensitive": False,
        "sample": [{"name": "adopt-probe-svc", "location": "l", "iac": False, "match_confidence": None}],
    }))
    grp = g["groups"][0]
    assert grp["drift_adoptable"] == 2


def test_drift_adoptable_never_negative():
    g = build_graph(_by_type_only(RUN_TYPE, {
        "count": 1, "declared_in_iac": 0, "not_in_iac": 1,
        "not_in_iac_control_plane": 5, "sensitive": False,  # malformed: cp > not_in_iac
        "sample": [{"name": "adopt-probe-svc", "location": "l", "iac": False, "match_confidence": None}],
    }))
    assert g["groups"][0]["drift_adoptable"] == 0


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


SUB_TYPE = "pubsub.googleapis.com/Subscription"


def _sub_inventory(sample_extra: dict) -> dict:
    """A one-subscription inventory whose single sample merges `sample_extra`."""
    return {
        "project": "p", "generated_at": "2026-07-07T00:00:00+00:00",
        "inventory_source": "cloud_asset_inventory", "freshness_caveat": "…",
        "iac_snapshot_sha": "sha", "total_resources": 1, "declared_in_iac": 0,
        "not_in_iac": 1,
        "by_type": {
            SUB_TYPE: {
                "count": 1, "declared_in_iac": 0, "not_in_iac": 1,
                "not_in_iac_control_plane": 0, "sensitive": False,
                "sample": [{"name": "adopt-probe-sub", "location": "global",
                            "iac": False, "match_confidence": None, **sample_extra}],
            }
        },
        "declared_not_found": [], "truncated": {"per_type_sample": 10},
    }


def test_node_carries_topic_when_sample_has_it():
    g = build_graph(_sub_inventory({"topic": "adopt-probe-topic"}))
    node = g["groups"][0]["nodes"][0]
    assert node["topic"] == "adopt-probe-topic"


def test_node_omits_topic_when_sample_lacks_it():
    g = build_graph(_sub_inventory({}))
    assert "topic" not in g["groups"][0]["nodes"][0]


def test_node_omits_topic_when_sample_topic_is_non_string():
    # Type-strict: a non-string (or empty) topic never reaches the client node.
    for bad in (123, "", None, {"x": 1}):
        g = build_graph(_sub_inventory({"topic": bad}))
        assert "topic" not in g["groups"][0]["nodes"][0]


def test_non_subscription_nodes_never_carry_topic():
    g = build_graph(_inventory())
    for grp in g["groups"]:
        for node in grp["nodes"]:
            assert "topic" not in node


def _run_inventory(sample_extra: dict, *, name: str = "adopt-probe-svc") -> dict:
    """A one-run-service inventory whose single sample merges `sample_extra`."""
    return {
        "project": "p", "generated_at": "2026-07-07T00:00:00+00:00",
        "inventory_source": "cloud_asset_inventory", "freshness_caveat": "…",
        "iac_snapshot_sha": "sha", "total_resources": 1, "declared_in_iac": 0,
        "not_in_iac": 1,
        "by_type": {
            RUN_TYPE: {
                "count": 1, "declared_in_iac": 0, "not_in_iac": 1,
                "not_in_iac_control_plane": 0, "sensitive": False,
                "sample": [{"name": name, "location": "asia-northeast1",
                            "iac": False, "match_confidence": None, **sample_extra}],
            }
        },
        "declared_not_found": [], "truncated": {"per_type_sample": 10},
    }


def test_node_carries_image_when_sample_has_it():
    g = build_graph(_run_inventory({"image": "gcr.io/cloudrun/hello"}))
    node = g["groups"][0]["nodes"][0]
    assert node["image"] == "gcr.io/cloudrun/hello"


def test_node_omits_image_when_sample_lacks_it():
    g = build_graph(_run_inventory({}))
    assert "image" not in g["groups"][0]["nodes"][0]


def test_node_omits_image_when_sample_image_is_non_string():
    # Type-strict: a non-string (or empty) image never reaches the client node.
    for bad in (123, "", None, {"x": 1}):
        g = build_graph(_run_inventory({"image": bad}))
        assert "image" not in g["groups"][0]["nodes"][0]


def test_control_plane_node_never_carries_image_even_if_sample_leaks_one():
    # Defense in depth on top of build_inventory's sample-level suppression: even
    # if a sample adversarially carries an image for a control-plane service, the
    # node build suppresses it (and still flags the node control_plane).
    g = build_graph(
        _run_inventory({"image": "gcr.io/p/coordinator"}, name="driftscribe-agent")
    )
    node = g["groups"][0]["nodes"][0]
    assert "image" not in node
    assert node["control_plane"] is True


def test_non_run_nodes_never_carry_image():
    g = build_graph(_inventory())
    for grp in g["groups"]:
        for node in grp["nodes"]:
            assert "image" not in node


def test_non_run_node_never_carries_image_even_if_sample_leaks_one():
    # Type-gated (Codex review): a subscription sample adversarially carrying an
    # image must not emit it at the node layer — defense in depth on top of the
    # reader only ever setting `image` on run rows.
    g = build_graph(_sub_inventory({"image": "gcr.io/p/x"}))
    assert "image" not in g["groups"][0]["nodes"][0]


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


# ---------------------------------------------------------------------------
# Task (ghost-nodes): plan_overlay DTO builder (Decision 3)
# ---------------------------------------------------------------------------

import pytest  # noqa: E402 — local to the append block; pytest is always available

from driftscribe_lib.iac_plan_summary import summarize_plan  # noqa: E402
from driftscribe_lib.infra_graph import (  # noqa: E402
    PLAN_RTYPE_TO_ASSET_TYPE,
    SENSITIVE_PLAN_RTYPES,
    plan_overlay,
    plan_overlay_unavailable,
)
from driftscribe_lib.infra_inventory import SENSITIVE_ASSET_TYPES  # noqa: E402
from driftscribe_lib.iac_hcl import _SUPPORTED_RESOURCE_ASSET_TYPES  # noqa: E402


def _rc_for_overlay(actions, *, rtype="google_pubsub_topic", name="t",
                    address=None, before=None, after=None,
                    b_sens=False, a_sens=False, mode="managed"):
    """Minimal resource_change dict for overlay tests."""
    return {
        "address": address or f"{rtype}.{name}",
        "mode": mode,
        "type": rtype,
        "name": name,
        "change": {
            "actions": list(actions),
            "before": before,
            "after": after,
            "before_sensitive": b_sens,
            "after_sensitive": a_sens,
        },
    }


def _plan_for_overlay(*rcs):
    return {"format_version": "1.2", "resource_changes": list(rcs)}


class TestPlanOverlay:
    def test_shape_and_counts_passthrough(self):
        # 2-entry plan: create topic + update bucket
        plan = _plan_for_overlay(
            _rc_for_overlay(
                ["create"], rtype="google_pubsub_topic", name="topic-rc",
                address="google_pubsub_topic.order_events",
                after={"name": "order-events", "location": "asia-northeast1"},
            ),
            _rc_for_overlay(
                ["update"], rtype="google_storage_bucket", name="bucket-rc",
                address="google_storage_bucket.assets",
                before={"name": "my-bucket"}, after={"name": "my-bucket"},
            ),
        )
        s = summarize_plan(plan)
        assert s is not None
        g = plan_overlay(47, s)

        assert g["pr_number"] == 47
        assert g["available"] is True
        assert g["reason"] is None
        assert g["counts"]["create"] == 1
        assert g["counts"]["update"] == 1
        assert g["hidden"] == 0
        assert len(g["entries"]) == 2

        e0 = g["entries"][0]
        assert e0["verb"] == "create"
        assert e0["rtype"] == "google_pubsub_topic"
        assert e0["type_label"] == "Pub/Sub topic"
        assert e0["name"] == "order-events"
        assert e0["address"] == "google_pubsub_topic.order_events"
        assert e0["asset_type"] == "pubsub.googleapis.com/Topic"
        assert e0["sensitive"] is False
        assert e0["location"] == "asia-northeast1"

        e1 = g["entries"][1]
        assert e1["verb"] == "update"
        assert e1["rtype"] == "google_storage_bucket"
        assert e1["asset_type"] == "storage.googleapis.com/Bucket"
        assert e1["sensitive"] is False

    def test_hidden_reflects_truncation(self):
        # 42 create rows -> entries capped at 40, hidden 2, counts["create"]==42
        rcs = [
            _rc_for_overlay(
                ["create"], rtype="google_pubsub_topic", name=f"t{i}",
                address=f"google_pubsub_topic.t{i}",
                after={"name": f"topic-{i}"},
            )
            for i in range(42)
        ]
        s = summarize_plan(_plan_for_overlay(*rcs))
        assert s is not None
        assert s.n_hidden == 2
        g = plan_overlay(1, s)
        assert len(g["entries"]) == 40
        assert g["hidden"] == 2
        assert g["counts"]["create"] == 42

    @pytest.mark.parametrize("rtype,expect_atype", [
        ("google_secret_manager_secret", "secretmanager.googleapis.com/Secret"),
        ("google_secret_manager_secret_version", "secretmanager.googleapis.com/SecretVersion"),
        ("google_secret_manager_regional_secret", None),
        ("google_secret_manager_regional_secret_version", None),
    ])
    def test_sensitive_rtypes_fully_redacted(self, rtype, expect_atype):
        plan = _plan_for_overlay(
            _rc_for_overlay(
                ["create"], rtype=rtype, name="s",
                address=f"{rtype}.s",
                after={"name": "my-secret", "location": "asia-northeast1"},
            ),
        )
        s = summarize_plan(plan)
        assert s is not None
        g = plan_overlay(7, s)
        assert len(g["entries"]) == 1
        e = g["entries"][0]
        assert e["sensitive"] is True
        assert e["name"] == ""
        assert e["address"] == ""
        assert e["location"] == ""
        assert e["asset_type"] == expect_atype

    def test_unmapped_rtype_gets_null_asset_type(self):
        plan = _plan_for_overlay(
            _rc_for_overlay(
                ["create"], rtype="google_project_iam_member", name="iam",
                address="google_project_iam_member.iam",
                after={"member": "serviceAccount:x@y.iam.gserviceaccount.com"},
            ),
        )
        s = summarize_plan(plan)
        assert s is not None
        g = plan_overlay(5, s)
        assert len(g["entries"]) == 1
        e = g["entries"][0]
        assert e["asset_type"] is None
        assert e["sensitive"] is False

    def test_unavailable_shape(self):
        g = plan_overlay_unavailable(7, "no_plan")
        assert g["available"] is False
        assert g["reason"] == "no_plan"
        assert g["pr_number"] == 7
        assert g["hidden"] == 0
        assert g["entries"] == []
        # all verbs present with zero counts
        for verb in ("create", "update", "destroy", "replace", "import", "forget", "change"):
            assert g["counts"][verb] == 0

    def test_resource_name_fixture_per_identity_rtype(self):
        # One create row per identity-resolver rtype + one with no name
        cases = [
            ("google_storage_bucket", "my-assets-bucket"),
            ("google_pubsub_topic", "order-events"),
            ("google_pubsub_subscription", "order-sub"),
            ("google_cloud_run_v2_service", "storefront"),
            ("google_service_account",
             "projects/p/serviceAccounts/worker@p.iam.gserviceaccount.com"),
        ]
        rcs = [
            _rc_for_overlay(
                ["create"], rtype=rt, name="rc",
                address=f"{rt}.rc",
                after={"name": expected_name},
            )
            for rt, expected_name in cases
        ]
        # One more with no "name" key in after
        rcs.append(_rc_for_overlay(
            ["create"], rtype="google_compute_network", name="vpc",
            address="google_compute_network.vpc",
            after={"auto_create_subnetworks": True},
        ))
        s = summarize_plan(_plan_for_overlay(*rcs))
        assert s is not None
        g = plan_overlay(9, s)

        for i, (rt, expected_name) in enumerate(cases):
            e = g["entries"][i]
            assert e["name"] == expected_name, f"rtype={rt}: got {e['name']!r}"

        # Last entry: no "name" in after -> resource_name="" -> overlay name=""
        last_e = g["entries"][len(cases)]
        assert last_e["name"] == ""
        assert last_e["address"] == "google_compute_network.vpc"


class TestRtypeMapping:
    def test_iac_hcl_pairs_match(self):
        for rtype, atype in _SUPPORTED_RESOURCE_ASSET_TYPES.items():
            assert PLAN_RTYPE_TO_ASSET_TYPE[rtype] == atype, (
                f"{rtype}: expected {atype!r} "
                f"but got {PLAN_RTYPE_TO_ASSET_TYPE.get(rtype)!r}"
            )

    def test_secret_rtypes_map_to_sensitive_asset_types(self):
        for rtype in ("google_secret_manager_secret",
                      "google_secret_manager_secret_version"):
            atype = PLAN_RTYPE_TO_ASSET_TYPE[rtype]
            assert atype in SENSITIVE_ASSET_TYPES, (
                f"{rtype} -> {atype!r} not in SENSITIVE_ASSET_TYPES"
            )

    def test_sensitive_plan_rtypes_cover_static_gate(self):
        from tools import iac_static_gate
        assert SENSITIVE_PLAN_RTYPES >= iac_static_gate.SECRET_MATERIAL_RESOURCE_TYPES


# ---------------------------------------------------------------------------
# Task (adopt-button-ui Phase 4): the per-group `adoptable` flag (design §6).
# A drift node whose group is adoptable gets an "Adopt into IaC" affordance in
# the map panel; the server is the single source of truth for adoptability —
# `ADOPTABLE_ASSET_TYPES` is COMPUTED from the denylist's adoptable HCL types
# (mapped through PLAN_RTYPE_TO_ASSET_TYPE), never hand-listed, so a denylist
# allowlist change propagates here automatically.
# ---------------------------------------------------------------------------

from driftscribe_lib.infra_graph import ADOPTABLE_ASSET_TYPES  # noqa: E402

TOPIC_TYPE = "pubsub.googleapis.com/Topic"
SUB_TYPE = "pubsub.googleapis.com/Subscription"
SA_TYPE = "iam.googleapis.com/ServiceAccount"


class TestAdoptableFlag:
    def test_adoptable_asset_types_drift_pin(self):
        # Resolved set must be EXACTLY the four adoptable types' CAI mappings —
        # catches a denylist-side ADOPTABLE_RESOURCE_TYPES change that the map
        # forgets to honor (the set is computed, not hand-listed).
        assert ADOPTABLE_ASSET_TYPES == frozenset({
            BUCKET_TYPE, TOPIC_TYPE, SUB_TYPE, RUN_TYPE,
        })

    def test_adoptable_groups_for_the_four_types(self):
        inv = _inventory(
            by_type={
                t: {
                    "count": 1, "declared_in_iac": 0, "not_in_iac": 1,
                    "sensitive": False,
                    "sample": [{"name": "n", "location": "g", "iac": False,
                                "match_confidence": None}],
                }
                for t in (BUCKET_TYPE, TOPIC_TYPE, SUB_TYPE, RUN_TYPE)
            }
        )
        g = build_graph(inv)
        by_atype = {grp["asset_type"]: grp["adoptable"] for grp in g["groups"]}
        assert by_atype == {
            BUCKET_TYPE: True, TOPIC_TYPE: True, SUB_TYPE: True, RUN_TYPE: True,
        }

    def test_non_adoptable_type_is_false(self):
        inv = _inventory(
            by_type={
                SA_TYPE: {
                    "count": 1, "declared_in_iac": 0, "not_in_iac": 1,
                    "sensitive": False,
                    "sample": [{"name": "ci-runner@p.iam.gserviceaccount.com",
                                "location": "g", "iac": False,
                                "match_confidence": None}],
                },
            }
        )
        g = build_graph(inv)
        assert g["groups"][0]["adoptable"] is False

    def test_sensitive_group_is_never_adoptable_even_if_type_were(self):
        # A SENSITIVE group is counts-only (no names) — it can never carry an
        # Adopt affordance, regardless of its underlying type. Force a bucket
        # type (adoptable) into the sensitive branch via the flag and confirm
        # the `and not sensitive` clause overrides the type membership.
        inv = _inventory(
            by_type={
                BUCKET_TYPE: {
                    "count": 1, "declared_in_iac": 0, "not_in_iac": 1,
                    "sensitive": True,  # forced sensitive
                },
            }
        )
        g = build_graph(inv)
        grp = g["groups"][0]
        assert grp["sensitive"] is True
        assert grp["adoptable"] is False

    def test_every_group_carries_the_adoptable_field(self):
        g = build_graph(_inventory())
        for grp in g["groups"]:
            assert "adoptable" in grp
            assert isinstance(grp["adoptable"], bool)
        by_atype = {grp["asset_type"]: grp["adoptable"] for grp in g["groups"]}
        # The default fixture: Cloud Run + bucket adoptable, secret group not.
        assert by_atype[RUN_TYPE] is True
        assert by_atype[BUCKET_TYPE] is True
        assert by_atype[SECRET_TYPE] is False  # sensitive counts-only group


# ---------------------------------------------------------------------------
# Item 10 (guided adoption order): deterministic "what to adopt first" ranking.
# Single source of truth, drift-pinned to the adoptable set — a new adoptable
# type CANNOT ship without a rank, a hint, and a plural label.
# ---------------------------------------------------------------------------

from driftscribe_lib.infra_graph import (  # noqa: E402
    _ADOPTION_PLURAL_LABELS,
    ADOPTION_GUIDE,
    ADOPTION_ORDER_HONESTY,
    adoption_order_sentence,
)


class TestAdoptionGuide:
    def test_guide_keys_are_exactly_the_adoptable_asset_types(self):
        assert set(ADOPTION_GUIDE) == set(ADOPTABLE_ASSET_TYPES)

    def test_plural_labels_keys_match_the_guide(self):
        assert set(_ADOPTION_PLURAL_LABELS) == set(ADOPTION_GUIDE)

    def test_ranks_are_unique_and_contiguous_from_1(self):
        ranks = sorted(rank for rank, _ in ADOPTION_GUIDE.values())
        assert ranks == list(range(1, len(ADOPTION_GUIDE) + 1))

    def test_hints_are_nonempty_and_never_safety_framed(self):
        # Honesty constraint (Codex must-fix 1): hints guide review comfort,
        # never imply one type is safer/riskier to adopt.
        for rank, hint in ADOPTION_GUIDE.values():
            assert hint and hint == hint.strip()
            lowered = hint.lower()
            for banned in ("risk", "danger", "blast", "safe"):
                assert banned not in lowered

    def test_order_sentence_is_derived_from_rank_order(self):
        assert adoption_order_sentence() == (
            "Storage buckets → Pub/Sub topics → Pub/Sub subscriptions → Cloud Run services"
        )

    def test_bucket_is_rank_1_and_run_service_is_last(self):
        assert ADOPTION_GUIDE[BUCKET_TYPE][0] == 1
        assert ADOPTION_GUIDE[RUN_TYPE][0] == len(ADOPTION_GUIDE)

    def test_honesty_note_says_zero_change_and_not_safety(self):
        # The load-bearing phrases every surface pins against.
        assert "same zero-change import" in ADOPTION_ORDER_HONESTY
        assert "not safety" in ADOPTION_ORDER_HONESTY


class TestAdoptRankInGraph:
    def _one_drift_group(self, atype: str) -> dict:
        return {
            atype: {
                "count": 1, "declared_in_iac": 0, "not_in_iac": 1,
                "sensitive": False,
                "sample": [{"name": "n", "location": "g", "iac": False,
                            "match_confidence": None}],
            }
        }

    def test_adoptable_group_carries_rank_and_hint(self):
        g = build_graph(_inventory(by_type=self._one_drift_group(BUCKET_TYPE)))
        grp = g["groups"][0]
        assert grp["adoptable"] is True
        assert grp["adopt_rank"] == 1
        assert grp["adopt_hint"] == ADOPTION_GUIDE[BUCKET_TYPE][1]

    def test_all_four_adoptable_types_carry_their_guide_rank(self):
        by_type = {}
        for t in (BUCKET_TYPE, TOPIC_TYPE, SUB_TYPE, RUN_TYPE):
            by_type.update(self._one_drift_group(t))
        g = build_graph(_inventory(by_type=by_type))
        got = {grp["asset_type"]: grp["adopt_rank"] for grp in g["groups"]}
        assert got == {t: ADOPTION_GUIDE[t][0]
                       for t in (BUCKET_TYPE, TOPIC_TYPE, SUB_TYPE, RUN_TYPE)}

    def test_non_adoptable_group_omits_rank_and_hint(self):
        # Omitted (not None) — mirrors the truncated_in_group convention.
        g = build_graph(_inventory(by_type=self._one_drift_group(SA_TYPE)))
        grp = g["groups"][0]
        assert grp["adoptable"] is False
        assert "adopt_rank" not in grp and "adopt_hint" not in grp

    def test_sensitive_group_omits_rank_and_hint(self):
        # adoptable is forced False on sensitive groups; rank must follow it.
        by_type = self._one_drift_group(BUCKET_TYPE)
        by_type[BUCKET_TYPE]["sensitive"] = True
        g = build_graph(_inventory(by_type=by_type))
        grp = g["groups"][0]
        assert grp["adoptable"] is False
        assert "adopt_rank" not in grp and "adopt_hint" not in grp


# --------------------------------------------------------------------------- #
# Control-plane adopt suppression (2026-06-12 ranking-filter follow-up to the
# item-14 tour): nodes whose identity the denylist's control-plane rules would
# refuse to import carry `control_plane: True` so adopt surfaces suppress the
# guaranteed-dead-end CTA. PARITY: same public constants, same semantics as
# the denylist; the parity test below drives both libraries with the same
# identity and asserts flag ⟺ import-blocked.
# --------------------------------------------------------------------------- #


def _one_node_inventory(atype: str, name: str) -> dict:
    return _inventory(
        total_resources=1,
        declared_in_iac=0,
        not_in_iac=1,
        by_type={
            atype: {
                "count": 1,
                "declared_in_iac": 0,
                "not_in_iac": 1,
                "sensitive": False,
                "sample": [
                    {"name": name, "location": "asia-northeast1", "iac": False,
                     "match_confidence": None},
                ],
            },
        },
    )


def _single_import_plan(rtype: str, attrs: dict) -> dict:
    """A minimal plan.json: ONE pure (no-op) import of the given identity."""
    return {
        "format_version": "1.2",
        "resource_changes": [
            {
                "address": f"{rtype}.adopt",
                "type": rtype,
                "name": "adopt",
                "change": {
                    "actions": ["no-op"],
                    "before": attrs,
                    "after": attrs,
                    "importing": {"id": "whatever"},
                },
            },
        ],
    }


class TestControlPlaneNodeFlag:
    def test_protected_bucket_node_is_flagged(self):
        g = build_graph(_one_node_inventory(BUCKET_TYPE, "acme-prod-tofu-artifacts"))
        assert g["groups"][0]["nodes"][0]["control_plane"] is True

    def test_state_bucket_suffix_also_flagged(self):
        g = build_graph(_one_node_inventory(BUCKET_TYPE, "acme-prod-tofu-state"))
        assert g["groups"][0]["nodes"][0]["control_plane"] is True

    def test_ordinary_bucket_node_carries_no_key(self):
        # Only-when-true (truncated_in_group style): non-control-plane graphs
        # stay byte-identical to the pre-flag era.
        g = build_graph(_one_node_inventory(BUCKET_TYPE, "acme-assets"))
        assert "control_plane" not in g["groups"][0]["nodes"][0]

    def test_service_managed_cloudbuild_bucket_is_flagged(self):
        # The original homepage-tour papercut: <project>_cloudbuild is Cloud
        # Build's auto-created staging bucket — flagged so its CTA is suppressed.
        g = build_graph(_one_node_inventory(BUCKET_TYPE, "acme-project_cloudbuild"))
        assert g["groups"][0]["nodes"][0]["control_plane"] is True

    def test_service_managed_prefix_bucket_is_flagged(self):
        g = build_graph(
            _one_node_inventory(BUCKET_TYPE, "gcf-v2-sources-12345-asia-northeast1")
        )
        assert g["groups"][0]["nodes"][0]["control_plane"] is True

    def test_gcf_v2_near_miss_bucket_carries_no_key(self):
        # Codex #1: the prefix set is the specific source/upload families, not a
        # bare "gcf-v2-", so an operator bucket like gcf-v2-assets stays adoptable.
        g = build_graph(_one_node_inventory(BUCKET_TYPE, "gcf-v2-assets"))
        assert "control_plane" not in g["groups"][0]["nodes"][0]

    def test_service_managed_bucket_flagged_through_full_cai_normalization(self):
        # Codex #3: the denylist matches the provider `name` attribute while the
        # graph matches the CAI-derived bare label. Pin they agree for buckets by
        # flowing a real CAI bucket resource name — the dotted .appspot.com case,
        # the trickiest for rsplit-based normalization — through build_inventory
        # into build_graph and asserting the flag survives.
        from driftscribe_lib.infra_inventory import CaiResource, build_inventory

        inv = build_inventory(
            [
                CaiResource(
                    name="//storage.googleapis.com/staging.my-project.appspot.com",
                    asset_type=BUCKET_TYPE,
                    location="asia-northeast1",
                )
            ],
            [],
            project="my-project",
            iac_snapshot_sha="sha1",
        )
        node = build_graph(inv)["groups"][0]["nodes"][0]
        assert node["label"] == "staging.my-project.appspot.com"
        assert node["control_plane"] is True

    def test_control_plane_service_node_is_flagged(self):
        g = build_graph(_one_node_inventory(RUN_TYPE, "driftscribe-agent"))
        assert g["groups"][0]["nodes"][0]["control_plane"] is True

    def test_workload_service_node_carries_no_key(self):
        g = build_graph(_one_node_inventory(RUN_TYPE, "storefront"))
        assert "control_plane" not in g["groups"][0]["nodes"][0]

    def test_type_scoped_a_topic_named_like_a_service_is_not_flagged(self):
        # Name collisions across types must not flag: Pub/Sub's only identity
        # rule is the eventarc- transport prefix, so a topic named
        # "driftscribe-agent" is adoptable and its import is admitted.
        g = build_graph(_one_node_inventory(TOPIC_TYPE, "driftscribe-agent"))
        assert "control_plane" not in g["groups"][0]["nodes"][0]

    def test_matchers_cover_only_adoptable_types(self):
        # The flag exists to suppress adopt CTAs; a matcher on a non-adoptable
        # type would be dead code. All four adoptable types now carry an
        # identity rule (Pub/Sub's is the eventarc- transport prefix).
        assert set(_CONTROL_PLANE_NODE_MATCHERS) == {
            "storage.googleapis.com/Bucket",
            "run.googleapis.com/Service",
            "pubsub.googleapis.com/Topic",
            "pubsub.googleapis.com/Subscription",
        }
        assert set(_CONTROL_PLANE_NODE_MATCHERS) <= ADOPTABLE_ASSET_TYPES

    @pytest.mark.parametrize(
        ("atype", "rtype", "attrs", "name", "expect_blocked"),
        [
            ("storage.googleapis.com/Bucket", "google_storage_bucket",
             {"name": "acme-prod-tofu-artifacts"}, "acme-prod-tofu-artifacts", True),
            ("storage.googleapis.com/Bucket", "google_storage_bucket",
             {"name": "acme-prod-tofu-state"}, "acme-prod-tofu-state", True),
            ("storage.googleapis.com/Bucket", "google_storage_bucket",
             {"name": "acme-assets"}, "acme-assets", False),
            # service-managed buckets: flagged AND blocked, same as control-plane
            ("storage.googleapis.com/Bucket", "google_storage_bucket",
             {"name": "acme-project_cloudbuild"}, "acme-project_cloudbuild", True),
            ("storage.googleapis.com/Bucket", "google_storage_bucket",
             {"name": "gcf-v2-sources-12345-asia-northeast1"},
             "gcf-v2-sources-12345-asia-northeast1", True),
            # tightened-prefix near-miss (Codex #1): a legitimate operator bucket
            # that merely starts with "gcf-v2-" must be neither flagged nor blocked
            ("storage.googleapis.com/Bucket", "google_storage_bucket",
             {"name": "gcf-v2-assets"}, "gcf-v2-assets", False),
            ("run.googleapis.com/Service", "google_cloud_run_v2_service",
             {"name": "driftscribe-agent"}, "driftscribe-agent", True),
            ("run.googleapis.com/Service", "google_cloud_run_v2_service",
             {"name": "storefront"}, "storefront", False),
            # Eventarc trigger transport: flagged AND blocked, both types
            ("pubsub.googleapis.com/Topic", "google_pubsub_topic",
             {"name": "eventarc-asia-northeast1-driftscribe-cloudrun-changes-823"},
             "eventarc-asia-northeast1-driftscribe-cloudrun-changes-823", True),
            ("pubsub.googleapis.com/Subscription", "google_pubsub_subscription",
             {"name": "eventarc-asia-northeast1-driftscribe-cloudrun-changes-sub-019"},
             "eventarc-asia-northeast1-driftscribe-cloudrun-changes-sub-019", True),
            ("pubsub.googleapis.com/Topic", "google_pubsub_topic",
             {"name": "adopt-probe-topic"}, "adopt-probe-topic", False),
            ("pubsub.googleapis.com/Subscription", "google_pubsub_subscription",
             {"name": "orders-sub"}, "orders-sub", False),
        ],
    )
    def test_flag_parity_with_denylist_import_admission(
        self, atype, rtype, attrs, name, expect_blocked
    ):
        # THE invariant this feature rests on: the node is flagged exactly when
        # a pure single import of that identity is denylist-blocked by an
        # identity rule (control-plane OR service-managed). Drives both
        # libraries end-to-end via their public surfaces.
        g = build_graph(_one_node_inventory(atype, name))
        flagged = g["groups"][0]["nodes"][0].get("control_plane") is True

        violations = evaluate(DenylistInput(plan=_single_import_plan(rtype, attrs)))
        blocked = any(
            v.rule.startswith("control-plane-")
            or v.rule.startswith("service-managed-")
            for v in violations
        )

        assert flagged is expect_blocked
        assert blocked is expect_blocked
        # A pure import of a NON-control-plane adoptable identity must be
        # fully admitted — no other rule may fire either.
        if not expect_blocked:
            assert violations == []

    @pytest.mark.parametrize(
        ("fixture", "atype", "expect_blocked"),
        [
            # REAL provider-generated plans (Codex 019eb932): better pins of
            # provider attribute shape than the synthetic ones above.
            ("import_control_plane_state_bucket.json",
             "storage.googleapis.com/Bucket", True),
            ("import_service_managed_bucket.json",
             "storage.googleapis.com/Bucket", True),
            ("real_import_bucket_pure_noop.json",
             "storage.googleapis.com/Bucket", False),
            ("real_import_run_pure_noop.json",
             "run.googleapis.com/Service", False),
        ],
    )
    def test_flag_parity_on_real_import_fixtures(self, fixture, atype, expect_blocked):
        plan = json.loads(
            (FIXTURES_DENYLIST / fixture).read_text(encoding="utf-8")
        )
        # Identity name as the provider emitted it — the graph node label for
        # the same live resource (infra_inventory uses the short name).
        rc = plan["resource_changes"][0]
        name = rc["change"]["after"]["name"]

        g = build_graph(_one_node_inventory(atype, name))
        flagged = g["groups"][0]["nodes"][0].get("control_plane") is True
        blocked = any(
            v.rule.startswith("control-plane-") or v.rule == "service-managed-bucket"
            for v in evaluate(DenylistInput(plan=plan))
        )
        assert flagged is expect_blocked
        assert blocked is expect_blocked

    def test_managed_control_plane_node_still_flagged(self):
        # The flag describes IDENTITY, not adoptability — a (hypothetically)
        # already-managed control-plane node keeps it; clients only consult it
        # on unmanaged rows anyway.
        inv = _one_node_inventory(BUCKET_TYPE, "acme-prod-tofu-state")
        inv["by_type"][BUCKET_TYPE]["sample"][0]["iac"] = True
        inv["by_type"][BUCKET_TYPE]["declared_in_iac"] = 1
        inv["by_type"][BUCKET_TYPE]["not_in_iac"] = 0
        g = build_graph(inv)
        assert g["groups"][0]["nodes"][0]["control_plane"] is True
