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
    # An import to a type with no v1 resolver: address unwrapped, asset_type None.
    src = ('import {\n to = google_storage_bucket.b\n'
           ' id = "my-proj/my-bucket"\n}')
    decls, _ = iac_hcl.extract_declared_identities({"i.tf": src})
    d = next(d for d in decls if d.source == "import_id")
    assert d.address == "google_storage_bucket.b"
    assert d.asset_type is None                   # unsupported type → not matchable


def test_unsupported_resource_type_is_identity_unresolved():
    files = {"x.tf": 'resource "google_storage_bucket" "b" { name = "x" }'}
    decls, _ = iac_hcl.extract_declared_identities(files)
    assert any(d.identity is None and d.address == "google_storage_bucket.b" for d in decls)


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
    # resource will resolve to.
    bad_import = (
        'import {\n'
        '  to = google_storage_bucket.payment_demo\n'
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
