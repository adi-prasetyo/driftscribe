from pathlib import Path

import pytest

from tools import iac_static_gate  # noqa: F401
from tools.iac_static_gate import GateInput, GateMode, evaluate


def test_module_imports():
    assert iac_static_gate is not None


def test_clean_agent_pr_passes():
    gi = GateInput(
        mode=GateMode.AGENT,
        changed_paths=("iac/cloudrun.tf",),
        hcl_files={"iac/cloudrun.tf": 'resource "google_cloud_run_v2_service" "x" {}\n'},
    )
    assert evaluate(gi) == []


# --- Task 2: path / file-type / foundation checks ---


def test_agent_pr_touching_outside_iac_is_rejected():
    gi = GateInput(GateMode.AGENT, ("iac/cloudrun.tf", ".github/workflows/ci.yml"), {})
    assert any(v.rule == "path-outside-iac" for v in evaluate(gi))


@pytest.mark.parametrize("path", [
    "iac/main.tofu", "iac/main.tofu.json", "iac/main.tf.json",
    "iac/prod.tfvars", "iac/x.auto.tfvars",
])
def test_agent_pr_non_tf_iac_file_is_rejected(path):
    # .tofu OVERRIDES a same-named .tf; .tf.json/.tfvars/.auto.tfvars are all
    # loaded by OpenTofu and would bypass a .tf-only gate.
    gi = GateInput(GateMode.AGENT, (path,), {})
    assert any(v.rule == "disallowed-file-type" for v in evaluate(gi)), path


def test_agent_pr_touching_lockfile_is_rejected():
    gi = GateInput(GateMode.AGENT, ("iac/.terraform.lock.hcl",), {})
    assert any(v.rule == "foundation-edit-agent-mode" for v in evaluate(gi))


@pytest.mark.parametrize("path", [
    "iac/versions.tf", "iac/providers.tf", "iac/variables.tf", "iac/imports.tf",
])
def test_agent_pr_touching_foundation_is_rejected(path):
    gi = GateInput(GateMode.AGENT, (path,), {})
    assert any(v.rule == "foundation-edit-agent-mode" for v in evaluate(gi)), path


def test_operator_mode_may_touch_foundation():
    gi = GateInput(GateMode.OPERATOR, ("iac/.terraform.lock.hcl", "iac/versions.tf"), {})
    assert evaluate(gi) == []


def test_operator_mode_still_governs_only_iac():
    # The gate only governs iac/; a .github edit in operator mode raises no
    # path-outside-iac (CODEOWNERS governs that file).
    gi = GateInput(GateMode.OPERATOR, ("iac/versions.tf", ".github/workflows/iac.yml"), {})
    assert all(v.rule != "path-outside-iac" for v in evaluate(gi))


def test_empty_diff_passes_in_both_modes():
    # The iac.yml workflow runs on EVERY PR (no paths filter) so it can be a
    # required status check. A PR whose diff contains no in-scope files yields
    # an empty changed_paths; with nothing to govern the gate must PASS in BOTH
    # modes — there is no foundation edit, no out-of-scope path, no HCL to scan.
    # (Contrast test_agent_pr_touching_outside_iac_is_rejected: a NON-empty
    # non-iac diff is still correctly rejected in agent mode.)
    for mode in (GateMode.OPERATOR, GateMode.AGENT):
        assert evaluate(GateInput(mode, (), {})) == [], mode


# --- Task 3: provider allowlist + source pinning + fail-closed parse ---


def test_disallowed_provider_required_providers_block():
    hcl = 'terraform { required_providers { aws = { source = "hashicorp/aws" } } }'
    gi = GateInput(GateMode.OPERATOR, ("iac/versions.tf",), {"iac/versions.tf": hcl})
    assert any(v.rule == "disallowed-provider" for v in evaluate(gi))


def test_spoofed_google_source_is_rejected():
    hcl = 'terraform { required_providers { google = { source = "evil/google" } } }'
    gi = GateInput(GateMode.OPERATOR, ("iac/versions.tf",), {"iac/versions.tf": hcl})
    assert any(v.rule == "disallowed-provider-source" for v in evaluate(gi))


def test_canonical_google_provider_is_allowed():
    hcl = '''
    terraform { required_providers { google = { source = "hashicorp/google" } } }
    provider "google" { project = "p" }
    '''
    gi = GateInput(GateMode.OPERATOR, ("iac/versions.tf",), {"iac/versions.tf": hcl})
    assert evaluate(gi) == []


def test_unparseable_hcl_fails_closed():
    gi = GateInput(GateMode.OPERATOR, ("iac/versions.tf",), {"iac/versions.tf": 'resource "x" { = = = }'})
    assert any(v.rule == "hcl-parse-error" for v in evaluate(gi))


# --- Task 4: module ban (all modules forbidden in v1) ---


def test_any_module_block_is_rejected():
    hcl = 'module "vpc" { source = "./vpc" }'
    gi = GateInput(GateMode.AGENT, ("iac/x.tf",), {"iac/x.tf": hcl})
    assert any(v.rule == "module-block-forbidden" for v in evaluate(gi))


def test_no_module_block_passes():
    gi = GateInput(GateMode.AGENT, ("iac/x.tf",), {"iac/x.tf": 'resource "google_x" "y" {}'})
    assert all(v.rule != "module-block-forbidden" for v in evaluate(gi))


# --- Task 5: provisioner / arbitrary-execution / forbidden-data-source / dynamic ---


@pytest.mark.parametrize("hcl", [
    'resource "google_x" "y" { provisioner "local-exec" { command = "echo hi" } }',
    'resource "google_x" "y" { provisioner "remote-exec" { inline = ["id"] } }',
    'resource "google_x" "y" { connection { host = "h" } }',
    'resource "null_resource" "r" {}',
    'resource "terraform_data" "r" {}',
])
def test_arbitrary_execution_constructs_rejected(hcl):
    gi = GateInput(GateMode.AGENT, ("iac/x.tf",), {"iac/x.tf": hcl})
    assert any(v.rule == "arbitrary-execution" for v in evaluate(gi)), hcl


@pytest.mark.parametrize("hcl", [
    'data "external" "e" { program = ["bash", "x.sh"] }',
    'data "terraform_remote_state" "s" { backend = "gcs" }',
])
def test_forbidden_data_sources_rejected(hcl):
    gi = GateInput(GateMode.AGENT, ("iac/x.tf",), {"iac/x.tf": hcl})
    assert any(v.rule == "forbidden-data-source" for v in evaluate(gi)), hcl


def test_dynamic_block_rejected():
    hcl = 'resource "google_x" "y" { dynamic "provisioner" { for_each = [1] content {} } }'
    gi = GateInput(GateMode.AGENT, ("iac/x.tf",), {"iac/x.tf": hcl})
    assert any(v.rule == "dynamic-block-forbidden" for v in evaluate(gi))


def test_plain_google_resource_has_no_execution_violation():
    hcl = 'resource "google_cloud_run_v2_service" "s" { name = "payment-demo" }'
    gi = GateInput(GateMode.AGENT, ("iac/x.tf",), {"iac/x.tf": hcl})
    assert evaluate(gi) == []


# --- Task D1-6: secret material is operator-only (AGENT-mode ban) ---


_SECRET_MATERIAL_HCL = [
    'resource "google_secret_manager_secret" "s" { secret_id = "plan-hmac-key" }',
    (
        'resource "google_secret_manager_secret_version" "v" '
        '{ secret = "x" secret_data = "supersecret" }'
    ),
    # Regional variants are real google provider resource types and must be
    # banned too — a regional container carries no secret_data, so without the
    # resource-type ban it would slip the gate entirely.
    (
        'resource "google_secret_manager_regional_secret" "s" '
        '{ secret_id = "plan-hmac-key" location = "us-central1" }'
    ),
    (
        'resource "google_secret_manager_regional_secret_version" "v" '
        '{ secret = "x" secret_data = "supersecret" }'
    ),
]


@pytest.mark.parametrize("hcl", _SECRET_MATERIAL_HCL)
def test_agent_authored_secret_material_rejected(hcl):
    gi = GateInput(GateMode.AGENT, ("iac/x.tf",), {"iac/x.tf": hcl})
    assert any(v.rule == "secret-material-forbidden" for v in evaluate(gi)), hcl


@pytest.mark.parametrize("hcl", _SECRET_MATERIAL_HCL)
def test_operator_mode_may_author_secret_material(hcl):
    # Operators legitimately declare secrets during bootstrap — the same secret
    # HCL must NOT raise secret-material-forbidden in OPERATOR mode.
    gi = GateInput(GateMode.OPERATOR, ("iac/secrets.tf",), {"iac/secrets.tf": hcl})
    assert all(v.rule != "secret-material-forbidden" for v in evaluate(gi)), hcl


def test_inline_secret_data_attribute_rejected_in_agent_mode():
    # Defense-in-depth: a `secret_data` attribute smuggled into a NON-secret
    # resource type is still rejected in AGENT mode...
    hcl = 'resource "google_x" "y" { secret_data = "supersecret" }'
    gi_agent = GateInput(GateMode.AGENT, ("iac/x.tf",), {"iac/x.tf": hcl})
    assert any(v.rule == "secret-material-forbidden" for v in evaluate(gi_agent))
    # ...but allowed in OPERATOR mode.
    gi_op = GateInput(GateMode.OPERATOR, ("iac/x.tf",), {"iac/x.tf": hcl})
    assert all(v.rule != "secret-material-forbidden" for v in evaluate(gi_op))


def test_benign_agent_resource_has_no_secret_material_violation():
    # A normal bucket in AGENT mode yields no secret-material-forbidden (and no
    # violations at all).
    hcl = 'resource "google_storage_bucket" "b" {}'
    gi = GateInput(GateMode.AGENT, ("iac/x.tf",), {"iac/x.tf": hcl})
    violations = evaluate(gi)
    assert all(v.rule != "secret-material-forbidden" for v in violations), violations
    assert violations == []


# --- Adversarial / regression lock-ins (security gate hardening) ---


def test_provisioner_nested_two_blocks_deep_is_caught():
    # provisioner buried inside dynamic -> content -> (another) dynamic content.
    hcl = (
        'resource "google_x" "y" { '
        'dynamic "a" { content { dynamic "b" { content { '
        'provisioner "local-exec" { command = "id" } } } } } }'
    )
    gi = GateInput(GateMode.AGENT, ("iac/x.tf",), {"iac/x.tf": hcl})
    rules = {v.rule for v in evaluate(gi)}
    assert "arbitrary-execution" in rules
    assert "dynamic-block-forbidden" in rules


def test_disallowed_provider_via_top_level_provider_block_only():
    # No required_providers entry — only a bare `provider "aws" {}` block.
    hcl = 'provider "aws" { region = "us-east-1" }'
    gi = GateInput(GateMode.OPERATOR, ("iac/providers.tf",), {"iac/providers.tf": hcl})
    assert any(v.rule == "disallowed-provider" for v in evaluate(gi))


def test_required_providers_mix_flags_only_the_disallowed_one():
    hcl = (
        'terraform { required_providers { '
        'google = { source = "hashicorp/google" } '
        'aws = { source = "hashicorp/aws" } } }'
    )
    gi = GateInput(GateMode.OPERATOR, ("iac/versions.tf",), {"iac/versions.tf": hcl})
    violations = [v for v in evaluate(gi) if v.rule == "disallowed-provider"]
    assert len(violations) == 1
    assert "aws" in violations[0].detail


# --- Regression: hcl2 dunder-metadata keys must not be read as semantic names ---


def test_commented_required_providers_no_false_provider():
    # hcl2 8.x injects metadata keys (e.g. __inline_comments__/__comments__)
    # into a block ONLY when the HCL contains comments. Those keys must not be
    # treated as provider names. A clean commented google block must pass.
    hcl = (
        "terraform {\n"
        "  # backend + encryption are foundation\n"
        "  required_providers {\n"
        '    google = {\n'
        '      source  = "hashicorp/google"  # canonical\n'
        '      version = "~> 6.0"\n'
        "    }\n"
        "  }\n"
        "}\n"
    )
    gi = GateInput(GateMode.OPERATOR, ("iac/versions.tf",), {"iac/versions.tf": hcl})
    violations = evaluate(gi)
    assert not any(v.rule == "disallowed-provider" for v in violations), violations
    assert violations == []


def _repo_root() -> Path:
    # tests/unit/<this file> -> repo root is two parents up.
    return Path(__file__).resolve().parents[2]


def test_real_committed_iac_tf_files_pass_operator_mode():
    # Pin "the real committed foundation files pass the gate" so the dunder-
    # metadata class of bug can never regress silently again.
    iac_dir = _repo_root() / "iac"
    tf_paths = sorted(iac_dir.glob("*.tf"))
    assert tf_paths, f"expected committed iac/*.tf files under {iac_dir}"
    rel = tuple(str(p.relative_to(_repo_root())) for p in tf_paths)
    hcl_files = {
        str(p.relative_to(_repo_root())): p.read_text(encoding="utf-8") for p in tf_paths
    }
    gi = GateInput(GateMode.OPERATOR, rel, hcl_files)
    assert evaluate(gi) == []


# ---------------------------------------------------------------------------
# Phase 2 import admission rules (design §5, AGENT mode only)
# ---------------------------------------------------------------------------

from tools.iac_static_gate import ADOPT_IMPORT_ID_SHAPES  # noqa: E402
from driftscribe_lib.iac_plan_denylist import ADOPTABLE_RESOURCE_TYPES  # noqa: E402


_BUCKET_IMPORT_PAIR = """\
resource "google_storage_bucket" "old_uploads" {
  name     = "my-old-uploads"
  location = "ASIA-NORTHEAST1"
}
import {
  to = google_storage_bucket.old_uploads
  id = "my-old-uploads"
}
"""

_BUCKET_IMPORT_PAIR_FILE_A = """\
resource "google_storage_bucket" "old_uploads" {
  name     = "my-old-uploads"
  location = "ASIA-NORTHEAST1"
}
"""

_BUCKET_IMPORT_PAIR_FILE_B = """\
import {
  to = google_storage_bucket.old_uploads
  id = "my-old-uploads"
}
"""


def _agent_gi(files: dict[str, str]) -> GateInput:
    return GateInput(
        mode=GateMode.AGENT,
        changed_paths=tuple(files.keys()),
        hcl_files=files,
    )


def _operator_gi(files: dict[str, str]) -> GateInput:
    return GateInput(
        mode=GateMode.OPERATOR,
        changed_paths=tuple(files.keys()),
        hcl_files=files,
    )


def test_import_shapes_cover_exactly_adoptable_types():
    """Drift pin: ADOPT_IMPORT_ID_SHAPES must match ADOPTABLE_RESOURCE_TYPES exactly."""
    assert set(ADOPT_IMPORT_ID_SHAPES) == set(ADOPTABLE_RESOURCE_TYPES)


def test_happy_adopt_pair_single_file_passes():
    """Happy path: import + matching resource in same changed file → no violations."""
    gi = _agent_gi({"iac/adopt_bucket.tf": _BUCKET_IMPORT_PAIR})
    assert evaluate(gi) == []


def test_happy_adopt_pair_split_across_two_files_passes():
    """Pair split across two changed files → still passes."""
    gi = _agent_gi({
        "iac/bucket.tf": _BUCKET_IMPORT_PAIR_FILE_A,
        "iac/imports_section.tf": _BUCKET_IMPORT_PAIR_FILE_B,
    })
    assert evaluate(gi) == []


def test_import_no_to_fires_undeclared():
    """No 'to' key → import-target-undeclared."""
    hcl = 'resource "google_storage_bucket" "b" {}\nimport { id = "my-bucket" }\n'
    violations = evaluate(_agent_gi({"iac/x.tf": hcl}))
    assert any(v.rule == "import-target-undeclared" for v in violations)


def test_import_module_address_fires_undeclared():
    """module.x.y address → import-target-undeclared (not a plain type.name)."""
    hcl = (
        'resource "google_storage_bucket" "b" {}\n'
        'import { to = module.infra.google_storage_bucket.b  id = "my-bucket" }\n'
    )
    violations = evaluate(_agent_gi({"iac/x.tf": hcl}))
    assert any(v.rule == "import-target-undeclared" for v in violations)


def test_import_three_component_address_fires_undeclared():
    """a.b.c address (three components) → import-target-undeclared."""
    hcl = (
        'resource "google_storage_bucket" "b" {}\n'
        'import { to = google_storage_bucket.b.extra  id = "my-bucket" }\n'
    )
    violations = evaluate(_agent_gi({"iac/x.tf": hcl}))
    assert any(v.rule == "import-target-undeclared" for v in violations)


def test_import_indexed_to_fires_indexed():
    """to = google_storage_bucket.b[0] → import-target-indexed."""
    hcl = (
        'resource "google_storage_bucket" "b" {}\n'
        'import { to = google_storage_bucket.b[0]  id = "my-bucket" }\n'
    )
    violations = evaluate(_agent_gi({"iac/x.tf": hcl}))
    assert any(v.rule == "import-target-indexed" for v in violations)
    assert not any(v.rule == "import-target-undeclared" for v in violations)


def test_import_target_resource_with_count_fires_indexed():
    """Target resource block uses count → import-target-indexed."""
    hcl = (
        'resource "google_storage_bucket" "b" { count = 2 }\n'
        'import { to = google_storage_bucket.b  id = "my-bucket" }\n'
    )
    violations = evaluate(_agent_gi({"iac/x.tf": hcl}))
    assert any(v.rule == "import-target-indexed" for v in violations)


def test_import_target_resource_with_for_each_fires_indexed():
    """Target resource block uses for_each → import-target-indexed."""
    hcl = (
        'resource "google_storage_bucket" "b" { for_each = toset([]) }\n'
        'import { to = google_storage_bucket.b  id = "my-bucket" }\n'
    )
    violations = evaluate(_agent_gi({"iac/x.tf": hcl}))
    assert any(v.rule == "import-target-indexed" for v in violations)


def test_import_service_account_type_fires_type_not_adoptable():
    """google_service_account pair → EXACTLY import-type-not-adoptable (the
    pair is declared, plain-addressed, literal-id — nothing else may fire)."""
    hcl = (
        'resource "google_service_account" "sa" { account_id = "my-sa" }\n'
        'import { to = google_service_account.sa  id = "projects/p/serviceAccounts/my-sa@p.iam.gserviceaccount.com" }\n'
    )
    violations = evaluate(_agent_gi({"iac/x.tf": hcl}))
    assert [v.rule for v in violations] == ["import-type-not-adoptable"]


def test_import_indexed_target_and_bad_id_co_emit():
    """Each independently-detectable violation is emitted separately — an
    indexed target does NOT swallow the id check (co-emission contract)."""
    hcl = (
        'resource "google_storage_bucket" "b" {}\n'
        'import { to = google_storage_bucket.b[0]  id = var.bucket_name }\n'
    )
    violations = evaluate(_agent_gi({"iac/x.tf": hcl}))
    assert {v.rule for v in violations} == {"import-target-indexed", "import-id-not-literal"}


def test_import_id_var_reference_fires_not_literal():
    """id = var.x → import-id-not-literal."""
    hcl = (
        'resource "google_storage_bucket" "b" {}\n'
        'import { to = google_storage_bucket.b  id = var.bucket_name }\n'
    )
    violations = evaluate(_agent_gi({"iac/x.tf": hcl}))
    assert any(v.rule == "import-id-not-literal" for v in violations)


def test_import_id_missing_fires_not_literal():
    """id missing entirely → import-id-not-literal."""
    hcl = (
        'resource "google_storage_bucket" "b" {}\n'
        'import { to = google_storage_bucket.b }\n'
    )
    violations = evaluate(_agent_gi({"iac/x.tf": hcl}))
    assert any(v.rule == "import-id-not-literal" for v in violations)


def test_import_id_with_interpolation_fires_not_literal():
    """id = "projects/${var.project}/..." → import-id-not-literal (contains ${)."""
    hcl = (
        'resource "google_pubsub_topic" "t" {}\n'
        'import { to = google_pubsub_topic.t  id = "projects/${var.project}/topics/my-t" }\n'
    )
    violations = evaluate(_agent_gi({"iac/x.tf": hcl}))
    assert any(v.rule == "import-id-not-literal" for v in violations)


def test_import_identity_attribute_fires_not_literal():
    """identity = {...} block → import-id-not-literal."""
    hcl = (
        'resource "google_storage_bucket" "b" {}\n'
        'import {\n  to = google_storage_bucket.b\n  id = "my-bucket"\n  identity = {}\n}\n'
    )
    violations = evaluate(_agent_gi({"iac/x.tf": hcl}))
    assert any(v.rule == "import-id-not-literal" for v in violations)


def test_import_bucket_id_with_slash_fires_shape():
    """Bucket id containing '/' doesn't match bare-name shape → import-id-not-literal."""
    hcl = (
        'resource "google_storage_bucket" "b" {}\n'
        'import { to = google_storage_bucket.b  id = "projects/p/buckets/my-bucket" }\n'
    )
    violations = evaluate(_agent_gi({"iac/x.tf": hcl}))
    assert any(v.rule == "import-id-not-literal" for v in violations)


def test_import_topic_id_bare_name_fires_shape():
    """Pub/Sub topic id as bare name (no projects/.../topics/) → import-id-not-literal."""
    hcl = (
        'resource "google_pubsub_topic" "t" {}\n'
        'import { to = google_pubsub_topic.t  id = "my-topic" }\n'
    )
    violations = evaluate(_agent_gi({"iac/x.tf": hcl}))
    assert any(v.rule == "import-id-not-literal" for v in violations)


def test_import_run_service_id_missing_locations_fires_shape():
    """Cloud Run service id without 'locations' component → import-id-not-literal."""
    hcl = (
        'resource "google_cloud_run_v2_service" "svc" {}\n'
        'import { to = google_cloud_run_v2_service.svc  id = "projects/p/services/my-svc" }\n'
    )
    violations = evaluate(_agent_gi({"iac/x.tf": hcl}))
    assert any(v.rule == "import-id-not-literal" for v in violations)


def test_import_foreach_on_block_fires_foreach_forbidden():
    """for_each on the import block → import-foreach-forbidden."""
    hcl = (
        'resource "google_storage_bucket" "b" {}\n'
        'import { for_each = toset(["a"])  to = google_storage_bucket.b  id = "my-bucket" }\n'
    )
    violations = evaluate(_agent_gi({"iac/x.tf": hcl}))
    assert any(v.rule == "import-foreach-forbidden" for v in violations)


def test_import_two_blocks_same_file_fires_batch_forbidden():
    """Two import blocks in same file → import-batch-forbidden."""
    hcl = (
        'resource "google_storage_bucket" "b1" {}\n'
        'resource "google_storage_bucket" "b2" {}\n'
        'import { to = google_storage_bucket.b1  id = "bucket-one" }\n'
        'import { to = google_storage_bucket.b2  id = "bucket-two" }\n'
    )
    violations = evaluate(_agent_gi({"iac/x.tf": hcl}))
    assert any(v.rule == "import-batch-forbidden" for v in violations)


def test_import_two_blocks_split_files_fires_batch_forbidden():
    """Two import blocks split across files → import-batch-forbidden."""
    file_a = (
        'resource "google_storage_bucket" "b1" {}\n'
        'import { to = google_storage_bucket.b1  id = "bucket-one" }\n'
    )
    file_b = (
        'resource "google_storage_bucket" "b2" {}\n'
        'import { to = google_storage_bucket.b2  id = "bucket-two" }\n'
    )
    violations = evaluate(_agent_gi({"iac/a.tf": file_a, "iac/b.tf": file_b}))
    assert any(v.rule == "import-batch-forbidden" for v in violations)


def test_import_target_in_unparseable_file_fails_closed():
    """Import targeting a resource declared only in an unparseable file →
    hcl-parse-error + import-target-undeclared (fail-closed)."""
    hcl_good = 'import { to = google_storage_bucket.b  id = "my-bucket" }\n'
    hcl_bad = "THIS IS NOT VALID HCL {\n"
    violations = evaluate(_agent_gi({"iac/a.tf": hcl_good, "iac/bad.tf": hcl_bad}))
    rules = {v.rule for v in violations}
    assert "hcl-parse-error" in rules
    assert "import-target-undeclared" in rules


def test_operator_mode_no_import_violations():
    """OPERATOR mode: import blocks in changed files emit no import-* violations."""
    hcl = (
        'import { to = google_storage_bucket.b  id = "my-bucket" }\n'
        'import { to = google_pubsub_topic.t  id = "bare-name" }\n'
    )
    violations = evaluate(_operator_gi({"iac/imports.tf": hcl}))
    assert not any(v.rule.startswith("import-") for v in violations)


def test_non_import_agent_pr_emits_no_import_rules():
    """Regression: a non-import agent PR must not emit any import-* rules."""
    hcl = 'resource "google_storage_bucket" "b" { name = "my-bucket" }\n'
    violations = evaluate(_agent_gi({"iac/bucket.tf": hcl}))
    assert not any(v.rule.startswith("import-") for v in violations)
