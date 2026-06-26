"""Integration tests for GET /trace/{trace_id}/pr-body (open-trace PR-body
disclosure, 2026-06-27).

Token-gated; binds to the persisted iac_apply decision (resolved by trace_id),
derives head_sha server-side, lazy-fetches + scrubs + caches the agent-authored
PR body, and is fail-soft to ``body: null``. No network: ``agent.main.get_repo``
is monkeypatched. The autouse conftest resets the PR-body cache between tests.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from agent.config import get_settings
from agent.main import app, get_state

_HEAD = "a1b2c3d4" * 5  # 40-char hex
_TRACE = "e" * 32


def _configure(monkeypatch, *, token="ghp_test"):
    monkeypatch.setenv("GITHUB_REPO", "adi-prasetyo/driftscribe")
    monkeypatch.setenv("GITHUB_TOKEN", token)
    monkeypatch.setenv("GCP_PROJECT", "")
    get_settings.cache_clear()


def _fake_repo(*, body, counter):
    def get_pull(n):
        counter["n"] += 1
        return SimpleNamespace(body=body)

    return SimpleNamespace(get_pull=get_pull)


def _raising_repo():
    def get_pull(n):
        raise RuntimeError("github down")

    return SimpleNamespace(get_pull=get_pull)


def _seed(*, action="iac_apply", pr=32, head=_HEAD, trace=_TRACE):
    state = get_state()
    state.record_event("ev-body", {})
    d = {
        "decision_id": "dec-body",
        "action": action,
        "trace_id": trace,
        "created_at": datetime(2026, 6, 26, tzinfo=timezone.utc),
    }
    if action == "iac_apply":
        d.update(
            {
                "pr_number": pr,
                "head_sha": head,
                "apply_status": "applied",
                "merge_state": "merged",
            }
        )
    state.record_decision("dec-body", "ev-body", d)


def test_pr_body_returned_on_cache_miss(monkeypatch):
    _configure(monkeypatch)
    counter = {"n": 0}
    body = "## Repoints payment-demo\n\nWhy: completes the C5f isolation.\n"
    monkeypatch.setattr(
        "agent.main.get_repo", lambda token, repo: _fake_repo(body=body, counter=counter)
    )
    _seed()
    resp = TestClient(app).get(f"/trace/{_TRACE}/pr-body")
    assert resp.status_code == 200
    j = resp.json()
    assert j["pr_number"] == 32
    assert j["head_sha"] == _HEAD
    assert j["body"] == body
    assert j["body_truncated"] is False
    assert j["cached"] is False
    assert counter["n"] == 1
    assert resp.headers.get("cache-control") == "no-store"


def test_pr_body_cache_hit_no_second_github_call(monkeypatch):
    _configure(monkeypatch)
    counter = {"n": 0}
    monkeypatch.setattr(
        "agent.main.get_repo", lambda token, repo: _fake_repo(body="hello", counter=counter)
    )
    _seed()
    client = TestClient(app)
    client.get(f"/trace/{_TRACE}/pr-body")
    second = client.get(f"/trace/{_TRACE}/pr-body").json()
    assert second["body"] == "hello"
    assert second["cached"] is True
    assert counter["n"] == 1


def test_pr_body_404_for_non_iac_decision(monkeypatch):
    _configure(monkeypatch)
    _seed(action="drift_issue")
    resp = TestClient(app).get(f"/trace/{_TRACE}/pr-body")
    assert resp.status_code == 404


def test_pr_body_404_when_no_decision_for_trace(monkeypatch):
    _configure(monkeypatch)
    resp = TestClient(app).get(f"/trace/{'d' * 32}/pr-body")
    assert resp.status_code == 404


def test_pr_body_400_on_bad_trace_id(monkeypatch):
    _configure(monkeypatch)
    resp = TestClient(app).get("/trace/NOT-HEX/pr-body")
    assert resp.status_code == 400
    assert resp.headers.get("cache-control") == "no-store"


def test_pr_body_null_when_no_github_token(monkeypatch):
    _configure(monkeypatch, token="")
    called = {"n": 0}

    def _get_repo(token, repo):
        called["n"] += 1
        return object()

    monkeypatch.setattr("agent.main.get_repo", _get_repo)
    _seed()
    resp = TestClient(app).get(f"/trace/{_TRACE}/pr-body")
    assert resp.status_code == 200
    assert resp.json()["body"] is None
    assert called["n"] == 0


def test_pr_body_null_when_token_removed_even_with_warm_cache(monkeypatch):
    # Codex completed-work review: the github-config gate runs BEFORE the cache
    # read, so a warm cached body must NOT leak once the token is unset (honours
    # the documented "no token -> body:null" contract).
    _configure(monkeypatch)
    counter = {"n": 0}
    monkeypatch.setattr(
        "agent.main.get_repo",
        lambda token, repo: _fake_repo(body="cached body", counter=counter),
    )
    _seed()
    client = TestClient(app)
    first = client.get(f"/trace/{_TRACE}/pr-body").json()
    assert first["body"] == "cached body"  # warmed the cache
    # Now unset the token: the gate-before-cache must hide even the warm body.
    monkeypatch.setenv("GITHUB_TOKEN", "")
    get_settings.cache_clear()
    second = client.get(f"/trace/{_TRACE}/pr-body").json()
    assert second["body"] is None
    assert counter["n"] == 1  # no second GitHub fetch either


def test_pr_body_fail_soft_on_github_error(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr("agent.main.get_repo", lambda token, repo: _raising_repo())
    _seed()
    resp = TestClient(app).get(f"/trace/{_TRACE}/pr-body")
    assert resp.status_code == 200
    assert resp.json()["body"] is None


def test_pr_body_scrubs_rollback_approval_token(monkeypatch):
    _configure(monkeypatch)
    body = "Roll back at https://x.run.app/approvals/abc?t=SECRETTOKEN now."
    monkeypatch.setattr(
        "agent.main.get_repo",
        lambda token, repo: SimpleNamespace(get_pull=lambda n: SimpleNamespace(body=body)),
    )
    _seed()
    j = TestClient(app).get(f"/trace/{_TRACE}/pr-body").json()
    assert "SECRETTOKEN" not in j["body"]
    assert "/approvals/abc?t=<redacted>" in j["body"]


def test_pr_body_truncates_oversize(monkeypatch):
    _configure(monkeypatch)
    big = "x" * 20000
    monkeypatch.setattr(
        "agent.main.get_repo",
        lambda token, repo: SimpleNamespace(get_pull=lambda n: SimpleNamespace(body=big)),
    )
    _seed()
    j = TestClient(app).get(f"/trace/{_TRACE}/pr-body").json()
    assert j["body_truncated"] is True
    assert len(j["body"]) == 16384
