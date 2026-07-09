"""Integration tests for the operator-seat demo window: anonymous visitors get
the SAME rollback approval link as the operator (docs/plans/2026-07-09-operator-
seat-demo-window.md).

This module previously pinned the hackathon A.2 serve-time scrub, which withheld
the tokenized ``?t=`` approval link from anonymous ``GET /decisions`` /
``GET /trace/{id}`` reads. The 2026-07-09 operator-seat decision REVERSED that
scrub (audit C1 for the rollback link): a visitor sits in the operator's seat,
so the rail's Approve CTA and the timeline's approval links must be live for
them. Safety comes from the bounded blast radius (single-use token, 15-min TTL,
the worker refuses no-op targets, self-healing baseline), not from withholding
the link. This file now pins the REVERSED contract as the risk-acceptance record.

Contract pinned here:

* ``GET /decisions`` — anonymous marker present OR absent → ``approval.approval_url``
  and the ``rendered_body`` link are served INTACT.
* ``GET /trace/{id}`` — anonymous marker present OR absent → the decision's
  approval link AND the event strings are served intact; the timeline cache is
  never scrubbed either.
* ``GET /runs/{id}`` — STILL always scrubbed (unauthenticated; decision_ids
  become enumerable through the demo-window /decisions; nothing in the UI reads
  it). The one surviving serve-time approval scrub.
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
# GET /decisions — anon now sees the live link (operator seat)
# --------------------------------------------------------------------------- #


def test_decisions_marker_keeps_approval_link():
    # Operator-seat reversal: the anonymous marker no longer scrubs the link.
    _seed_rollback_decision()
    resp = TestClient(app).get("/decisions", headers=_MARKER)
    assert resp.status_code == 200
    row = resp.json()["decisions"][0]
    assert row["approval"]["approval_url"] == _URL
    assert _URL in row["rendered_body"]


def test_decisions_without_marker_keeps_approval_link():
    # The operator (CF JWT via Access, or run.app + token — never marked) keeps
    # the rail's approve CTA — unchanged by the reversal.
    _seed_rollback_decision()
    resp = TestClient(app).get("/decisions")
    assert resp.status_code == 200
    row = resp.json()["decisions"][0]
    assert row["approval"]["approval_url"] == _URL


# --------------------------------------------------------------------------- #
# GET /runs/{decision_id} — STILL always scrubbed (the one survivor)
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
# GET /trace/{trace_id} — anon now sees the live link (operator seat)
# --------------------------------------------------------------------------- #


def _install_stub_with_link_event() -> None:
    # An event embedding the tokenized link in a free-prose string — the
    # shape redact_event does NOT catch (it kills secret-named KEYS in
    # structured payloads; prose strings only get userinfo-URL stripping).
    # The final_response entry lets the stability tracker complete/cache the
    # timeline for the cache pin below.
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


def test_trace_marker_keeps_decision_and_events():
    _seed_rollback_decision()
    _install_stub_with_link_event()
    resp = TestClient(app).get(f"/trace/{_TRACE}", headers=_MARKER)
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"]["approval"]["approval_url"] == _URL
    assert _URL in body["events"][0]["summary"]


def test_trace_without_marker_keeps_link():
    _seed_rollback_decision()
    _install_stub_with_link_event()
    resp = TestClient(app).get(f"/trace/{_TRACE}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"]["approval"]["approval_url"] == _URL
    assert _URL in body["events"][0]["summary"]


def test_trace_cache_serves_link_to_anon_and_operator(monkeypatch):
    """After the reversal, BOTH an anonymous (marked) poll and an operator
    (unmarked) poll of the same completed, cached trace see the live tokenized
    link — the cache is no longer scrubbed on either path."""
    _seed_rollback_decision()
    _install_stub_with_link_event()
    client = TestClient(app)

    # Same fake-clock pattern as test_trace_endpoint.py: completion requires
    # the stable signature to age past the grace window.
    fake_now = [1000.0]
    monkeypatch.setattr("agent.main.time.monotonic", lambda: fake_now[0])

    r1 = client.get(f"/trace/{_TRACE}", headers=_MARKER)
    assert r1.status_code == 200
    fake_now[0] += _STABILITY_GRACE_S + 1.0
    r2 = client.get(f"/trace/{_TRACE}", headers=_MARKER)
    assert r2.json()["complete"] is True

    # Operator poll (no marker) — served FROM the cache, link intact.
    r3 = client.get(f"/trace/{_TRACE}")
    assert r3.json()["fetched_from_cache"] is True
    assert _URL in r3.json()["events"][0]["summary"]

    # Anonymous cache-hit poll — link intact too (operator seat).
    r4 = client.get(f"/trace/{_TRACE}", headers=_MARKER)
    assert r4.json()["fetched_from_cache"] is True
    assert _URL in r4.json()["events"][0]["summary"]
