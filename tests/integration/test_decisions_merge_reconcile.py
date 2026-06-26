"""Integration tests for the serve-time merge_state reconcile (2026-06-27).

A stale ``applied`` + ``merge_state="failed"`` iac_apply decision is promoted to
``merged`` on the /decisions and /trace serve paths when GitHub confirms the PR
merged at the as-applied head_sha — compute-only (no persist), backed by a
terminal-state cache. The rail and the open-trace card must agree.

No network: ``agent.main.get_repo`` is monkeypatched to a fake repo. The autouse
conftest resets the merge-status cache between tests.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from agent.config import get_settings
from agent.main import app, get_state

_HEAD = "a1b2c3d4" * 5  # 40-char hex
_TRACE = "f" * 32


def _configure(monkeypatch, *, token="ghp_test"):
    monkeypatch.setenv("GITHUB_REPO", "adi-prasetyo/driftscribe")
    monkeypatch.setenv("GITHUB_TOKEN", token)
    monkeypatch.setenv("GCP_PROJECT", "")  # keep the cache + state on InMemory
    get_settings.cache_clear()


def _fake_repo(*, merged, head_sha, counter):
    def get_pull(n):
        counter["n"] += 1
        return SimpleNamespace(merged=merged, head=SimpleNamespace(sha=head_sha))

    return SimpleNamespace(get_pull=get_pull)


def _raising_repo(counter):
    def get_pull(n):
        counter["n"] += 1
        raise RuntimeError("github down")

    return SimpleNamespace(get_pull=get_pull)


def _seed_applied_failed(*, pr=32, head=_HEAD, trace=_TRACE):
    state = get_state()
    state.record_event("ev-recon", {})
    state.record_decision(
        "dec-recon",
        "ev-recon",
        {
            "decision_id": "dec-recon",
            "action": "iac_apply",
            "trace_id": trace,
            "pr_number": pr,
            "head_sha": head,
            "apply_status": "applied",
            "merge_state": "failed",
            "created_at": datetime(2026, 5, 30, 11, 16, tzinfo=timezone.utc),
        },
    )


def test_decisions_reconciles_applied_failed_to_merged(monkeypatch):
    _configure(monkeypatch)
    counter = {"n": 0}
    monkeypatch.setattr(
        "agent.main.get_repo",
        lambda token, repo: _fake_repo(merged=True, head_sha=_HEAD, counter=counter),
    )
    _seed_applied_failed()
    row = TestClient(app).get("/decisions").json()["decisions"][0]
    assert row["merge_state"] == "merged"
    assert row["merge_reconciled"] is True
    assert counter["n"] == 1


def test_decisions_reconcile_is_cached_no_second_github_call(monkeypatch):
    _configure(monkeypatch)
    counter = {"n": 0}
    monkeypatch.setattr(
        "agent.main.get_repo",
        lambda token, repo: _fake_repo(merged=True, head_sha=_HEAD, counter=counter),
    )
    _seed_applied_failed()
    client = TestClient(app)
    client.get("/decisions")
    client.get("/decisions")
    assert counter["n"] == 1  # merged=True is terminal → cached, no 2nd probe


def test_decisions_not_merged_leaves_failed(monkeypatch):
    _configure(monkeypatch)
    counter = {"n": 0}
    monkeypatch.setattr(
        "agent.main.get_repo",
        lambda token, repo: _fake_repo(merged=False, head_sha=_HEAD, counter=counter),
    )
    _seed_applied_failed()
    row = TestClient(app).get("/decisions").json()["decisions"][0]
    assert row["merge_state"] == "failed"
    assert "merge_reconciled" not in row


def test_decisions_head_moved_does_not_reconcile(monkeypatch):
    # merged, but at a DIFFERENT head than what was applied → must NOT promote (MF1).
    _configure(monkeypatch)
    counter = {"n": 0}
    monkeypatch.setattr(
        "agent.main.get_repo",
        lambda token, repo: _fake_repo(merged=True, head_sha="9" * 40, counter=counter),
    )
    _seed_applied_failed(head=_HEAD)
    row = TestClient(app).get("/decisions").json()["decisions"][0]
    assert row["merge_state"] == "failed"


def test_decisions_no_token_leaves_failed_and_no_github(monkeypatch):
    _configure(monkeypatch, token="")
    called = {"n": 0}

    def _get_repo(token, repo):
        called["n"] += 1
        return object()

    monkeypatch.setattr("agent.main.get_repo", _get_repo)
    _seed_applied_failed()
    row = TestClient(app).get("/decisions").json()["decisions"][0]
    assert row["merge_state"] == "failed"
    assert "merge_reconciled" not in row
    assert called["n"] == 0


def test_decisions_github_error_is_fail_soft(monkeypatch):
    _configure(monkeypatch)
    counter = {"n": 0}
    monkeypatch.setattr("agent.main.get_repo", lambda token, repo: _raising_repo(counter))
    _seed_applied_failed()
    resp = TestClient(app).get("/decisions")
    assert resp.status_code == 200
    assert resp.json()["decisions"][0]["merge_state"] == "failed"


def test_trace_agrees_with_decisions_reconcile(monkeypatch):
    _configure(monkeypatch)
    counter = {"n": 0}
    monkeypatch.setattr(
        "agent.main.get_repo",
        lambda token, repo: _fake_repo(merged=True, head_sha=_HEAD, counter=counter),
    )
    _seed_applied_failed(trace=_TRACE)
    resp = TestClient(app).get(f"/trace/{_TRACE}")
    assert resp.status_code == 200
    assert resp.json()["decision"]["merge_state"] == "merged"
    assert resp.json()["decision"]["merge_reconciled"] is True
