"""Pin the structured log shape for ``tool_call`` (with args) and
``tool_result`` events. Phase 19.A.3 (transparency UI) extends the
Phase 18.B.2 ``tool_call`` log line to carry redacted ``tool_args`` and
adds a brand-new ``tool_result`` event emitted whenever ADK yields a
``function_response`` part.

The redact-at-emit invariant is the security boundary: the durable Cloud
Logging copy must never carry raw credentials. Every field that could
plausibly carry a secret (``tool_args`` dict, ``result_preview`` JSON
string) runs through :func:`agent.secret_guard.redact_event` BEFORE
``_log.info`` builds the structured record.

Field schema (consumed by the Phase 19 ``/trace`` endpoint + the
transparency UI):

  event=tool_call    trace_id=<hex32>  workload=<name>  tool_name=<n>
                      tool_args=<redacted dict>
  event=tool_result  trace_id=<hex32>  workload=<name>  tool_name=<n>
                      result_preview=<redacted JSON ≤2000 chars>
                      result_ok=<bool, False iff dict has error/errors>
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import adk_agent
from agent.workload_context import reset_workload, set_workload
from tests.unit._adk_stubs import StubEvent as _Ev, StubPart as _P


def _fc(name, args=None):
    """Build a SimpleNamespace shaped like google.genai.types.FunctionCall."""
    return SimpleNamespace(name=name, args=args)


def _fr(name, response=None):
    """Build a SimpleNamespace shaped like google.genai.types.FunctionResponse."""
    return SimpleNamespace(name=name, response=response)


# --------------------------------------------------------------------------- #
# Invariant (a): ``tool_call`` log lines now carry a redacted ``tool_args``.
# --------------------------------------------------------------------------- #


async def _stub_tool_call_with_args(*args, **kwargs):
    # The model picks a tool with structured args, then emits a final.
    yield _Ev(
        [_P(function_call=_fc("read_drift", {"region": "us-east1", "service": "checkout"}))],
        partial=False,
    )
    yield _Ev(
        [_P(text='{"action":"no_op","env_diffs":[],"rationale":"matches","confidence":0.9}')],
        partial=False,
        final=True,
    )


@pytest.mark.asyncio
async def test_run_chat_tool_call_carries_redacted_tool_args(caplog, drift_workload_env):
    caplog.set_level(logging.INFO, logger="driftscribe.agent.adk_agent")
    token = set_workload("drift")
    try:
        with patch.object(adk_agent, "Runner") as runner_cls:
            runner_cls.return_value.run_async = _stub_tool_call_with_args
            await adk_agent.run_chat("hi", workload="drift")
    finally:
        reset_workload(token)

    tool_calls = [r for r in caplog.records if getattr(r, "event", None) == "tool_call"]
    assert len(tool_calls) == 1
    rec = tool_calls[0]
    assert getattr(rec, "tool_name") == "read_drift"
    # tool_args is the structured arg dict (plain values pass through —
    # nothing here is secret-named).
    assert getattr(rec, "tool_args") == {"region": "us-east1", "service": "checkout"}


async def _stub_tool_call_with_secret_args(*args, **kwargs):
    yield _Ev(
        [_P(function_call=_fc("read_drift", {"DATABASE_URL": "postgres://u:p@h/d", "region": "us-east1"}))],
        partial=False,
    )
    yield _Ev(
        [_P(text='{"action":"no_op","env_diffs":[],"rationale":"matches","confidence":0.9}')],
        partial=False,
        final=True,
    )


@pytest.mark.asyncio
async def test_run_chat_tool_call_args_are_redacted_at_emit(caplog, drift_workload_env):
    """Key-aware redaction at emit — DATABASE_URL never reaches Cloud Logging."""
    caplog.set_level(logging.INFO, logger="driftscribe.agent.adk_agent")
    token = set_workload("drift")
    try:
        with patch.object(adk_agent, "Runner") as runner_cls:
            runner_cls.return_value.run_async = _stub_tool_call_with_secret_args
            await adk_agent.run_chat("hi", workload="drift")
    finally:
        reset_workload(token)

    tool_calls = [r for r in caplog.records if getattr(r, "event", None) == "tool_call"]
    assert len(tool_calls) == 1
    args = getattr(tool_calls[0], "tool_args")
    assert args["DATABASE_URL"] == "<redacted>"
    assert args["region"] == "us-east1"


# --------------------------------------------------------------------------- #
# Invariant (b): one ``tool_result`` log line per function_response part.
# --------------------------------------------------------------------------- #


async def _stub_tool_result_ok(*args, **kwargs):
    # Sequence: tool_call → tool_result(success) → final.
    yield _Ev(
        [_P(function_call=_fc("read_drift", {}))],
        partial=False,
    )
    yield _Ev(
        [_P(function_response=_fr("read_drift", {"status": "ok", "env": {"PAYMENT_MODE": "live"}}))],
        partial=False,
    )
    yield _Ev(
        [_P(text='{"action":"no_op","env_diffs":[],"rationale":"matches","confidence":0.9}')],
        partial=False,
        final=True,
    )


@pytest.mark.asyncio
async def test_run_chat_emits_tool_result_for_function_response(caplog, drift_workload_env):
    caplog.set_level(logging.INFO, logger="driftscribe.agent.adk_agent")
    token = set_workload("drift")
    try:
        with patch.object(adk_agent, "Runner") as runner_cls:
            runner_cls.return_value.run_async = _stub_tool_result_ok
            await adk_agent.run_chat("hi", workload="drift")
    finally:
        reset_workload(token)

    results = [r for r in caplog.records if getattr(r, "event", None) == "tool_result"]
    assert len(results) == 1
    rec = results[0]
    assert getattr(rec, "tool_name") == "read_drift"
    assert getattr(rec, "result_ok") is True
    preview = getattr(rec, "result_preview")
    assert isinstance(preview, str)
    assert len(preview) <= 2000
    # Plain (non-secret) keys in result_preview pass through.
    assert "PAYMENT_MODE" in preview
    assert "live" in preview


async def _stub_tool_result_error(*args, **kwargs):
    yield _Ev(
        [_P(function_call=_fc("read_drift", {}))],
        partial=False,
    )
    yield _Ev(
        [_P(function_response=_fr("read_drift", {"error": "worker timeout"}))],
        partial=False,
    )
    yield _Ev(
        [_P(text='{"action":"no_op","env_diffs":[],"rationale":"matches","confidence":0.9}')],
        partial=False,
        final=True,
    )


@pytest.mark.asyncio
async def test_run_chat_tool_result_marks_error_dicts(caplog, drift_workload_env):
    caplog.set_level(logging.INFO, logger="driftscribe.agent.adk_agent")
    token = set_workload("drift")
    try:
        with patch.object(adk_agent, "Runner") as runner_cls:
            runner_cls.return_value.run_async = _stub_tool_result_error
            await adk_agent.run_chat("hi", workload="drift")
    finally:
        reset_workload(token)

    results = [r for r in caplog.records if getattr(r, "event", None) == "tool_result"]
    assert len(results) == 1
    assert getattr(results[0], "result_ok") is False


async def _stub_tool_result_errors_plural(*args, **kwargs):
    """``errors`` (plural) should also flip result_ok to False."""
    yield _Ev(
        [_P(function_call=_fc("read_drift", {}))],
        partial=False,
    )
    yield _Ev(
        [_P(function_response=_fr("read_drift", {"errors": ["bad config"]}))],
        partial=False,
    )
    yield _Ev(
        [_P(text='{"action":"no_op","env_diffs":[],"rationale":"matches","confidence":0.9}')],
        partial=False,
        final=True,
    )


@pytest.mark.asyncio
async def test_run_chat_tool_result_marks_errors_plural_dicts(caplog, drift_workload_env):
    caplog.set_level(logging.INFO, logger="driftscribe.agent.adk_agent")
    token = set_workload("drift")
    try:
        with patch.object(adk_agent, "Runner") as runner_cls:
            runner_cls.return_value.run_async = _stub_tool_result_errors_plural
            await adk_agent.run_chat("hi", workload="drift")
    finally:
        reset_workload(token)

    results = [r for r in caplog.records if getattr(r, "event", None) == "tool_result"]
    assert len(results) == 1
    assert getattr(results[0], "result_ok") is False


# --------------------------------------------------------------------------- #
# Invariant (c): both fields run through redact_event before emit.
# --------------------------------------------------------------------------- #


async def _stub_tool_result_credentialed_url(*args, **kwargs):
    yield _Ev(
        [_P(function_call=_fc("read_drift", {}))],
        partial=False,
    )
    yield _Ev(
        [_P(function_response=_fr("read_drift", {"connect_string": "postgres://u:p@host/db"}))],
        partial=False,
    )
    yield _Ev(
        [_P(text='{"action":"no_op","env_diffs":[],"rationale":"matches","confidence":0.9}')],
        partial=False,
        final=True,
    )


@pytest.mark.asyncio
async def test_run_chat_tool_result_strips_credentialed_urls(caplog, drift_workload_env):
    caplog.set_level(logging.INFO, logger="driftscribe.agent.adk_agent")
    token = set_workload("drift")
    try:
        with patch.object(adk_agent, "Runner") as runner_cls:
            runner_cls.return_value.run_async = _stub_tool_result_credentialed_url
            await adk_agent.run_chat("hi", workload="drift")
    finally:
        reset_workload(token)

    results = [r for r in caplog.records if getattr(r, "event", None) == "tool_result"]
    preview = getattr(results[0], "result_preview")
    assert "u:p@" not in preview
    assert "<redacted>" in preview


# Same invariants on the /recheck path (run_agent).


@pytest.mark.asyncio
async def test_run_agent_tool_call_carries_redacted_tool_args(caplog, drift_workload_env):
    caplog.set_level(logging.INFO, logger="driftscribe.agent.adk_agent")
    token = set_workload("drift")
    try:
        with patch.object(adk_agent, "Runner") as runner_cls:
            runner_cls.return_value.run_async = _stub_tool_call_with_secret_args
            await adk_agent.run_agent("hi", workload="drift")
    finally:
        reset_workload(token)

    tool_calls = [r for r in caplog.records if getattr(r, "event", None) == "tool_call"]
    assert len(tool_calls) == 1
    args = getattr(tool_calls[0], "tool_args")
    assert args["DATABASE_URL"] == "<redacted>"


@pytest.mark.asyncio
async def test_run_agent_emits_tool_result_for_function_response(caplog, drift_workload_env):
    caplog.set_level(logging.INFO, logger="driftscribe.agent.adk_agent")
    token = set_workload("drift")
    try:
        with patch.object(adk_agent, "Runner") as runner_cls:
            runner_cls.return_value.run_async = _stub_tool_result_ok
            await adk_agent.run_agent("hi", workload="drift")
    finally:
        reset_workload(token)

    results = [r for r in caplog.records if getattr(r, "event", None) == "tool_result"]
    assert len(results) == 1
    assert getattr(results[0], "result_ok") is True


# --------------------------------------------------------------------------- #
# Invariant (g): the optional ``iac_pr_sink`` captures a CONFIRMED first-authoring
# infra PR (open_infra_pr only) so the single-agent stream can surface a clickable
# approval CTA. It must match on the tool NAME (open_infra_pr_tool), never on the
# result shape — upgrade_propose_pr returns the same pr_number/pr_url fields but is
# NOT an /iac-approvals PR.
# --------------------------------------------------------------------------- #


def test_emit_event_logs_captures_iac_pr_for_open_infra_pr():
    from agent.adk_tools import open_infra_pr_tool

    sink: dict = {}
    ev = _Ev(
        [_P(function_response=_fr(
            open_infra_pr_tool.__name__,
            {"status": "opened", "pr_number": 12,
             "pr_url": "https://github.com/o/r/pull/12", "branch": "infra/x"},
        ))],
        partial=False,
    )
    adk_agent._emit_event_logs(ev, iac_pr_sink=sink)
    assert sink == {"pr_number": 12, "pr_url": "https://github.com/o/r/pull/12"}


def test_emit_event_logs_does_not_capture_iac_pr_for_other_tools():
    """An upgrade PR (same pr_number/pr_url shape, DIFFERENT tool name) must NOT
    populate the iac_pr sink — only open_infra_pr maps to an /iac-approvals page."""
    sink: dict = {}
    ev = _Ev(
        [_P(function_response=_fr(
            "upgrade_propose_pr_tool",
            {"status": "opened", "pr_number": 99, "pr_url": "https://x/pull/99"},
        ))],
        partial=False,
    )
    adk_agent._emit_event_logs(ev, iac_pr_sink=sink)
    assert sink == {}


def test_emit_event_logs_iac_pr_sink_last_write_wins():
    """Two open_infra_pr results in one run → the sink reflects the LAST one."""
    from agent.adk_tools import open_infra_pr_tool

    sink: dict = {}
    ev1 = _Ev([_P(function_response=_fr(
        open_infra_pr_tool.__name__,
        {"status": "opened", "pr_number": 1, "pr_url": "https://x/pull/1"}))],
        partial=False)
    ev2 = _Ev([_P(function_response=_fr(
        open_infra_pr_tool.__name__,
        {"status": "opened", "pr_number": 2, "pr_url": "https://x/pull/2"}))],
        partial=False)
    adk_agent._emit_event_logs(ev1, iac_pr_sink=sink)
    adk_agent._emit_event_logs(ev2, iac_pr_sink=sink)
    assert sink == {"pr_number": 2, "pr_url": "https://x/pull/2"}


def test_emit_event_logs_iac_pr_sink_ignored_for_malformed_open_infra_pr():
    """A confirmed-shape gate: an open_infra_pr result missing pr_url leaves the
    sink empty (no half-formed pointer)."""
    from agent.adk_tools import open_infra_pr_tool

    sink: dict = {}
    ev = _Ev([_P(function_response=_fr(
        open_infra_pr_tool.__name__, {"status": "opened", "pr_number": 5}))],
        partial=False)
    adk_agent._emit_event_logs(ev, iac_pr_sink=sink)
    assert sink == {}


# --------------------------------------------------------------------------- #
# Invariant (h) (CRITICAL): nested secret-keyed values must be redacted via
# the structured-then-serialize order, not just regex on the JSON string.
# --------------------------------------------------------------------------- #


async def _stub_tool_result_nested_secrets(*args, **kwargs):
    yield _Ev(
        [_P(function_call=_fc("read_drift", {}))],
        partial=False,
    )
    yield _Ev(
        [_P(function_response=_fr(
            "read_drift",
            {"DATABASE_URL": "postgres://u:p@h/d", "nested": {"PASSWORD": "abc"}},
        ))],
        partial=False,
    )
    yield _Ev(
        [_P(text='{"action":"no_op","env_diffs":[],"rationale":"matches","confidence":0.9}')],
        partial=False,
        final=True,
    )


@pytest.mark.asyncio
async def test_run_chat_tool_result_redacts_nested_secret_keys_before_serialize(
    caplog, drift_workload_env
):
    """The CRITICAL bug from the Codex v2 review: if ``redact_event``
    runs on the JSON STRING rather than on the structured dict, the
    key-aware half of the redaction surface never fires on nested
    secret-keyed values. Pin the structured-first order here.
    """
    caplog.set_level(logging.INFO, logger="driftscribe.agent.adk_agent")
    token = set_workload("drift")
    try:
        with patch.object(adk_agent, "Runner") as runner_cls:
            runner_cls.return_value.run_async = _stub_tool_result_nested_secrets
            await adk_agent.run_chat("hi", workload="drift")
    finally:
        reset_workload(token)

    results = [r for r in caplog.records if getattr(r, "event", None) == "tool_result"]
    assert len(results) == 1
    preview = getattr(results[0], "result_preview")
    # Parse the preview as JSON and assert both secret-keyed values are
    # redacted — this is the structured-then-serialize invariant. A
    # plain regex pass on the JSON string would catch the userinfo in
    # DATABASE_URL but miss the nested PASSWORD entirely.
    parsed = json.loads(preview)
    assert parsed["DATABASE_URL"] == "<redacted>"
    assert parsed["nested"]["PASSWORD"] == "<redacted>"
    # Also confirm the raw secret string never appears in the preview.
    assert "abc" not in preview
    assert "u:p@" not in preview
