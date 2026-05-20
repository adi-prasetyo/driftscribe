"""Pin the ``final_response`` event invariant: exactly one log line per
``run_chat`` / ``run_agent``, only when the collected final text is
non-empty, with a redacted ≤2000-char preview + a ``response_kind``
classifier (``"json"`` / ``"text"``).

The transparency UI (Phase 19.B) uses the presence of this event in the
trace as the deterministic completion signal — caching + grace-window
logic in :func:`agent.main.get_trace` gate on ``final_response`` rather
than on ADK's per-iteration ``llm_usage`` (which fires multiple times in
a multi-turn run). Pinning "exactly one emit, no false positives" is
therefore load-bearing for the UI's correctness, not just an aesthetic
field-shape concern.

Three CRITICAL-from-Codex-v2 invariants:

- (g) Zero ``final_response`` emit on the no-text error path (where the
  loop raises ``RuntimeError("ADK agent produced no final response")``).
- Empty preview emit must NOT fire — the accepted-text precondition
  guards against the v2 bug that would emit ``response_preview=""``
  immediately before the raise.
- Multi-turn runs (multiple ``llm_usage`` events) still produce exactly
  one ``final_response``.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import adk_agent
from agent.workload_context import reset_workload, set_workload
from tests.unit._adk_stubs import StubEvent as _Ev, StubPart as _P


def _usage(prompt=10, candidates=10, thoughts=0, total=20):
    return SimpleNamespace(
        prompt_token_count=prompt,
        candidates_token_count=candidates,
        thoughts_token_count=thoughts,
        total_token_count=total,
    )


# --------------------------------------------------------------------------- #
# Invariants (d) + (e): one final_response per run, with preview + kind.
# --------------------------------------------------------------------------- #


async def _stub_single_turn_json(*args, **kwargs):
    yield _Ev(
        [_P(text='{"action":"no_op","env_diffs":[],"rationale":"matches","confidence":0.9}')],
        partial=False,
        final=True,
        usage=_usage(),
    )


@pytest.mark.asyncio
async def test_run_chat_emits_exactly_one_final_response(caplog, drift_workload_env):
    caplog.set_level(logging.INFO, logger="driftscribe.agent.adk_agent")
    token = set_workload("drift")
    try:
        with patch.object(adk_agent, "Runner") as runner_cls:
            runner_cls.return_value.run_async = _stub_single_turn_json
            await adk_agent.run_chat("hi", workload="drift")
    finally:
        reset_workload(token)

    finals = [r for r in caplog.records if getattr(r, "event", None) == "final_response"]
    assert len(finals) == 1
    rec = finals[0]
    preview = getattr(rec, "response_preview")
    assert isinstance(preview, str)
    assert len(preview) <= 2000
    assert preview.startswith("{")
    assert getattr(rec, "response_kind") == "json"
    assert getattr(rec, "workload") == "drift"


async def _stub_single_turn_text(*args, **kwargs):
    """Plain-text final response (not JSON). Used by /chat for the
    natural-language reply path."""
    yield _Ev(
        [_P(text="The drift is resolved. No further action needed.")],
        partial=False,
        final=True,
        usage=_usage(),
    )


@pytest.mark.asyncio
async def test_run_chat_final_response_kind_text(caplog, drift_workload_env):
    caplog.set_level(logging.INFO, logger="driftscribe.agent.adk_agent")
    token = set_workload("drift")
    try:
        with patch.object(adk_agent, "Runner") as runner_cls:
            runner_cls.return_value.run_async = _stub_single_turn_text
            await adk_agent.run_chat("hi", workload="drift")
    finally:
        reset_workload(token)

    finals = [r for r in caplog.records if getattr(r, "event", None) == "final_response"]
    assert len(finals) == 1
    assert getattr(finals[0], "response_kind") == "text"


# --------------------------------------------------------------------------- #
# Invariant (f): multi-turn runs still produce exactly one final_response.
# --------------------------------------------------------------------------- #


async def _stub_multi_turn(*args, **kwargs):
    # Turn 1: tool_call event with usage.
    yield _Ev(
        [_P(function_call=SimpleNamespace(name="read_drift", args={}))],
        partial=False,
        usage=_usage(),
    )
    # Turn 2: a thought + usage emit but no final.
    yield _Ev(
        [_P(text="reasoning step", thought=True)],
        partial=False,
        usage=_usage(),
    )
    # Turn 3: final response with another usage emit.
    yield _Ev(
        [_P(text='{"action":"no_op","env_diffs":[],"rationale":"matches","confidence":0.9}')],
        partial=False,
        final=True,
        usage=_usage(),
    )


@pytest.mark.asyncio
async def test_run_chat_multi_turn_emits_exactly_one_final_response(caplog, drift_workload_env):
    caplog.set_level(logging.INFO, logger="driftscribe.agent.adk_agent")
    token = set_workload("drift")
    try:
        with patch.object(adk_agent, "Runner") as runner_cls:
            runner_cls.return_value.run_async = _stub_multi_turn
            await adk_agent.run_chat("hi", workload="drift")
    finally:
        reset_workload(token)

    # Sanity: multiple llm_usage events.
    usages = [r for r in caplog.records if getattr(r, "event", None) == "llm_usage"]
    assert len(usages) >= 2
    # But exactly one final_response.
    finals = [r for r in caplog.records if getattr(r, "event", None) == "final_response"]
    assert len(finals) == 1


# --------------------------------------------------------------------------- #
# Invariant (g) CRITICAL: zero final_response on the no-text error path.
# --------------------------------------------------------------------------- #


async def _stub_no_text_final(*args, **kwargs):
    """A final event whose parts are empty / thought-only — the loop
    must NOT emit a ``final_response`` log line AND must raise
    ``RuntimeError``. Pins the v2 bug where the emit fired with
    ``response_preview=""`` right before the raise."""
    # Final event with empty parts list.
    yield _Ev([], partial=False, final=True)


@pytest.mark.asyncio
async def test_run_chat_no_final_response_emit_on_empty_text(caplog, drift_workload_env):
    caplog.set_level(logging.INFO, logger="driftscribe.agent.adk_agent")
    token = set_workload("drift")
    try:
        with patch.object(adk_agent, "Runner") as runner_cls:
            runner_cls.return_value.run_async = _stub_no_text_final
            with pytest.raises(RuntimeError, match="no final response"):
                await adk_agent.run_chat("hi", workload="drift")
    finally:
        reset_workload(token)

    finals = [r for r in caplog.records if getattr(r, "event", None) == "final_response"]
    assert finals == [], f"expected zero final_response emits, got {len(finals)}"


async def _stub_thought_only_final(*args, **kwargs):
    """A final event with only a thought part (no response text). The
    loop strips thoughts and ends up with empty accepted text → no
    final_response emit + RuntimeError."""
    yield _Ev(
        [_P(text="thinking through it", thought=True)],
        partial=False,
        final=True,
    )


@pytest.mark.asyncio
async def test_run_chat_no_final_response_emit_on_thought_only_final(caplog, drift_workload_env):
    caplog.set_level(logging.INFO, logger="driftscribe.agent.adk_agent")
    token = set_workload("drift")
    try:
        with patch.object(adk_agent, "Runner") as runner_cls:
            runner_cls.return_value.run_async = _stub_thought_only_final
            with pytest.raises(RuntimeError, match="no final response"):
                await adk_agent.run_chat("hi", workload="drift")
    finally:
        reset_workload(token)

    finals = [r for r in caplog.records if getattr(r, "event", None) == "final_response"]
    assert finals == []


@pytest.mark.asyncio
async def test_run_agent_no_final_response_emit_on_empty_text(caplog, drift_workload_env):
    caplog.set_level(logging.INFO, logger="driftscribe.agent.adk_agent")
    token = set_workload("drift")
    try:
        with patch.object(adk_agent, "Runner") as runner_cls:
            runner_cls.return_value.run_async = _stub_no_text_final
            with pytest.raises(RuntimeError, match="no final response"):
                await adk_agent.run_agent("hi", workload="drift")
    finally:
        reset_workload(token)

    finals = [r for r in caplog.records if getattr(r, "event", None) == "final_response"]
    assert finals == []


# --------------------------------------------------------------------------- #
# Same invariants on the /recheck path (run_agent).
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_run_agent_emits_exactly_one_final_response(caplog, drift_workload_env):
    caplog.set_level(logging.INFO, logger="driftscribe.agent.adk_agent")
    token = set_workload("drift")
    try:
        with patch.object(adk_agent, "Runner") as runner_cls:
            runner_cls.return_value.run_async = _stub_single_turn_json
            proposal = await adk_agent.run_agent("hi", workload="drift")
    finally:
        reset_workload(token)

    assert proposal is not None
    finals = [r for r in caplog.records if getattr(r, "event", None) == "final_response"]
    assert len(finals) == 1
    rec = finals[0]
    assert getattr(rec, "response_kind") == "json"
    assert getattr(rec, "response_preview").startswith("{")


@pytest.mark.asyncio
async def test_run_agent_multi_turn_emits_exactly_one_final_response(caplog, drift_workload_env):
    caplog.set_level(logging.INFO, logger="driftscribe.agent.adk_agent")
    token = set_workload("drift")
    try:
        with patch.object(adk_agent, "Runner") as runner_cls:
            runner_cls.return_value.run_async = _stub_multi_turn
            await adk_agent.run_agent("hi", workload="drift")
    finally:
        reset_workload(token)

    finals = [r for r in caplog.records if getattr(r, "event", None) == "final_response"]
    assert len(finals) == 1


# --------------------------------------------------------------------------- #
# Preview length cap: huge JSON gets truncated to 2000 chars.
# --------------------------------------------------------------------------- #


async def _stub_huge_response(*args, **kwargs):
    big = '{"data":"' + ("x" * 5000) + '"}'
    yield _Ev(
        [_P(text=big)],
        partial=False,
        final=True,
    )


@pytest.mark.asyncio
async def test_run_chat_final_response_preview_truncated_to_2000(caplog, drift_workload_env):
    caplog.set_level(logging.INFO, logger="driftscribe.agent.adk_agent")
    token = set_workload("drift")
    try:
        with patch.object(adk_agent, "Runner") as runner_cls:
            runner_cls.return_value.run_async = _stub_huge_response
            await adk_agent.run_chat("hi", workload="drift")
    finally:
        reset_workload(token)

    finals = [r for r in caplog.records if getattr(r, "event", None) == "final_response"]
    assert len(finals) == 1
    assert len(getattr(finals[0], "response_preview")) == 2000


# --------------------------------------------------------------------------- #
# Codex v3 follow-up: pin redact-before-truncate ordering. If we truncated
# first, a credentialed URL straddling the 2000-char boundary could be cut
# mid-userinfo, the regex would no longer match, and a partial credential
# would leak into Cloud Logging.
# --------------------------------------------------------------------------- #


async def _stub_response_with_credential_near_boundary(*args, **kwargs):
    # Pack the prefix so the credentialed URL lands such that the userinfo
    # `u:p@` would be cut mid-segment by a naive `text[:2000]`.
    # The URL "postgres://u:p@host/db" is 22 chars. Put it so the cut
    # would land just past "postgres://u" with truncate-first ordering.
    prefix = "A" * 1989  # 1989 chars
    big = prefix + "postgres://u:p@host/db" + " trailing stuff"
    # Truncate-then-redact would cut at char 2000 → "...postgres://u" — no
    # `:p@` match → leaks "u" with no redaction sentinel.
    yield _Ev(
        [_P(text=big)],
        partial=False,
        final=True,
    )


@pytest.mark.asyncio
async def test_run_chat_final_response_redacts_credential_straddling_boundary(
    caplog, drift_workload_env
):
    """Pin redact-then-truncate (NOT the reverse) so a credentialed URL
    that straddles the 2000-char preview boundary still gets its
    userinfo replaced — never leaked as a partial credential.

    With truncate-first, the preview would end with ``postgres://u`` —
    the regex no longer matches the truncated text, ``u`` leaks. With
    redact-first, the userinfo regex matches first against the full
    text (yielding ``postgres://<redacted>@host/db``), and the
    truncation cuts a section that may or may not include the
    ``<redacted>@`` sentinel but provably contains no userinfo.
    """
    caplog.set_level(logging.INFO, logger="driftscribe.agent.adk_agent")
    token = set_workload("drift")
    try:
        with patch.object(adk_agent, "Runner") as runner_cls:
            runner_cls.return_value.run_async = _stub_response_with_credential_near_boundary
            await adk_agent.run_chat("hi", workload="drift")
    finally:
        reset_workload(token)

    finals = [r for r in caplog.records if getattr(r, "event", None) == "final_response"]
    assert len(finals) == 1
    preview = getattr(finals[0], "response_preview")
    # Length cap holds.
    assert len(preview) <= 2000
    # The security invariant: no userinfo leakage in any form. With
    # truncate-first the preview would end with `postgres://u` (the
    # first char of the username, cut from `u:p@host/db`). The
    # production code now redacts before truncating, so the worst case
    # is the truncation happens INSIDE the `<redacted>` sentinel —
    # never inside the original `u:p` segment.
    assert "u:p@" not in preview
    # Look at the last 50 chars of the preview — the userinfo segment,
    # if it was going to leak, would surface here.
    tail = preview[-50:]
    # Critical: the username ``u`` (the first char of the credential
    # pair) must not appear at the end of the preview as raw text.
    # ``postgres://`` ending the preview is acceptable — that's the
    # scheme + separator, not credentials. The bad case is
    # ``postgres://u`` at the tail (which would leak the username).
    assert not tail.endswith("postgres://u")


# --------------------------------------------------------------------------- #
# Codex v3 follow-up: whitespace-only final text must take the no-final-
# response error path, not surface as a parse error downstream.
# --------------------------------------------------------------------------- #


async def _stub_whitespace_only_final(*args, **kwargs):
    yield _Ev(
        [_P(text="   \n  \t")],
        partial=False,
        final=True,
    )


@pytest.mark.asyncio
async def test_run_agent_whitespace_only_final_raises_no_final_response(
    caplog, drift_workload_env
):
    caplog.set_level(logging.INFO, logger="driftscribe.agent.adk_agent")
    token = set_workload("drift")
    try:
        with patch.object(adk_agent, "Runner") as runner_cls:
            runner_cls.return_value.run_async = _stub_whitespace_only_final
            with pytest.raises(RuntimeError, match="no final response"):
                await adk_agent.run_agent("hi", workload="drift")
    finally:
        reset_workload(token)

    finals = [r for r in caplog.records if getattr(r, "event", None) == "final_response"]
    assert finals == []


@pytest.mark.asyncio
async def test_run_chat_whitespace_only_final_raises_no_final_response(
    caplog, drift_workload_env
):
    caplog.set_level(logging.INFO, logger="driftscribe.agent.adk_agent")
    token = set_workload("drift")
    try:
        with patch.object(adk_agent, "Runner") as runner_cls:
            runner_cls.return_value.run_async = _stub_whitespace_only_final
            with pytest.raises(RuntimeError, match="no final response"):
                await adk_agent.run_chat("hi", workload="drift")
    finally:
        reset_workload(token)

    finals = [r for r in caplog.records if getattr(r, "event", None) == "final_response"]
    assert finals == []


# --------------------------------------------------------------------------- #
# Codex v3 follow-up: a malformed ADK runner that yields TWO is_final_response
# events still produces exactly one final_response emit (the flag holds).
# --------------------------------------------------------------------------- #


async def _stub_two_final_events(*args, **kwargs):
    """Pathological: ADK yields two events with is_final_response=True.
    The ``final_response_logged`` flag must prevent a second emit."""
    yield _Ev(
        [_P(text='{"action":"no_op","env_diffs":[],"rationale":"first","confidence":0.9}')],
        partial=False,
        final=True,
    )
    yield _Ev(
        [_P(text='{"action":"no_op","env_diffs":[],"rationale":"second","confidence":0.8}')],
        partial=False,
        final=True,
    )


@pytest.mark.asyncio
async def test_run_chat_two_final_events_emit_only_once(caplog, drift_workload_env):
    caplog.set_level(logging.INFO, logger="driftscribe.agent.adk_agent")
    token = set_workload("drift")
    try:
        with patch.object(adk_agent, "Runner") as runner_cls:
            runner_cls.return_value.run_async = _stub_two_final_events
            await adk_agent.run_chat("hi", workload="drift")
    finally:
        reset_workload(token)

    finals = [r for r in caplog.records if getattr(r, "event", None) == "final_response"]
    assert len(finals) == 1, f"flag failed to dedupe: got {len(finals)} emits"
    # The first event's preview wins (flag set on first emit).
    assert "first" in getattr(finals[0], "response_preview")


@pytest.mark.asyncio
async def test_run_agent_two_final_events_emit_only_once(caplog, drift_workload_env):
    caplog.set_level(logging.INFO, logger="driftscribe.agent.adk_agent")
    token = set_workload("drift")
    try:
        with patch.object(adk_agent, "Runner") as runner_cls:
            runner_cls.return_value.run_async = _stub_two_final_events
            await adk_agent.run_agent("hi", workload="drift")
    finally:
        reset_workload(token)

    finals = [r for r in caplog.records if getattr(r, "event", None) == "final_response"]
    assert len(finals) == 1
    assert "first" in getattr(finals[0], "response_preview")
