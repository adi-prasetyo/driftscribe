"""Unit tests for the pure inventory-summary builder (Task 3).

Each Step-1 checklist case is its own test function. The builder is pure (no
network): we hand it CaiResource records + a DeclaredIdentity set and assert the
bounded summary shape, type-aware matching, confidence carry-through,
sensitive-type counts-only, sample capping, and conditioned declared_not_found
reason codes + identity redaction.
"""
from pathlib import Path

from driftscribe_lib.iac_hcl import DeclaredIdentity, extract_declared_identities
from driftscribe_lib.infra_inventory import (
    SENSITIVE_ASSET_TYPES,
    CaiResource,
    build_inventory,
    normalize_cai_name,
)

IAC = Path(__file__).resolve().parents[2] / "iac"

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


BUCKET_TYPE = "storage.googleapis.com/Bucket"


def _run(short_name: str) -> CaiResource:
    return CaiResource(
        name=f"//run.googleapis.com/projects/p/locations/l/services/{short_name}",
        asset_type=RUN_TYPE,
        location="l",
    )


def _bucket(short_name: str) -> CaiResource:
    return CaiResource(
        name=f"//storage.googleapis.com/projects/_/buckets/{short_name}",
        asset_type=BUCKET_TYPE,
        location="US",
    )


class TestControlPlaneDriftCount:
    """`not_in_iac_control_plane` counts unmanaged resources that are DriftScribe's
    own control plane / service-managed — so the graph can subtract them from the
    displayed drift. Computed over EVERY resource (not the ≤10 sample), keyed by
    the same short name used as the node label."""

    def test_key_present_and_zero_when_no_control_plane(self):
        out = build_inventory([_run("adopt-probe-svc")], [], project="p", iac_snapshot_sha="s")
        assert out["by_type"][RUN_TYPE]["not_in_iac"] == 1
        assert out["by_type"][RUN_TYPE]["not_in_iac_control_plane"] == 0

    def test_own_worker_service_counts_as_control_plane_drift(self):
        out = build_inventory([_run("driftscribe-agent")], [], project="p", iac_snapshot_sha="s")
        entry = out["by_type"][RUN_TYPE]
        assert entry["not_in_iac"] == 1
        assert entry["not_in_iac_control_plane"] == 1

    def test_mixed_worker_and_probe_separates_actionable_from_control_plane(self):
        out = build_inventory(
            [_run("driftscribe-agent"), _run("driftscribe-reader"), _run("adopt-probe-svc")],
            [],
            project="p",
            iac_snapshot_sha="s",
        )
        entry = out["by_type"][RUN_TYPE]
        assert entry["not_in_iac"] == 3
        assert entry["not_in_iac_control_plane"] == 2  # the two workers; probe is actionable

    def test_tofu_state_bucket_counts_as_control_plane_drift(self):
        out = build_inventory(
            [_bucket("driftscribe-hack-2026-tofu-state"), _bucket("driftscribe-hack-2026-assets")],
            [],
            project="p",
            iac_snapshot_sha="s",
        )
        entry = out["by_type"][BUCKET_TYPE]
        assert entry["not_in_iac"] == 2
        assert entry["not_in_iac_control_plane"] == 1  # tofu-state; assets is actionable

    def test_non_adoptable_type_never_control_plane_even_if_unmanaged(self):
        keys = [CaiResource(name="//cloudkms.googleapis.com/x/cryptoKeys/k", asset_type="cloudkms.googleapis.com/CryptoKey", location="l")]
        out = build_inventory(keys, [], project="p", iac_snapshot_sha="s")
        entry = out["by_type"]["cloudkms.googleapis.com/CryptoKey"]
        assert entry["not_in_iac"] == 1
        assert entry["not_in_iac_control_plane"] == 0

    def test_managed_control_plane_resource_is_not_counted(self):
        decl = DeclaredIdentity(
            identity="projects/p/locations/l/services/driftscribe-agent",
            address="google_cloud_run_v2_service.agent",
            source="import_id",
            confidence="high",
            asset_type=RUN_TYPE,
        )
        out = build_inventory([_run("driftscribe-agent")], [decl], project="p", iac_snapshot_sha="s")
        entry = out["by_type"][RUN_TYPE]
        assert entry["declared_in_iac"] == 1
        assert entry["not_in_iac"] == 0
        assert entry["not_in_iac_control_plane"] == 0

    def test_control_plane_drift_counts_beyond_sample_cap(self):
        # 12 control-plane workers > _SAMPLE_CAP (10): the count must reflect ALL,
        # not just the sampled nodes.
        workers = [_run("driftscribe-agent") for _ in range(12)]
        out = build_inventory(workers, [], project="p", iac_snapshot_sha="s")
        entry = out["by_type"][RUN_TYPE]
        assert entry["not_in_iac"] == 12
        assert entry["not_in_iac_control_plane"] == 12


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


# --------------------------------------------------------------------------- #
# Phase 2 — new resource types match their live CAI resource end-to-end. CAI
# `name` forms below are the real ones; the post-// path is what the resolver's
# identity template must equal (verified against live CAI on driftscribe-hack-2026).
# --------------------------------------------------------------------------- #

BUCKET_TYPE = "storage.googleapis.com/Bucket"
TOPIC_TYPE = "pubsub.googleapis.com/Topic"
SUB_TYPE = "pubsub.googleapis.com/Subscription"
SA_TYPE = "iam.googleapis.com/ServiceAccount"


def _decl(identity, asset_type, address="x.y"):
    return DeclaredIdentity(
        identity=identity, address=address, source="derived_resource",
        confidence="derived", asset_type=asset_type,
    )


def test_bucket_declaration_matches_live_bucket():
    res = [CaiResource(
        name="//storage.googleapis.com/my-assets-bucket",
        asset_type=BUCKET_TYPE, location="asia-northeast1",
    )]
    decl = _decl("my-assets-bucket", BUCKET_TYPE, "google_storage_bucket.assets")
    out = build_inventory(res, [decl], project="p", iac_snapshot_sha="sha1")
    assert out["declared_in_iac"] == 1
    entry = out["by_type"][BUCKET_TYPE]
    assert entry["declared_in_iac"] == 1
    assert entry["sample"][0]["iac"] is True
    assert out["declared_not_found"] == []          # matched → not reported missing


def test_pubsub_topic_declaration_matches_live_topic():
    res = [CaiResource(
        name="//pubsub.googleapis.com/projects/p/topics/order-events",
        asset_type=TOPIC_TYPE, location="global",
    )]
    decl = _decl("projects/p/topics/order-events", TOPIC_TYPE, "google_pubsub_topic.orders")
    out = build_inventory(res, [decl], project="p", iac_snapshot_sha="sha1")
    assert out["declared_in_iac"] == 1
    assert out["by_type"][TOPIC_TYPE]["sample"][0]["iac"] is True
    assert out["declared_not_found"] == []


def test_pubsub_subscription_declaration_matches_live_subscription():
    res = [CaiResource(
        name="//pubsub.googleapis.com/projects/p/subscriptions/orders-sub",
        asset_type=SUB_TYPE, location="global",
    )]
    decl = _decl(
        "projects/p/subscriptions/orders-sub", SUB_TYPE,
        "google_pubsub_subscription.orders",
    )
    out = build_inventory(res, [decl], project="p", iac_snapshot_sha="sha1")
    assert out["declared_in_iac"] == 1
    assert out["by_type"][SUB_TYPE]["sample"][0]["iac"] is True
    assert out["declared_not_found"] == []


def test_service_account_declaration_matches_live_sa_email_form():
    res = [CaiResource(
        name=("//iam.googleapis.com/projects/p/serviceAccounts/"
              "storefront-sa@p.iam.gserviceaccount.com"),
        asset_type=SA_TYPE, location="global",
    )]
    decl = _decl(
        "projects/p/serviceAccounts/storefront-sa@p.iam.gserviceaccount.com",
        SA_TYPE, "google_service_account.storefront",
    )
    out = build_inventory(res, [decl], project="p", iac_snapshot_sha="sha1")
    assert out["declared_in_iac"] == 1
    assert out["by_type"][SA_TYPE]["sample"][0]["iac"] is True
    assert out["declared_not_found"] == []


def test_bucket_identity_mismatch_falls_into_not_in_iac():
    # A declared bucket whose name differs from the live one must NOT force-match.
    res = [CaiResource(
        name="//storage.googleapis.com/actual-bucket",
        asset_type=BUCKET_TYPE, location="asia-northeast1",
    )]
    decl = _decl("declared-bucket", BUCKET_TYPE, "google_storage_bucket.assets")
    out = build_inventory(res, [decl], project="p", iac_snapshot_sha="sha1")
    assert out["declared_in_iac"] == 0
    assert out["by_type"][BUCKET_TYPE]["sample"][0]["iac"] is False
    assert len(out["declared_not_found"]) == 1
    dnf = out["declared_not_found"][0]
    assert dnf["possible_causes"] == ["cai_lag", "not_yet_applied", "format_mismatch"]
    assert dnf["identity"] == "declared-bucket"   # supported-but-unmatched carries identity


def test_service_account_numeric_uniqueid_cai_form_does_not_match_email_decl():
    # Design said "test both forms": this project's CAI uses the email form, but
    # CAI CAN return the numeric uniqueId form for an SA. The resolver derives the
    # EMAIL-form identity, so a numeric-form live SA won't match — a tracked
    # limitation. Pin it: numeric live SA + email decl → not-in-IaC + format_mismatch.
    res = [CaiResource(
        name="//iam.googleapis.com/projects/p/serviceAccounts/123456789012345678901",
        asset_type=SA_TYPE, location="global",
    )]
    decl = _decl(
        "projects/p/serviceAccounts/storefront-sa@p.iam.gserviceaccount.com",
        SA_TYPE, "google_service_account.storefront",
    )
    out = build_inventory(res, [decl], project="p", iac_snapshot_sha="sha1")
    assert out["declared_in_iac"] == 0
    assert out["by_type"][SA_TYPE]["sample"][0]["iac"] is False
    assert out["declared_not_found"][0]["possible_causes"] == \
        ["cai_lag", "not_yet_applied", "format_mismatch"]


# --------------------------------------------------------------------------- #
# Phase 2 — TRUE end-to-end: run the real resolver (extract_declared_identities),
# then feed ITS output into build_inventory against real //service/... CAI names.
# These pin the whole labeling chain so a future resolver-template edit can't
# silently break matching while each half's isolated tests still pass.
# --------------------------------------------------------------------------- #

_VARS = 'variable "project_id" { default = "p" }\n'


def test_real_iac_declarations_resolve_and_round_trip_inventory():
    # The real committed iac/*.tf, resolved by the real resolver, must round-trip
    # to a clean, fully-matched inventory. This is written to be ROBUST to iac/
    # growth (the create-class dogfood loop adds resources to iac/ in a PR
    # BEFORE they exist live), so it does NOT hardcode a resource count or live
    # set. Instead it asserts the load-bearing invariants:
    #   1. every file parses,
    #   2. no SUPPORTED-type resource fails to resolve to an identity — a
    #      Pub/Sub topic/subscription (or Cloud Run service) authored without an
    #      explicit `project`/`location` resolves to None and would false-drift;
    #      this guard fails CI on exactly that bug (caught a real one in the
    #      checkout build-out),
    #   3. the two long-lived resources still pin the resolver's derived identity
    #      to their real CAI name forms,
    #   4. synthesizing the live CAI set from the resolved declarations yields a
    #      fully-matched inventory (nothing in declared_not_found).
    # A newly-added resource's match against its REAL live CAI name is proven by
    # apply plus the per-type resolver tests above.
    files = {p.name: p.read_text(encoding="utf-8") for p in IAC.glob("*.tf")}
    declared, parse_errors = extract_declared_identities(files)
    assert parse_errors == []

    unresolved_supported = [
        d for d in declared if d.asset_type is not None and d.identity is None
    ]
    assert unresolved_supported == [], (
        "supported iac/ resources failed to resolve (false-drift risk — likely "
        f"a missing explicit project/location): {[d.address for d in unresolved_supported]}"
    )

    declared_identities = {d.identity for d in declared}
    assert (
        "projects/driftscribe-hack-2026/locations/asia-northeast1/services/payment-demo"
        in declared_identities
    )
    assert "driftscribe-hack-2026-c6e-probe" in declared_identities

    # Synthesize one live CAI record per resolved declaration. normalize_cai_name
    # strips the `//<service>/` scheme prefix, so the derived identity is what the
    # matcher compares — a placeholder host derived from the asset_type is fine.
    matchable = [
        d for d in declared if d.asset_type is not None and d.identity is not None
    ]
    live = [
        CaiResource(
            name=f"//{d.asset_type.split('/')[0]}/{d.identity}",
            asset_type=d.asset_type,
            location="asia-northeast1",
        )
        for d in matchable
    ]
    out = build_inventory(
        live, declared, project="driftscribe-hack-2026", iac_snapshot_sha="sha1",
    )
    assert out["declared_in_iac"] == len(matchable)
    assert out["declared_not_found"] == []
    assert out["by_type"][BUCKET_TYPE]["sample"][0]["iac"] is True
    assert out["by_type"][RUN_TYPE]["sample"][0]["iac"] is True


def test_end_to_end_topic_resolver_output_matches_live():
    files = {
        "v.tf": _VARS,
        "t.tf": ('resource "google_pubsub_topic" "orders" {\n'
                 '  name = "order-events"\n  project = var.project_id\n}'),
    }
    declared, _ = extract_declared_identities(files)
    live = [CaiResource(
        name="//pubsub.googleapis.com/projects/p/topics/order-events",
        asset_type=TOPIC_TYPE, location="global",
    )]
    out = build_inventory(live, declared, project="p", iac_snapshot_sha="sha1")
    assert out["declared_in_iac"] == 1
    assert out["declared_not_found"] == []


def test_end_to_end_subscription_resolver_output_matches_live():
    files = {
        "v.tf": _VARS,
        "s.tf": ('resource "google_pubsub_subscription" "orders" {\n'
                 '  name = "orders-sub"\n  project = var.project_id\n}'),
    }
    declared, _ = extract_declared_identities(files)
    live = [CaiResource(
        name="//pubsub.googleapis.com/projects/p/subscriptions/orders-sub",
        asset_type=SUB_TYPE, location="global",
    )]
    out = build_inventory(live, declared, project="p", iac_snapshot_sha="sha1")
    assert out["declared_in_iac"] == 1
    assert out["declared_not_found"] == []


def test_end_to_end_service_account_resolver_output_matches_live():
    files = {
        "v.tf": _VARS,
        "sa.tf": ('resource "google_service_account" "sf" {\n'
                  '  account_id = "storefront-sa"\n  project = var.project_id\n}'),
    }
    declared, _ = extract_declared_identities(files)
    live = [CaiResource(
        name=("//iam.googleapis.com/projects/p/serviceAccounts/"
              "storefront-sa@p.iam.gserviceaccount.com"),
        asset_type=SA_TYPE, location="global",
    )]
    out = build_inventory(live, declared, project="p", iac_snapshot_sha="sha1")
    assert out["declared_in_iac"] == 1
    assert out["declared_not_found"] == []


def test_end_to_end_noncai_form_bucket_import_is_false_drift():
    # An import id written in an alternate (non-CAI) OpenTofu spelling is taken
    # verbatim and will NOT match the live bucket's bare-name CAI form — it shows
    # as drift. Pins the documented import-id contract (Codex review should-fix).
    files = {
        "i.tf": ('import {\n to = google_storage_bucket.assets\n'
                 ' id = "p/my-assets-bucket"\n}'),   # CAI form is the bare name
    }
    declared, _ = extract_declared_identities(files)
    live = [CaiResource(
        name="//storage.googleapis.com/my-assets-bucket",
        asset_type=BUCKET_TYPE, location="asia-northeast1",
    )]
    out = build_inventory(live, declared, project="p", iac_snapshot_sha="sha1")
    assert out["declared_in_iac"] == 0                       # verbatim id mismatched
    assert out["by_type"][BUCKET_TYPE]["sample"][0]["iac"] is False
    assert out["declared_not_found"][0]["possible_causes"] == \
        ["cai_lag", "not_yet_applied", "format_mismatch"]
