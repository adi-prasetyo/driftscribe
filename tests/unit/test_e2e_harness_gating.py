import subprocess
import sys
from pathlib import Path


def test_e2e_marker_registered():
    body = Path("pyproject.toml").read_text()
    assert 'e2e: end-to-end tests' in body


def test_workers_still_in_testpaths():
    body = Path("pyproject.toml").read_text()
    assert '"workers"' in body


def test_default_pytest_does_not_collect_e2e_dir():
    body = Path("pyproject.toml").read_text()
    assert '"tests/e2e"' not in body


def test_e2e_conftest_skips_without_env(monkeypatch):
    monkeypatch.delenv("DRIFTSCRIBE_E2E_URL", raising=False)
    monkeypatch.delenv("DRIFTSCRIBE_E2E_TOKEN", raising=False)
    # Run (not --collect-only) so fixture-level pytest.skip actually fires;
    # --collect-only doesn't invoke fixtures, so skips never trigger there.
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/e2e", "-m", "e2e"],
        capture_output=True, text=True,
    )
    combined = (result.stdout + result.stderr).lower()
    assert result.returncode == 0, f"e2e harness should exit clean when env is missing; got {result.returncode}\n{combined}"
    assert ("0 tests collected" in combined
            or "skipped" in combined
            or result.returncode == 5), (
        f"expected all e2e tests to skip without env vars; combined output:\n{combined}"
    )
