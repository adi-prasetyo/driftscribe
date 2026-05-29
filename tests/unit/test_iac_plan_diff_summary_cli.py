"""CLI tests for `python -m tools.iac_plan_diff_summary`."""

import subprocess
import sys


def _run(stdin: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tools.iac_plan_diff_summary", *args],
        input=stdin,
        capture_output=True,
        text=True,
    )


def _valid_args(**overrides) -> list[str]:
    head = "a" * 40
    run_dir = "run-1234567890-1"
    base = {
        "--head-sha": head,
        "--plan-sha256": "c" * 64,
        "--plan-json-sha256": "d" * 64,
        "--generation-plan": "1700000000000000",
        "--generation-json": "1700000000000001",
        "--generation-metadata": "1700000000000002",
        "--artifact-uri-plan": f"gs://driftscribe-hack-2026-tofu-artifacts/pr-42/{head}/{run_dir}/plan.tfplan",
        "--artifact-uri-json": f"gs://driftscribe-hack-2026-tofu-artifacts/pr-42/{head}/{run_dir}/plan.json",
        "--artifact-uri-metadata": f"gs://driftscribe-hack-2026-tofu-artifacts/pr-42/{head}/{run_dir}/metadata.json",
        "--opentofu-version": "1.12.0",
    }
    base.update(overrides)
    return [flag + "=" + value for flag, value in base.items()]


def test_cli_round_trips_stdin_into_body():
    res = _run("Plan: 1 to add\n", _valid_args())
    assert res.returncode == 0, res.stderr
    assert "Plan: 1 to add" in res.stdout
    assert "1.12.0" in res.stdout


def test_cli_exit_1_on_malformed_sha():
    res = _run("x\n", _valid_args(**{"--head-sha": "nope"}))
    assert res.returncode == 1
    assert "head_sha" in res.stderr
