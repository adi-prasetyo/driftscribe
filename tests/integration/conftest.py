"""Integration test fixtures.

Replaces the previous pattern of module-level ``os.environ[...]`` writes,
which polluted the pytest session and risked leaking into Phase 3+ tests.
"""

import pytest

from agent.auth import verify_token
from agent.config import get_settings
from agent.main import _reset_state_for_tests, app


@pytest.fixture(autouse=True)
def _agent_settings(monkeypatch, request):
    """Set DriftScribe settings for every integration test, then reset cache.

    autouse so individual tests don't have to opt in. monkeypatch undoes the
    env mutations at test teardown; we additionally clear the lru_cache on
    ``get_settings`` and drop the StateStore singleton so each test gets a
    fresh Settings() and an empty InMemoryStateStore.

    Also disables the /recheck token guard by default via
    ``app.dependency_overrides[verify_token]``. The token guard's own tests
    (``test_token_guard.py``) opt OUT via the ``no_auth_override`` marker so
    they exercise the real ``verify_token`` dependency end-to-end.
    """
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("GCP_PROJECT", "test-proj")
    monkeypatch.setenv("CONTRACT_PATH", "demo/ops-contract.yaml")
    monkeypatch.setenv("GITHUB_REPO", "theghostsquad00/driftscribe")
    monkeypatch.setenv("USE_ADK", "false")
    # Phase 17.A.3: drift worker URLs are read at first ``load_workload("drift")``
    # call. The ``/recheck`` and ``/eventarc`` paths now pre-resolve the
    # workload (Codex review fix — previously the classifier path would
    # silently fall through to drift even when the request named a
    # different workload). Setting placeholder URLs autouse-wide is the
    # cleanest way to keep every existing recheck test green without
    # forcing each one to opt in. Tests that specifically exercise the
    # missing-env-var 503 path (test_workload_routing.py) clear these on
    # entry via ``monkeypatch.delenv`` and clear the workload cache.
    monkeypatch.setenv("READER_URL", "https://reader.test")
    monkeypatch.setenv("DOCS_URL", "https://docs.test")
    monkeypatch.setenv("ROLLBACK_URL", "https://rollback.test")
    monkeypatch.setenv("NOTIFIER_URL", "https://notifier.test")
    # DRIFTSCRIBE_TOKEN intentionally NOT set here. Tests that don't care
    # about auth get the dependency_overrides[verify_token] bypass below, so
    # the env var is never consulted. The token-guard tests in
    # test_token_guard.py opt out of the bypass and set their own value via
    # _set_token() — a stale autouse env value would shadow that and hide bugs.
    get_settings.cache_clear()
    _reset_state_for_tests()
    # Clear the workload cache so each test gets a fresh resolution
    # against the env state above. Without this, a test that delenv'd a
    # worker URL would still get the previously-cached resolution.
    import agent.workloads.registry as _registry_mod
    _registry_mod._WORKLOAD_CACHE.clear()

    # Bypass verify_token for tests that don't explicitly exercise the guard.
    # test_token_guard.py marks its tests so we leave the real dep in place.
    skip_override = request.node.get_closest_marker("no_auth_override") is not None
    if not skip_override:
        app.dependency_overrides[verify_token] = lambda: None

    yield

    app.dependency_overrides.pop(verify_token, None)
    get_settings.cache_clear()
    _reset_state_for_tests()
    _registry_mod._WORKLOAD_CACHE.clear()


def pytest_configure(config):
    """Register the ``no_auth_override`` marker so tests can opt out of the
    autouse verify_token override (only test_token_guard.py uses it)."""
    config.addinivalue_line(
        "markers",
        "no_auth_override: keep the real verify_token dependency wired",
    )
