"""Shared test fixtures (top-level — applies to both unit and integration).

Keep this file thin: only fixtures that are genuinely reused across the
unit/integration split belong here. Domain-specific helpers stay in the
test files that need them.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def drift_workload_env(monkeypatch):
    """Set the four drift worker URL env vars and reset the workload cache.

    The drift workload's :class:`~agent.workloads.WorkloadResolution` reads
    ``READER_URL`` / ``DOCS_URL`` / ``ROLLBACK_URL`` / ``NOTIFIER_URL`` at
    resolve time. Tests that call :func:`agent.workloads.load_workload`
    (directly, or transitively via :func:`agent.adk_agent.run_agent`) need
    those env vars set *before* the resolve happens — and need the
    module-level cache cleared so the monkeypatched values are honored
    rather than a stale cached resolution.

    The cache is cleared on both setup and teardown: setup so a prior
    test's cache doesn't shadow these env vars, teardown so this test's
    cached resolution doesn't leak placeholder URLs into a downstream
    test that wants the real env (or no env).
    """
    monkeypatch.setenv("READER_URL", "https://reader.test")
    monkeypatch.setenv("DOCS_URL", "https://docs.test")
    monkeypatch.setenv("ROLLBACK_URL", "https://rollback.test")
    monkeypatch.setenv("NOTIFIER_URL", "https://notifier.test")
    import agent.workloads.registry as registry_mod
    registry_mod._WORKLOAD_CACHE.clear()
    yield
    registry_mod._WORKLOAD_CACHE.clear()


@pytest.fixture
def upgrade_workload_env(monkeypatch):
    """Set the three upgrade-relevant worker URL env vars and reset caches.

    The upgrade workload's :class:`~agent.workloads.WorkloadResolution`
    reads ``UPGRADE_READER_URL`` / ``UPGRADE_DOCS_URL`` /
    ``NOTIFIER_URL`` at resolve time (the upgrade workload reuses the
    shared notifier). Mirrors :func:`drift_workload_env` in shape; both
    fixtures clear ``_WORKLOAD_CACHE`` on setup AND teardown so a test
    that exercises one workload doesn't poison subsequent tests that
    exercise the other.

    The upgrade tool wrappers in :mod:`agent.adk_tools` also cache the
    resolved :class:`~agent.workloads.UpgradeTarget` via
    :func:`functools.lru_cache` on the helper ``_get_upgrade_target``;
    clear that cache too so a test that exercises the LLM-facing tools
    sees the current contract+registry state.
    """
    monkeypatch.setenv("UPGRADE_READER_URL", "https://upgrade-reader.test")
    monkeypatch.setenv("UPGRADE_DOCS_URL", "https://upgrade-docs.test")
    monkeypatch.setenv("NOTIFIER_URL", "https://notifier.test")
    import agent.adk_tools as adk_tools_mod
    import agent.workloads.registry as registry_mod
    registry_mod._WORKLOAD_CACHE.clear()
    adk_tools_mod._get_upgrade_target.cache_clear()
    yield
    registry_mod._WORKLOAD_CACHE.clear()
    adk_tools_mod._get_upgrade_target.cache_clear()
