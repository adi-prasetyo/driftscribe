"""Unit tests for ``driftscribe_lib.logging`` (Phase 15.2).

Pins the JSON log format, the ContextVar plumbing, and the idempotency
contract of ``setup()``. We deliberately do NOT exercise the FastAPI
middleware here — that lives in ``tests/integration/test_trace_propagation.py``.

Test isolation: ``setup()`` mutates ``logging.getLogger()`` (process-wide).
The ``_clean_root_logger`` autouse fixture snapshots and restores the root
handlers + level around every test so a setup() call in one test cannot
bleed into another. We intentionally do NOT use a session-scope wipe —
that masks order bugs.
"""
from __future__ import annotations

import io
import json
import logging
import re

import pytest

from driftscribe_lib import logging as ds_logging
from driftscribe_lib.logging import (
    JSONFormatter,
    TraceIdFilter,
    current_trace_id_or_new,
    get_trace_id,
    new_trace_id,
    reset_trace_id,
    set_trace_id,
    setup,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clean_root_logger():
    """Snapshot/restore root handlers + level around every test.

    setup() is process-global; without this, a test that called setup()
    would leak the handler into every subsequent test (and the worker
    main.py modules import setup() at module load time too, so the order
    matters in CI).
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    # Drop only OUR marked handlers for the duration of the test (so a
    # prior module-level setup() from a worker import doesn't poison
    # the idempotency assertions). Non-ours stay.
    root.handlers = [
        h for h in root.handlers if not getattr(h, "_driftscribe_json_handler", False)
    ]
    yield
    root.handlers = saved_handlers
    root.setLevel(saved_level)


@pytest.fixture
def _clean_trace_id():
    """Reset the ContextVar to "" after each test so a forgotten reset
    doesn't pollute the next test's assertions."""
    token = set_trace_id("")
    yield
    reset_trace_id(token)


# --------------------------------------------------------------------------- #
# new_trace_id
# --------------------------------------------------------------------------- #


def test_new_trace_id_is_32_char_lowercase_hex() -> None:
    tid = new_trace_id()
    assert isinstance(tid, str)
    assert len(tid) == 32
    assert re.fullmatch(r"[0-9a-f]{32}", tid), tid


def test_new_trace_id_is_unique_across_calls() -> None:
    ids = {new_trace_id() for _ in range(50)}
    # 50 UUIDv4 hex values colliding is ~zero probability; if this fails,
    # somebody swapped in a non-random generator.
    assert len(ids) == 50


# --------------------------------------------------------------------------- #
# get/set/reset trace id
# --------------------------------------------------------------------------- #


def test_set_trace_id_returns_token_usable_by_reset() -> None:
    assert get_trace_id() == ""
    token = set_trace_id("abc")
    assert get_trace_id() == "abc"
    reset_trace_id(token)
    assert get_trace_id() == ""


def test_set_trace_id_round_trip_nested() -> None:
    """Nested set/reset must restore the previous value, not the default."""
    outer = set_trace_id("outer")
    inner = set_trace_id("inner")
    assert get_trace_id() == "inner"
    reset_trace_id(inner)
    assert get_trace_id() == "outer"
    reset_trace_id(outer)
    assert get_trace_id() == ""


# --------------------------------------------------------------------------- #
# TraceIdFilter
# --------------------------------------------------------------------------- #


def test_filter_injects_trace_id_when_set(_clean_trace_id) -> None:
    f = TraceIdFilter()
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname="x", lineno=1,
        msg="hi", args=(), exc_info=None,
    )
    token = set_trace_id("deadbeef" * 4)
    try:
        assert f.filter(record) is True
        assert record.trace_id == "deadbeef" * 4
    finally:
        reset_trace_id(token)


def test_filter_uses_dash_when_unset(_clean_trace_id) -> None:
    """When no trace_id is bound, the filter must inject ``"-"`` (not "")
    so the JSON log line has a non-empty placeholder that's easy to grep."""
    f = TraceIdFilter()
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname="x", lineno=1,
        msg="hi", args=(), exc_info=None,
    )
    assert f.filter(record) is True
    assert record.trace_id == "-"


# --------------------------------------------------------------------------- #
# JSONFormatter
# --------------------------------------------------------------------------- #


def _make_record(
    name: str = "test",
    level: int = logging.INFO,
    msg: str = "hello",
    args: tuple = (),
    exc_info=None,
    extra: dict | None = None,
) -> logging.LogRecord:
    rec = logging.LogRecord(
        name=name, level=level, pathname="x.py", lineno=1,
        msg=msg, args=args, exc_info=exc_info,
    )
    if extra:
        for k, v in extra.items():
            setattr(rec, k, v)
    return rec


def test_formatter_emits_valid_json_with_required_keys() -> None:
    f = JSONFormatter("driftscribe-agent")
    rec = _make_record(msg="recheck: drift detected")
    # The filter normally injects trace_id; simulate that here.
    rec.trace_id = "abc"
    out = f.format(rec)
    payload = json.loads(out)  # round-trips as JSON
    assert payload["msg"] == "recheck: drift detected"
    assert payload["service"] == "driftscribe-agent"
    assert payload["level"] == "INFO"
    assert payload["trace_id"] == "abc"
    assert "time" in payload
    # time field must be ISO-8601-ish, ending in Z (UTC marker).
    assert payload["time"].endswith("Z"), payload["time"]


def test_formatter_includes_extra_fields() -> None:
    """``log.info(msg, extra={"decision_id": "..."})`` must surface
    ``decision_id`` as a top-level key, not nested."""
    f = JSONFormatter("svc")
    rec = _make_record(extra={"decision_id": "dec-123", "worker": "reader"})
    rec.trace_id = "t1"
    payload = json.loads(f.format(rec))
    assert payload["decision_id"] == "dec-123"
    assert payload["worker"] == "reader"


def test_formatter_filters_standard_log_record_attrs() -> None:
    """The formatter must NOT emit internal LogRecord attrs like
    ``pathname``, ``args``, ``msecs`` as top-level JSON keys — those
    would pollute every log line."""
    f = JSONFormatter("svc")
    rec = _make_record()
    rec.trace_id = "t"
    payload = json.loads(f.format(rec))
    for k in ("pathname", "args", "msecs", "relativeCreated", "thread"):
        assert k not in payload, f"leaked standard attr {k!r}"


def test_formatter_includes_exception_info() -> None:
    """``log.exception(...)`` must produce an ``exc_info`` JSON field with
    the formatted traceback so ops can grep it."""
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        exc_info = sys.exc_info()
    f = JSONFormatter("svc")
    rec = _make_record(level=logging.ERROR, msg="oops", exc_info=exc_info)
    rec.trace_id = "t"
    payload = json.loads(f.format(rec))
    assert "exc_info" in payload
    assert "ValueError" in payload["exc_info"]
    assert "boom" in payload["exc_info"]


def test_formatter_default_str_for_unserializable_values() -> None:
    """``json.dumps`` defaults to ``str`` on unknown types so a non-
    JSON-safe extra value (e.g. a Path or a custom object) doesn't
    crash logging at runtime."""
    from pathlib import Path
    f = JSONFormatter("svc")
    rec = _make_record(extra={"path": Path("/tmp/foo")})
    rec.trace_id = "t"
    payload = json.loads(f.format(rec))  # must not raise
    assert payload["path"] == "/tmp/foo"


def test_formatter_message_args_are_interpolated() -> None:
    """``log.info("hello %s", "world")`` must produce
    ``"msg": "hello world"``, not ``"hello %s"``."""
    f = JSONFormatter("svc")
    rec = _make_record(msg="hello %s", args=("world",))
    rec.trace_id = "t"
    payload = json.loads(f.format(rec))
    assert payload["msg"] == "hello world"


# --------------------------------------------------------------------------- #
# setup()
# --------------------------------------------------------------------------- #


def _our_handlers(root: logging.Logger) -> list[logging.Handler]:
    return [h for h in root.handlers if getattr(h, "_driftscribe_json_handler", False)]


def test_setup_returns_named_logger() -> None:
    lg = setup("driftscribe-agent")
    assert isinstance(lg, logging.Logger)
    assert lg.name == "driftscribe-agent"


def test_setup_attaches_exactly_one_driftscribe_handler() -> None:
    setup("svc-a")
    root = logging.getLogger()
    assert len(_our_handlers(root)) == 1


def test_setup_is_idempotent_on_repeat_calls() -> None:
    """Calling setup() twice (e.g. via dev reloader or duplicate import)
    must not attach a second handler."""
    setup("svc-a")
    setup("svc-a")
    setup("svc-a")
    root = logging.getLogger()
    assert len(_our_handlers(root)) == 1


def test_setup_updates_formatter_service_name_on_repeat() -> None:
    """A second setup() with a different service name should update the
    existing handler's formatter rather than attach a new one (Codex
    review of plan: less surprising in test pytest where multiple
    workers may be imported in one process)."""
    setup("svc-a")
    setup("svc-b")
    root = logging.getLogger()
    handlers = _our_handlers(root)
    assert len(handlers) == 1
    formatter = handlers[0].formatter
    assert isinstance(formatter, JSONFormatter)
    # Render a record and confirm the new service name is in the output.
    rec = _make_record()
    rec.trace_id = "t"
    payload = json.loads(formatter.format(rec))
    assert payload["service"] == "svc-b"


def test_setup_preserves_non_driftscribe_handlers() -> None:
    """A pre-existing handler (e.g. pytest's caplog, or an embedder's
    custom handler) must NOT be removed by setup()."""
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    try:
        setup("svc")
        assert sentinel in root.handlers, "setup() removed a non-driftscribe handler"
    finally:
        root.removeHandler(sentinel)


def test_setup_emits_json_end_to_end() -> None:
    """End-to-end: setup() → log.info → captured stream produces parseable JSON."""
    # Run setup so the root handler is in place, then redirect the
    # handler's stream to a StringIO so we can capture.
    lg = setup("svc")
    buf = io.StringIO()
    root = logging.getLogger()
    our_h = _our_handlers(root)[0]
    saved_stream = our_h.stream
    our_h.stream = buf
    try:
        token = set_trace_id("trace-xyz")
        try:
            lg.info("hello %s", "world", extra={"decision_id": "dec-1"})
        finally:
            reset_trace_id(token)
    finally:
        our_h.stream = saved_stream

    line = buf.getvalue().strip()
    assert line, "no log line was emitted"
    payload = json.loads(line)
    assert payload["msg"] == "hello world"
    assert payload["trace_id"] == "trace-xyz"
    assert payload["decision_id"] == "dec-1"
    assert payload["service"] == "svc"
    assert payload["level"] == "INFO"


# --------------------------------------------------------------------------- #
# install_trace_middleware exposed at module level
# --------------------------------------------------------------------------- #


def test_install_trace_middleware_is_exposed() -> None:
    """Smoke check: the helper exists and is callable. Behavior is
    covered by ``tests/integration/test_trace_propagation.py`` against
    real FastAPI apps."""
    assert callable(ds_logging.install_trace_middleware)


# --------------------------------------------------------------------------- #
# current_trace_id_or_new — outbound-safe accessor
# --------------------------------------------------------------------------- #


def test_current_trace_id_or_new_returns_bound_value_when_valid(
    _clean_trace_id,
) -> None:
    """A well-formed bound id is returned unchanged (no fresh mint)."""
    tid = "f" * 32
    token = set_trace_id(tid)
    try:
        assert current_trace_id_or_new() == tid
    finally:
        reset_trace_id(token)


def test_current_trace_id_or_new_mints_when_unset(_clean_trace_id) -> None:
    out = current_trace_id_or_new()
    assert re.fullmatch(r"[0-9a-f]{32}", out)


def test_current_trace_id_or_new_mints_when_bound_value_is_malformed(
    _clean_trace_id,
) -> None:
    """A non-conformant value (somehow bound outside the middleware) is
    replaced with a freshly minted id rather than propagated."""
    token = set_trace_id("not-a-uuid")
    try:
        out = current_trace_id_or_new()
        assert out != "not-a-uuid"
        assert re.fullmatch(r"[0-9a-f]{32}", out)
    finally:
        reset_trace_id(token)


def test_current_trace_id_or_new_mints_when_bound_value_is_uppercase(
    _clean_trace_id,
) -> None:
    """Uppercase 32-hex is rejected by the validating accessor — the
    middleware normalizes inbound, but if something else bound an
    uppercase value we still hand a lowercase fresh id outbound."""
    token = set_trace_id("A" * 32)
    try:
        out = current_trace_id_or_new()
        assert out != "A" * 32
        assert re.fullmatch(r"[0-9a-f]{32}", out)
    finally:
        reset_trace_id(token)
