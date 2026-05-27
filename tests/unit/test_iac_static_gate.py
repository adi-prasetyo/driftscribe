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
