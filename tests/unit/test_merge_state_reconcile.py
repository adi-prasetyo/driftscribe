"""Unit tests for the serve-time merge_state reconcile (2026-06-27 follow-up).

``reconcile_merge_state`` promotes a stale ``applied`` + ``merge_state="failed"``
decision to ``merged`` when GitHub confirms the PR is merged at the as-applied
``head_sha`` — compute-only, never persists. Backed by a per-PR terminal-state
cache so a confirmed merge is never re-probed.

No network: a fake repo + an in-memory merge cache injected via the test seam.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from agent.main import (
    _IAC_PR_MERGE_FORMAT_VERSION,
    reconcile_merge_state,
    _resolve_pr_merged,
    _set_iac_pr_merge_cache_store_for_tests,
)
from agent.per_pr_cache import InMemoryPerPrCacheStore


@pytest.fixture
def merge_cache():
    store = InMemoryPerPrCacheStore()
    _set_iac_pr_merge_cache_store_for_tests(store)
    yield store
    _set_iac_pr_merge_cache_store_for_tests(None)


def _settings(token="ghp_x", repo="o/r"):
    return SimpleNamespace(github_token=token, github_repo=repo)


def _fake_repo(*, merged, head_sha):
    calls = {"n": 0}

    def get_pull(n):
        calls["n"] += 1
        return SimpleNamespace(merged=merged, head=SimpleNamespace(sha=head_sha))

    return SimpleNamespace(get_pull=get_pull, calls=calls)


def _provider(repo):
    return lambda: repo


def _dec(**kw):
    base = {
        "action": "iac_apply",
        "apply_status": "applied",
        "merge_state": "failed",
        "pr_number": 32,
        "head_sha": "c" * 40,
    }
    base.update(kw)
    return base


# --- happy path ------------------------------------------------------------

def test_promotes_applied_failed_to_merged(merge_cache):
    repo = _fake_repo(merged=True, head_sha="c" * 40)
    out = reconcile_merge_state(_dec(), repo_provider=_provider(repo), settings=_settings())
    assert out["merge_state"] == "merged"
    assert out["merge_reconciled"] is True


def test_does_not_mutate_input(merge_cache):
    repo = _fake_repo(merged=True, head_sha="c" * 40)
    dec = _dec()
    reconcile_merge_state(dec, repo_provider=_provider(repo), settings=_settings())
    assert dec["merge_state"] == "failed"
    assert "merge_reconciled" not in dec


# --- the head-match guard (Codex MF1) --------------------------------------

def test_refuses_when_head_moved(merge_cache):
    # merged, but at a DIFFERENT head than what was applied → not a reconcile.
    repo = _fake_repo(merged=True, head_sha="d" * 40)
    dec = _dec(head_sha="c" * 40)
    out = reconcile_merge_state(dec, repo_provider=_provider(repo), settings=_settings())
    assert out is dec  # identity, unchanged


def test_leaves_failed_when_not_merged(merge_cache):
    repo = _fake_repo(merged=False, head_sha="c" * 40)
    dec = _dec()
    out = reconcile_merge_state(dec, repo_provider=_provider(repo), settings=_settings())
    assert out is dec


# --- entry-gate identity cases ---------------------------------------------

@pytest.mark.parametrize("dec", [
    {"action": "drift_report", "apply_status": "applied", "merge_state": "failed"},
    _dec(apply_status="waiting_for_rebake"),
    _dec(merge_state="merged"),
    _dec(merge_state="n/a"),
    _dec(pr_number=0),
    _dec(pr_number="32"),
    _dec(pr_number=True),  # bool must not pass as int
    _dec(head_sha=""),
    _dec(head_sha=None),
    "not-a-dict",
])
def test_identity_for_ineligible(dec, merge_cache):
    repo = _fake_repo(merged=True, head_sha="c" * 40)
    out = reconcile_merge_state(dec, repo_provider=_provider(repo), settings=_settings())
    assert out is dec
    assert repo.calls["n"] == 0  # never probed GitHub


# --- gating + fail-soft ----------------------------------------------------

def test_no_token_is_identity_and_no_github(merge_cache):
    repo = _fake_repo(merged=True, head_sha="c" * 40)
    dec = _dec()
    out = reconcile_merge_state(dec, repo_provider=_provider(repo), settings=_settings(token=""))
    assert out is dec
    assert repo.calls["n"] == 0


def test_repo_provider_none_is_identity(merge_cache):
    dec = _dec()
    out = reconcile_merge_state(dec, repo_provider=lambda: None, settings=_settings())
    assert out is dec


def test_github_error_is_fail_soft_identity(merge_cache):
    def boom():
        raise RuntimeError("github down")
    repo = SimpleNamespace(get_pull=lambda n: (_ for _ in ()).throw(RuntimeError("boom")))
    dec = _dec()
    out = reconcile_merge_state(dec, repo_provider=_provider(repo), settings=_settings())
    assert out is dec


# --- terminal-state cache --------------------------------------------------

def test_merged_true_is_cached_no_second_github_call(merge_cache):
    repo = _fake_repo(merged=True, head_sha="c" * 40)
    prov = _provider(repo)
    a = _resolve_pr_merged(32, "c" * 40, repo_provider=prov, settings=_settings())
    b = _resolve_pr_merged(32, "c" * 40, repo_provider=prov, settings=_settings())
    assert a is True and b is True
    assert repo.calls["n"] == 1  # second resolve served from cache


def test_cache_miss_on_head_sha_change(merge_cache):
    repo = _fake_repo(merged=True, head_sha="c" * 40)
    prov = _provider(repo)
    _resolve_pr_merged(32, "c" * 40, repo_provider=prov, settings=_settings())
    # a different applied head_sha for the same PR is a cache miss → re-probe
    repo2 = _fake_repo(merged=True, head_sha="e" * 40)
    out = _resolve_pr_merged(32, "e" * 40, repo_provider=_provider(repo2), settings=_settings())
    assert out is True
    assert repo2.calls["n"] == 1


def test_merged_false_within_short_ttl_is_served_from_cache(merge_cache):
    # A FRESH merged=False is within the short TTL → served, no GitHub probe.
    merge_cache.set(
        32,
        {
            "format_version": _IAC_PR_MERGE_FORMAT_VERSION,
            "head_sha": "c" * 40,
            "merged": False,
            "written_at": time.time(),
        },
    )
    repo = _fake_repo(merged=True, head_sha="c" * 40)
    out = _resolve_pr_merged(32, "c" * 40, repo_provider=_provider(repo), settings=_settings())
    assert out is False
    assert repo.calls["n"] == 0


def test_stale_merged_false_is_reprobed_not_served(merge_cache):
    # A STALE merged=False (written 200s ago, past _IAC_PR_MERGE_UNMERGED_TTL_S=120)
    # must be a cache MISS → re-probe, since a not-yet-merged PR may have merged.
    # Guards against a regression that makes a False verdict terminal.
    merge_cache.set(
        32,
        {
            "format_version": _IAC_PR_MERGE_FORMAT_VERSION,
            "head_sha": "c" * 40,
            "merged": False,
            "written_at": time.time() - 200,
        },
    )
    repo = _fake_repo(merged=True, head_sha="c" * 40)
    out = _resolve_pr_merged(32, "c" * 40, repo_provider=_provider(repo), settings=_settings())
    assert out is True  # stale False NOT served
    assert repo.calls["n"] == 1  # a fresh probe was made
