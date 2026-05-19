"""Structured JSON logging with trace-ID propagation (Phase 15.2).

Replaces the Phase 11.2 stub. Public surface:

- :func:`setup` — idempotent root-logger configuration; returns a
  service-named :class:`logging.Logger`. Existing call sites
  (``log = setup_logging("reader-agent")`` in every worker's main.py)
  keep working unchanged.
- :func:`new_trace_id` — mint a 32-char lowercase hex trace id (UUIDv4).
- :func:`get_trace_id` / :func:`set_trace_id` / :func:`reset_trace_id` —
  ContextVar plumbing for binding the trace id to the current async
  task. ``set_trace_id`` returns a :class:`Token` that ``reset_trace_id``
  consumes; this is the standard ContextVar lifecycle and is what the
  HTTP middleware uses to scope the binding to a single request.
- :func:`install_trace_middleware` — mount FastAPI middleware that
  reads ``X-Trace-Id`` (or mints one), binds it to the ContextVar for
  the duration of the request, and echoes it back on the response.

JSON output (one event per line) carries ``time``, ``trace_id``,
``service``, ``level``, ``logger``, ``msg``, ``exc_info`` (when an
exception is logged), plus any ``extra={...}`` fields the caller
passed to the log call. Non-JSON-serializable extras fall back to
``str(value)`` so a stray ``Path`` or domain object never crashes
logging in production.

Design notes (cf. Codex review of Phase 15.2 plan):

- Root handlers we install are marked with the private attribute
  ``_driftscribe_json_handler = True``. ``setup()`` only manages
  marked handlers — uvicorn's, pytest's caplog's, and any embedder's
  pre-existing handlers stay untouched.
- A repeat ``setup()`` with a different service name updates the
  formatter's service field in place rather than attaching a second
  handler. This keeps multi-import test scenarios (workers all
  importing setup at module load) predictable.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import uuid
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "JSONFormatter",
    "TraceIdFilter",
    "get_trace_id",
    "install_trace_middleware",
    "new_trace_id",
    "reset_trace_id",
    "set_trace_id",
    "setup",
]


# ContextVar that holds the inbound (or freshly-minted) trace id for the
# current request. Default of "" (not None) is intentional — TraceIdFilter
# converts "" to "-" before injecting, so log lines outside a request
# context get a non-empty placeholder.
_TRACE_ID: ContextVar[str] = ContextVar("trace_id", default="")


# LogRecord attributes the stdlib sets on every record. We filter these
# out when serializing extras so the JSON output isn't polluted with
# ``pathname``, ``thread``, etc. on every line. ``trace_id`` is omitted
# because we surface it as a top-level field; it would otherwise also
# appear (the TraceIdFilter sets it as a record attr).
_STANDARD_LOG_RECORD_ATTRS = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "message", "module",
    "msecs", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "taskName", "thread", "threadName",
})


# 32-char lowercase hex (UUIDv4 with dashes stripped). We deliberately do
# NOT accept dashed-UUID form on the inbound header: keeping the wire
# format constrained to one shape makes log greps simpler.
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")


def new_trace_id() -> str:
    """Return a fresh 32-char lowercase hex trace id (UUIDv4)."""
    return uuid.uuid4().hex


def get_trace_id() -> str:
    """Return the trace id bound to the current context, or ``""``."""
    return _TRACE_ID.get()


def set_trace_id(tid: str) -> Token[str]:
    """Bind ``tid`` to the current context. Returns a Token for reset."""
    return _TRACE_ID.set(tid)


def reset_trace_id(token: Token[str]) -> None:
    """Restore the previous trace-id binding using ``token``."""
    _TRACE_ID.reset(token)


class TraceIdFilter(logging.Filter):
    """Inject the current ContextVar value as ``record.trace_id``.

    Set to ``"-"`` when the ContextVar is empty so the JSON line has a
    visually obvious placeholder for "outside-of-request" log lines
    (module-level loggers, startup messages, etc.).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = _TRACE_ID.get() or "-"
        return True


class JSONFormatter(logging.Formatter):
    """Serialize a :class:`LogRecord` as a single JSON line.

    Required keys: ``time``, ``trace_id``, ``service``, ``level``,
    ``logger``, ``msg``. Optional: ``exc_info`` (formatted traceback
    when an exception was passed), plus any ``extra={...}`` fields the
    caller attached to the log record.
    """

    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        # getMessage() applies %-formatting with record.args. Calling it
        # once and stashing the result means the extras-walk below sees
        # ``record.message`` already populated (matches stdlib behavior).
        msg = record.getMessage()
        when = datetime.fromtimestamp(record.created, tz=timezone.utc)
        # millisecond precision, Z suffix — Cloud Logging parses this as
        # a structured timestamp without further hints.
        time_str = when.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        payload: dict[str, Any] = {
            "time": time_str,
            "trace_id": getattr(record, "trace_id", "-"),
            "service": self.service,
            "level": record.levelname,
            "logger": record.name,
            "msg": msg,
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        # Surface caller-supplied extras as top-level keys. Skip stdlib
        # LogRecord builtins and anything we've already populated.
        for key, value in record.__dict__.items():
            if key in _STANDARD_LOG_RECORD_ATTRS:
                continue
            if key in payload or key == "trace_id":
                continue
            payload[key] = value
        # ``default=str`` keeps logging crash-proof against domain
        # objects (Path, datetime, etc.) in extras.
        return json.dumps(payload, default=str)


def _our_handler(root: logging.Logger) -> logging.Handler | None:
    """Return the single DriftScribe-marked handler on ``root``, if any."""
    for h in root.handlers:
        if getattr(h, "_driftscribe_json_handler", False):
            return h
    return None


def setup(service_name: str, level: int | str | None = None) -> logging.Logger:
    """Configure the root logger for JSON output. Idempotent.

    Behavior:

    - First call: attaches a single StreamHandler (stderr) with
      :class:`JSONFormatter` + :class:`TraceIdFilter`. The handler is
      marked with ``_driftscribe_json_handler = True`` so subsequent
      calls can find it.
    - Subsequent calls: do NOT attach another handler. If ``service_name``
      changed (e.g. a second worker module imports setup() in the same
      pytest process), the existing handler's formatter is updated to
      the new service name. Level is re-applied each call.
    - Pre-existing non-DriftScribe handlers (pytest caplog, uvicorn,
      embedder code) are preserved.

    Returns a :class:`logging.Logger` named ``service_name`` so callers
    can keep their existing ``log = setup_logging("reader-agent")``
    pattern unchanged.
    """
    root = logging.getLogger()
    effective_level = level if level is not None else os.environ.get("LOG_LEVEL", "INFO")
    root.setLevel(effective_level)

    existing = _our_handler(root)
    if existing is None:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JSONFormatter(service_name))
        handler.addFilter(TraceIdFilter())
        # Marker: how setup() recognizes its own handler on repeat calls.
        handler._driftscribe_json_handler = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    else:
        # Update in place so a second setup() with a different service
        # name (e.g. test pytest importing multiple worker modules)
        # doesn't keep emitting the first service name forever.
        formatter = existing.formatter
        if isinstance(formatter, JSONFormatter):
            formatter.service = service_name
        else:
            existing.setFormatter(JSONFormatter(service_name))

    return logging.getLogger(service_name)


def install_trace_middleware(app: Any) -> None:
    """Mount Starlette HTTP middleware that binds a trace id per request.

    Behavior:

    - Reads inbound ``X-Trace-Id`` header. If present and matches
      ``^[0-9a-f]{32}$`` (case-insensitive — we normalize to lowercase),
      adopt it. Otherwise mint a fresh UUIDv4 hex. Malformed headers
      are silently discarded — a misbehaving upstream cannot poison
      our log correlation.
    - Sets the ContextVar via :func:`set_trace_id`, holding the Token.
    - Wraps ``call_next`` in try/finally so an unhandled exception
      can't leak the binding into the next request.
    - Echoes the trace id back on the response as ``X-Trace-Id``. For
      handled FastAPI responses this works as expected; for truly
      unhandled exceptions Starlette's default exception handler may
      construct the 500 response after our finally has run, in which
      case the response will not carry the header. That's an
      acceptable trade-off for the hackathon — the log line still
      carries trace_id (the binding was active during the log call).

    Called once per FastAPI app, typically right after
    ``app = FastAPI(...)``. Safe to import at module load time.
    """

    @app.middleware("http")
    async def trace_id_middleware(request, call_next):
        raw = request.headers.get("X-Trace-Id", "")
        candidate = raw.lower()
        tid = candidate if _HEX32_RE.match(candidate) else new_trace_id()
        token = set_trace_id(tid)
        try:
            response = await call_next(request)
        finally:
            reset_trace_id(token)
        response.headers["X-Trace-Id"] = tid
        return response
