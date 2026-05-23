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
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/e2e", "-m", "e2e", "--collect-only"],
        capture_output=True, text=True,
    )
    combined = (result.stdout + result.stderr).lower()
    assert ("0 tests collected" in combined
            or "skipping" in combined
            or result.returncode == 5)
