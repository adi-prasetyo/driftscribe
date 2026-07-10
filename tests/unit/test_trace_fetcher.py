"""Tests for the TraceFetcher abstraction (Phase 19.A.5).

Three test classes:

* StubTraceFetcher exercises the in-memory shape used by integration tests.
* CloudLoggingFetcher's hex32 trace_id guard fails closed against
  filter-string injection — verified without touching network by patching
  ``google.cloud.logging.Client`` at import time inside the test.
* The exact filter string built for a known trace_id is snapshot-tested so a
  future refactor can't silently regress to ``labels.*`` or
  ``textPayload``-based filtering — both would return zero hits in
  production despite the test that produced them still "working".
"""

from __future__ import annotations

import re
import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from agent.trace_fetcher import (
    CloudLoggingFetcher,
    StubTraceFetcher,
    _EVENT_KINDS,
    _entry_to_dict,
)


# ---------------------------------------------------------------------------
# StubTraceFetcher
# ---------------------------------------------------------------------------


def test_stub_filters_by_trace_id():
    a = "a" * 32
    b = "b" * 32
    f = StubTraceFetcher(
        entries=[
            {"trace_id": a, "event": "llm_thought", "timestamp": "2026-05-21T00:00:00Z"},
            {"trace_id": b, "event": "llm_thought", "timestamp": "2026-05-21T00:00:01Z"},
        ]
    )
    out = f.fetch(a)
    assert len(out) == 1
    assert out[0]["trace_id"] == a


def test_stub_respects_limit():
    a = "a" * 32
    f = StubTraceFetcher(
        entries=[{"trace_id": a, "event": "llm_thought", "i": i} for i in range(5)]
    )
    out = f.fetch(a, limit=2)
    assert len(out) == 2


def test_stub_excludes_entries_missing_or_unknown_event_kind():
    """Mirrors CloudLoggingFetcher's allowlist: a kind-less or unrecognized
    ``event`` must be excluded even when the trace_id matches, exactly like
    prod's ``jsonPayload.event=(...)`` clause would exclude it."""
    a = "a" * 32
    f = StubTraceFetcher(
        entries=[
            {"trace_id": a, "event": "llm_thought"},
            {"trace_id": a},  # no event field — e.g. inherited-context plumbing log
            {"trace_id": a, "event": "httpx_noise"},  # unknown kind
        ]
    )
    out = f.fetch(a)
    assert len(out) == 1
    assert out[0]["event"] == "llm_thought"


def test_stub_counts_calls():
    f = StubTraceFetcher(entries=[])
    assert f.calls == 0
    f.fetch("a" * 32)
    f.fetch("a" * 32)
    assert f.calls == 2


def test_stub_returns_empty_for_unknown_trace_id():
    f = StubTraceFetcher(entries=[{"trace_id": "a" * 32}])
    assert f.fetch("b" * 32) == []


# ---------------------------------------------------------------------------
# Test scaffolding for CloudLoggingFetcher
# ---------------------------------------------------------------------------


def _install_fake_cloud_logging(monkeypatch, list_entries_impl):
    """Patch ``google.cloud.logging.Client`` so we never touch network.

    Returns a MagicMock that tests can inspect (e.g. to read the captured
    ``filter_=`` kwarg). ``list_entries_impl`` is invoked with the same
    kwargs the production code would have passed.
    """
    fake_client = MagicMock()
    fake_client.list_entries.side_effect = list_entries_impl

    fake_module = types.ModuleType("google.cloud.logging")
    fake_module.Client = MagicMock(return_value=fake_client)
    # Also stub the parent ``google.cloud`` package to be safe — pytest's
    # import system may not have google.cloud.logging loaded yet in a unit
    # test environment.
    monkeypatch.setitem(sys.modules, "google.cloud.logging", fake_module)
    return fake_client, fake_module.Client


# ---------------------------------------------------------------------------
# CloudLoggingFetcher — trace_id guard
# ---------------------------------------------------------------------------


def test_cloud_logging_fetcher_rejects_bad_trace_id(monkeypatch):
    """Non-hex32 trace_ids must short-circuit BEFORE hitting the client.

    This is the filter-string injection guard. If a caller could pass
    arbitrary text, they could break out of the trace_id="..." quoted
    string in the Cloud Logging filter language and broaden the query.
    """
    fake_client, _ = _install_fake_cloud_logging(monkeypatch, lambda **_: iter([]))
    f = CloudLoggingFetcher(project="test-proj")
    # Replace the lazily-constructed client with the fake one.
    f._client = fake_client

    # Various malformed trace_ids must all return [] without calling
    # list_entries.
    assert f.fetch("") == []
    assert f.fetch("not-a-hex") == []
    assert f.fetch("A" * 32) == []  # uppercase hex rejected (regex is lowercase only)
    assert f.fetch("a" * 31) == []
    assert f.fetch("a" * 33) == []
    assert f.fetch('a" OR resource.type="gce_instance" AND "') == []
    # Trailing-newline regression: Python's ``$`` in ``re.match`` mode also
    # matches just-before-a-final ``\n``, so ``"a"*32 + "\n"`` would slip
    # past a ``match`` call and get interpolated literally into the Cloud
    # Logging filter string. ``fullmatch`` closes that hole.
    assert f.fetch("a" * 32 + "\n") == []
    # Belt-and-braces variations on the same theme.
    assert f.fetch("a" * 32 + "\r\n") == []
    assert f.fetch("\n" + "a" * 32) == []
    assert fake_client.list_entries.call_count == 0


def test_cloud_logging_fetcher_accepts_valid_trace_id(monkeypatch):
    fake_client, _ = _install_fake_cloud_logging(monkeypatch, lambda **_: iter([]))
    f = CloudLoggingFetcher(project="test-proj")
    f._client = fake_client

    # Empty result: the fast phase misses, so the wide phase also runs —
    # exactly two queries, never more.
    assert f.fetch("a" * 32) == []
    assert fake_client.list_entries.call_count == 2


# ---------------------------------------------------------------------------
# CloudLoggingFetcher — filter string snapshot
# ---------------------------------------------------------------------------


def test_cloud_logging_fetcher_filter_string_shape(monkeypatch):
    """Snapshot the filter string built for a known trace_id.

    Protects against accidentally regressing to ``labels.*`` or
    ``textPayload``-based filtering — both would compile fine but match
    zero entries in production because Phase 18's JSONFormatter writes our
    extras under ``jsonPayload.*``.
    """
    captured: dict = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return iter([])

    fake_client, _ = _install_fake_cloud_logging(monkeypatch, _capture)
    f = CloudLoggingFetcher(project="test-proj")
    f._client = fake_client

    trace = "0123456789abcdef0123456789abcdef"
    f.fetch(trace, limit=250)

    # The static head is snapshot-pinned (guards against regressing to
    # ``labels.*`` / ``textPayload``, or losing the event-kind allowlist);
    # the ``timestamp>=`` floor is time-dependent, so it's matched by shape,
    # not value. The floor itself is REQUIRED — without it Cloud Logging only
    # searches ~the last day.
    assert captured["filter_"].startswith(
        'resource.type="cloud_run_revision" '
        'AND resource.labels.service_name="driftscribe-agent" '
        f'AND jsonPayload.trace_id="{trace}" '
        'AND jsonPayload.event=('
        '"llm_thought" OR "tool_call" OR "tool_result" OR "llm_usage" '
        'OR "mcp_call" OR "final_response") '
        'AND timestamp>="'
    )
    assert re.search(
        r'AND timestamp>="\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"$',
        captured["filter_"],
    )
    # DESC so recently-ingested (live) traces stay fast under the wide floor;
    # the /trace endpoint re-sorts ascending for display.
    assert captured["order_by"] == "timestamp desc"
    assert captured["page_size"] == 250
    assert captured["max_results"] == 250


def test_event_kinds_allowlist_is_exactly_the_six_load_bearing_kinds():
    """Pins the allowlist to exactly the six kinds the pipeline emits.

    Missing ``tool_result`` breaks the SPA's result_preview pairing; missing
    ``final_response`` means ``_observe_and_check_stability`` (agent/main.py)
    can never observe completion. This is an explicit allowlist (not a bare
    existence check), so a regression here is a silent behavior change, not
    a loud failure — worth pinning directly.
    """
    assert _EVENT_KINDS == (
        "llm_thought",
        "tool_call",
        "tool_result",
        "llm_usage",
        "mcp_call",
        "final_response",
    )


def test_cloud_logging_fetcher_filter_uses_custom_service_name(monkeypatch):
    """A non-default service_name flows into the filter unchanged."""
    captured: dict = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return iter([])

    fake_client, _ = _install_fake_cloud_logging(monkeypatch, _capture)
    f = CloudLoggingFetcher(project="test-proj", service_name="some-other-service")
    f._client = fake_client

    f.fetch("a" * 32)
    assert 'resource.labels.service_name="some-other-service"' in captured["filter_"]


def test_cloud_logging_fetcher_timestamp_floor_tracks_lookback(monkeypatch):
    """The ``timestamp>=`` floor is ``now - lookback_days`` and moves with it.

    Regression guard for the bug this clause fixes: a filter with no timestamp
    floor makes Cloud Logging search only ~the last day, so any trace older
    than 24h returns empty. A short lookback yields a recent floor; a long one
    yields an older floor.
    """
    captured: dict = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return iter([])

    fake_client, _ = _install_fake_cloud_logging(monkeypatch, _capture)

    def floor_of(days: int) -> datetime:
        f = CloudLoggingFetcher(project="test-proj", lookback_days=days)
        f._client = fake_client
        f.fetch("c" * 32)
        m = re.search(r'timestamp>="([^"]+)"', captured["filter_"])
        assert m, captured["filter_"]
        return datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )

    now = datetime.now(timezone.utc)
    short = floor_of(1)
    long = floor_of(400)
    # ~1 day back vs ~400 days back, with generous slack for clock drift + the
    # whole-second truncation in _timestamp_floor.
    assert timedelta(hours=23) < (now - short) < timedelta(hours=25)
    assert timedelta(days=399) < (now - long) < timedelta(days=401)


# ---------------------------------------------------------------------------
# CloudLoggingFetcher — two-phase query (2026-07-06 /trace outage regression)
# ---------------------------------------------------------------------------
#
# entries.list latency grows with the width of the ``timestamp`` window
# (~1.4s @ 1d vs ~17s @ 400d measured on prod), so an unconditional
# retention-deep query blew the endpoint's fetch budget on every call. The
# fetcher now queries a narrow fast floor first and only widens on a miss.


def _fake_entry(ts_iso: str, insert_id: str = "i1") -> MagicMock:
    e = MagicMock()
    e.payload = {"trace_id": "d" * 32, "event": "llm_thought", "timestamp": ts_iso}
    e.timestamp = None
    e.insert_id = insert_id
    return e


def _iso_ago(**delta) -> str:
    t = datetime.now(timezone.utc) - timedelta(**delta)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _floor_age_of(filter_str: str) -> timedelta:
    m = re.search(r'timestamp>="([^"]+)"', filter_str)
    assert m, filter_str
    floor = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    return datetime.now(timezone.utc) - floor


def test_two_phase_fresh_trace_resolves_in_fast_phase_alone(monkeypatch):
    """A recent trace is served by the narrow query — the wide one never runs.

    This IS the outage fix: the hot paths (live poll, post-turn backfill,
    recent opens) must never pay the retention-deep window's latency.
    """
    calls: list[dict] = []

    def _impl(**kwargs):
        calls.append(kwargs)
        return iter([_fake_entry(_iso_ago(hours=1))])

    fake_client, _ = _install_fake_cloud_logging(monkeypatch, _impl)
    f = CloudLoggingFetcher(project="test-proj")
    f._client = fake_client

    out = f.fetch("d" * 32)
    assert len(out) == 1
    assert len(calls) == 1
    # The single query used the FAST floor (~2 days), not the wide default.
    assert timedelta(days=1, hours=23) < _floor_age_of(calls[0]["filter_"]) < timedelta(
        days=2, hours=1
    )


def test_two_phase_old_trace_falls_through_to_wide_phase(monkeypatch):
    """A fast-phase miss reruns with the retention-deep floor and returns it."""
    calls: list[dict] = []

    def _impl(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return iter([])  # fast phase: nothing in the last 2 days
        return iter([_fake_entry(_iso_ago(days=30))])

    fake_client, _ = _install_fake_cloud_logging(monkeypatch, _impl)
    f = CloudLoggingFetcher(project="test-proj")
    f._client = fake_client

    out = f.fetch("d" * 32)
    assert len(out) == 1
    assert len(calls) == 2
    assert _floor_age_of(calls[0]["filter_"]) < timedelta(days=3)
    assert _floor_age_of(calls[1]["filter_"]) > timedelta(days=399)


def test_two_phase_straddle_guard_reruns_wide(monkeypatch):
    """Fast-phase entries hugging the fast floor force the wide rerun.

    A trace whose oldest visible entry sits within the guard band of the
    fast floor may extend past the window edge; serving the narrow result
    could cache a head-truncated timeline as complete.
    """
    calls: list[dict] = []
    near_floor = _iso_ago(days=2, hours=-3)  # 3h ABOVE the 2d floor (inside 6h guard)

    def _impl(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return iter([_fake_entry(near_floor)])
        return iter([_fake_entry(near_floor), _fake_entry(_iso_ago(days=2, hours=2), "i0")])

    fake_client, _ = _install_fake_cloud_logging(monkeypatch, _impl)
    f = CloudLoggingFetcher(project="test-proj")
    f._client = fake_client

    out = f.fetch("d" * 32)
    assert len(calls) == 2
    assert len(out) == 2  # the wide (superset) result wins


def test_two_phase_unparseable_timestamp_fails_toward_wide(monkeypatch):
    """A fast-phase entry with a garbage timestamp must NOT be trusted as
    complete — fail toward the wide (correct, slower) phase."""
    calls: list[dict] = []

    def _impl(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return iter([_fake_entry("not-a-timestamp")])
        return iter([_fake_entry(_iso_ago(hours=1))])

    fake_client, _ = _install_fake_cloud_logging(monkeypatch, _impl)
    f = CloudLoggingFetcher(project="test-proj")
    f._client = fake_client

    out = f.fetch("d" * 32)
    assert len(calls) == 2
    assert len(out) == 1


def test_two_phase_collapses_when_lookback_narrower_than_fast_floor(monkeypatch):
    """lookback_days <= the fast floor means one query, at the configured
    lookback — a second identical-or-wider query would be pure waste."""
    calls: list[dict] = []

    def _impl(**kwargs):
        calls.append(kwargs)
        return iter([])

    fake_client, _ = _install_fake_cloud_logging(monkeypatch, _impl)
    f = CloudLoggingFetcher(project="test-proj", lookback_days=1)
    f._client = fake_client

    assert f.fetch("d" * 32) == []
    assert len(calls) == 1
    assert _floor_age_of(calls[0]["filter_"]) < timedelta(days=1, hours=1)


# ---------------------------------------------------------------------------
# _entry_to_dict
# ---------------------------------------------------------------------------


def test_entry_to_dict_dict_payload_passthrough():
    ts = datetime(2026, 5, 21, 1, 0, 0, tzinfo=timezone.utc)
    entry = MagicMock()
    entry.payload = {"trace_id": "a" * 32, "event": "llm_thought", "msg": "hi"}
    entry.timestamp = ts
    entry.insert_id = "ins-1"

    d = _entry_to_dict(entry)
    assert d["trace_id"] == "a" * 32
    assert d["event"] == "llm_thought"
    assert d["msg"] == "hi"
    assert d["timestamp"] == "2026-05-21T01:00:00+00:00"
    assert d["insert_id"] == "ins-1"


def test_entry_to_dict_text_payload_wraps_in_text_key():
    """Non-dict payloads (textPayload entries) get wrapped under ``text``.

    Shouldn't happen for our own logs (JSONFormatter always emits dicts),
    but a stray textPayload entry from a startup log or external library
    should still flow through without crashing.
    """
    entry = MagicMock()
    entry.payload = "raw text payload"
    entry.timestamp = datetime(2026, 5, 21, 0, 0, 0, tzinfo=timezone.utc)
    entry.insert_id = "ins-2"

    d = _entry_to_dict(entry)
    assert d == {
        "text": "raw text payload",
        "timestamp": "2026-05-21T00:00:00+00:00",
        "insert_id": "ins-2",
    }


def test_entry_to_dict_handles_missing_timestamp_and_insert_id():
    entry = MagicMock()
    entry.payload = {"event": "x"}
    entry.timestamp = None
    entry.insert_id = None

    d = _entry_to_dict(entry)
    assert d["timestamp"] == ""
    assert d["insert_id"] == ""


def test_entry_to_dict_does_not_overwrite_existing_keys():
    """If the payload already carries timestamp/insert_id, prefer it.

    Defensive — JSONFormatter writes its own ``timestamp`` ISO string and
    a trace_id but never an insert_id. If a future change adds either,
    don't silently overwrite with the LogEntry-level value.
    """
    entry = MagicMock()
    entry.payload = {
        "event": "x",
        "timestamp": "2026-01-01T00:00:00Z",
        "insert_id": "payload-supplied",
    }
    entry.timestamp = datetime(2099, 1, 1, tzinfo=timezone.utc)
    entry.insert_id = "log-entry-supplied"

    d = _entry_to_dict(entry)
    assert d["timestamp"] == "2026-01-01T00:00:00Z"
    assert d["insert_id"] == "payload-supplied"


# ---------------------------------------------------------------------------
# Smoke: Protocol compatibility
# ---------------------------------------------------------------------------


def test_stub_satisfies_trace_fetcher_protocol():
    """StubTraceFetcher must be usable wherever TraceFetcher is expected.

    Protocols are structural, so this is a compile-time check at best.
    Confirm at runtime by calling ``fetch`` through the Protocol.
    """
    from agent.trace_fetcher import TraceFetcher  # noqa: F401

    f: TraceFetcher = StubTraceFetcher()
    assert f.fetch("a" * 32) == []


# ---------------------------------------------------------------------------
# CloudLoggingFetcher — created_at hint window (2026-07-10 slow-replay fix)
# ---------------------------------------------------------------------------
#
# When the /trace endpoint already knows WHEN the trace happened (the decision
# doc's created_at), the fetcher queries ONE bounded window around that moment
# instead of the fast(2d)+wide(400d) two-phase walk. entries.list latency
# tracks the width of the timestamp window, so old traces get fast-phase
# latency instead of the ~17s retention-deep scan.


def _ceiling_age_of(filter_str: str) -> timedelta:
    m = re.search(r'timestamp<="([^"]+)"', filter_str)
    assert m, filter_str
    ceil = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    return datetime.now(timezone.utc) - ceil


def test_hinted_fetch_uses_single_bounded_window(monkeypatch):
    """With a hint, ONE query runs, floored at hint-2d and ceilinged at
    hint+1d — never the fast phase, never the wide phase."""
    calls: list[dict] = []
    hint = datetime.now(timezone.utc) - timedelta(days=30)

    def _impl(**kwargs):
        calls.append(kwargs)
        mid = (hint - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return iter([_fake_entry(mid)])

    fake_client, _ = _install_fake_cloud_logging(monkeypatch, _impl)
    f = CloudLoggingFetcher(project="test-proj")
    f._client = fake_client

    out = f.fetch("d" * 32, around=hint)
    assert len(out) == 1
    assert len(calls) == 1
    # floor ≈ 32 days ago (hint-2d), ceiling ≈ 29 days ago (hint+1d);
    # ±5 min slack for whole-second truncation + test wall time.
    assert timedelta(days=31, hours=23) < _floor_age_of(calls[0]["filter_"]) < timedelta(days=32, hours=1)
    assert timedelta(days=28, hours=23) < _ceiling_age_of(calls[0]["filter_"]) < timedelta(days=29, hours=1)


def test_hinted_fetch_empty_is_authoritative(monkeypatch):
    """AMENDED per Task 0: a bounded-window MISS returns empty WITHOUT the
    wide fallback. iac_apply timelines are structurally empty (the apply
    pipeline emits none of the six event kinds — verified against prod
    2026-07-10), so falling back wide on empty would re-pay the ~17s scan on
    exactly the rows users open; and the wide query cannot rescue a
    lagging-ingestion entry anyway (ingestion lag hides entries from EVERY
    window equally). The decision doc is written by the same request that
    emits the events, so the ±(2d/1d) window cannot miss real ones."""
    calls: list[dict] = []

    def _impl(**kwargs):
        calls.append(kwargs)
        return iter([])

    fake_client, _ = _install_fake_cloud_logging(monkeypatch, _impl)
    f = CloudLoggingFetcher(project="test-proj")
    f._client = fake_client

    out = f.fetch("d" * 32, around=datetime.now(timezone.utc) - timedelta(days=90))
    assert out == []
    assert len(calls) == 1
    assert 'timestamp<="' in calls[0]["filter_"]  # the one call was the bounded one


@pytest.mark.parametrize(
    "edge_offset",
    [
        timedelta(days=-2) + timedelta(hours=3),   # 3h above the floor (inside 6h guard)
        timedelta(days=1) - timedelta(hours=3),    # 3h below the ceiling (inside 6h guard)
    ],
    ids=["lower-edge", "upper-edge"],
)
def test_hinted_fetch_edge_hugging_entry_reruns_wide(monkeypatch, edge_offset):
    """An entry hugging EITHER edge of the hint window may mean the trace
    extends past it — rerun wide rather than serve (and let the endpoint
    cache) a truncated timeline."""
    calls: list[dict] = []
    hint = datetime.now(timezone.utc) - timedelta(days=30)
    near_edge = (hint + edge_offset).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _impl(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return iter([_fake_entry(near_edge)])
        return iter([_fake_entry(near_edge), _fake_entry(_iso_ago(days=33), "i0")])

    fake_client, _ = _install_fake_cloud_logging(monkeypatch, _impl)
    f = CloudLoggingFetcher(project="test-proj")
    f._client = fake_client

    out = f.fetch("d" * 32, around=hint)
    assert len(calls) == 2
    assert len(out) == 2  # the wide (superset) result wins


def test_hinted_fetch_naive_datetime_treated_as_utc(monkeypatch):
    """A tz-naive hint must not crash; it's interpreted as UTC (mirrors
    _straddles_fast_floor's convention)."""
    calls: list[dict] = []
    hint = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)

    def _impl(**kwargs):
        calls.append(kwargs)
        mid = _iso_ago(days=30, minutes=5)
        return iter([_fake_entry(mid)])

    fake_client, _ = _install_fake_cloud_logging(monkeypatch, _impl)
    f = CloudLoggingFetcher(project="test-proj")
    f._client = fake_client

    out = f.fetch("d" * 32, around=hint)
    assert len(out) == 1
    assert len(calls) == 1


def test_hinted_fetch_ignored_when_lookback_narrower_than_fast_floor(monkeypatch):
    """A deployment with lookback_days <= the fast floor keeps its existing
    single-query behavior even when a hint is supplied."""
    calls: list[dict] = []

    def _impl(**kwargs):
        calls.append(kwargs)
        return iter([])

    fake_client, _ = _install_fake_cloud_logging(monkeypatch, _impl)
    f = CloudLoggingFetcher(project="test-proj", lookback_days=1)
    f._client = fake_client

    assert f.fetch("d" * 32, around=datetime.now(timezone.utc) - timedelta(days=30)) == []
    assert len(calls) == 1
    assert _floor_age_of(calls[0]["filter_"]) < timedelta(days=1, hours=1)
    assert 'timestamp<="' not in calls[0]["filter_"]


def test_hinted_fetch_ignored_when_window_outside_lookback(monkeypatch):
    """Codex review MAJOR: a hint older than the configured lookback horizon
    must NOT let the bounded window search entries the deployment's
    ``lookback_days`` deliberately excludes. lookback=30d + hint@90d ago →
    the standard two-phase runs (fast floor, then 30d floor), no ceiling."""
    calls: list[dict] = []

    def _impl(**kwargs):
        calls.append(kwargs)
        return iter([])

    fake_client, _ = _install_fake_cloud_logging(monkeypatch, _impl)
    f = CloudLoggingFetcher(project="test-proj", lookback_days=30)
    f._client = fake_client

    out = f.fetch("d" * 32, around=datetime.now(timezone.utc) - timedelta(days=90))
    assert out == []
    assert len(calls) == 2
    assert _floor_age_of(calls[0]["filter_"]) < timedelta(days=3)          # fast phase
    assert timedelta(days=29) < _floor_age_of(calls[1]["filter_"]) < timedelta(days=31)  # configured wide
    assert 'timestamp<="' not in calls[0]["filter_"]
    assert 'timestamp<="' not in calls[1]["filter_"]


def test_hinted_fetch_used_when_window_inside_lookback(monkeypatch):
    """Companion to the horizon guard: lookback=30d + hint@10d ago → the
    bounded window DOES run (it fits entirely inside the horizon)."""
    calls: list[dict] = []
    hint = datetime.now(timezone.utc) - timedelta(days=10)

    def _impl(**kwargs):
        calls.append(kwargs)
        mid = (hint - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return iter([_fake_entry(mid)])

    fake_client, _ = _install_fake_cloud_logging(monkeypatch, _impl)
    f = CloudLoggingFetcher(project="test-proj", lookback_days=30)
    f._client = fake_client

    out = f.fetch("d" * 32, around=hint)
    assert len(out) == 1
    assert len(calls) == 1
    assert 'timestamp<="' in calls[0]["filter_"]


def test_hinted_fetch_rejects_bad_trace_id_before_client(monkeypatch):
    """The hex32 injection guard still short-circuits first, hint or not."""
    fake_client, _ = _install_fake_cloud_logging(monkeypatch, lambda **_: iter([]))
    f = CloudLoggingFetcher(project="test-proj")
    f._client = fake_client
    assert f.fetch("nope", around=datetime.now(timezone.utc)) == []
    assert fake_client.list_entries.call_count == 0


def test_stub_accepts_and_records_around_kwarg():
    """StubTraceFetcher mirrors the Protocol so integration tests can assert
    the endpoint threaded the hint through."""
    f = StubTraceFetcher(entries=[])
    hint = datetime(2026, 6, 1, tzinfo=timezone.utc)
    f.fetch("a" * 32, around=hint)
    assert f.last_around == hint
    f.fetch("a" * 32)
    assert f.last_around is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
