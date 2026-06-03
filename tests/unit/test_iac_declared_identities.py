from pathlib import Path

from driftscribe_lib import iac_hcl

IAC = Path(__file__).resolve().parents[2] / "iac"

IMPORTS_TF = '''
import {
  to = google_cloud_run_v2_service.payment_demo
  id = "projects/driftscribe-hack-2026/locations/asia-northeast1/services/payment-demo"
}
'''
VARIABLES_TF = '''
variable "project_id" { type = string\n default = "driftscribe-hack-2026" }
variable "region" { type = string\n default = "asia-northeast1" }
'''
CLOUDRUN_TF = '''
resource "google_cloud_run_v2_service" "payment_demo" {
  name     = "payment-demo"
  location = var.region
  project  = var.project_id
}
'''


PD_IDENTITY = "projects/driftscribe-hack-2026/locations/asia-northeast1/services/payment-demo"


def test_import_block_identity_is_high_confidence_with_address_and_asset_type():
    decls, parse_errors = iac_hcl.extract_declared_identities({"imports.tf": IMPORTS_TF})
    assert parse_errors == []
    ids = {d.identity: d for d in decls}
    assert PD_IDENTITY in ids
    d = ids[PD_IDENTITY]
    assert d.source == "import_id"
    assert d.confidence == "high"
    # `to` parses as "${google_cloud_run_v2_service.payment_demo}"; must be
    # unwrapped to the bare address and the asset_type inferred from the type.
    assert d.address == "google_cloud_run_v2_service.payment_demo"
    assert d.asset_type == "run.googleapis.com/Service"


def test_cloud_run_resource_resolves_var_defaults_to_derived_identity():
    files = {"variables.tf": VARIABLES_TF, "cloudrun.tf": CLOUDRUN_TF}
    decls, _ = iac_hcl.extract_declared_identities(files)
    derived = [d for d in decls if d.source == "derived_resource"]
    assert any(
        d.identity == PD_IDENTITY and d.confidence == "derived"
        and d.asset_type == "run.googleapis.com/Service"
        for d in derived
    )


def test_import_and_resource_agree_high_confidence_wins_keeps_asset_type():
    files = {"imports.tf": IMPORTS_TF, "variables.tf": VARIABLES_TF, "cloudrun.tf": CLOUDRUN_TF}
    decls, _ = iac_hcl.extract_declared_identities(files)
    matches = [d for d in decls if d.identity == PD_IDENTITY]
    assert len(matches) == 1                      # de-duped
    assert matches[0].confidence == "high"        # import wins
    assert matches[0].asset_type == "run.googleapis.com/Service"  # not lost in dedup


def test_unsupported_import_target_has_no_asset_type():
    # An import to a type with no resolver: address unwrapped, asset_type None.
    # (Uses google_compute_instance — a type Phase 2 deliberately does NOT support.)
    src = ('import {\n to = google_compute_instance.b\n'
           ' id = "my-proj/zone/inst"\n}')
    decls, _ = iac_hcl.extract_declared_identities({"i.tf": src})
    d = next(d for d in decls if d.source == "import_id")
    assert d.address == "google_compute_instance.b"
    assert d.asset_type is None                   # unsupported type → not matchable


def test_unsupported_resource_type_is_identity_unresolved():
    files = {"x.tf": 'resource "google_compute_instance" "b" { name = "x" }'}
    decls, _ = iac_hcl.extract_declared_identities(files)
    assert any(d.identity is None and d.address == "google_compute_instance.b" for d in decls)


def test_runtime_valued_attribute_is_unresolved():
    files = {
        "x.tf": 'resource "google_cloud_run_v2_service" "s" { name = "n" location = google_x.y.loc project = "p" }'
    }
    decls, _ = iac_hcl.extract_declared_identities(files)
    assert any(d.identity is None for d in decls)


def test_parse_error_is_reported():
    decls, parse_errors = iac_hcl.extract_declared_identities({"bad.tf": 'resource "x" {{{ '})
    assert "bad.tf" in parse_errors


def test_unsupported_import_and_supported_resource_sharing_identity_stay_distinct():
    """Edge case (Codex review): an unsupported high-confidence import
    (asset_type=None) and a supported derived resource that resolve to the SAME
    identity string must remain TWO distinct DeclaredIdentity entries — dedup is
    keyed by (asset_type, identity), so the None-typed import never inherits the
    supported asset_type and can never become matchable in Task 3.
    """
    shared = "projects/driftscribe-hack-2026/locations/asia-northeast1/services/payment-demo"
    # An import to an UNSUPPORTED type but with the same id string the Cloud Run
    # resource will resolve to. (google_compute_instance is not supported.)
    bad_import = (
        'import {\n'
        '  to = google_compute_instance.payment_demo\n'
        f'  id = "{shared}"\n'
        '}'
    )
    files = {
        "bad_import.tf": bad_import,
        "variables.tf": VARIABLES_TF,
        "cloudrun.tf": CLOUDRUN_TF,
    }
    decls, _ = iac_hcl.extract_declared_identities(files)
    matches = [d for d in decls if d.identity == shared]
    assert len(matches) == 2
    by_type = {d.asset_type: d for d in matches}
    # The unsupported import keeps asset_type None (never matchable).
    assert None in by_type
    assert by_type[None].source == "import_id"
    # The supported derived resource keeps its real asset_type.
    assert "run.googleapis.com/Service" in by_type
    assert by_type["run.googleapis.com/Service"].source == "derived_resource"


def test_real_files_payment_demo_high_confidence():
    """Regression against the actual committed iac/*.tf — guards drift between
    the resolver and the real HCL (the class of bug CI caught in Phase A)."""
    files = {
        "imports.tf": (IAC / "imports.tf").read_text(encoding="utf-8"),
        "variables.tf": (IAC / "variables.tf").read_text(encoding="utf-8"),
        "cloudrun.tf": (IAC / "cloudrun.tf").read_text(encoding="utf-8"),
    }
    decls, parse_errors = iac_hcl.extract_declared_identities(files)
    assert parse_errors == []
    matches = [d for d in decls if d.identity == PD_IDENTITY]
    assert len(matches) == 1                      # import wins over derived
    assert matches[0].confidence == "high"
    assert matches[0].asset_type == "run.googleapis.com/Service"


# --------------------------------------------------------------------------- #
# Phase 2 — new resource types (bucket / Pub/Sub topic+subscription / SA).
# Identity formats are grounded against the LIVE project's CAI names AFTER
# normalize_cai_name: bucket → bare name; pubsub → projects/<p>/{topics,
# subscriptions}/<n>; SA → projects/<p>/serviceAccounts/<acct>@<p>.iam.g…com.
# --------------------------------------------------------------------------- #

BUCKET_TYPE = "storage.googleapis.com/Bucket"
TOPIC_TYPE = "pubsub.googleapis.com/Topic"
SUB_TYPE = "pubsub.googleapis.com/Subscription"
SA_TYPE = "iam.googleapis.com/ServiceAccount"


def _derived(decls, asset_type):
    """The single derived-resource declaration of a given asset_type."""
    return next(
        d for d in decls
        if d.source == "derived_resource" and d.asset_type == asset_type
    )


def test_bucket_resolves_to_bare_name_not_a_project_path():
    # CAI bucket name is the bare global name (//storage.googleapis.com/<BUCKET>
    # → <BUCKET>); the identity must be the bare name, NOT projects/_/buckets/…
    files = {
        "variables.tf": VARIABLES_TF,
        "b.tf": ('resource "google_storage_bucket" "assets" {\n'
                 '  name = "my-assets-bucket"\n  project = var.project_id\n'
                 '  location = var.region\n}'),
    }
    decls, _ = iac_hcl.extract_declared_identities(files)
    d = _derived(decls, BUCKET_TYPE)
    assert d.identity == "my-assets-bucket"
    assert d.confidence == "derived"


def test_bucket_resolves_without_project_attribute():
    # The bucket identity is just the bare name; project/location don't enter it,
    # so a bucket block with no `project` still resolves (unlike the path types).
    files = {"b.tf": 'resource "google_storage_bucket" "assets" { name = "my-assets-bucket" }'}
    decls, _ = iac_hcl.extract_declared_identities(files)
    assert _derived(decls, BUCKET_TYPE).identity == "my-assets-bucket"


def test_leaf_name_resolves_from_var_default():
    # The leaf NAME component (not just project) resolves a var.x literal default
    # via _resolve_scalar — and the _is_short guard still applies to it.
    files = {
        "variables.tf": 'variable "bucket_name" { default = "var-default-bucket" }',
        "b.tf": 'resource "google_storage_bucket" "assets" { name = var.bucket_name }',
    }
    decls, _ = iac_hcl.extract_declared_identities(files)
    assert _derived(decls, BUCKET_TYPE).identity == "var-default-bucket"


def test_pubsub_topic_resolves_to_project_topic_path():
    files = {
        "variables.tf": VARIABLES_TF,
        "t.tf": ('resource "google_pubsub_topic" "orders" {\n'
                 '  name = "order-events"\n  project = var.project_id\n}'),
    }
    decls, _ = iac_hcl.extract_declared_identities(files)
    assert _derived(decls, TOPIC_TYPE).identity == \
        "projects/driftscribe-hack-2026/topics/order-events"


def test_pubsub_subscription_resolves_to_project_subscription_path():
    files = {
        "variables.tf": VARIABLES_TF,
        "s.tf": ('resource "google_pubsub_subscription" "orders" {\n'
                 '  name = "orders-sub"\n  project = var.project_id\n}'),
    }
    decls, _ = iac_hcl.extract_declared_identities(files)
    assert _derived(decls, SUB_TYPE).identity == \
        "projects/driftscribe-hack-2026/subscriptions/orders-sub"


def test_service_account_resolves_to_email_form_path():
    # CAI here uses the email form: projects/<p>/serviceAccounts/<acct>@<p>.iam…
    files = {
        "variables.tf": VARIABLES_TF,
        "sa.tf": ('resource "google_service_account" "storefront" {\n'
                  '  account_id = "storefront-sa"\n  project = var.project_id\n}'),
    }
    decls, _ = iac_hcl.extract_declared_identities(files)
    assert _derived(decls, SA_TYPE).identity == (
        "projects/driftscribe-hack-2026/serviceAccounts/"
        "storefront-sa@driftscribe-hack-2026.iam.gserviceaccount.com"
    )


def test_pubsub_topic_without_project_is_unresolved():
    # Path-templated types require an explicit, statically-resolvable project
    # (consistent with Cloud Run). Omitted project → unresolved, never guessed.
    files = {"t.tf": 'resource "google_pubsub_topic" "orders" { name = "order-events" }'}
    decls, _ = iac_hcl.extract_declared_identities(files)
    assert _derived(decls, TOPIC_TYPE).identity is None


def test_service_account_without_project_is_unresolved():
    files = {"sa.tf": 'resource "google_service_account" "sf" { account_id = "storefront-sa" }'}
    decls, _ = iac_hcl.extract_declared_identities(files)
    assert _derived(decls, SA_TYPE).identity is None


def test_full_path_name_is_not_double_prefixed():
    # A topic name mistakenly written in full path form must NOT template into
    # projects/p/topics/projects/p/topics/t — the "/" guard returns unresolved.
    files = {
        "variables.tf": VARIABLES_TF,
        "t.tf": ('resource "google_pubsub_topic" "orders" {\n'
                 '  name = "projects/driftscribe-hack-2026/topics/order-events"\n'
                 '  project = var.project_id\n}'),
    }
    decls, _ = iac_hcl.extract_declared_identities(files)
    assert _derived(decls, TOPIC_TYPE).identity is None


def test_import_to_bucket_now_gets_storage_asset_type():
    # Inverse of the old "unsupported import" case: a bucket import now infers the
    # storage asset_type, and a bare-name id is the matchable identity.
    src = ('import {\n to = google_storage_bucket.assets\n'
           ' id = "my-assets-bucket"\n}')
    decls, _ = iac_hcl.extract_declared_identities({"i.tf": src})
    d = next(d for d in decls if d.source == "import_id")
    assert d.asset_type == BUCKET_TYPE
    assert d.identity == "my-assets-bucket"   # bare name matches CAI


def test_unsupported_import_sharing_identity_with_new_bucket_stays_distinct():
    # The (asset_type, identity) dedup must keep a None-typed import distinct from
    # a newly-SUPPORTED bucket sharing the same identity string (extends the
    # Cloud-Run distinctness invariant to a Phase-2 type).
    shared = "my-assets-bucket"
    files = {
        "variables.tf": VARIABLES_TF,
        "bad.tf": f'import {{\n to = google_compute_instance.assets\n id = "{shared}"\n}}',
        "b.tf": ('resource "google_storage_bucket" "assets" {\n'
                 f'  name = "{shared}"\n  project = var.project_id\n}}'),
    }
    decls, _ = iac_hcl.extract_declared_identities(files)
    matches = {d.asset_type: d for d in decls if d.identity == shared}
    assert None in matches and matches[None].source == "import_id"
    assert BUCKET_TYPE in matches and matches[BUCKET_TYPE].source == "derived_resource"


def test_real_files_all_iac_tf_resolve_bucket_and_cloud_run():
    # Regression against EVERY committed iac/*.tf (Codex: glob, don't hand-pick).
    # The live c6e_probe bucket must resolve to its bare name with the storage
    # asset_type, and payment-demo must still resolve to its Cloud Run path.
    files = {p.name: p.read_text(encoding="utf-8") for p in IAC.glob("*.tf")}
    decls, parse_errors = iac_hcl.extract_declared_identities(files)
    assert parse_errors == []
    by_identity = {d.identity: d for d in decls}
    assert "driftscribe-hack-2026-c6e-probe" in by_identity
    assert by_identity["driftscribe-hack-2026-c6e-probe"].asset_type == BUCKET_TYPE
    assert PD_IDENTITY in by_identity
    assert by_identity[PD_IDENTITY].asset_type == "run.googleapis.com/Service"
