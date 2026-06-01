"""Phase D5-5: ``author_slices_parallel()`` — N slice-author sub-agents run
IN PARALLEL via ADK ``ParallelAgent``, then a DETERMINISTIC, FAIL-CLOSED
barrier collects each slice's file-write and merges them.

These tests MOCK the run — no Gemini. They reuse the ``agent.fanout.Runner``
patch seam (same as ``tests/unit/test_fanout_decompose.py``), but the root
agent here is a ``ParallelAgent`` whose ``sub_agents`` are the per-slice
author agents. The fake ``Runner.run_async`` simulates the N sub-agents by
iterating ``root.sub_agents``, finding each one's pinned ``submit_slice_file``
closure (the callable in ``.tools`` whose ``__name__`` is ``submit_slice_file``)
and calling it with a per-agent chosen content — exactly as the live model
would via the function-call protocol — then yielding one or more stub events,
some carrying a ``.branch`` so the per-slice event-tagging path is exercised.

What is pinned here (load-bearing):

- N=3 disjoint slices all submit → ``AuthorResult.files`` has 3 writes IN
  SLICE ORDER with matching content, and citations mapped per path.
- a slice whose agent never submits (empty sink) → ``FanoutError`` with
  ``kind is AUTHORING`` and NO result returned.
- a slice that submits empty/whitespace content → ``FanoutError`` AUTHORING.
- the run raises a generic ``Exception`` (sub-agent crash / TaskGroup raise)
  → ``FanoutError`` AUTHORING, sinks discarded, no partial result.
- the run raises ``asyncio.CancelledError`` → it PROPAGATES unchanged (outer
  request cancellation is never converted to a FanoutError).
- post-merge bounds: total bytes over the lib ceiling → ``FanoutError``
  AUTHORING (translated from ``validate_file_writes``'s ``EditorPolicyError``).
- ``event_sink`` receives tagged payloads (branch / target_path / slice_id),
  and NO forwarded payload is a ``final_response`` (per-slice finals suppressed).
- a ``ParallelAgent`` is actually constructed with N ``sub_agents`` (so the
  concurrency primitive is really used) — captured via the fake Runner.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from agent import fanout
from agent.fanout import (
    AuthorResult,
    FanoutError,
    FanoutFailureKind,
    SliceSpec,
    author_slices_parallel,
)
from tests.unit._adk_stubs import StubEvent as _Ev, StubPart as _P


def _find_submit_slice_file(agent):
    """Pull the ``submit_slice_file`` closure out of a slice agent's tools.

    ``build_slice_author_agent`` wires the agent's tools as
    ``[*read_tool_values, submit_slice_file]``; the submit closure's
    ``__name__`` is ``submit_slice_file``. The stub model 'calls the tool' by
    grabbing that very closure (bound to this slice's sink) and invoking it,
    exactly as the live model would via the function-call protocol."""
    return next(
        t for t in agent.tools if getattr(t, "__name__", "") == "submit_slice_file"
    )


def _fake_parallel_runner(content_by_index, *, extra_events=None, raise_exc=None):
    """Build a fake ``Runner`` whose ``run_async`` drives a ``ParallelAgent``.

    ``content_by_index`` maps a sub-agent ordinal (0-based, in
    ``root.sub_agents`` order) → the content that sub-agent's
    ``submit_slice_file`` is called with. An index absent from the map (or a
    ``None`` value) means that sub-agent NEVER submits (empty sink).

    ``extra_events`` is an optional list of stub events the run yields AFTER
    simulating the submits (e.g. branch-tagged thought/tool events to exercise
    the per-slice tagging path). ``raise_exc``, if set, is raised by
    ``run_async`` instead of yielding (simulates a sub-agent crash → TaskGroup
    raise, or an outer ``asyncio.CancelledError``).

    Returns ``(make_runner, captured)``; ``captured["root"]`` is the agent the
    Runner was constructed with (the ``ParallelAgent``), so the test can assert
    it carries N ``sub_agents``.
    """
    captured: dict = {}

    class _FakeRunner:
        def __init__(self, *, agent, app_name, session_service):
            captured["root"] = agent
            captured["app_name"] = app_name
            captured["session_service"] = session_service

        async def run_async(self, *, user_id, session_id, new_message):
            captured["user_id"] = user_id
            captured["session_id"] = session_id
            if raise_exc is not None:
                raise raise_exc
            root = captured["root"]
            for i, sub in enumerate(root.sub_agents):
                content = content_by_index.get(i)
                if content is None:
                    continue
                submit = _find_submit_slice_file(sub)
                submit(content=content, citations=[f"doc-{i}"])
            for ev in extra_events or []:
                yield ev
            # A final natural-language event (suppressed by the barrier — it
            # must NOT be collected into any reply, and must NOT be tagged as a
            # final_response payload).
            yield _Ev([_P(text="all done")], partial=False, final=True)

    return _FakeRunner, captured


def _specs(n):
    return [
        SliceSpec(goal=f"build thing {i}", target_path=f"iac/slice_{i}.tf")
        for i in range(n)
    ]


# Read tools are irrelevant to these tests (the stub model never invokes them);
# pass an empty mapping so author_slices_parallel does NOT call the real
# provision resolver (which needs worker URL env).
_NO_READ_TOOLS: dict = {}


# --------------------------------------------------------------------------- #
# Happy path: N disjoint slices all submit
# --------------------------------------------------------------------------- #


async def test_three_slices_all_submit_returns_writes_in_order():
    specs = _specs(3)
    make_runner, captured = _fake_parallel_runner(
        {0: "content-zero\n", 1: "content-one\n", 2: "content-two\n"}
    )
    with patch.object(fanout, "Runner", make_runner):
        result = await author_slices_parallel(specs, read_tools=_NO_READ_TOOLS)

    assert isinstance(result, AuthorResult)
    assert [w["path"] for w in result.files] == [
        "iac/slice_0.tf",
        "iac/slice_1.tf",
        "iac/slice_2.tf",
    ]
    assert [w["content"] for w in result.files] == [
        "content-zero\n",
        "content-one\n",
        "content-two\n",
    ]
    # Citations are mapped per target_path.
    assert result.citations == {
        "iac/slice_0.tf": ["doc-0"],
        "iac/slice_1.tf": ["doc-1"],
        "iac/slice_2.tf": ["doc-2"],
    }


async def test_parallel_agent_constructed_with_n_sub_agents():
    """The concurrency primitive is REALLY used: the root agent the Runner
    gets is a ``ParallelAgent`` with one sub-agent per slice, in slice order."""
    from google.adk.agents import ParallelAgent

    specs = _specs(3)
    make_runner, captured = _fake_parallel_runner(
        {0: "a\n", 1: "b\n", 2: "c\n"}
    )
    with patch.object(fanout, "Runner", make_runner):
        await author_slices_parallel(specs, read_tools=_NO_READ_TOOLS)

    root = captured["root"]
    assert isinstance(root, ParallelAgent)
    assert len(root.sub_agents) == 3
    # Sub-agent names are unique-by-construction: a "<slice_index>_" prefix
    # ahead of the slugged target_path, in slice order.
    assert root.sub_agents[0].name == "driftscribe_slice_0_iac_slice_0_tf"
    assert root.sub_agents[2].name == "driftscribe_slice_2_iac_slice_2_tf"


# --------------------------------------------------------------------------- #
# Fail-closed: a slice never submits / submits empty content
# --------------------------------------------------------------------------- #


async def test_slice_never_submits_is_authoring_failure():
    specs = _specs(3)
    # Slice index 1 never submits (absent from the map).
    make_runner, _ = _fake_parallel_runner({0: "a\n", 2: "c\n"})
    with patch.object(fanout, "Runner", make_runner):
        with pytest.raises(FanoutError) as ei:
            await author_slices_parallel(specs, read_tools=_NO_READ_TOOLS)
    assert ei.value.kind is FanoutFailureKind.AUTHORING
    assert ei.value.status == 502
    # The offending slice's path is named so the operator can see which failed.
    assert "iac/slice_1.tf" in ei.value.detail


async def test_slice_submits_whitespace_content_is_authoring_failure():
    specs = _specs(2)
    make_runner, _ = _fake_parallel_runner({0: "a\n", 1: "   \n\t  "})
    with patch.object(fanout, "Runner", make_runner):
        with pytest.raises(FanoutError) as ei:
            await author_slices_parallel(specs, read_tools=_NO_READ_TOOLS)
    assert ei.value.kind is FanoutFailureKind.AUTHORING


# --------------------------------------------------------------------------- #
# Fail-closed: the run itself raises
# --------------------------------------------------------------------------- #


async def test_run_raises_generic_exception_is_authoring_failure_no_partial():
    """A sub-agent crash makes the TaskGroup cancel siblings and raise through.
    The barrier must DISCARD all sink writes and surface AUTHORING — never a
    partial AuthorResult."""
    specs = _specs(3)
    make_runner, _ = _fake_parallel_runner(
        {}, raise_exc=RuntimeError("sub-agent boom")
    )
    with patch.object(fanout, "Runner", make_runner):
        with pytest.raises(FanoutError) as ei:
            await author_slices_parallel(specs, read_tools=_NO_READ_TOOLS)
    assert ei.value.kind is FanoutFailureKind.AUTHORING
    assert ei.value.status == 502
    assert "sub-agent boom" in ei.value.detail


async def test_cancelled_error_propagates_unchanged():
    """An outer ``asyncio.CancelledError`` (request cancellation) must propagate
    AS-IS — never be converted into a FanoutError. This is the load-bearing
    re-raise-before-broad-except contract."""
    specs = _specs(2)
    make_runner, _ = _fake_parallel_runner(
        {}, raise_exc=asyncio.CancelledError()
    )
    with patch.object(fanout, "Runner", make_runner):
        with pytest.raises(asyncio.CancelledError):
            await author_slices_parallel(specs, read_tools=_NO_READ_TOOLS)


# --------------------------------------------------------------------------- #
# Fail-closed: post-merge byte bounds (translated validate_file_writes)
# --------------------------------------------------------------------------- #


async def test_post_merge_total_bytes_over_bound_is_authoring_failure(monkeypatch):
    """Even when every slice submits non-empty content, the post-merge barrier
    runs ``validate_file_writes`` (disjoint paths + byte bounds). Exceeding the
    aggregate ceiling raises ``EditorPolicyError``, which the barrier translates
    into a FanoutError(AUTHORING) — the library error must NEVER leak."""
    # Shrink the lib's aggregate ceiling so two small files trip it. Patch on
    # the iac_editor_policy module the barrier calls through.
    import driftscribe_lib.iac_editor_policy as policy
    monkeypatch.setattr(policy, "MAX_TOTAL_BYTES", 10)

    specs = _specs(2)
    make_runner, _ = _fake_parallel_runner(
        {0: "aaaaaaaa\n", 1: "bbbbbbbb\n"}  # > 10 bytes combined
    )
    with patch.object(fanout, "Runner", make_runner):
        with pytest.raises(FanoutError) as ei:
            await author_slices_parallel(specs, read_tools=_NO_READ_TOOLS)
    assert ei.value.kind is FanoutFailureKind.AUTHORING
    # The lib's reason is carried through; its status is preserved.
    assert "total payload too large" in ei.value.detail
    assert ei.value.status == 422


# --------------------------------------------------------------------------- #
# event_sink: tagged payloads + per-slice finals suppressed
# --------------------------------------------------------------------------- #


async def test_event_sink_receives_tagged_payloads_no_final_response():
    specs = _specs(2)
    # A branch-tagged thought event from slice 0's branch, plus a tool_call
    # event from slice 1's branch. The branch is "<parallel>.<sub_agent.name>".
    branch0 = f"driftscribe_fanout.driftscribe_slice_0_{_slug('iac/slice_0.tf')}"
    branch1 = f"driftscribe_fanout.driftscribe_slice_1_{_slug('iac/slice_1.tf')}"
    thought_ev = _branch_event(
        [_P(text="thinking about slice 0", thought=True)], branch0
    )
    from types import SimpleNamespace

    tool_ev = _branch_event(
        [_P(function_call=SimpleNamespace(name="read_live_env", args={}))],
        branch1,
    )
    make_runner, _ = _fake_parallel_runner(
        {0: "a\n", 1: "b\n"}, extra_events=[thought_ev, tool_ev]
    )

    seen: list = []
    with patch.object(fanout, "Runner", make_runner):
        await author_slices_parallel(
            specs, read_tools=_NO_READ_TOOLS, event_sink=seen.append
        )

    # No forwarded payload is a final_response (per-slice finals suppressed).
    assert all(p.get("event") != "final_response" for p in seen)
    # At least one forwarded payload carries the slice tag (branch +
    # target_path + slice_id), and the mapping is correct.
    thought_payloads = [p for p in seen if p.get("event") == "llm_thought"]
    assert thought_payloads, "expected a tagged llm_thought payload"
    tp = thought_payloads[0]
    assert tp["branch"] == branch0
    assert tp["target_path"] == "iac/slice_0.tf"
    assert tp["slice_id"] == 0

    tool_payloads = [p for p in seen if p.get("event") == "tool_call"]
    assert tool_payloads, "expected a tagged tool_call payload"
    tcp = tool_payloads[0]
    assert tcp["branch"] == branch1
    assert tcp["target_path"] == "iac/slice_1.tf"
    assert tcp["slice_id"] == 1


# --------------------------------------------------------------------------- #
# Regression: slug-colliding DISJOINT paths must NOT corrupt branch tagging
# --------------------------------------------------------------------------- #


async def test_slug_colliding_disjoint_paths_tag_to_distinct_slices():
    """Two VALID, DISJOINT target paths that SLUG to the same string
    (``iac/foo-bar.tf`` and ``iac/foo_bar.tf`` both slug to ``iac_foo_bar_tf``)
    must still map back to their OWN slice.

    Before the idx-prefix fix the two sub-agents shared the identical ADK name
    ``driftscribe_slice_iac_foo_bar_tf`` → they collapsed onto the SAME
    ParallelAgent branch AND ``name_to_slice`` overwrote slice 0 with slice 1,
    so an event from slice 0's branch got mis-tagged as ``slice_id=1`` /
    ``target_path="iac/foo_bar.tf"``. With unique-by-construction names each
    branch's trailing sub-agent segment resolves to exactly one slice.
    """
    specs = [
        SliceSpec(goal="bucket dash", target_path="iac/foo-bar.tf"),
        SliceSpec(goal="bucket underscore", target_path="iac/foo_bar.tf"),
    ]

    captured: dict = {}

    class _FakeRunner:
        def __init__(self, *, agent, app_name, session_service):
            captured["root"] = agent

        async def run_async(self, *, user_id, session_id, new_message):
            root = captured["root"]
            # Submit each slice's file via its own pinned closure.
            for i, sub in enumerate(root.sub_agents):
                _find_submit_slice_file(sub)(content=f"resource s{i} {{}}\n")
            # Emit ONE branch-tagged thought event per sub-agent, with the
            # branch built from that sub-agent's REAL name (the same shape ADK
            # stamps: "<parallel.name>.<sub_agent.name>"). With colliding names
            # both branches would be identical; with the fix they differ.
            for sub in root.sub_agents:
                branch = f"{root.name}.{sub.name}"
                yield _branch_event(
                    [_P(text=f"thinking on {sub.name}", thought=True)], branch
                )
            yield _Ev([_P(text="all done")], partial=False, final=True)

    seen: list = []
    with patch.object(fanout, "Runner", _FakeRunner):
        await author_slices_parallel(
            specs, read_tools=_NO_READ_TOOLS, event_sink=seen.append
        )

    root = captured["root"]
    # The two sub-agent names must be distinct (no collision by construction).
    assert root.sub_agents[0].name != root.sub_agents[1].name

    thoughts = [p for p in seen if p.get("event") == "llm_thought"]
    # One tagged thought per slice, each resolving to its OWN slice.
    by_slice = {p["slice_id"]: p for p in thoughts}
    assert set(by_slice) == {0, 1}, (
        f"expected events tagged slice_id 0 and 1, got {sorted(by_slice)}"
    )
    assert by_slice[0]["target_path"] == "iac/foo-bar.tf"
    assert by_slice[0]["branch"].endswith(root.sub_agents[0].name)
    assert by_slice[1]["target_path"] == "iac/foo_bar.tf"
    assert by_slice[1]["branch"].endswith(root.sub_agents[1].name)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _slug(target_path):
    from agent.fanout import _slug_target_path

    return _slug_target_path(target_path)


def _branch_event(parts, branch):
    """A StubEvent carrying a ``.branch`` attr (the per-sub-agent isolation
    branch ADK stamps on each event: ``<parallel.name>.<sub_agent.name>``)."""
    ev = _Ev(parts, partial=False, final=False)
    ev.branch = branch
    return ev
