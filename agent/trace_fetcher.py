"""TraceFetcher abstraction for the /trace endpoint (Phase 19.A.5).

The `/trace/{trace_id}` endpoint (added in a later Phase 19.A step) needs to
read structured log entries Cloud Run shipped to Cloud Logging, filtered to
just the entries that belong to one DriftScribe decision. This module hides
the two implementations behind a single Protocol so tests can override the
fetcher via FastAPI's ``app.dependency_overrides`` without touching network.

Two implementations:

* :class:`CloudLoggingFetcher` — production. Uses the sync google-cloud-logging
  client (promoted to a direct dep in Phase 19.A.5 so a future ADK version
  dropping the OTEL exporter doesn't silently break /trace).
* :class:`StubTraceFetcher` — in-memory. Used by the unit/integration suite via
  ``app.dependency_overrides`` so the test process never touches GCP.

The fetcher is instantiated lazily by ``get_trace_fetcher()`` in
``agent/main.py`` — process-wide singleton, reset between integration tests
via ``_reset_trace_fetcher_for_tests``.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Protocol

# Cloud trace IDs are 16-byte hex (32 chars, lowercase). We use this both as
# a sanity check on the URL parameter and — more importantly — as a
# defense-in-depth guard against filter-string injection into the Cloud
# Logging query language. Phase 19.A.4 generates trace_ids via
# ``current_trace_id_or_new()`` which conforms to this format.
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")

# Default lower bound (in days) for the ``timestamp`` clause on every Cloud
# Logging query. This is load-bearing, not a perf tweak: OBSERVED behavior of
# ``entries.list`` is that a filter with NO timestamp clause only searches
# roughly the last day of logs, so a trace older than ~24h comes back empty
# even though the entry is still well within the log bucket's retention. That
# regressed "open trace" for any conversation older than a day (durable turns,
# unreachable reasoning). Pinning a floor at ``now - lookback`` forces Cloud
# Logging to search the full retained window. 400 ≥ the ``_Default`` bucket's
# 365-day retention, so any still-retained trace is reachable; entries older
# than retention don't exist, so a wider floor buys nothing. Override with
# ``TRACE_LOG_LOOKBACK_DAYS`` if a deployment lengthens retention.
_DEFAULT_TRACE_LOOKBACK_DAYS = 400

# The event kinds the pipeline actually emits (verified by grep over agent/ +
# driftscribe_lib/): ``llm_thought``/``tool_call``/``tool_result``/``llm_usage``/
# ``final_response`` from agent/adk_agent.py:873 and ``mcp_call`` from
# agent/mcp/developer_knowledge.py:432. ALL SIX are load-bearing — dropping any
# one regresses a real consumer:
#   * ``tool_result`` — the SPA pairs it with ``tool_call`` for result_preview
#     (Timeline.svelte); without it every tool call renders with no result.
#   * ``final_response`` — required by ``_observe_and_check_stability``
#     (agent/main.py:719) to ever flip the ``complete`` flag; without it a
#     trace polls forever and never caches as done.
# This is an explicit allowlist, not an existence check (``jsonPayload.event:*``),
# so future stray event-bearing plumbing logs can't ride the trace_id filter in
# unnoticed — a new kind needs a deliberate addition here.
_EVENT_KINDS = (
    "llm_thought",
    "tool_call",
    "tool_result",
    "llm_usage",
    "mcp_call",
    "final_response",
)

# Floor (in days) for the FAST first phase of the two-phase query below.
# entries.list latency grows with the width of the ``timestamp`` window —
# measured against prod (2026-07-06, same trace_id, same 23 entries):
# ~1.4s @ 1d, ~2.7s @ 30d, ~3.9s @ 60d, ~17s @ 400d. The endpoint's Future
# budget is a hard wall, so querying the retention-deep window UNCONDITIONALLY
# (the original PR #204 shape) turned every /trace call into a timeout → the
# post-turn mcp_call backfill and every "open trace" 503'd. Two days covers
# every hot path (the ~1/sec live poll, the post-turn backfill, opens on
# recent conversations); only a miss pays for the wide window.
_FAST_PHASE_LOOKBACK_DAYS = 2

# If the OLDEST entry the fast phase returned sits within this band above the
# fast floor, the trace may extend past the floor (partially out of window) —
# rerun wide rather than serve (and cache) a head-truncated timeline. 6h is
# generous: real traces (one chat turn, one worker fan-out, one C2 plan build)
# span minutes.
_FAST_PHASE_STRADDLE_GUARD = _dt.timedelta(hours=6)


class TraceFetcher(Protocol):
    """Return the structured log entries for one trace, order UNSPECIFIED.

    Implementations may return any order (``CloudLoggingFetcher`` pulls
    ``timestamp desc``); the ``/trace`` endpoint re-sorts ascending by
    (timestamp, insert_id) before rendering, so ordering is endpoint-owned, not
    a fetcher contract. Each entry is a dict from the structured JSON payload —
    Phase 18's ``JSONFormatter`` puts our extras at the top of ``jsonPayload``,
    and Cloud Run's stdout parser turns that into ``entry.payload`` on the
    client side.
    """

    def fetch(self, trace_id: str, *, limit: int = 500) -> list[dict]: ...


class CloudLoggingFetcher:
    """Production. Reads from Cloud Logging via the sync Python client.

    Per-process singleton; instantiated lazily so tests that don't go near
    GCP don't pull in google-cloud-logging at import time. Caller MUST hold a
    service account with ``roles/logging.viewer`` (granted in 19.A.0).

    Note: ``Client.list_entries()`` in google-cloud-logging 3.15.x has NO
    timeout parameter — time-bounding happens at the endpoint level via
    ``concurrent.futures.Future.result(timeout=...)``, not here. The
    data-size bound is ``max_results=limit`` (default 500).
    """

    def __init__(
        self,
        project: str,
        service_name: str = "driftscribe-agent",
        lookback_days: int = _DEFAULT_TRACE_LOOKBACK_DAYS,
    ):
        # Lazy import: keeps unit tests that never construct this class from
        # paying the google-cloud-logging import cost (and from needing
        # network or ADC to be wired during import).
        from google.cloud import logging as cloud_logging

        self._client = cloud_logging.Client(project=project)
        self._service = service_name
        self._lookback_days = lookback_days

    def _timestamp_floor(self, days: int) -> str:
        """RFC3339 ``now - days`` floor for the query's ``timestamp`` clause.

        Computed per fetch (not cached) so a long-lived singleton doesn't pin a
        stale floor. Whole-second ``...Z`` form — Cloud Logging's filter parser
        accepts it and it keeps the snapshot test's regex simple.
        """
        floor = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)
        return floor.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _query(self, trace_id: str, *, floor_days: int, limit: int) -> list[dict]:
        # Filter syntax confirmed correct for our JSONFormatter — Cloud Run's
        # structured-stdout pipeline puts our extras under ``jsonPayload.*``
        # (NOT ``labels.*`` or ``textPayload``). The snapshot test in
        # test_trace_fetcher.py protects against accidental regression here.
        #
        # The ``timestamp>=`` floor is REQUIRED for correctness, not just
        # speed — without it Cloud Logging only searches ~the last day and a
        # trace older than 24h returns empty (see _DEFAULT_TRACE_LOOKBACK_DAYS).
        # The floor is a constant we build (never caller input), so it can't
        # widen the injection surface the _HEX32_RE guard in ``fetch`` closes.
        #
        # The ``jsonPayload.event=(...)`` clause is REQUIRED for correctness
        # too, not just cleanliness — every log line emitted inside the
        # request context inherits ``trace_id`` via the ContextVar
        # (driftscribe_lib.logging), so without this clause plumbing logs
        # (httpx, PyGithub) ride along and pollute the timeline. See
        # ``_EVENT_KINDS`` for why the allowlist is exactly these six kinds.
        event_clause = " OR ".join(f'"{k}"' for k in _EVENT_KINDS)
        filter_str = (
            f'resource.type="cloud_run_revision" '
            f'AND resource.labels.service_name="{self._service}" '
            f'AND jsonPayload.trace_id="{trace_id}" '
            f'AND jsonPayload.event=({event_clause}) '
            f'AND timestamp>="{self._timestamp_floor(floor_days)}"'
        )
        # ``timestamp desc``: Cloud Logging recommends descending order for
        # recently-ingested logs, so the ~1/sec live-poll path (whose trace is
        # seconds old) stays fast. The /trace endpoint re-sorts ascending by
        # (timestamp, insert_id) before render, so the display order is
        # unchanged; and if a pathologically chatty trace ever exceeds
        # ``limit`` entries, this keeps the NEWEST ``limit`` (dropping the
        # oldest tail) rather than stalling on the oldest.
        entries_iter = self._client.list_entries(
            filter_=filter_str,
            order_by="timestamp desc",
            page_size=limit,
            max_results=limit,
        )
        return [_entry_to_dict(e) for e in entries_iter]

    def _straddles_fast_floor(self, entries: list[dict]) -> bool:
        """True when the fast phase's OLDEST entry sits suspiciously close to
        the fast floor — the trace may continue past the window edge, so the
        caller must rerun wide rather than serve a head-truncated timeline
        (which ``get_trace`` could then cache as complete).

        Unparseable timestamps count as straddling: fail toward the wide
        (correct, slower) phase, never toward silent truncation.
        """
        guard_floor = (
            _dt.datetime.now(_dt.timezone.utc)
            - _dt.timedelta(days=_FAST_PHASE_LOOKBACK_DAYS)
            + _FAST_PHASE_STRADDLE_GUARD
        )
        for e in entries:
            raw = str(e.get("timestamp", ""))
            try:
                ts = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=_dt.timezone.utc)
            except ValueError:
                return True
            if ts <= guard_floor:
                return True
        return False

    def fetch(self, trace_id: str, *, limit: int = 500) -> list[dict]:
        # ``fullmatch`` (not ``match``) is load-bearing here: Python's ``$``
        # in ``re.match`` mode also matches just-before-a-final ``\n``, so
        # ``"a"*32 + "\n"`` would slip past a ``match`` call and get
        # interpolated literally into the filter string. The guard's job is
        # fail-closed at the security boundary — no corner exemptions.
        if not _HEX32_RE.fullmatch(trace_id):
            # Fail-closed against filter-string injection — the trace_id flows
            # straight into the Cloud Logging filter language, so anything that
            # doesn't look like our 32-hex format gets refused at the door.
            return []
        # Two-phase query (2026-07-06 outage lesson): entries.list latency
        # grows with the width of the ``timestamp`` window (measurements at
        # _FAST_PHASE_LOOKBACK_DAYS above), and a single retention-deep query
        # blew the endpoint's fetch budget on EVERY call — the /trace 503s
        # took the post-turn mcp_call backfill and all trace replays down
        # with them. The narrow phase serves the hot paths in ~1.5s; the wide
        # phase runs only when the narrow one proves insufficient.
        fast_days = min(_FAST_PHASE_LOOKBACK_DAYS, self._lookback_days)
        entries = self._query(trace_id, floor_days=fast_days, limit=limit)
        if self._lookback_days <= fast_days:
            return entries
        if entries and not self._straddles_fast_floor(entries):
            return entries
        return self._query(trace_id, floor_days=self._lookback_days, limit=limit)


class StubTraceFetcher:
    """In-memory. Used by tests via ``app.dependency_overrides``."""

    def __init__(self, entries: list[dict] | None = None):
        self.entries = entries or []
        # Exposed so tests can assert cache / dedup behavior at the
        # ``/trace`` endpoint layer once that lands.
        self.calls = 0

    def fetch(self, trace_id: str, *, limit: int = 500) -> list[dict]:
        self.calls += 1
        # Mirrors CloudLoggingFetcher's ``jsonPayload.event=(...)`` clause so
        # dev/dry-run parity holds: an entry with no ``event`` key, or one
        # outside ``_EVENT_KINDS``, would never come back from prod either.
        return [
            e
            for e in self.entries
            if e.get("trace_id") == trace_id and e.get("event") in _EVENT_KINDS
        ][:limit]


def _entry_to_dict(entry: Any) -> dict:
    """Convert a google-cloud-logging LogEntry to our payload dict.

    JSONFormatter (driftscribe_lib/logging.py) writes every field at the top
    of ``jsonPayload``, so ``entry.payload`` is already the structured event
    dict we want. We additionally surface ``timestamp`` (ISO-8601) and
    ``insert_id`` (Cloud Logging's per-entry unique string) at the top level
    so callers can sort / dedupe without reaching into the LogEntry object.
    """
    if isinstance(entry.payload, dict):
        d = dict(entry.payload)
    else:
        d = {"text": entry.payload}
    d.setdefault("timestamp", entry.timestamp.isoformat() if entry.timestamp else "")
    d.setdefault("insert_id", entry.insert_id or "")
    return d
