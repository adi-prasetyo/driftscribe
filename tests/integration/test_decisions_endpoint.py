"""Integration tests for ``GET /decisions`` (Phase 19.A.7).

Backs the operator-facing decision-history panel on
``/ui/transparency``. Pins the route contract:

* 401 without the ``X-DriftScribe-Token`` header (when the guard is
  configured) — same shape as /recheck.
* 400 on an out-of-range ``limit`` query param, with
  ``Cache-Control: no-store`` so an operator's browser doesn't cache
  a transient validation error.
* 200 OK with newest-first decision shape on the happy path.
* ``Cache-Control: no-store`` on every success response — the listing
  reflects mutable server state, no proxy / browser cache should hold
  a stale view.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from agent.config import get_settings
from agent.main import app, get_state


# --------------------------------------------------------------------------- #
# Happy path — 200 OK, newest-first, Cache-Control: no-store
# --------------------------------------------------------------------------- #


def test_list_decisions_returns_newest_first():
    """The endpoint must surface decisions sorted by ``created_at``
    descending. ``record_decision`` backfills ``created_at`` with
    ``datetime.now(UTC)`` for InMemoryStateStore — we pass explicit
    timestamps in the payload so the ordering is deterministic
    independent of wall clock."""
    state = get_state()
    base = datetime(2026, 5, 21, 0, 0, 0, tzinfo=timezone.utc)
    for i in range(3):
        state.record_event(f"ev-{i}", {})
        state.record_decision(
            f"dec-{i}",
            f"ev-{i}",
            {
                "action": "no_op",
                "n": i,
                "created_at": base + timedelta(seconds=i),
            },
        )

    client = TestClient(app)
    resp = client.get("/decisions")
    assert resp.status_code == 200
    body = resp.json()
    assert list(body.keys()) == ["decisions"]
    ns = [d["n"] for d in body["decisions"]]
    assert ns == [2, 1, 0]


def test_list_decisions_scrubs_secret_in_rationale():
    """PR 2 — the rail/listing must not surface a secret quoted in an LLM
    rationale. The store is reset per test, so the single recorded decision is
    the only row."""
    state = get_state()
    secret = "sk-RAIL-7777"
    state.record_event("ev-rail", {})
    state.record_decision(
        "dec-rail",
        "ev-rail",
        {
            "decision_id": "dec-rail",
            "action": "drift_issue",
            "trace_id": "c" * 32,
            "rationale": f"DB_PASSWORD changed to {secret}.",
            "diffs": [
                {"name": "DB_PASSWORD", "live": secret,
                 "contract_status": "present_disallow_manual"}
            ],
        },
    )
    resp = TestClient(app).get("/decisions?limit=50")
    assert resp.status_code == 200
    row = resp.json()["decisions"][0]
    assert secret not in row["rationale"]          # rationale prose scrubbed
    assert "DB_PASSWORD" in row["rationale"]        # var name survives
    assert row["diffs"][0]["live"] == secret        # diffs[] left raw (PR 1's job)


def test_list_decisions_respects_limit_query_param():
    state = get_state()
    base = datetime(2026, 5, 21, 0, 0, 0, tzinfo=timezone.utc)
    for i in range(5):
        state.record_event(f"ev-{i}", {})
        state.record_decision(
            f"dec-{i}",
            f"ev-{i}",
            {
                "action": "no_op",
                "n": i,
                "created_at": base + timedelta(seconds=i),
            },
        )

    client = TestClient(app)
    resp = client.get("/decisions?limit=2")
    assert resp.status_code == 200
    body = resp.json()
    ns = [d["n"] for d in body["decisions"]]
    # The two newest.
    assert ns == [4, 3]


def test_list_decisions_empty_store_returns_empty_list():
    client = TestClient(app)
    resp = client.get("/decisions")
    assert resp.status_code == 200
    assert resp.json() == {"decisions": []}


def test_list_decisions_sets_cache_control_no_store():
    """Operator surface — never browser-cache a listing that
    reflects mutable server state."""
    client = TestClient(app)
    resp = client.get("/decisions")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"


# --------------------------------------------------------------------------- #
# Validation — 400 on out-of-range limit, with Cache-Control: no-store
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", [0, -1, 201, 1000])
def test_list_decisions_400_on_out_of_range_limit(bad: int):
    """Bound the listing so a misconfigured caller can't pull the
    whole collection in one request. Both edges of the range are
    explicit (1..200 inclusive). The 400 path also carries
    ``Cache-Control: no-store`` — matches the 19.A.6 pattern where
    HTTPExceptions don't inherit mutations from the injected
    ``response`` argument."""
    client = TestClient(app)
    resp = client.get(f"/decisions?limit={bad}")
    assert resp.status_code == 400
    assert "1..200" in resp.json()["detail"]
    assert resp.headers.get("cache-control") == "no-store"


@pytest.mark.parametrize("good", [1, 50, 200])
def test_list_decisions_accepts_boundary_limits(good: int):
    """Pin the inclusive boundaries — 1 and 200 must both succeed."""
    client = TestClient(app)
    resp = client.get(f"/decisions?limit={good}")
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Token guard — 401 without the X-DriftScribe-Token header
# --------------------------------------------------------------------------- #
#
# The autouse conftest fixture installs a ``dependency_overrides[verify_token]``
# bypass so other tests don't have to think about auth. We opt OUT of that
# bypass via the ``no_auth_override`` marker on the class below so this
# section exercises the real guard end-to-end.


@pytest.mark.no_auth_override
class TestDecisionsTokenGuard:
    """Token guard for /decisions matches /recheck (Phase 11.1)."""

    def _set_token(self, monkeypatch, value: str) -> None:
        monkeypatch.setenv("DRIFTSCRIBE_TOKEN", value)
        get_settings.cache_clear()

    def test_without_token_returns_401(self, monkeypatch):
        self._set_token(monkeypatch, "test-token-value-123")
        client = TestClient(app)
        resp = client.get("/decisions")
        assert resp.status_code == 401

    def test_correct_token_succeeds(self, monkeypatch):
        self._set_token(monkeypatch, "test-token-value-123")
        client = TestClient(app)
        resp = client.get(
            "/decisions",
            headers={"X-DriftScribe-Token": "test-token-value-123"},
        )
        assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Response shape — what the UI consumes
# --------------------------------------------------------------------------- #


def test_list_decisions_response_shape_includes_full_decision_dict():
    """The endpoint returns the full decision dict (action, trace_id,
    rationale, …) — the UI renders these fields directly. Pin the
    shape so a future refactor that accidentally projects to a
    narrower view is caught here."""
    state = get_state()
    state.record_event("ev-1", {})
    fixed = datetime(2026, 5, 21, 0, 0, 0, tzinfo=timezone.utc)
    state.record_decision(
        "dec-1",
        "ev-1",
        {
            "action": "drift_issue",
            "trace_id": "a" * 32,
            "rationale": "PAYMENT_MODE drifted",
            "created_at": fixed,
        },
    )

    client = TestClient(app)
    resp = client.get("/decisions")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["decisions"]) == 1
    d = body["decisions"][0]
    assert d["action"] == "drift_issue"
    assert d["trace_id"] == "a" * 32
    assert d["rationale"] == "PAYMENT_MODE drifted"
    # ``created_at`` survives JSON serialization (FastAPI's default
    # encoder turns datetimes into ISO-8601 strings).
    assert "created_at" in d
