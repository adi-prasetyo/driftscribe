from pathlib import Path
import yaml


def test_e2e_workflow_exists():
    assert Path(".github/workflows/e2e.yml").exists()


def test_e2e_workflow_is_manual_dispatch_only():
    data = yaml.safe_load(Path(".github/workflows/e2e.yml").read_text())
    triggers = data.get("on") or data.get(True)
    assert isinstance(triggers, dict)
    assert "workflow_dispatch" in triggers
    assert "push" not in triggers
    assert "pull_request" not in triggers


def test_e2e_workflow_uses_wif_not_long_lived_keys():
    body = Path(".github/workflows/e2e.yml").read_text()
    assert "workload_identity_provider" in body
    assert "credentials_json" not in body
    assert "id-token: write" in body


def test_e2e_workflow_uses_environment_gate():
    body = Path(".github/workflows/e2e.yml").read_text()
    assert "environment: e2e" in body


def test_e2e_workflow_uses_correct_secret_names():
    body = Path(".github/workflows/e2e.yml").read_text()
    assert "coordinator-shared-token" in body
    assert "upgrade-docs-github-pat" in body
    assert "OPERATOR_TOKEN" not in body
    assert "HITL_HMAC_KEY" not in body


def test_ui_job_runs_even_if_python_fails():
    body = Path(".github/workflows/e2e.yml").read_text()
    assert "needs: python-e2e" in body
    assert "if: ${{ always()" in body or "if: always()" in body
