"""Unit tests for the pure inventory-summary builder (Task 3).

Each Step-1 checklist case is its own test function. The builder is pure (no
network): we hand it CaiResource records + a DeclaredIdentity set and assert the
bounded summary shape, type-aware matching, confidence carry-through,
sensitive-type counts-only, sample capping, and conditioned declared_not_found
reason codes + identity redaction.
"""
from driftscribe_lib.iac_hcl import DeclaredIdentity
from driftscribe_lib.infra_inventory import (
    SENSITIVE_ASSET_TYPES,
    CaiResource,
    build_inventory,
    normalize_cai_name,
)

RUN_TYPE = "run.googleapis.com/Service"
PD_NAME = "projects/p/locations/l/services/payment-demo"
PD_CAI = "//run.googleapis.com/projects/p/locations/l/services/payment-demo"
SECRET_TYPE = "secretmanager.googleapis.com/Secret"


def _high(identity=PD_NAME, asset_type=RUN_TYPE, address="google_cloud_run_v2_service.payment_demo"):
    return DeclaredIdentity(
        identity=identity, address=address, source="import_id",
        confidence="high", asset_type=asset_type,
    )


def test_normalize_cai_name_strips_scheme_prefix():
    assert normalize_cai_name(
        "//run.googleapis.com/projects/p/locations/l/services/s"
    ) == "projects/p/locations/l/services/s"


def test_normalize_cai_name_passthrough_when_no_scheme():
    assert normalize_cai_name("projects/p/locations/l/services/s") == \
        "projects/p/locations/l/services/s"


def test_matching_high_identity_marks_sample_iac_true_and_rolls_up_declared():
    res = [CaiResource(name=PD_CAI, asset_type=RUN_TYPE, location="l")]
    out = build_inventory(
        res, [_high()], project="p", iac_snapshot_sha="sha1",
    )
    assert out["declared_in_iac"] == 1
    entry = out["by_type"][RUN_TYPE]
    assert entry["declared_in_iac"] == 1
    sample = entry["sample"][0]
    assert sample["iac"] is True
    assert sample["match_confidence"] == "high"


def test_non_matching_resource_is_iac_false_and_rolls_into_not_in_iac():
    res = [CaiResource(
        name="//run.googleapis.com/projects/p/locations/l/services/other",
        asset_type=RUN_TYPE, location="l",
    )]
    out = build_inventory(res, [_high()], project="p", iac_snapshot_sha="sha1")
    assert out["not_in_iac"] == 1
    assert out["declared_in_iac"] == 0
    sample = out["by_type"][RUN_TYPE]["sample"][0]
    assert sample["iac"] is False
    assert sample["match_confidence"] is None


def test_type_aware_non_force_match_for_unsupported_import():
    # A declared identity whose asset_type is None (unsupported import) shares
    # the SAME identity string as a live resource. It must NOT force-match: the
    # live resource stays iac=False, and the declaration lands in
    # declared_not_found with possible_causes=["asset_type_not_supported"].
    unsupported = DeclaredIdentity(
        identity=PD_NAME, address="google_storage_bucket.b",
        source="import_id", confidence="high", asset_type=None,
    )
    res = [CaiResource(name=PD_CAI, asset_type=RUN_TYPE, location="l")]
    out = build_inventory(res, [unsupported], project="p", iac_snapshot_sha="sha1")
    assert out["declared_in_iac"] == 0
    assert out["by_type"][RUN_TYPE]["sample"][0]["iac"] is False
    dnf = out["declared_not_found"]
    assert len(dnf) == 1
    assert dnf[0]["possible_causes"] == ["asset_type_not_supported"]


def test_unresolved_identity_declared_not_found_has_identity_unresolved_cause():
    decl = DeclaredIdentity(
        identity=None, address="google_storage_bucket.b",
        source="derived_resource", confidence="derived", asset_type=None,
    )
    out = build_inventory([], [decl], project="p", iac_snapshot_sha="sha1")
    dnf = out["declared_not_found"]
    assert len(dnf) == 1
    entry = dnf[0]
    assert entry["possible_causes"] == ["identity_unresolved"]
    assert "identity" not in entry           # no identity field
    assert entry["address"] == "google_storage_bucket.b"  # address carries it


def test_resolved_supported_unmatched_declaration_has_lag_causes():
    # Supported + resolved identity, but no live resource matches it.
    out = build_inventory([], [_high()], project="p", iac_snapshot_sha="sha1")
    dnf = out["declared_not_found"]
    assert len(dnf) == 1
    assert dnf[0]["possible_causes"] == ["cai_lag", "not_yet_applied", "format_mismatch"]
    assert dnf[0]["identity"] == PD_NAME


def test_sample_capping_keeps_true_count_but_caps_sample_at_10():
    res = [
        CaiResource(
            name=f"//run.googleapis.com/projects/p/locations/l/services/s{i}",
            asset_type=RUN_TYPE, location="l",
        )
        for i in range(25)
    ]
    out = build_inventory(res, [], project="p", iac_snapshot_sha="sha1")
    entry = out["by_type"][RUN_TYPE]
    assert entry["count"] == 25
    assert len(entry["sample"]) <= 10


def test_sensitive_type_is_counts_only_no_sample_key():
    res = [
        CaiResource(
            name="//secretmanager.googleapis.com/projects/p/secrets/api-key",
            asset_type=SECRET_TYPE, location="global",
        )
    ]
    out = build_inventory(res, [], project="p", iac_snapshot_sha="sha1")
    entry = out["by_type"][SECRET_TYPE]
    assert entry["sensitive"] is True
    assert "sample" not in entry
    assert entry["count"] == 1


def test_declared_identity_with_no_live_match_appears_in_declared_not_found():
    out = build_inventory([], [_high()], project="p", iac_snapshot_sha="sha1")
    dnf = out["declared_not_found"]
    assert len(dnf) == 1
    entry = dnf[0]
    assert entry["source"] == "import_id"
    assert entry["confidence"] == "high"
    assert entry["possible_causes"]  # non-empty


def test_declared_not_found_with_sensitive_asset_type_redacts_identity():
    decl = DeclaredIdentity(
        identity="projects/p/secrets/api-key",
        address="google_secret_manager_secret.api_key",
        source="import_id", confidence="high", asset_type=SECRET_TYPE,
    )
    out = build_inventory([], [decl], project="p", iac_snapshot_sha="sha1")
    entry = out["declared_not_found"][0]
    assert entry.get("identity_redacted") is True
    assert "identity" not in entry


def test_output_carries_source_caveat_and_snapshot_sha():
    out = build_inventory([], [], project="p", iac_snapshot_sha="deadbeef")
    assert out["inventory_source"] == "cloud_asset_inventory"
    assert out["freshness_caveat"]  # non-empty
    assert out["iac_snapshot_sha"] == "deadbeef"


def test_counts_are_internally_consistent():
    res = [
        CaiResource(name=PD_CAI, asset_type=RUN_TYPE, location="l"),
        CaiResource(
            name="//run.googleapis.com/projects/p/locations/l/services/other",
            asset_type=RUN_TYPE, location="l",
        ),
        CaiResource(
            name="//secretmanager.googleapis.com/projects/p/secrets/k",
            asset_type=SECRET_TYPE, location="global",
        ),
    ]
    out = build_inventory(res, [_high()], project="p", iac_snapshot_sha="sha1")
    assert out["total_resources"] == 3
    assert out["declared_in_iac"] + out["not_in_iac"] == out["total_resources"]
    assert out["declared_in_iac"] == 1


def test_by_type_counts_sum_to_total():
    res = [
        CaiResource(name=PD_CAI, asset_type=RUN_TYPE, location="l"),
        CaiResource(
            name="//secretmanager.googleapis.com/projects/p/secrets/k",
            asset_type=SECRET_TYPE, location="global",
        ),
    ]
    out = build_inventory(res, [_high()], project="p", iac_snapshot_sha="sha1")
    summed = sum(e["count"] for e in out["by_type"].values())
    assert summed == out["total_resources"]


def test_declared_set_status_parse_error_only_when_parse_not_ok():
    ok = build_inventory([], [], project="p", iac_snapshot_sha="sha1")
    assert "declared_set_status" not in ok
    degraded = build_inventory(
        [], [], project="p", iac_snapshot_sha="sha1", declared_parse_ok=False,
    )
    assert degraded["declared_set_status"] == "parse_error"


def test_sensitive_asset_types_is_exactly_the_two_secret_types():
    assert SENSITIVE_ASSET_TYPES == frozenset({
        "secretmanager.googleapis.com/Secret",
        "secretmanager.googleapis.com/SecretVersion",
    })
