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
