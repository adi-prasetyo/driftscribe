"""CLI tests for ``python -m tools.iac_plan_denylist``.

The CLI is a thin wrapper around :func:`tools.iac_plan_denylist.evaluate`
plus :func:`tools.iac_plan_denylist.load_plan_json`. These tests pin its
exit-code contract (0 / 1 / 2) and ASCII-only stderr output.
"""

import subprocess
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent.parent / "fixtures" / "iac_plan_denylist"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(*args: str) -> subprocess.CompletedProcess:
    """Invoke ``python -m tools.iac_plan_denylist`` with the given args.

    Uses the same interpreter as the test runner so the module's import
    path is identical to the pytest environment.
    """
    return subprocess.run(
        [sys.executable, "-m", "tools.iac_plan_denylist", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_no_args_exits_2_with_usage():
    proc = _run()
    assert proc.returncode == 2
    assert "usage:" in (proc.stderr + proc.stdout).lower()


def test_nonexistent_file_exits_2():
    proc = _run(str(FIXTURES / "does_not_exist.json"))
    assert proc.returncode == 2
    assert "cannot read" in proc.stderr.lower() or "error" in proc.stderr.lower()


def test_unparseable_plan_exits_1():
    proc = _run(str(FIXTURES / "unparseable_empty_file.json"))
    assert proc.returncode == 1
    assert "plan-json-unparseable" in proc.stderr


def test_clean_plan_exits_0():
    proc = _run(str(FIXTURES / "benign_no_op.json"))
    assert proc.returncode == 0
    assert "OK" in proc.stdout or "0 violations" in proc.stdout


def test_control_plane_change_exits_1_and_names_rule():
    proc = _run(str(FIXTURES / "control_plane_coordinator_update.json"))
    assert proc.returncode == 1
    assert "control-plane-service" in proc.stderr


@pytest.mark.parametrize(
    "fixture",
    [
        "control_plane_coordinator_update.json",
        "delete_unprotected_resource.json",
        "wif_pool_update.json",
    ],
)
def test_cli_output_is_ascii_only(fixture):
    """ASCII-only CLI output (Codex nit) — no em-dashes, no Unicode in
    stderr/stdout messages emitted by the CLI. This catches an accidental
    em-dash slipping in via a future f-string. Fixtures whose _test_intent
    contains Unicode are fine because the CLI never echoes that field.
    """
    proc = _run(str(FIXTURES / fixture))
    for stream_name, content in (("stdout", proc.stdout), ("stderr", proc.stderr)):
        try:
            content.encode("ascii")
        except UnicodeEncodeError as e:
            pytest.fail(f"{fixture}: non-ASCII in {stream_name}: {e!r}: {content!r}")
