"""CLI tests for `python -m tools.iac_plan_metadata`."""

import json
import subprocess
import sys


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tools.iac_plan_metadata"],
        env=env,
        capture_output=True,
        text=True,
    )


def _valid_env(**overrides) -> dict[str, str]:
    head = "a" * 40
    run_dir = "run-1234567890-1"
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": ".",
        "META_REPO": "adi-prasetyo/driftscribe",
        "META_PR_NUMBER": "42",
        "META_HEAD_SHA": head,
        "META_BASE_SHA": "b" * 40,
        "META_WORKFLOW_RUN_ID": "1234567890",
        "META_WORKFLOW_RUN_ATTEMPT": "1",
        "META_ARTIFACT_URI_PLAN": f"gs://driftscribe-hack-2026-tofu-artifacts/pr-42/{head}/{run_dir}/plan.tfplan",
        "META_ARTIFACT_URI_JSON": f"gs://driftscribe-hack-2026-tofu-artifacts/pr-42/{head}/{run_dir}/plan.json",
        "META_GENERATION_PLAN": "1700000000000000",
        "META_GENERATION_JSON": "1700000000000001",
        "META_PLAN_SHA256": "c" * 64,
        "META_PLAN_JSON_SHA256": "d" * 64,
        "META_OPENTOFU_VERSION": "1.12.0",
        "META_PROVIDER_LOCKFILE_SHA256": "e" * 64,
    }
    env.update(overrides)
    return env


def test_cli_emits_canonical_json_on_stdout():
    res = _run(_valid_env())
    assert res.returncode == 0, res.stderr
    parsed = json.loads(res.stdout)
    assert parsed["schema_version"] == "c2.v1"
    assert parsed["pr_number"] == 42


def test_cli_exit_2_on_missing_env():
    env = _valid_env()
    del env["META_HEAD_SHA"]
    res = _run(env)
    assert res.returncode == 2
    assert "META_HEAD_SHA" in res.stderr


def test_cli_exit_1_on_invalid_field():
    env = _valid_env(META_HEAD_SHA="not-a-sha")
    res = _run(env)
    assert res.returncode == 1
    assert "head_sha" in res.stderr
