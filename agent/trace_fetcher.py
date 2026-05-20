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

import re
from typing import Any, Protocol

# Cloud trace IDs are 16-byte hex (32 chars, lowercase). We use this both as
# a sanity check on the URL parameter and — more importantly — as a
# defense-in-depth guard against filter-string injection into the Cloud
# Logging query language. Phase 19.A.4 generates trace_ids via
# ``current_trace_id_or_new()`` which conforms to this format.
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")


class TraceFetcher(Protocol):
    """Return entries ordered by (timestamp asc, insert_id asc).

    Each entry is a dict from the structured JSON payload — Phase 18's
    ``JSONFormatter`` puts our extras at the top of ``jsonPayload``, and Cloud
    Run's stdout parser turns that into ``entry.payload`` on the client side.
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

    def __init__(self, project: str, service_name: str = "driftscribe-agent"):
        # Lazy import: keeps unit tests that never construct this class from
        # paying the google-cloud-logging import cost (and from needing
        # network or ADC to be wired during import).
        from google.cloud import logging as cloud_logging

        self._client = cloud_logging.Client(project=project)
        self._service = service_name

    def fetch(self, trace_id: str, *, limit: int = 500) -> list[dict]:
        if not _HEX32_RE.match(trace_id):
            # Fail-closed against filter-string injection — the trace_id flows
            # straight into the Cloud Logging filter language, so anything that
            # doesn't look like our 32-hex format gets refused at the door.
            return []
        # Filter syntax confirmed correct for our JSONFormatter — Cloud Run's
        # structured-stdout pipeline puts our extras under ``jsonPayload.*``
        # (NOT ``labels.*`` or ``textPayload``). The snapshot test in
        # test_trace_fetcher.py protects against accidental regression here.
        filter_str = (
            f'resource.type="cloud_run_revision" '
            f'AND resource.labels.service_name="{self._service}" '
            f'AND jsonPayload.trace_id="{trace_id}"'
        )
        entries_iter = self._client.list_entries(
            filter_=filter_str,
            order_by="timestamp asc",
            page_size=limit,
            max_results=limit,
        )
        return [_entry_to_dict(e) for e in entries_iter]


class StubTraceFetcher:
    """In-memory. Used by tests via ``app.dependency_overrides``."""

    def __init__(self, entries: list[dict] | None = None):
        self.entries = entries or []
        # Exposed so tests can assert cache / dedup behavior at the
        # ``/trace`` endpoint layer once that lands.
        self.calls = 0

    def fetch(self, trace_id: str, *, limit: int = 500) -> list[dict]:
        self.calls += 1
        return [e for e in self.entries if e.get("trace_id") == trace_id][:limit]


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
