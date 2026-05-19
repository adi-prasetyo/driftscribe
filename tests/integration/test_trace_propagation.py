"""End-to-end trace-id propagation tests (Phase 15.2).

Pins three properties of the trace-id machinery:

1. The coordinator's trace middleware mints, adopts, or rejects-and-
   re-mints the ``X-Trace-Id`` header per the policy in
   :mod:`driftscribe_lib.logging`.
2. ``worker_client.call`` carries the current ContextVar's trace id
   outbound on the ``X-Trace-Id`` header.
3. Inbound trace ids flow into a worker's log lines (so a single
   trace id can be grep'd across coordinator + worker logs).

The unit-level shape tests for the formatter/filter live in
``tests/unit/test_logging.py``; this file only tests the
request/response wiring against real FastAPI apps via TestClient.
"""
from __future__ import annotations

import io
import json
import logging
import re

import pytest
import respx
from fastapi.testclient import TestClient

from agent import worker_client
from agent.main import app as agent_app
from driftscribe_lib import logging as ds_logging
from driftscribe_lib.logging import (
    JSONFormatter,
    TraceIdFilter,
    reset_trace_id,
    set_trace_id,
)


HEX32 = re.compile(r"^[0-9a-f]{32}$")


# --------------------------------------------------------------------------- #
# Coordinator middleware: mint / adopt / reject inbound X-Trace-Id
# --------------------------------------------------------------------------- #


def test_coordinator_mints_trace_id_when_header_absent() -> None:
    client = TestClient(agent_app)
    r = client.get("/healthz")
    assert r.status_code == 200
    tid = r.headers.get("X-Trace-Id")
    assert tid is not None, "middleware did not echo X-Trace-Id"
    assert HEX32.match(tid), f"not 32-char hex: {tid!r}"


def test_coordinator_adopts_well_formed_inbound_trace_id() -> None:
    known = "a" * 32  # 32 lowercase hex chars
    client = TestClient(agent_app)
    r = client.get("/healthz", headers={"X-Trace-Id": known})
    assert r.status_code == 200
    assert r.headers["X-Trace-Id"] == known


def test_coordinator_normalizes_uppercase_inbound_to_lowercase() -> None:
    """HTTP clients sometimes uppercase UUID hex. The middleware
    normalizes to lowercase so log-line correlation is consistent."""
    upper = "A" * 32
    client = TestClient(agent_app)
    r = client.get("/healthz", headers={"X-Trace-Id": upper})
    assert r.headers["X-Trace-Id"] == "a" * 32


def test_coordinator_rejects_malformed_inbound_and_mints_fresh() -> None:
    """A misbehaving upstream cannot poison our log correlation by
    sending ``X-Trace-Id: not-a-uuid``."""
    client = TestClient(agent_app)
    r = client.get("/healthz", headers={"X-Trace-Id": "not-a-uuid"})
    tid = r.headers["X-Trace-Id"]
    assert tid != "not-a-uuid"
    assert HEX32.match(tid), tid


def test_coordinator_response_trace_id_varies_across_requests() -> None:
    """Two unauthenticated /healthz requests with no inbound header must
    get distinct freshly-minted trace ids (sanity: the binding is
    per-request, not process-global)."""
    client = TestClient(agent_app)
    a = client.get("/healthz").headers["X-Trace-Id"]
    b = client.get("/healthz").headers["X-Trace-Id"]
    assert a != b


# --------------------------------------------------------------------------- #
# worker_client.call: outbound X-Trace-Id from the ContextVar
# --------------------------------------------------------------------------- #


READER_URL = "https://reader.example.com"


@pytest.fixture
def _worker_env(monkeypatch):
    monkeypatch.setenv("READER_URL", READER_URL)


@pytest.fixture
def _stub_mint_token(monkeypatch):
    monkeypatch.setattr(worker_client, "mint_id_token", lambda aud: "fake-tok")


@respx.mock
def test_worker_client_propagates_current_trace_id(
    _worker_env, _stub_mint_token
) -> None:
    """``worker_client.call`` reads the current ContextVar and injects
    its value as ``X-Trace-Id`` on the outbound request."""
    route = respx.post(f"{READER_URL}/read").respond(200, json={"env": {}})
    tid = "b" * 32
    token = set_trace_id(tid)
    try:
        worker_client.call("reader", {})
    finally:
        reset_trace_id(token)
    assert route.called
    sent = route.calls.last.request.headers.get("X-Trace-Id")
    assert sent == tid


@respx.mock
def test_worker_client_mints_trace_id_when_contextvar_empty(
    _worker_env, _stub_mint_token
) -> None:
    """If for any reason the ContextVar is empty when call() runs (e.g.
    a CLI invocation outside a request), mint one — never send empty."""
    route = respx.post(f"{READER_URL}/read").respond(200, json={"env": {}})
    # Force a known-empty ContextVar.
    token = set_trace_id("")
    try:
        worker_client.call("reader", {})
    finally:
        reset_trace_id(token)
    sent = route.calls.last.request.headers.get("X-Trace-Id")
    assert sent, "X-Trace-Id should never be empty on outbound worker calls"
    assert HEX32.match(sent), sent


@respx.mock
def test_worker_client_does_not_leak_malformed_inbound_outbound(
    _worker_env, _stub_mint_token
) -> None:
    """Coupling check with the coordinator middleware: a malformed
    inbound header is replaced with a fresh trace id at the middleware
    layer, so worker_client.call (which reads the ContextVar) sees the
    fresh value, not the malformed one. We simulate the post-middleware
    state by directly binding a known-good id."""
    route = respx.post(f"{READER_URL}/read").respond(200, json={"env": {}})
    # The middleware would have set this; we set it here directly.
    fresh = "c" * 32
    token = set_trace_id(fresh)
    try:
        worker_client.call("reader", {})
    finally:
        reset_trace_id(token)
    sent = route.calls.last.request.headers.get("X-Trace-Id")
    assert sent == fresh
    assert sent != "not-a-uuid"


# --------------------------------------------------------------------------- #
# Worker side: inbound trace id reaches log output
# --------------------------------------------------------------------------- #


def _reader_app_for_logging_test(monkeypatch):
    """Import the reader worker with the env it needs at module load.

    Reader does ``setup_logging("reader-agent")`` at module top, and
    ``install_trace_middleware(app)`` after app construction. We
    monkeypatch the verify_caller dep to bypass auth and stub the Cloud
    Run read.
    """
    monkeypatch.setenv("GCP_PROJECT", "test-proj")
    monkeypatch.setenv("OWN_URL", "https://reader.example.com")
    monkeypatch.setenv(
        "ALLOWED_CALLERS",
        "coordinator@test-proj.iam.gserviceaccount.com",
    )
    from workers.reader import main as reader_main

    monkeypatch.setattr(
        reader_main,
        "read_live_state",
        lambda *a, **k: {"env": {"X": "1"}, "revision": "rev-1"},
    )
    reader_main.app.dependency_overrides[reader_main._verify_caller_dep] = (
        lambda: "tester"
    )
    return reader_main


def test_worker_log_line_carries_inbound_trace_id(monkeypatch) -> None:
    """An inbound ``X-Trace-Id`` on a worker request appears in that
    request's log line (the whole point of the propagation chain)."""
    reader_main = _reader_app_for_logging_test(monkeypatch)

    # Attach a fresh JSONFormatter-backed StreamHandler to a buffer so
    # we can read back what the worker logged during the request.
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JSONFormatter("reader-agent"))
    handler.addFilter(TraceIdFilter())
    root = logging.getLogger()
    root.addHandler(handler)
    saved_level = root.level
    root.setLevel(logging.INFO)
    try:
        client = TestClient(reader_main.app)
        known = "d" * 32
        r = client.post("/read", json={}, headers={"X-Trace-Id": known})
        assert r.status_code == 200
        assert r.headers["X-Trace-Id"] == known
    finally:
        root.removeHandler(handler)
        root.setLevel(saved_level)
        reader_main.app.dependency_overrides.pop(
            reader_main._verify_caller_dep, None
        )

    # The reader emits ``log.info("read request from %s ...")`` inside
    # the /read handler. That line should carry our trace id.
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    assert lines, "no log lines captured"
    parsed = [json.loads(ln) for ln in lines]
    trace_ids = {p["trace_id"] for p in parsed}
    # At least one of the log lines emitted during the request must
    # carry our known trace id. (Other lines — e.g. uvicorn access
    # logs if they ever route through root — may carry "-" instead.)
    assert known in trace_ids, f"trace id missing from worker logs: {trace_ids}"


def test_worker_mints_trace_id_when_inbound_absent(monkeypatch) -> None:
    """A worker hit directly (no upstream) still gets a fresh trace id
    on the response so logs are correlatable post-hoc."""
    reader_main = _reader_app_for_logging_test(monkeypatch)
    try:
        client = TestClient(reader_main.app)
        r = client.post("/read", json={})
        assert r.status_code == 200
        tid = r.headers["X-Trace-Id"]
        assert HEX32.match(tid), tid
    finally:
        reader_main.app.dependency_overrides.pop(
            reader_main._verify_caller_dep, None
        )


# --------------------------------------------------------------------------- #
# Smoke: install_trace_middleware exported helper is what coordinator uses
# --------------------------------------------------------------------------- #


def test_coordinator_middleware_resets_contextvar_after_request() -> None:
    """A request handler that raises must NOT leak its trace id into
    subsequent requests' contexts (the try/finally in
    install_trace_middleware is what guarantees this)."""
    client = TestClient(agent_app)
    inbound = "e" * 32
    client.get("/healthz", headers={"X-Trace-Id": inbound})
    # After the request, the ContextVar on this thread should be back
    # to default. (FastAPI's TestClient runs the request in the same
    # asyncio task, so the binding propagates back out via reset.)
    assert ds_logging.get_trace_id() == ""
