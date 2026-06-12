"""Integration tests for the demo-window approval-link scrub (hackathon A.2).

Rollback decisions persist ``approval.approval_url`` carrying the live,
single-use ``?t=`` HMAC token (and ``rendered_body`` embeds the same URL).
During the hackathon judging window the Worker injects the operator token for
anonymous visitors on ``GET /decisions`` / ``GET /trace/{id}`` and marks those
requests ``X-DriftScribe-Demo-Anonymous: 1`` — without a serve-time scrub the
token would be harvestable CROSS-SESSION (Codex A.2 catch: a visitor could
deny a pending operator rollback, or execute one if the dial were ever at
Propose+Apply).

Contract pinned here:

* ``GET /decisions`` — marker present → ``approval.approval_url`` dropped +
  ``rendered_body`` token redacted; marker absent (operator) → raw preserved.
* ``GET /trace/{id}`` — marker present → decision scrubbed AND event strings
  redacted; the in-process timeline cache is NEVER poisoned by the
  per-request scrub (an operator poll after an anonymous poll sees the link).
* ``GET /runs/{id}`` — ALWAYS scrubbed (unauthenticated; decision_ids become
  enumerable through the demo-window /decisions; nothing in the UI reads it).
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from agent.main import _STABILITY_GRACE_S, app, get_state, get_trace_fetcher
from agent.trace_fetcher import StubTraceFetcher

_TRACE = "f" * 32
_TOKEN = "tok-SECRET_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
_URL = f"https://driftscribe.example.com/approvals/ap-123?t={_TOKEN}"
_MARKER = {"X-DriftScribe-Demo-Anonymous": "1"}


def _seed_rollback_decision(decision_id: str = "dec-rb") -> None:
    state = get_state()
    state.record_event(f"ev-{decision_id}", {})
    state.record_decision(
        decision_id,
        f"ev-{decision_id}",
        {
            "decision_id": decision_id,
            "action": "rollback",
            "trace_id": _TRACE,
            "rationale": "manual env edit detected",
            "rendered_body": f"Operator approval required:\n\n<{_URL}>\n",
            "approval": {
                "approval_id": "ap-123",
                "approval_url": _URL,
                "expires_at": "2026-06-12T12:00:00Z",
            },
        },
    )


# --------------------------------------------------------------------------- #
# GET /decisions
# --------------------------------------------------------------------------- #


def test_decisions_marker_scrubs_approval_link():
    _seed_rollback_decision()
    resp = TestClient(app).get("/decisions", headers=_MARKER)
    assert resp.status_code == 200
    assert _TOKEN not in resp.text
    row = resp.json()["decisions"][0]
    assert "approval_url" not in row["approval"]
    # Non-secret approval fields survive.
    assert row["approval"]["approval_id"] == "ap-123"
    assert "?t=<redacted>" in row["rendered_body"]


def test_decisions_without_marker_keeps_approval_link():
    # The operator (CF JWT via Access, or run.app + token — never marked)
    # keeps the rail's approve CTA.
    _seed_rollback_decision()
    resp = TestClient(app).get("/decisions")
    assert resp.status_code == 200
    row = resp.json()["decisions"][0]
    assert row["approval"]["approval_url"] == _URL


# --------------------------------------------------------------------------- #
# GET /runs/{decision_id} — always scrubbed
# --------------------------------------------------------------------------- #


def test_runs_always_scrubs_approval_link():
    _seed_rollback_decision()
    resp = TestClient(app).get("/runs/dec-rb")
    assert resp.status_code == 200
    assert _TOKEN not in resp.text
    body = resp.json()
    assert "approval_url" not in body["approval"]
    assert "?t=<redacted>" in body["rendered_body"]


# --------------------------------------------------------------------------- #
# GET /trace/{trace_id}
# --------------------------------------------------------------------------- #


def _install_stub_with_link_event() -> None:
    # An event embedding the tokenized link in a free-prose string — the
    # shape redact_event does NOT catch (it kills secret-named KEYS in
    # structured payloads; prose strings only get userinfo-URL stripping).
    # The final_response entry lets the stability tracker complete/cache the
    # timeline for the cache-poisoning pin below.
    stub = StubTraceFetcher(
        entries=[
            {
                "event": "tool_result",
                "trace_id": _TRACE,
                "timestamp": "2026-06-12T00:00:00Z",
                "insert_id": "1",
                "summary": f"rollback proposed, approve at {_URL}",
            },
            {
                "event": "final_response",
                "trace_id": _TRACE,
                "text": "done",
                "timestamp": "2026-06-12T00:00:01Z",
                "insert_id": "2",
            },
        ]
    )
    app.dependency_overrides[get_trace_fetcher] = lambda: stub


def test_trace_marker_scrubs_decision_and_events():
    _seed_rollback_decision()
    _install_stub_with_link_event()
    resp = TestClient(app).get(f"/trace/{_TRACE}", headers=_MARKER)
    assert resp.status_code == 200
    assert _TOKEN not in resp.text
    body = resp.json()
    assert "approval_url" not in body["decision"]["approval"]
    assert "?t=<redacted>" in body["events"][0]["summary"]


def test_trace_without_marker_keeps_link():
    _seed_rollback_decision()
    _install_stub_with_link_event()
    resp = TestClient(app).get(f"/trace/{_TRACE}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"]["approval"]["approval_url"] == _URL
    assert _URL in body["events"][0]["summary"]


def test_trace_scrub_does_not_poison_cache_for_operator(monkeypatch):
    """An anonymous (marked) poll must not leave scrubbed events in the
    server-side timeline cache: a later operator (unmarked) poll of the SAME
    completed trace must still see the tokenized link (and the cache-hit
    anonymous poll must still scrub)."""
    _seed_rollback_decision()
    _install_stub_with_link_event()
    client = TestClient(app)

    # Same fake-clock pattern as test_trace_endpoint.py: completion requires
    # the stable signature to age past the grace window.
    fake_now = [1000.0]
    monkeypatch.setattr("agent.main.time.monotonic", lambda: fake_now[0])

    r1 = client.get(f"/trace/{_TRACE}", headers=_MARKER)
    assert r1.status_code == 200
    assert _TOKEN not in r1.text
    fake_now[0] += _STABILITY_GRACE_S + 1.0
    r2 = client.get(f"/trace/{_TRACE}", headers=_MARKER)
    assert r2.json()["complete"] is True
    assert _TOKEN not in r2.text

    # Operator poll (no marker) — served FROM the cache, link intact.
    r3 = client.get(f"/trace/{_TRACE}")
    assert r3.json()["fetched_from_cache"] is True
    assert _URL in r3.json()["events"][0]["summary"]

    # Anonymous cache-hit poll — still scrubbed.
    r4 = client.get(f"/trace/{_TRACE}", headers=_MARKER)
    assert r4.json()["fetched_from_cache"] is True
    assert _TOKEN not in r4.text
