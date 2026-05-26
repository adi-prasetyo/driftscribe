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
