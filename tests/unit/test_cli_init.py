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


def test_init_skips_secret_named_vars(tmp_path):
    """Vars whose names match the secret-name heuristic must NOT be written to
    the contract (they'd leak as plaintext into a public PR). The CLI must warn
    the operator naming each skipped var."""
    with patch("agent.cli.read_live_env") as m:
        m.return_value = {"PAYMENT_MODE": "mock", "API_TOKEN": "abc123"}
        result = runner.invoke(app, [
            "init",
            "--service", "x", "--region", "asia-northeast1",
            "--project", "p", "--github-repo", "x/x",
            "--output", str(tmp_path / "ops-contract.yaml"),
        ])
    assert result.exit_code == 0, result.output
    out = yaml.safe_load((tmp_path / "ops-contract.yaml").read_text())
    assert "PAYMENT_MODE" in out["expected_env"]
    assert "API_TOKEN" not in out["expected_env"]
    # Operator must be told what was skipped and why
    assert "API_TOKEN" in result.output
    assert "secret" in result.output.lower()


def test_init_skips_credentialed_url_values(tmp_path):
    """Vars whose VALUE looks like a credential URL (scheme://user:pass@host)
    must be skipped even if the name is innocuous like DB_HOST."""
    with patch("agent.cli.read_live_env") as m:
        m.return_value = {"DB_HOST": "postgres://u:pw@h/d", "PAYMENT_MODE": "mock"}
        result = runner.invoke(app, [
            "init",
            "--service", "x", "--region", "asia-northeast1",
            "--project", "p", "--github-repo", "x/x",
            "--output", str(tmp_path / "ops-contract.yaml"),
        ])
    assert result.exit_code == 0, result.output
    out = yaml.safe_load((tmp_path / "ops-contract.yaml").read_text())
    assert "PAYMENT_MODE" in out["expected_env"]
    assert "DB_HOST" not in out["expected_env"]
    assert "DB_HOST" in result.output
    # Value-based warning should mention credential / URL phrasing
    assert "credential" in result.output.lower() or "url" in result.output.lower()


def test_init_skips_both_kinds_in_warning_output(tmp_path):
    """A single invocation surfacing both kinds of skip should emit both
    warnings."""
    with patch("agent.cli.read_live_env") as m:
        m.return_value = {
            "PAYMENT_MODE": "mock",
            "API_TOKEN": "abc123",
            "DB_HOST": "postgres://u:pw@h/d",
        }
        result = runner.invoke(app, [
            "init",
            "--service", "x", "--region", "asia-northeast1",
            "--project", "p", "--github-repo", "x/x",
            "--output", str(tmp_path / "ops-contract.yaml"),
        ])
    assert result.exit_code == 0, result.output
    out = yaml.safe_load((tmp_path / "ops-contract.yaml").read_text())
    assert list(out["expected_env"].keys()) == ["PAYMENT_MODE"]
    assert "API_TOKEN" in result.output
    assert "DB_HOST" in result.output
    # Hint for the operator on how to recover
    assert "Secret Manager" in result.output or "secret manager" in result.output.lower()


def test_init_applies_docs_file_and_section_overrides(tmp_path):
    """--docs-file and --docs-section must propagate into every var's docs block."""
    with patch("agent.cli.read_live_env") as m:
        m.return_value = {"PAYMENT_MODE": "mock", "FEATURE_X": "false"}
        result = runner.invoke(app, [
            "init",
            "--service", "x", "--region", "asia-northeast1",
            "--project", "p", "--github-repo", "x/x",
            "--output", str(tmp_path / "ops-contract.yaml"),
            "--docs-file", "custom/path.md",
            "--docs-section", "Custom Section",
        ])
    assert result.exit_code == 0, result.output
    out = yaml.safe_load((tmp_path / "ops-contract.yaml").read_text())
    for name, spec in out["expected_env"].items():
        assert spec["docs"]["file"] == "custom/path.md", name
        assert spec["docs"]["section"] == "Custom Section", name
