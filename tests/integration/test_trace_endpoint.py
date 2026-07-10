"""Integration tests for ``GET /trace/{trace_id}`` (Phase 19.A.6).

Pins the operator-facing reasoning-timeline endpoint:

* Fail-closed input validation on the ``trace_id`` URL parameter
  (matches CloudLoggingFetcher's hex32 guard).
* Empty events for unknown trace_ids — never 404 (the operator may be
  watching a request the agent hasn't finished writing yet).
* Defense-in-depth redaction at render: a pre-Phase-19 emit-time leak
  (or any future code path that bypasses ``redact_event`` at the emit
  site) is still scrubbed here.
* The **observed-stability** completion gate — completion is judged
  by how long OUR observations have held a stable signature, NOT by
  the timestamps inside log entries (Cloud Logging documents a 0-60s
  live-tail buffer where entries can arrive out of order; a
  late-arriving ``final_response`` would otherwise mark a freshly-
  observed-but-actually-incomplete timeline as complete on the first
  poll and cache it).
* Real fetch-timeout via a ``Future.result(timeout=...)`` boundary
  (the sync google-cloud-logging client has no native timeout kwarg).
* ``Cache-Control: no-store`` on every response.
* Stable secondary sort by ``insert_id`` for same-millisecond events.
* Enrichment with the persisted decision document.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent.main import (
    _TRACE_FETCH_LIMIT,
    _TRACE_FETCH_TIMEOUT_S,
    _STABILITY_GRACE_S,
    app,
    get_state,
    get_trace_fetcher,
)
from agent.trace_fetcher import StubTraceFetcher, TraceFetcher


_TRACE_A = "a" * 32
_TRACE_B = "b" * 32


def _stub_with(entries: list[dict]) -> StubTraceFetcher:
    """Build a StubTraceFetcher pre-loaded with the given entries."""
    return StubTraceFetcher(entries=entries)


def _install_fetcher(fetcher: TraceFetcher) -> None:
    """Override the FastAPI dep so the route uses our stub."""
    app.dependency_overrides[get_trace_fetcher] = lambda: fetcher


# --------------------------------------------------------------------------- #
# 1. Input validation — fail closed on non-hex32 trace_ids
# --------------------------------------------------------------------------- #


def test_trace_endpoint_400_on_bad_trace_id():
    """Any non-hex32 trace_id must 400 BEFORE the fetcher is touched.

    Carried forward from 19.A.5's ``fullmatch`` guard. The fetcher's
    ``.calls`` counter pins that the rejection happens at the
    URL-parameter layer, not inside the fetcher (defense in depth —
    both guards must be in place).

    Note: httpx (TestClient) rejects raw ``\n``/``\r`` in URLs at the
    client layer, so the trailing-newline injection that 19.A.5 added
    ``fullmatch`` to defend against is exercised in the unit tests for
    :class:`CloudLoggingFetcher`. This route-level test asserts the
    400 shape for everything the client will actually transmit."""
    stub = _stub_with([])
    _install_fetcher(stub)
    client = TestClient(app)

    # Shapes that reach the handler — all 400 from the explicit guard.
    for bad in [
        "not-a-hex" + "x" * 23,  # length 32 but not hex
        "A" * 32,                 # uppercase rejected — regex is lowercase only
        "a" * 31,                 # too short
        "a" * 33,                 # too long
        "a" * 31 + "g",           # not in [0-9a-f]
    ]:
        resp = client.get(f"/trace/{bad}")
        assert resp.status_code == 400, (bad, resp.status_code, resp.text)
        # Codex 19.A.6 review MEDIUM: the 400 exception path must
        # ALSO carry ``Cache-Control: no-store``. The route-level
        # ``response.headers[...]`` assign is dropped when an
        # HTTPException is raised; we have to pass ``headers=`` on
        # the exception itself.
        assert resp.headers.get("cache-control") == "no-store"

    # Fetcher was never invoked — guard short-circuits at the URL layer.
    assert stub.calls == 0


# --------------------------------------------------------------------------- #
# 2. Unknown trace_id → empty events, 200, complete=False
# --------------------------------------------------------------------------- #


def test_trace_endpoint_returns_empty_events_for_unknown_trace():
    stub = _stub_with([])  # nothing for any trace_id
    _install_fetcher(stub)
    client = TestClient(app)

    resp = client.get(f"/trace/{_TRACE_A}")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "trace_id": _TRACE_A,
        "events": [],
        "decision": None,
        "complete": False,
        "fetched_from_cache": False,
    }


# --------------------------------------------------------------------------- #
# 3. Redact-at-render: credentialed URLs in llm_thought text are scrubbed
# --------------------------------------------------------------------------- #


def test_trace_endpoint_redacts_credentialed_urls_in_thought_text():
    """A pre-Phase-19 emit (or any future site that skips
    ``redact_event``) would smuggle a credentialed URL into the
    response. The render-time pass must scrub it."""
    raw_thought = (
        "I should connect to postgres://operator:hunter2@db.example/prod"
        " to verify the schema."
    )
    stub = _stub_with(
        [
            {
                "trace_id": _TRACE_A,
                "event": "llm_thought",
                "thought_text": raw_thought,
                "timestamp": "2026-05-21T00:00:00Z",
                "insert_id": "ins-1",
            }
        ]
    )
    _install_fetcher(stub)
    client = TestClient(app)

    resp = client.get(f"/trace/{_TRACE_A}")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["events"]) == 1
    rendered = body["events"][0]["thought_text"]
    # Userinfo is stripped, the rest of the URL is preserved so the
    # reader can still see the host/path.
    assert "hunter2" not in rendered
    assert "operator" not in rendered
    assert "<redacted>@db.example/prod" in rendered


# --------------------------------------------------------------------------- #
# 4. CRITICAL — observed-stability completion gate (Codex v2 fix)
# --------------------------------------------------------------------------- #


def test_trace_endpoint_requires_final_response_AND_stability_grace(monkeypatch):
    """Completion gates on observed-stability, NOT log timestamps.

    Cloud Logging documents a 0-60s live-tail buffer where entries
    can arrive out of order. If we used the entry timestamps to
    decide "the timeline has settled", a late-arriving
    ``final_response`` carrying a 30-second-old timestamp would mark
    a freshly-observed timeline as complete on the FIRST poll — and
    we'd cache an incomplete view. The fix tracks stability in
    process state: a signature has to hold steady for
    ``_STABILITY_GRACE_S`` of OUR wall-clock observations before we
    mark the timeline complete and cache it.

    monkeypatch ``time.monotonic`` (read by the trace state, NOT by
    the executor's internal timing — the executor uses the absolute
    timer in select(), not ``time.monotonic``) to fast-forward
    without sleeping.
    """
    # Build the "agent finished" timeline.
    entries = [
        {
            "trace_id": _TRACE_A,
            "event": "llm_thought",
            "thought_text": "thinking",
            "timestamp": "2026-05-21T00:00:00Z",
            "insert_id": "ins-1",
        },
        {
            "trace_id": _TRACE_A,
            "event": "final_response",
            "text": "done",
            "timestamp": "2026-05-21T00:00:01Z",
            "insert_id": "ins-2",
        },
    ]
    stub = _stub_with(entries)
    _install_fetcher(stub)
    client = TestClient(app)

    fake_now = [1000.0]

    def fake_monotonic() -> float:
        return fake_now[0]

    # Patch ``time.monotonic`` as seen by agent.main (the trace state
    # helpers and the cache TTL all import via ``time.monotonic``).
    monkeypatch.setattr("agent.main.time.monotonic", fake_monotonic)

    # --- Poll 1: final_response present, but no observation history.
    # The signature has been seen for 0s of wall-clock. complete=False,
    # NOT cached.
    resp1 = client.get(f"/trace/{_TRACE_A}")
    assert resp1.status_code == 200
    body1 = resp1.json()
    assert body1["complete"] is False
    assert body1["fetched_from_cache"] is False
    assert stub.calls == 1  # fetched fresh

    # --- Poll 2: jump past the grace window. Same signature ⇒
    # complete=True, cached.
    fake_now[0] += _STABILITY_GRACE_S + 1.0
    resp2 = client.get(f"/trace/{_TRACE_A}")
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["complete"] is True
    # This poll itself didn't come from cache — it computed completion
    # and stored. But subsequent polls do.
    assert body2["fetched_from_cache"] is False
    assert stub.calls == 2

    # --- Poll 3: served from cache; fetcher NOT touched.
    resp3 = client.get(f"/trace/{_TRACE_A}")
    assert resp3.status_code == 200
    body3 = resp3.json()
    assert body3["complete"] is True
    assert body3["fetched_from_cache"] is True
    assert stub.calls == 2  # unchanged — cache hit

    # --- Mid-cycle: a NEW event arrives for a different trace_id and
    # then we add an event to OUR trace before the cache TTL — but our
    # trace is cached already, so the new event won't be seen until
    # cache expires. To exercise the "signature change resets
    # stability" path, use a DIFFERENT trace_id that hasn't been cached.
    entries_b_v1 = [
        {
            "trace_id": _TRACE_B,
            "event": "llm_thought",
            "thought_text": "first",
            "timestamp": "2026-05-21T00:00:00Z",
            "insert_id": "ins-b1",
        },
        {
            "trace_id": _TRACE_B,
            "event": "final_response",
            "text": "done",
            "timestamp": "2026-05-21T00:00:02Z",
            "insert_id": "ins-b2",
        },
    ]
    stub_b = _stub_with(entries_b_v1)
    _install_fetcher(stub_b)

    # Poll 1 for trace B — observation recorded.
    resp_b1 = client.get(f"/trace/{_TRACE_B}")
    assert resp_b1.status_code == 200
    assert resp_b1.json()["complete"] is False

    # Mid-cycle the agent emits another llm_thought BEFORE the
    # final_response had really settled — signature changes. Reset
    # the stub with the larger timeline.
    entries_b_v2 = entries_b_v1 + [
        {
            "trace_id": _TRACE_B,
            "event": "llm_thought",
            "thought_text": "more thinking",
            "timestamp": "2026-05-21T00:00:03Z",
            "insert_id": "ins-b3",
        },
    ]
    stub_b.entries = entries_b_v2

    # Even if we jump past the grace window, the signature changed so
    # the stability clock RESTARTS — first observation at the new
    # signature. complete=False.
    fake_now[0] += _STABILITY_GRACE_S + 1.0
    resp_b2 = client.get(f"/trace/{_TRACE_B}")
    assert resp_b2.status_code == 200
    assert resp_b2.json()["complete"] is False

    # Now hold the signature for the grace window — complete=True.
    fake_now[0] += _STABILITY_GRACE_S + 1.0
    resp_b3 = client.get(f"/trace/{_TRACE_B}")
    assert resp_b3.status_code == 200
    assert resp_b3.json()["complete"] is True


# --------------------------------------------------------------------------- #
# 5. Real timeout via the Future boundary
# --------------------------------------------------------------------------- #


class _SlowFetcher:
    """Sleeps longer than the endpoint's fetch timeout to exercise the
    ``Future.result(timeout=...)`` boundary. Intentionally NOT subclassing
    StubTraceFetcher — the slowness is the point."""

    def __init__(self, sleep_s: float):
        self.sleep_s = sleep_s
        self.calls = 0

    def fetch(
        self, trace_id: str, *, limit: int = 500, around: Any = None
    ) -> list[dict]:
        self.calls += 1
        time.sleep(self.sleep_s)
        return []


def test_trace_endpoint_full_page_is_never_complete_or_cached(monkeypatch):
    """A fetch at the ``_TRACE_FETCH_LIMIT`` cap is treated as truncated.

    The fetcher pulls ``timestamp desc``, so a full page keeps the newest
    ``final_response`` (which _observe_and_check_stability treats as "done")
    while dropping the OLDEST entries. Without the truncation guard the endpoint
    would bless that head-missing timeline complete and cache it. The guard
    forces ``complete=False`` and skips the cache even past the stability grace,
    so every poll refetches (``stub.calls`` keeps climbing).
    """
    # Exactly _TRACE_FETCH_LIMIT matching entries, ending in a final_response —
    # enough to satisfy stability but flagged as possibly-truncated.
    entries = [
        {
            "trace_id": _TRACE_A,
            "event": "llm_thought",
            "thought_text": f"t{i}",
            "timestamp": f"2026-05-21T00:00:{i % 60:02d}Z",
            "insert_id": f"ins-{i:04d}",
        }
        for i in range(_TRACE_FETCH_LIMIT - 1)
    ]
    entries.append(
        {
            "trace_id": _TRACE_A,
            "event": "final_response",
            "text": "done",
            "timestamp": "2026-05-21T01:00:00Z",
            "insert_id": "ins-final",
        }
    )
    assert len(entries) == _TRACE_FETCH_LIMIT
    stub = _stub_with(entries)
    _install_fetcher(stub)
    client = TestClient(app)

    fake_now = [1000.0]
    monkeypatch.setattr("agent.main.time.monotonic", lambda: fake_now[0])

    # Poll 1: full page, no observation history yet → incomplete anyway.
    body1 = client.get(f"/trace/{_TRACE_A}").json()
    assert len(body1["events"]) == _TRACE_FETCH_LIMIT
    assert body1["complete"] is False
    assert stub.calls == 1

    # Poll 2: past the stability grace — WITHOUT the truncation guard this would
    # flip to complete=True and cache. The guard keeps it False...
    fake_now[0] += _STABILITY_GRACE_S + 1.0
    body2 = client.get(f"/trace/{_TRACE_A}").json()
    assert body2["complete"] is False
    assert body2["fetched_from_cache"] is False
    assert stub.calls == 2

    # Poll 3: ...and nothing was cached, so it refetches again.
    body3 = client.get(f"/trace/{_TRACE_A}").json()
    assert body3["complete"] is False
    assert body3["fetched_from_cache"] is False
    assert stub.calls == 3


def test_trace_endpoint_503_on_fetch_timeout(monkeypatch):
    """A fetcher that sleeps longer than the configured timeout must
    yield a 503 (not a 500, not a 200-with-empty-events). The route
    runs the fetch on its own ThreadPoolExecutor so the SYNC
    google-cloud-logging client (which has no native timeout kwarg)
    can still be time-bounded.

    We shrink the timeout for the test so the suite stays fast — the
    production default is 25s; we test against a tiny window."""
    # Shrink the production timeout to keep this test fast.
    monkeypatch.setattr("agent.main._TRACE_FETCH_TIMEOUT_S", 0.2)

    slow = _SlowFetcher(sleep_s=1.5)
    _install_fetcher(slow)
    client = TestClient(app)

    resp = client.get(f"/trace/{_TRACE_A}")
    assert resp.status_code == 503
    assert "timed out" in resp.json()["detail"]
    # The 503 exception path also carries ``no-store`` — operator
    # browsers must not cache a transient timeout view.
    assert resp.headers.get("cache-control") == "no-store"


# --------------------------------------------------------------------------- #
# 6. Cache-Control header is always no-store
# --------------------------------------------------------------------------- #


def test_trace_endpoint_sets_cache_control_no_store():
    """The operator-facing endpoint must never be browser-cached.

    The in-process completion cache is server-side only; allowing a
    browser/proxy cache would defeat the "refetch in-flight traces"
    property AND let a stale view outlive its server-side TTL."""
    stub = _stub_with([])
    _install_fetcher(stub)
    client = TestClient(app)

    resp = client.get(f"/trace/{_TRACE_A}")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"


# --------------------------------------------------------------------------- #
# 7. Stable secondary sort by insert_id for same-millisecond events
# --------------------------------------------------------------------------- #


def test_trace_endpoint_orders_by_timestamp_then_insert_id():
    """Same-millisecond events would otherwise shuffle. ``insert_id``
    breaks the tie deterministically so reloads of the same timeline
    produce the same ordering."""
    same_ts = "2026-05-21T00:00:00.000Z"
    # Intentionally pre-shuffled.
    entries = [
        {
            "trace_id": _TRACE_A,
            "event": "tool_call",
            "timestamp": same_ts,
            "insert_id": "ins-c",
        },
        {
            "trace_id": _TRACE_A,
            "event": "llm_thought",
            "timestamp": same_ts,
            "insert_id": "ins-a",
        },
        {
            "trace_id": _TRACE_A,
            "event": "tool_result",
            "timestamp": same_ts,
            "insert_id": "ins-b",
        },
    ]
    stub = _stub_with(entries)
    _install_fetcher(stub)
    client = TestClient(app)

    resp = client.get(f"/trace/{_TRACE_A}")
    assert resp.status_code == 200
    body = resp.json()
    insert_ids = [e["insert_id"] for e in body["events"]]
    assert insert_ids == ["ins-a", "ins-b", "ins-c"]


# --------------------------------------------------------------------------- #
# 8. Enrichment with the persisted decision document
# --------------------------------------------------------------------------- #


def test_trace_endpoint_cache_hit_rereads_decision_so_it_doesnt_freeze_null(
    monkeypatch,
):
    """Codex 19.A.6 review MEDIUM: the cache must not freeze a stale
    ``decision: null``.

    Scenario: ``_observe_and_check_stability`` can return True (and we
    cache the payload) BEFORE ``record_decision`` lands in Firestore,
    because ``final_response`` is emitted during ADK execution but the
    decision document is persisted later in ``_do_recheck`` /
    ``_do_rollback``. If the cached payload carried ``decision: None``
    we'd freeze that null for the full 300s TTL. The fix re-reads the
    decision from StateStore on EVERY response, including cache hits.

    Pin: poll 1 caches (no decision yet). Decision is then written.
    Poll 2 (cache hit) must surface the decision."""
    entries = [
        {
            "trace_id": _TRACE_A,
            "event": "llm_thought",
            "thought_text": "thinking",
            "timestamp": "2026-05-21T00:00:00Z",
            "insert_id": "ins-1",
        },
        {
            "trace_id": _TRACE_A,
            "event": "final_response",
            "text": "done",
            "timestamp": "2026-05-21T00:00:01Z",
            "insert_id": "ins-2",
        },
    ]
    stub = _stub_with(entries)
    _install_fetcher(stub)

    fake_now = [1000.0]

    def fake_monotonic() -> float:
        return fake_now[0]

    monkeypatch.setattr("agent.main.time.monotonic", fake_monotonic)

    client = TestClient(app)

    # Poll 1: records the observation. complete=False.
    r1 = client.get(f"/trace/{_TRACE_A}")
    assert r1.status_code == 200
    assert r1.json()["complete"] is False
    assert r1.json()["decision"] is None

    # Poll 2: grace window has elapsed; signature unchanged. cached
    # with complete=True. No decision yet.
    fake_now[0] += _STABILITY_GRACE_S + 1.0
    r2 = client.get(f"/trace/{_TRACE_A}")
    assert r2.status_code == 200
    assert r2.json()["complete"] is True
    assert r2.json()["decision"] is None

    # Now the side-effect path lands and the decision is persisted.
    state = get_state()
    state.record_event("ev-late", {})
    state.record_decision(
        "dec-late",
        "ev-late",
        {"action": "drift_issue", "trace_id": _TRACE_A, "rationale": "x"},
    )

    # Poll 3: cache HIT (fetcher.calls unchanged), but decision is
    # surfaced because we re-read on every response.
    fetch_calls_before = stub.calls
    r3 = client.get(f"/trace/{_TRACE_A}")
    assert r3.status_code == 200
    body3 = r3.json()
    assert body3["fetched_from_cache"] is True
    assert stub.calls == fetch_calls_before  # cache hit, no refetch
    assert body3["decision"] is not None
    assert body3["decision"]["action"] == "drift_issue"


def test_trace_endpoint_enriches_with_decision_when_present():
    """If the StateStore has a decision for this trace_id, the payload
    surfaces it under ``decision`` so the UI can show the final action
    alongside the events."""
    state = get_state()
    state.record_event("ev-1", {})
    decision_doc: dict[str, Any] = {
        "action": "drift_issue",
        "trace_id": _TRACE_A,
        "event_key": "ev-1",
        "rationale": "PAYMENT_MODE drifted",
    }
    state.record_decision("dec-1", "ev-1", decision_doc)

    stub = _stub_with(
        [
            {
                "trace_id": _TRACE_A,
                "event": "llm_thought",
                "thought_text": "hi",
                "timestamp": "2026-05-21T00:00:00Z",
                "insert_id": "ins-1",
            }
        ]
    )
    _install_fetcher(stub)
    client = TestClient(app)

    resp = client.get(f"/trace/{_TRACE_A}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] is not None
    assert body["decision"]["action"] == "drift_issue"
    assert body["decision"]["trace_id"] == _TRACE_A


def test_trace_endpoint_scrubs_secret_in_rationale():
    """PR 2 — the raw-rationale leak. A persisted decision whose rationale
    quotes a secret value present in its diffs must come back with that value
    redacted; the var name survives and the timeline is unaffected."""
    state = get_state()
    state.record_event("ev-scrub", {})
    secret = "sk-LEAK-9999"
    state.record_decision(
        "dec-scrub",
        "ev-scrub",
        {
            "action": "drift_issue",
            "trace_id": _TRACE_A,
            "event_key": "ev-scrub",
            "rationale": f"API_TOKEN rotated to {secret} per the contract.",
            "diffs": [
                {"name": "API_TOKEN", "expected": None, "live": secret,
                 "contract_status": "present_disallow_manual"}
            ],
        },
    )
    _install_fetcher(_stub_with([]))
    client = TestClient(app)

    resp = client.get(f"/trace/{_TRACE_A}")
    assert resp.status_code == 200
    body = resp.json()
    # PR 2 scrubs the free-text RATIONALE prose...
    assert secret not in body["decision"]["rationale"]
    assert "API_TOKEN" in body["decision"]["rationale"]   # var name preserved
    # ...but deliberately leaves diffs[] RAW (the decision is unredacted by
    # design; the SPA's env-diff card redacts diff cells client-side — PR 1).
    assert body["decision"]["diffs"][0]["live"] == secret


# --------------------------------------------------------------------------- #
# Sanity — module-level constant defaults haven't drifted
# --------------------------------------------------------------------------- #


def test_module_constants_have_documented_defaults():
    """The plan calls out specific values — pin them so a refactor that
    inadvertently shrinks the stability grace (causing flaky
    completion) is caught here, not in the field."""
    assert _STABILITY_GRACE_S == pytest.approx(30.0)
    # 25s: fast narrow phase + worst-case retention-deep wide phase (~17s
    # measured @ 400d). 5.0 was the narrow-only budget and 503'd every wide
    # query (2026-07-06 /trace outage).
    assert _TRACE_FETCH_TIMEOUT_S == pytest.approx(25.0)
    # The per-fetch cap doubles as the truncation threshold (see the full-page
    # guard test above), so pin it too.
    assert _TRACE_FETCH_LIMIT == 500


# --------------------------------------------------------------------------- #
# created_at hint threading (2026-07-10 slow-replay fix)
# --------------------------------------------------------------------------- #

_TRACE_H = "c" * 32


def test_trace_endpoint_passes_decision_created_at_as_hint():
    """The endpoint reads the decision BEFORE the log fetch; its created_at
    must reach the fetcher as the ``around`` hint so old traces get the
    bounded window instead of the retention-deep walk."""
    state = get_state()
    state.record_event("ev-hint", {})
    created = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    state.record_decision(
        "dec-hint",
        "ev-hint",
        {
            "action": "iac_apply",
            "trace_id": _TRACE_H,
            "event_key": "ev-hint",
            "apply_status": "applied",
            "merge_state": "merged",
            "created_at": created,
        },
    )
    stub = _stub_with([])
    _install_fetcher(stub)
    client = TestClient(app)

    resp = client.get(f"/trace/{_TRACE_H}")
    assert resp.status_code == 200
    assert stub.last_around == created


def test_trace_endpoint_no_decision_means_no_hint():
    """Chat-turn traces have no decision doc — the fetcher must get
    around=None and keep the exact two-phase hot path."""
    stub = _stub_with([])
    _install_fetcher(stub)
    client = TestClient(app)

    resp = client.get(f"/trace/{'e' * 32}")
    assert resp.status_code == 200
    assert stub.last_around is None


def test_decision_created_at_hint_parses_defensively():
    """Unit-ish checks on the helper: datetime passes through (naive → UTC),
    ISO strings parse, garbage degrades to None instead of raising."""
    from agent.main import _decision_created_at_hint

    aware = datetime(2026, 6, 1, tzinfo=timezone.utc)
    assert _decision_created_at_hint({"created_at": aware}) == aware
    naive = datetime(2026, 6, 1)
    assert _decision_created_at_hint({"created_at": naive}).tzinfo is not None
    parsed = _decision_created_at_hint({"created_at": "2026-06-01T12:00:00Z"})
    assert parsed == datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert _decision_created_at_hint({"created_at": "not-a-date"}) is None
    assert _decision_created_at_hint({"created_at": 12345}) is None
    assert _decision_created_at_hint({}) is None
    assert _decision_created_at_hint(None) is None
