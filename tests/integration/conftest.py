"""Integration test fixtures.

Replaces the previous pattern of module-level ``os.environ[...]`` writes,
which polluted the pytest session and risked leaking into Phase 3+ tests.
"""

import pytest

from agent.config import get_settings


@pytest.fixture(autouse=True)
def _agent_settings(monkeypatch):
    """Set DriftScribe settings for every integration test, then reset cache.

    autouse so individual tests don't have to opt in. monkeypatch undoes the
    env mutations at test teardown; we additionally clear the lru_cache on
    ``get_settings`` so each test gets a fresh Settings() instance reading
    the current env.
    """
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("GCP_PROJECT", "test-proj")
    monkeypatch.setenv("CONTRACT_PATH", "demo/ops-contract.yaml")
    monkeypatch.setenv("GITHUB_REPO", "theghostsquad00/driftscribe")
    monkeypatch.setenv("USE_ADK", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
