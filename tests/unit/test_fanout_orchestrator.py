"""Unit tests for the D5-6 fan-out orchestrator
:func:`agent.fanout.run_provision_fanout_stream`.

The orchestrator ties the already-built fan-out engine (decompose →
parallel author → deterministic barrier) into ONE streaming entrypoint that
makes the SINGLE convergent editor call. It yields the SAME item shapes as
``agent.adk_agent.run_chat_stream`` — ``{"type":"event","event":{...}}`` items
then exactly one ``{"type":"result", ...}`` — under one monotonic ``seq``.

Mocking is at the seams: ``decompose`` / ``author_slices_parallel`` are module
globals in ``agent.fanout`` (patch them there); the orchestrator imports
``run_chat_stream`` / ``_emit_*`` from ``agent.adk_agent`` and
``derive_iac_pr_authority`` from ``agent.adk_tools`` LAZILY at call time, so we
patch those at their SOURCE module. ``call_open_infra_pr`` is patched on
``agent.worker_client``. ``resolve_provision_read_tools`` is patched to ``{}``
so no real workload/ADK is loaded.
"""
from __future__ import annotations

import asyncio
import importlib

import pytest

from agent import fanout
from agent.adk_tools import IacPrAuthority
from agent.fanout import (
    AuthorResult,
    DecomposeResult,
    FanoutError,
    FanoutFailureKind,
    SliceSpec,
)

from ._adk_stubs import StubEvent, StubPart


# --------------------------------------------------------------------------- #
# Helpers / common fixtures
# --------------------------------------------------------------------------- #


# The orchestrator imports run_chat_stream / _emit_* from agent.adk_agent,
# derive_iac_pr_authority from agent.adk_tools, and call_open_infra_pr from
# agent.worker_client LAZILY at call time — each ``import`` reads sys.modules.
# We therefore patch the attribute on the LIVE sys.modules object (resolved via
# importlib.import_module), NOT via monkeypatch's string target. They normally
# coincide, but test_coordinator_tool_inventory's reimport probe restores
# sys.modules WITHOUT restoring the ``agent`` package attribute — so a string
# target (which resolves ``agent.<sub>`` as a package attribute) can patch a
# STALE module while the orchestrator's ``import`` reads the restored original.
# Patching the live module object dodges that pre-existing isolation hazard.
def _live(dotted: str):
    return importlib.import_module(dotted)


async def _drain(prompt="do the thing", session_id="sid-fixed",
                 autonomy_mode="propose_apply"):
    """Drain the orchestrator's async generator into a list.

    Defaults to ``autonomy_mode="propose_apply"`` so the existing
    orchestrator tests exercise today's (full-autonomy) behavior; the
    dial-specific tests pass an explicit mode.
    """
    return [
        item
        async for item in fanout.run_provision_fanout_stream(
            prompt, session_id, autonomy_mode=autonomy_mode
        )
    ]


def _events(items):
    return [it for it in items if it["type"] == "event"]


def _final_responses(items):
    return [
        it
        for it in _events(items)
        if it["event"].get("event") == "final_response"
    ]


def _result(items):
    results = [it for it in items if it["type"] == "result"]
    assert len(results) == 1, f"expected exactly one result, got {len(results)}"
    return results[0]


@pytest.fixture(autouse=True)
def _no_real_read_tools(monkeypatch):
    """Never load the real provision workload/ADK in these unit tests."""
    monkeypatch.setattr(fanout, "resolve_provision_read_tools", lambda: {})


def _two_slice_plan():
    return DecomposeResult(
        slices=[
            SliceSpec(goal="make bucket A", target_path="iac/a.tf"),
            SliceSpec(goal="make bucket B", target_path="iac/b.tf"),
        ],
        pr_title="Add buckets A and B",
        pr_body_intro="This PR adds two independent buckets.",
    )


def _two_file_author_result():
    return AuthorResult(
        files=[
            {"path": "iac/a.tf", "content": "resource a {}"},
            {"path": "iac/b.tf", "content": "resource b {}"},
        ],
        citations={"iac/a.tf": ["doc-a"], "iac/b.tf": []},
    )


def _patch_decompose(monkeypatch, *, result=None, exc=None, emits=None):
    """Patch ``agent.fanout.decompose`` with an async fake.

    If ``emits`` is given (a list of raw events), the fake calls ``event_sink``
    with each before returning ``result`` / raising ``exc``.
    """

    async def _fake(prompt, *, read_tools=None, event_sink=None):
        if emits and event_sink is not None:
            for ev in emits:
                event_sink(ev)
        if exc is not None:
            raise exc
        return result

    monkeypatch.setattr(fanout, "decompose", _fake)


def _patch_author(monkeypatch, *, result=None, exc=None, emits=None):
    """Patch ``agent.fanout.author_slices_parallel`` with an async fake.

    ``emits`` is a list of already-tagged payload dicts the engine would have
    forwarded to ``event_sink`` (each carries ``branch``/``slice_id``/etc.).
    """

    async def _fake(specs, *, read_tools=None, event_sink=None):
        if emits and event_sink is not None:
            for payload in emits:
                event_sink(dict(payload))
        if exc is not None:
            raise exc
        return result

    monkeypatch.setattr(fanout, "author_slices_parallel", _fake)


def _patch_authority(monkeypatch, target_repo="owner/repo", branch="infra/x-1-ab"):
    sentinel = IacPrAuthority(target_repo=target_repo, branch=branch)
    monkeypatch.setattr(
        _live("agent.adk_tools"), "derive_iac_pr_authority", lambda title: sentinel
    )
    return sentinel


def _patch_open_pr(monkeypatch, *, capture=None, result=None, exc=None):
    def _fake(target_repo, branch, title, body, files, *, dispatch_plan_builder=False):
        if capture is not None:
            capture["call"] = {
                "target_repo": target_repo,
                "branch": branch,
                "title": title,
                "body": body,
                "files": files,
            }
        if exc is not None:
            raise exc
        return result or {
            "status": "opened",
            "pr_number": 42,
            "pr_url": "https://github.com/owner/repo/pull/42",
            "branch": branch,
        }

    monkeypatch.setattr(_live("agent.worker_client"), "call_open_infra_pr", _fake)


# --------------------------------------------------------------------------- #
# 1. multi-slice happy path
# --------------------------------------------------------------------------- #


def test_multi_slice_happy_path_single_editor_call(monkeypatch):
    _patch_decompose(monkeypatch, result=_two_slice_plan())
    _patch_author(monkeypatch, result=_two_file_author_result())
    sentinel = _patch_authority(
        monkeypatch, target_repo="owner/repo", branch="infra/buckets-9-ab"
    )
    capture: dict = {}
    _patch_open_pr(monkeypatch, capture=capture)

    items = asyncio.run(_drain())

    # Editor called EXACTLY once with merged files + derived authority.
    call = capture["call"]
    assert call["target_repo"] == sentinel.target_repo
    assert call["branch"] == sentinel.branch
    assert call["files"] == _two_file_author_result().files
    assert call["title"] == "Add buckets A and B"

    result = _result(items)
    assert result["tool_calls"] == ["open_infra_pr"]
    assert result["session_id"] == "sid-fixed"
    assert "42" in result["reply"]
    # The exact next-steps wording is reused from open_infra_pr_tool.
    assert "C2 plan-builder" in result["reply"]
    assert "/iac-approvals/" in result["reply"]
    assert "re-bake (C6)" in result["reply"]
    # The terminal item carries the structured approval pointer so the SPA can
    # render a clickable first-authoring "Review & approve" CTA.
    assert result["iac_pr"] == {
        "pr_number": 42,
        "pr_url": "https://github.com/owner/repo/pull/42",
    }


def test_multi_slice_editor_called_without_base_kwarg(monkeypatch):
    """The convergent editor call passes NO ``base`` (call_open_infra_pr pins
    base="main" internally; passing base= would crash)."""
    _patch_decompose(monkeypatch, result=_two_slice_plan())
    _patch_author(monkeypatch, result=_two_file_author_result())
    _patch_authority(monkeypatch)

    seen: dict = {}

    def _fake(target_repo, branch, title, body, files, *args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return {"status": "opened", "pr_number": 1, "pr_url": "u", "branch": branch}

    monkeypatch.setattr(_live("agent.worker_client"), "call_open_infra_pr", _fake)

    asyncio.run(_drain())

    assert "base" not in seen["kwargs"]
    assert seen["args"] == ()  # no extra positional (no base) beyond the five


# --------------------------------------------------------------------------- #
# 2. single-slice fallback
# --------------------------------------------------------------------------- #


def test_single_slice_delegates_to_run_chat_stream(monkeypatch):
    plan = DecomposeResult(
        slices=[SliceSpec(goal="one thing", target_path="iac/one.tf")],
        pr_title="One",
        pr_body_intro="intro",
    )
    _patch_decompose(monkeypatch, result=plan)

    author_called = {"n": 0}

    async def _author_should_not_run(*a, **k):
        author_called["n"] += 1
        raise AssertionError("author_slices_parallel must not run on 1 slice")

    monkeypatch.setattr(fanout, "author_slices_parallel", _author_should_not_run)

    open_pr_called = {"n": 0}

    def _open_pr(*a, **k):
        open_pr_called["n"] += 1
        return {}

    monkeypatch.setattr(_live("agent.worker_client"), "call_open_infra_pr", _open_pr)

    delegated_to: dict = {}

    async def _fake_run_chat_stream(prompt, session_id=None, *, workload="drift", autonomy_mode="propose_apply", prior_turns=None):
        delegated_to["prompt"] = prompt
        delegated_to["session_id"] = session_id
        delegated_to["workload"] = workload
        yield {"type": "event", "event": {"event": "delegated_marker"}}
        yield {
            "type": "result",
            "reply": "from legacy path",
            "tool_calls": ["read_live_env"],
            "session_id": session_id,
        }

    monkeypatch.setattr(_live("agent.adk_agent"), "run_chat_stream", _fake_run_chat_stream)

    items = asyncio.run(_drain(prompt="single thing"))

    assert author_called["n"] == 0
    assert open_pr_called["n"] == 0
    assert delegated_to == {
        "prompt": "single thing",
        "session_id": "sid-fixed",
        "workload": "provision",
    }
    # The delegated items pass through unchanged.
    assert any(
        it["type"] == "event" and it["event"].get("event") == "delegated_marker"
        for it in items
    )
    result = _result(items)
    assert result["reply"] == "from legacy path"
    assert result["tool_calls"] == ["read_live_env"]


# --------------------------------------------------------------------------- #
# 3. non-policy decompose failure → fail open (delegate)
# --------------------------------------------------------------------------- #


def test_non_policy_decompose_failure_fails_open(monkeypatch):
    _patch_decompose(
        monkeypatch,
        exc=FanoutError(
            502, "no plan", kind=FanoutFailureKind.DECOMPOSE_NON_POLICY
        ),
    )

    open_pr_called = {"n": 0}
    monkeypatch.setattr(
        _live("agent.worker_client"),
        "call_open_infra_pr",
        lambda *a, **k: open_pr_called.__setitem__("n", open_pr_called["n"] + 1),
    )

    delegated: dict = {}

    async def _fake_run_chat_stream(prompt, session_id=None, *, workload="drift", autonomy_mode="propose_apply", prior_turns=None):
        delegated["workload"] = workload
        yield {
            "type": "result",
            "reply": "fallback reply",
            "tool_calls": [],
            "session_id": session_id,
        }

    monkeypatch.setattr(_live("agent.adk_agent"), "run_chat_stream", _fake_run_chat_stream)

    items = asyncio.run(_drain())

    assert open_pr_called["n"] == 0
    assert delegated["workload"] == "provision"
    assert _result(items)["reply"] == "fallback reply"


# --------------------------------------------------------------------------- #
# 4. policy decompose failure → fail closed, no delegation, no editor
# --------------------------------------------------------------------------- #


def test_policy_decompose_failure_fails_closed(monkeypatch):
    _patch_decompose(
        monkeypatch,
        exc=FanoutError(
            403, "iac/providers.tf is a foundation file", kind=FanoutFailureKind.POLICY
        ),
    )

    open_pr_called = {"n": 0}
    monkeypatch.setattr(
        _live("agent.worker_client"),
        "call_open_infra_pr",
        lambda *a, **k: open_pr_called.__setitem__("n", open_pr_called["n"] + 1),
    )

    chat_called = {"n": 0}

    async def _chat_should_not_run(*a, **k):
        chat_called["n"] += 1
        raise AssertionError("must not delegate on a POLICY failure")
        yield  # pragma: no cover

    monkeypatch.setattr(_live("agent.adk_agent"), "run_chat_stream", _chat_should_not_run)

    items = asyncio.run(_drain())

    assert open_pr_called["n"] == 0
    assert chat_called["n"] == 0
    result = _result(items)
    assert result["tool_calls"] == []
    assert "foundation file" in result["reply"]
    # Exactly one final_response, surfacing the violation.
    frs = _final_responses(items)
    assert len(frs) == 1


# --------------------------------------------------------------------------- #
# 5. editor WorkerClientError → surfaced, no fabricated PR
# --------------------------------------------------------------------------- #


def test_editor_worker_error_is_surfaced_no_fabricated_pr(monkeypatch):
    from agent import worker_client

    _patch_decompose(monkeypatch, result=_two_slice_plan())
    _patch_author(monkeypatch, result=_two_file_author_result())
    _patch_authority(monkeypatch)
    _patch_open_pr(
        monkeypatch,
        exc=worker_client.WorkerClientError(
            403, "forbidden by editor policy", "tofu_editor"
        ),
    )

    items = asyncio.run(_drain())

    result = _result(items)
    assert result["tool_calls"] == ["open_infra_pr"]
    assert "403" in result["reply"]
    # No fabricated PR number/url in the reply.
    assert "pull/42" not in result["reply"]
    frs = _final_responses(items)
    assert len(frs) == 1


def test_editor_malformed_200_does_not_fabricate_a_pr(monkeypatch):
    """A 200 from the editor that omits ``pr_number`` must NOT surface a
    ``PR #None`` fabricated success — it is treated as an attempted-but-failed
    editor outcome (``tool_calls == ["open_infra_pr"]``, no PR number/url)."""
    _patch_decompose(monkeypatch, result=_two_slice_plan())
    _patch_author(monkeypatch, result=_two_file_author_result())
    _patch_authority(monkeypatch)
    # Worker returns 200 but with no pr_number/pr_url (contract violation).
    _patch_open_pr(monkeypatch, result={"status": "opened"})

    items = asyncio.run(_drain())

    result = _result(items)
    assert result["tool_calls"] == ["open_infra_pr"]
    # Never a fabricated "#None" / "None" PR.
    assert "#None" not in result["reply"]
    assert "Opened infrastructure PR" not in result["reply"]
    # No approval pointer for an unconfirmed PR → no first-authoring CTA.
    assert "iac_pr" not in result
    assert len(_final_responses(items)) == 1


def test_editor_200_with_pr_number_but_no_url_does_not_fabricate_a_pr(monkeypatch):
    """A 200 that carries a ``pr_number`` but OMITS ``pr_url`` must also
    fail-closed — surfacing a success reply here would render a ``(... None)``
    URL. The call WAS made (``tool_calls == ["open_infra_pr"]``) but no PR is
    fabricated."""
    _patch_decompose(monkeypatch, result=_two_slice_plan())
    _patch_author(monkeypatch, result=_two_file_author_result())
    _patch_authority(monkeypatch)
    # Worker returns 200 with a pr_number but NO pr_url (contract violation).
    _patch_open_pr(monkeypatch, result={"status": "opened", "pr_number": 7})

    items = asyncio.run(_drain())

    result = _result(items)
    assert result["tool_calls"] == ["open_infra_pr"]
    # Not a fabricated success — no "Opened infrastructure PR", no "None" url.
    assert "Opened infrastructure PR" not in result["reply"]
    assert "None" not in result["reply"]
    assert "iac_pr" not in result
    assert len(_final_responses(items)) == 1


def test_editor_200_with_bool_pr_number_is_not_confirmed(monkeypatch):
    """A 200 whose ``pr_number`` is a bool (``True``/``False`` subclass ``int``)
    is NOT a valid PR — the success gate is aligned with ``iac_pr_pointer`` so it
    fails-closed (no fabricated ``PR #True``) and carries no approval pointer."""
    _patch_decompose(monkeypatch, result=_two_slice_plan())
    _patch_author(monkeypatch, result=_two_file_author_result())
    _patch_authority(monkeypatch)
    _patch_open_pr(
        monkeypatch, result={"status": "opened", "pr_number": True, "pr_url": "u"}
    )

    items = asyncio.run(_drain())

    result = _result(items)
    assert result["tool_calls"] == ["open_infra_pr"]
    assert "Opened infrastructure PR" not in result["reply"]
    assert "iac_pr" not in result
    assert len(_final_responses(items)) == 1


def test_editor_200_with_empty_pr_url_is_not_confirmed(monkeypatch):
    """A 200 with an empty-string ``pr_url`` is malformed — it fails-closed (no
    fabricated ``(...)`` empty URL) and carries no approval pointer."""
    _patch_decompose(monkeypatch, result=_two_slice_plan())
    _patch_author(monkeypatch, result=_two_file_author_result())
    _patch_authority(monkeypatch)
    _patch_open_pr(
        monkeypatch, result={"status": "opened", "pr_number": 7, "pr_url": ""}
    )

    items = asyncio.run(_drain())

    result = _result(items)
    assert result["tool_calls"] == ["open_infra_pr"]
    assert "Opened infrastructure PR" not in result["reply"]
    assert "iac_pr" not in result
    assert len(_final_responses(items)) == 1


# --------------------------------------------------------------------------- #
# 6. exactly ONE final_response, emitted AFTER the editor outcome
# --------------------------------------------------------------------------- #


def test_exactly_one_final_response_after_all_events(monkeypatch):
    # decompose emits one real event into the buffer.
    decompose_ev = StubEvent(
        [StubPart(text="planning", thought=True)], partial=False
    )
    _patch_decompose(monkeypatch, result=_two_slice_plan(), emits=[decompose_ev])
    # author emits two tagged payloads.
    author_emits = [
        {"event": "llm_thought", "thought": "a", "branch": "driftscribe_fanout.x",
         "slice_id": 0, "target_path": "iac/a.tf"},
        {"event": "tool_result", "name": "submit_slice_file", "branch": "y",
         "slice_id": 1, "target_path": "iac/b.tf"},
    ]
    _patch_author(
        monkeypatch, result=_two_file_author_result(), emits=author_emits
    )
    _patch_authority(monkeypatch)
    _patch_open_pr(monkeypatch)

    items = asyncio.run(_drain())

    frs = _final_responses(items)
    assert len(frs) == 1
    fr_seq = frs[0]["event"]["seq"]
    other_seqs = [
        it["event"]["seq"]
        for it in _events(items)
        if it["event"].get("event") != "final_response"
    ]
    assert other_seqs, "expected decompose/author events before the final"
    assert fr_seq > max(other_seqs)


# --------------------------------------------------------------------------- #
# 7. seq monotonic + tag fields survive
# --------------------------------------------------------------------------- #


def test_seq_monotonic_and_tags_survive(monkeypatch):
    decompose_ev = StubEvent(
        [StubPart(text="planning", thought=True)], partial=False
    )
    _patch_decompose(monkeypatch, result=_two_slice_plan(), emits=[decompose_ev])
    author_emits = [
        {"event": "llm_thought", "thought": "a", "branch": "b.slice0",
         "slice_id": 0, "target_path": "iac/a.tf"},
        {"event": "tool_call", "name": "submit_slice_file", "branch": "b.slice1",
         "slice_id": 1, "target_path": "iac/b.tf"},
        {"event": "tool_result", "name": "submit_slice_file", "branch": "b.slice1",
         "slice_id": 1, "target_path": "iac/b.tf"},
    ]
    _patch_author(
        monkeypatch, result=_two_file_author_result(), emits=author_emits
    )
    _patch_authority(monkeypatch)
    _patch_open_pr(monkeypatch)

    items = asyncio.run(_drain())

    seqs = [it["event"]["seq"] for it in _events(items)]
    # strictly increasing, contiguous, starting at 1.
    assert seqs == list(range(1, len(seqs) + 1))

    # decompose event carries phase=decompose
    decompose_events = [
        it["event"] for it in _events(items)
        if it["event"].get("phase") == "decompose"
    ]
    assert decompose_events, "decompose events should be tagged phase=decompose"

    # author events keep their slice/branch tags into the yielded dicts.
    author_events = [
        it["event"] for it in _events(items)
        if it["event"].get("slice_id") is not None
    ]
    assert any(e["target_path"] == "iac/a.tf" and e["branch"] == "b.slice0"
               for e in author_events)
    assert any(e["target_path"] == "iac/b.tf" and e["slice_id"] == 1
               for e in author_events)


# --------------------------------------------------------------------------- #
# 8. shared-helper authority (no independent derivation)
# --------------------------------------------------------------------------- #


def test_orchestrator_uses_shared_authority_helper(monkeypatch):
    _patch_decompose(monkeypatch, result=_two_slice_plan())
    _patch_author(monkeypatch, result=_two_file_author_result())
    _patch_authority(
        monkeypatch,
        target_repo="owner/sentinel-repo",
        branch="infra/sentinel-branch",
    )
    capture: dict = {}
    _patch_open_pr(monkeypatch, capture=capture)

    asyncio.run(_drain())

    assert capture["call"]["target_repo"] == "owner/sentinel-repo"
    assert capture["call"]["branch"] == "infra/sentinel-branch"


# --------------------------------------------------------------------------- #
# 9. authoring FanoutError → fail closed, editor not called
# --------------------------------------------------------------------------- #


def test_authoring_failure_fails_closed_no_editor(monkeypatch):
    _patch_decompose(monkeypatch, result=_two_slice_plan())
    _patch_author(
        monkeypatch,
        exc=FanoutError(
            502, "slice iac/a.tf produced no file", kind=FanoutFailureKind.AUTHORING
        ),
    )
    _patch_authority(monkeypatch)

    open_pr_called = {"n": 0}
    monkeypatch.setattr(
        _live("agent.worker_client"),
        "call_open_infra_pr",
        lambda *a, **k: open_pr_called.__setitem__("n", open_pr_called["n"] + 1),
    )

    items = asyncio.run(_drain())

    assert open_pr_called["n"] == 0
    result = _result(items)
    assert result["tool_calls"] == []
    assert "produced no file" in result["reply"]
    assert len(_final_responses(items)) == 1


# --------------------------------------------------------------------------- #
# Body composition + title/body policy
# --------------------------------------------------------------------------- #


def test_body_policy_overflow_fails_closed_no_editor(monkeypatch):
    """An over-length body trips validate_title_body BEFORE any editor call."""
    plan = DecomposeResult(
        slices=[
            SliceSpec(goal="g1", target_path="iac/a.tf"),
            SliceSpec(goal="g2", target_path="iac/b.tf"),
        ],
        pr_title="ok title",
        pr_body_intro="x" * 30_000,  # blows past MAX_BODY (20_000)
    )
    _patch_decompose(monkeypatch, result=plan)
    _patch_author(monkeypatch, result=_two_file_author_result())
    _patch_authority(monkeypatch)

    open_pr_called = {"n": 0}
    monkeypatch.setattr(
        _live("agent.worker_client"),
        "call_open_infra_pr",
        lambda *a, **k: open_pr_called.__setitem__("n", open_pr_called["n"] + 1),
    )

    items = asyncio.run(_drain())

    assert open_pr_called["n"] == 0
    result = _result(items)
    assert result["tool_calls"] == []
    assert len(_final_responses(items)) == 1


def test_compose_fanout_pr_body_includes_intro_paths_and_citations():
    plan = _two_slice_plan()
    author_result = _two_file_author_result()

    body = fanout._compose_fanout_pr_body(plan, author_result)

    assert plan.pr_body_intro in body
    assert "`iac/a.tf`" in body
    assert "`iac/b.tf`" in body
    assert "make bucket A" in body
    # iac/a.tf has a citation; it should appear; iac/b.tf has none.
    assert "doc-a" in body


def test_compose_success_reply_mentions_pr_and_paths():
    plan = _two_slice_plan()
    author_result = _two_file_author_result()
    worker_result = {
        "status": "opened",
        "pr_number": 99,
        "pr_url": "https://github.com/owner/repo/pull/99",
    }

    reply = fanout._compose_success_reply(worker_result, plan, author_result)

    assert "99" in reply
    assert "iac/a.tf" in reply
    assert "iac/b.tf" in reply
    assert "C2 plan-builder" in reply


# --------------------------------------------------------------------------- #
# Early generator close mid-author → author task is cancelled + reaped (no
# orphaned task, no unretrieved-exception warning).
# --------------------------------------------------------------------------- #


def test_early_generator_close_reaps_author_task(monkeypatch):
    """If the consumer closes the generator after the first author event, the
    orchestrator's finally cancels AND reaps the author task — the task ends up
    done (cancelled), never orphaned."""
    _patch_decompose(monkeypatch, result=_two_slice_plan())
    _patch_authority(monkeypatch)
    _patch_open_pr(monkeypatch)

    captured: dict = {}

    async def _slow_author(specs, *, read_tools=None, event_sink=None):
        # Record the task that runs us so the test can assert on its exact
        # identity, emit one event so the consumer gets something, then block
        # forever (simulating in-flight authoring) until cancelled.
        captured["task"] = asyncio.current_task()
        event_sink({"event": "llm_thought", "thought": "x", "branch": "b",
                    "slice_id": 0, "target_path": "iac/a.tf"})
        await asyncio.sleep(3600)
        return _two_file_author_result()  # pragma: no cover - never reached

    monkeypatch.setattr(fanout, "author_slices_parallel", _slow_author)

    async def _scenario():
        gen = fanout.run_provision_fanout_stream(
            "prompt", "sid", autonomy_mode="propose_apply"
        )
        # Pull the first author event, then close the generator mid-author and
        # let the finally cancel + reap the in-flight author task.
        first = await gen.__anext__()
        assert first["type"] == "event"
        await gen.aclose()
        # Give the cancelled task a turn to finish being reaped.
        await asyncio.sleep(0)

    asyncio.run(_scenario())
    # The EXACT author task was cancelled + reaped — never orphaned.
    author_task = captured["task"]
    assert author_task.done()
    assert author_task.cancelled()


# --------------------------------------------------------------------------- #
# Pending-approval notifications (Wave 2 item 7) — fanout direct path
# --------------------------------------------------------------------------- #


def test_multi_slice_confirmed_pr_notifies_once(monkeypatch):
    """A confirmed PR through the direct call_open_infra_pr path triggers
    exactly ONE notifier call (channel=approval, severity=medium), and the
    result item still carries iac_pr unchanged."""
    _patch_decompose(monkeypatch, result=_two_slice_plan())
    _patch_author(monkeypatch, result=_two_file_author_result())
    _patch_authority(monkeypatch, target_repo="owner/repo", branch="infra/x-1-ab")
    _patch_open_pr(monkeypatch)

    notifier_calls = []

    def _fake_call(worker, payload):
        if worker == "notifier":
            notifier_calls.append(payload)
            return {"status": "sent"}
        return {}

    monkeypatch.setattr(_live("agent.worker_client"), "call", _fake_call)

    items = asyncio.run(_drain())

    assert len(notifier_calls) == 1, f"expected 1 notifier call, got {len(notifier_calls)}"
    n = notifier_calls[0]
    assert n["channel"] == "approval"
    assert n["severity"] == "medium"
    assert "/iac-approvals/42" in n["body"]
    # The fanout site must pass the plan's REAL title + the worker's pr_url
    # (closes the pass-empty-title-at-the-fanout-site hole).
    assert "Add buckets A and B" in n["body"]
    assert "https://github.com/owner/repo/pull/42" in n["body"]

    result = _result(items)
    assert result["iac_pr"] == {
        "pr_number": 42,
        "pr_url": "https://github.com/owner/repo/pull/42",
    }


def test_multi_slice_malformed_pr_result_no_notify(monkeypatch):
    """A malformed/unconfirmed worker response (pointer None) → ZERO notifier
    calls; the existing fail-closed reply path is unchanged."""
    _patch_decompose(monkeypatch, result=_two_slice_plan())
    _patch_author(monkeypatch, result=_two_file_author_result())
    _patch_authority(monkeypatch)
    # Worker returns a result with no pr_number/pr_url — unconfirmed
    _patch_open_pr(monkeypatch, result={"status": "opened"})

    notifier_calls = []

    def _fake_call(worker, payload):
        if worker == "notifier":
            notifier_calls.append(payload)
            return {"status": "sent"}
        return {}

    monkeypatch.setattr(_live("agent.worker_client"), "call", _fake_call)

    items = asyncio.run(_drain())

    assert notifier_calls == []
    result = _result(items)
    assert "iac_pr" not in result


def test_multi_slice_editor_worker_error_no_notify(monkeypatch):
    """Editor worker error → ZERO notifier calls."""
    from agent import worker_client

    _patch_decompose(monkeypatch, result=_two_slice_plan())
    _patch_author(monkeypatch, result=_two_file_author_result())
    _patch_authority(monkeypatch)
    _patch_open_pr(
        monkeypatch,
        exc=worker_client.WorkerClientError(403, "forbidden", "tofu_editor"),
    )

    notifier_calls = []

    def _fake_call(worker, payload):
        if worker == "notifier":
            notifier_calls.append(payload)
            return {"status": "sent"}
        return {}

    monkeypatch.setattr(_live("agent.worker_client"), "call", _fake_call)

    asyncio.run(_drain())

    assert notifier_calls == []


def test_multi_slice_notifier_failure_suppressed_stream_completes(monkeypatch):
    """If the notifier itself raises, the exception is suppressed and the
    stream completes normally (result carries iac_pr)."""
    from agent import worker_client as wc

    _patch_decompose(monkeypatch, result=_two_slice_plan())
    _patch_author(monkeypatch, result=_two_file_author_result())
    _patch_authority(monkeypatch, target_repo="owner/repo", branch="infra/x-1-ab")
    _patch_open_pr(monkeypatch)

    def _fake_call(worker, payload):
        if worker == "notifier":
            raise wc.WorkerClientError(503, "down", "notifier")
        return {}

    monkeypatch.setattr(_live("agent.worker_client"), "call", _fake_call)

    items = asyncio.run(_drain())

    result = _result(items)
    # Stream completes normally — iac_pr still present
    assert result["iac_pr"] == {
        "pr_number": 42,
        "pr_url": "https://github.com/owner/repo/pull/42",
    }


# --------------------------------------------------------------------------- #
# Autonomy dial (ClickOps item 11) — fan-out gating
# --------------------------------------------------------------------------- #


def test_provision_fanout_observe_delegates_without_editor_call(monkeypatch):
    """Observe: a multi-slice prompt delegates the WHOLE stream to the
    single-agent run_chat_stream at entry; decompose/author/editor never run."""
    decompose_called = {"n": 0}

    async def _decompose_should_not_run(*a, **k):
        decompose_called["n"] += 1
        raise AssertionError("decompose must not run in Observe (entry-delegated)")

    monkeypatch.setattr(fanout, "decompose", _decompose_should_not_run)

    open_pr_called = {"n": 0}
    monkeypatch.setattr(
        _live("agent.worker_client"),
        "call_open_infra_pr",
        lambda *a, **k: open_pr_called.__setitem__("n", open_pr_called["n"] + 1),
    )

    delegated: dict = {}

    async def _fake_run_chat_stream(prompt, session_id=None, *, workload="drift",
                                    autonomy_mode="propose_apply", prior_turns=None):
        delegated["workload"] = workload
        delegated["autonomy_mode"] = autonomy_mode
        yield {
            "type": "result",
            "reply": "observe single-agent reply",
            "tool_calls": [],
            "session_id": session_id,
        }

    monkeypatch.setattr(_live("agent.adk_agent"), "run_chat_stream", _fake_run_chat_stream)

    items = asyncio.run(_drain(autonomy_mode="observe"))

    assert decompose_called["n"] == 0
    assert open_pr_called["n"] == 0
    assert delegated == {"workload": "provision", "autonomy_mode": "observe"}
    assert _result(items)["reply"] == "observe single-agent reply"


def test_provision_fanout_precall_guard_fails_closed(monkeypatch):
    """Drive the committed (N>=2) branch directly with autonomy_mode='observe'
    so the entry delegation is bypassed (monkeypatch the mode check at the
    seam): the pre-editor-call guard refuses; call_open_infra_pr not called."""
    _patch_decompose(monkeypatch, result=_two_slice_plan())
    _patch_author(monkeypatch, result=_two_file_author_result())
    _patch_authority(monkeypatch)

    open_pr_called = {"n": 0}
    monkeypatch.setattr(
        _live("agent.worker_client"),
        "call_open_infra_pr",
        lambda *a, **k: open_pr_called.__setitem__("n", open_pr_called["n"] + 1),
    )

    # Bypass the entry delegation: make the entry mode-check think it's
    # propose (so it proceeds into the committed branch) but leave the
    # pre-call guard's mode_allows seeing 'observe'. We do that by patching
    # mode_allows on the fanout module to return False for the propose tier
    # while passing autonomy_mode='propose' to skip the entry observe branch.
    monkeypatch.setattr(fanout, "mode_allows", lambda mode, tier: False)

    items = asyncio.run(_drain(autonomy_mode="propose"))

    assert open_pr_called["n"] == 0
    result = _result(items)
    assert "was not opened" in result["reply"]
    assert result["tool_calls"] == []


def test_provision_fanout_single_slice_passes_mode_through(monkeypatch):
    """A single-slice plan in propose delegates to run_chat_stream WITH the
    same autonomy_mode."""
    plan = DecomposeResult(
        slices=[SliceSpec(goal="one thing", target_path="iac/one.tf")],
        pr_title="One",
        pr_body_intro="intro",
    )
    _patch_decompose(monkeypatch, result=plan)
    monkeypatch.setattr(
        _live("agent.worker_client"),
        "call_open_infra_pr",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no editor on 1 slice")),
    )

    delegated: dict = {}

    async def _fake_run_chat_stream(prompt, session_id=None, *, workload="drift",
                                    autonomy_mode="propose_apply", prior_turns=None):
        delegated["autonomy_mode"] = autonomy_mode
        yield {
            "type": "result",
            "reply": "delegated",
            "tool_calls": [],
            "session_id": session_id,
        }

    monkeypatch.setattr(_live("agent.adk_agent"), "run_chat_stream", _fake_run_chat_stream)

    asyncio.run(_drain(autonomy_mode="propose"))
    assert delegated["autonomy_mode"] == "propose"


def test_provision_fanout_propose_opens_pr(monkeypatch):
    """propose: the committed branch opens the PR (the dial must not over-block
    proposals — open_infra_pr is propose-tier)."""
    _patch_decompose(monkeypatch, result=_two_slice_plan())
    _patch_author(monkeypatch, result=_two_file_author_result())
    _patch_authority(monkeypatch, target_repo="owner/repo", branch="infra/x-1-ab")
    capture: dict = {}
    _patch_open_pr(monkeypatch, capture=capture)

    items = asyncio.run(_drain(autonomy_mode="propose"))

    assert "call" in capture  # editor WAS called
    result = _result(items)
    assert result["tool_calls"] == ["open_infra_pr"]
