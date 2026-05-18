from unittest.mock import patch

import yaml
from typer.testing import CliRunner

from agent.cli import app

runner = CliRunner()


def test_init_writes_contract_with_conservative_defaults(tmp_path):
    with patch("agent.cli.read_live_env") as m:
        m.return_value = {"PAYMENT_MODE": "mock", "FEATURE_X": "false"}
        result = runner.invoke(app, [
            "init",
            "--service", "payment-demo",
            "--region", "asia-northeast1",
            "--project", "my-proj",
            "--github-repo", "theghostsquad00/driftscribe",
            "--output", str(tmp_path / "ops-contract.yaml"),
        ])
    assert result.exit_code == 0, result.output

    out = yaml.safe_load((tmp_path / "ops-contract.yaml").read_text())
    assert out["cloud_run_service"] == "payment-demo"
    assert out["region"] == "asia-northeast1"
    assert out["github_repo"] == "theghostsquad00/driftscribe"
    assert "PAYMENT_MODE" in out["expected_env"]
    assert "FEATURE_X" in out["expected_env"]
    # Conservative default: no manual change without explicit approval
    assert out["expected_env"]["PAYMENT_MODE"]["allow_manual_change"] is False


def test_init_writes_string_values_even_for_boolean_like_envs(tmp_path):
    with patch("agent.cli.read_live_env") as m:
        m.return_value = {"FLAG": "false", "COUNT": "42"}
        result = runner.invoke(app, [
            "init",
            "--service", "x", "--region", "asia-northeast1",
            "--project", "p", "--github-repo", "x/x",
            "--output", str(tmp_path / "ops-contract.yaml"),
        ])
    assert result.exit_code == 0, result.output
    # Read raw text so we can verify the YAML literal form
    raw = (tmp_path / "ops-contract.yaml").read_text()
    # Both values quoted so YAML doesn't auto-coerce on next load
    assert "value: 'false'" in raw or 'value: "false"' in raw
    assert "value: '42'" in raw or 'value: "42"' in raw


def test_init_resulting_contract_loads_via_load_contract(tmp_path):
    # Round-trip: init writes → load_contract parses it cleanly
    from agent.contract import load_contract
    out_path = tmp_path / "ops-contract.yaml"
    with patch("agent.cli.read_live_env") as m:
        m.return_value = {"PAYMENT_MODE": "mock", "FEATURE_X": "false"}
        result = runner.invoke(app, [
            "init",
            "--service", "payment-demo", "--region", "asia-northeast1",
            "--project", "p", "--github-repo", "x/x",
            "--output", str(out_path),
        ])
    assert result.exit_code == 0
    contract = load_contract(out_path)
    assert contract.expected_env["PAYMENT_MODE"].value == "mock"
    assert contract.expected_env["PAYMENT_MODE"].allow_manual_change is False


def test_init_prints_next_steps(tmp_path):
    with patch("agent.cli.read_live_env") as m:
        m.return_value = {"PAYMENT_MODE": "mock"}
        result = runner.invoke(app, [
            "init",
            "--service", "x", "--region", "asia-northeast1",
            "--project", "p", "--github-repo", "x/x",
            "--output", str(tmp_path / "ops-contract.yaml"),
        ])
    assert result.exit_code == 0
    # "Review before PR" guidance per Codex
    assert "review" in result.output.lower() or "Review" in result.output
    assert "gh pr create" in result.output


def test_init_with_empty_live_env_writes_empty_expected_env(tmp_path):
    with patch("agent.cli.read_live_env") as m:
        m.return_value = {}
        result = runner.invoke(app, [
            "init",
            "--service", "x", "--region", "asia-northeast1",
            "--project", "p", "--github-repo", "x/x",
            "--output", str(tmp_path / "ops-contract.yaml"),
        ])
    assert result.exit_code == 0
    out = yaml.safe_load((tmp_path / "ops-contract.yaml").read_text())
    assert out["expected_env"] == {}
