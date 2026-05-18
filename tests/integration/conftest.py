"""Integration test fixtures.

Replaces the previous pattern of module-level ``os.environ[...]`` writes,
which polluted the pytest session and risked leaking into Phase 3+ tests.
"""

import pytest

from agent.config import get_settings
from agent.main import _reset_state_for_tests


@pytest.fixture(autouse=True)
def _agent_settings(monkeypatch):
    """Set DriftScribe settings for every integration test, then reset cache.

    autouse so individual tests don't have to opt in. monkeypatch undoes the
    env mutations at test teardown; we additionally clear the lru_cache on
    ``get_settings`` and drop the StateStore singleton so each test gets a
    fresh Settings() and an empty InMemoryStateStore.
    """
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("GCP_PROJECT", "test-proj")
    monkeypatch.setenv("CONTRACT_PATH", "demo/ops-contract.yaml")
    monkeypatch.setenv("GITHUB_REPO", "theghostsquad00/driftscribe")
    monkeypatch.setenv("USE_ADK", "false")
    get_settings.cache_clear()
    _reset_state_for_tests()
    yield
    get_settings.cache_clear()
    _reset_state_for_tests()
