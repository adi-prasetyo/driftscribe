"""Phase D5-4: the ``decompose()`` structured plan agent + typed failures.

``decompose()`` is the ONE structured LLM call that runs BEFORE the parallel
authoring: it turns the operator's prompt into a validated plan — a list of
INDEPENDENT one-file slices plus an overall PR title/intro. It is the stage
that decides whether a request fans out into 2+ independent ``iac/`` slices
or stays a single coupled change (which the caller routes to the legacy
single-agent path).

These tests MOCK the ADK run — they never hit Gemini. The mock seam mirrors
``tests/unit/test_run_chat_stream.py``: patch ``Runner`` on the module under
test (:mod:`agent.fanout`) and set ``runner_cls.return_value.run_async`` to a
stub async generator. The stub simulates the model by invoking the agent's
own ``submit_plan`` tool (captured from the ``Runner(agent=...)`` constructor
call) with a chosen payload, then yielding a final event. This exercises the
real ``make_submit_plan`` sink-recording + ``decompose`` parse/validate path
while keeping the LLM out.

Failure typing is load-bearing and pinned here:

- model produces a usable plan with 2+ independent slices → ``DecomposeResult``
  with the ``SliceSpec``s + title/intro; ``validate_slice_specs`` was applied.
- model produces a colliding-path / foundation-path plan → ``FanoutError``
  with ``kind is FanoutFailureKind.POLICY`` (orchestrator fails CLOSED).
- model produces a single 1-slice plan → ``DecomposeResult`` with
  ``len(slices) == 1`` and NO exception (the CALLER decides to fall back).
- model never calls ``submit_plan`` (empty sink) → ``FanoutError`` with
  ``kind is FanoutFailureKind.DECOMPOSE_NON_POLICY`` (fails OPEN).
- model produces a malformed plan (missing target_path / pr_title) →
  ``FanoutError`` with ``kind is FanoutFailureKind.DECOMPOSE_NON_POLICY``.

Plus a fresh-session isolation check, and the OFFLINE agent-construction /
tool-declaration test (the RISK note): build the decompose agent and prove
ADK can construct the ``submit_plan`` FunctionDeclaration without error.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from agent import fanout
from agent.fanout import (
    DecomposeResult,
    FanoutError,
    FanoutFailureKind,
    SliceSpec,
    decompose,
    make_submit_plan,
)
from tests.unit._adk_stubs import StubEvent as _Ev, StubPart as _P


@pytest.fixture
def provision_workload_env(monkeypatch):
    """Set the worker URL env vars the provision workload resolves at load
    time, and clear the workload cache on setup + teardown. Mirrors the
    fixture in tests/unit/test_provision_workload.py — ``decompose`` defaults
    its ``read_tools`` to ``resolve_provision_read_tools()``, which loads the
    provision workload, so the env must be primed."""
    monkeypatch.setenv("READER_URL", "https://reader.test")
    monkeypatch.setenv("INFRA_READER_URL", "https://infra-reader.test")
    monkeypatch.setenv("TOFU_EDITOR_URL", "https://tofu-editor.test")
    import agent.workloads.registry as registry_mod
    registry_mod._WORKLOAD_CACHE.clear()
    yield
    registry_mod._WORKLOAD_CACHE.clear()


def _find_submit_plan(agent):
    """Pull the ``submit_plan`` callable out of an agent's tool list.

    ``decompose`` builds the agent's tools as ``[*read_tool_values,
    submit_plan]``; the submit tool's ``__name__`` is ``submit_plan``. This is
    how the stub model 'calls the tool' — it grabs the very closure
    ``decompose`` wired to the sink and invokes it, exactly as the live model
    would via the function-call protocol."""
    return next(t for t in agent.tools if getattr(t, "__name__", "") == "submit_plan")


def _model_calls_submit_plan(plan_payload):
    """Build a fake ``Runner`` whose ``run_async`` simulates the model calling
    ``submit_plan`` once with ``plan_payload`` (a JSON string), then yielding a
    final event.

    Returns ``(make_runner, captured)`` where ``make_runner(*, agent, **kw)``
    is the stand-in ``Runner`` constructor (so we can capture the agent + the
    session wiring) and ``captured`` is a dict the test can inspect.
    """
    captured: dict = {}

    class _FakeRunner:
        def __init__(self, *, agent, app_name, session_service):
            captured["agent"] = agent
            captured["app_name"] = app_name
            captured["session_service"] = session_service

        async def run_async(self, *, user_id, session_id, new_message):
            captured["user_id"] = user_id
            captured["session_id"] = session_id
            # Simulate the model: call the agent's submit_plan tool once.
            submit = _find_submit_plan(captured["agent"])
            if plan_payload is not None:
                submit(plan_json=plan_payload)
            # Then a final natural-language event (decompose consumes events;
            # the plan came via the tool sink, not this text).
            yield _Ev([_P(text="done")], partial=False, final=True)

    return _FakeRunner, captured


def _two_slice_payload() -> str:
    return json.dumps({
        "slices": [
            {"goal": "create a GCS bucket", "target_path": "iac/bucket.tf"},
            {"goal": "create a VPC network", "target_path": "iac/network.tf"},
        ],
        "pr_title": "Add bucket and network",
        "pr_body_intro": "This change adds two independent iac files.",
    })


# --------------------------------------------------------------------------- #
# make_submit_plan tool factory (sink-recording, no validation)
# --------------------------------------------------------------------------- #


def test_make_submit_plan_records_raw_into_sink_and_acks():
    sink: dict = {}
    tool = make_submit_plan(sink)
    assert tool.__name__ == "submit_plan"
    payload = _two_slice_payload()
    ack = tool(plan_json=payload)
    assert sink["plan_json"] == payload
    assert ack["status"] == "recorded"


def test_make_submit_plan_does_not_validate():
    """The tool only RECORDS — even garbage is accepted (validation is
    ``decompose``'s job, not the tool's)."""
    sink: dict = {}
    tool = make_submit_plan(sink)
    ack = tool(plan_json="not json at all")
    assert sink["plan_json"] == "not json at all"
    assert ack["status"] == "recorded"


# --------------------------------------------------------------------------- #
# decompose() — happy path
# --------------------------------------------------------------------------- #


async def test_decompose_two_independent_slices_returns_result(provision_workload_env):
    make_runner, _ = _model_calls_submit_plan(_two_slice_payload())
    with patch.object(fanout, "Runner", make_runner):
        result = await decompose("add a bucket and a network")

    assert isinstance(result, DecomposeResult)
    assert len(result.slices) == 2
    assert all(isinstance(s, SliceSpec) for s in result.slices)
    assert {s.target_path for s in result.slices} == {"iac/bucket.tf", "iac/network.tf"}
    assert result.pr_title == "Add bucket and network"
    assert result.pr_body_intro == "This change adds two independent iac files."


async def test_decompose_single_slice_returns_result_no_exception(provision_workload_env):
    """A 1-slice plan is VALID and returned — decompose() does NOT raise; the
    CALLER decides to fall back to the legacy single-agent path."""
    payload = json.dumps({
        "slices": [{"goal": "tweak the bucket lifecycle", "target_path": "iac/bucket.tf"}],
        "pr_title": "Tweak bucket",
        "pr_body_intro": "Single coupled change.",
    })
    make_runner, _ = _model_calls_submit_plan(payload)
    with patch.object(fanout, "Runner", make_runner):
        result = await decompose("tweak the bucket")

    assert isinstance(result, DecomposeResult)
    assert len(result.slices) == 1
    assert result.slices[0].target_path == "iac/bucket.tf"


# --------------------------------------------------------------------------- #
# decompose() — POLICY failures propagate UNCHANGED (fail CLOSED)
# --------------------------------------------------------------------------- #


async def test_decompose_colliding_paths_is_policy(provision_workload_env):
    payload = json.dumps({
        "slices": [
            {"goal": "create bucket A", "target_path": "iac/bucket.tf"},
            {"goal": "create bucket B", "target_path": "iac/bucket.tf"},
        ],
        "pr_title": "Two buckets",
        "pr_body_intro": "oops same path",
    })
    make_runner, _ = _model_calls_submit_plan(payload)
    with patch.object(fanout, "Runner", make_runner):
        with pytest.raises(FanoutError) as ei:
            await decompose("two buckets")
    assert ei.value.kind is FanoutFailureKind.POLICY


async def test_decompose_foundation_path_is_policy(provision_workload_env):
    payload = json.dumps({
        "slices": [
            {"goal": "add a provider", "target_path": "iac/providers.tf"},
            {"goal": "create a bucket", "target_path": "iac/bucket.tf"},
        ],
        "pr_title": "Provider + bucket",
        "pr_body_intro": "intro",
    })
    make_runner, _ = _model_calls_submit_plan(payload)
    with patch.object(fanout, "Runner", make_runner):
        with pytest.raises(FanoutError) as ei:
            await decompose("provider and bucket")
    assert ei.value.kind is FanoutFailureKind.POLICY


# --------------------------------------------------------------------------- #
# decompose() — DECOMPOSE_NON_POLICY failures (fail OPEN)
# --------------------------------------------------------------------------- #


async def test_decompose_never_calls_submit_plan_is_non_policy(provision_workload_env):
    """The model produced no plan (empty sink) → DECOMPOSE_NON_POLICY so the
    orchestrator falls open to the single-agent path."""
    make_runner, _ = _model_calls_submit_plan(None)  # never call submit_plan
    with patch.object(fanout, "Runner", make_runner):
        with pytest.raises(FanoutError) as ei:
            await decompose("do something")
    assert ei.value.kind is FanoutFailureKind.DECOMPOSE_NON_POLICY
    assert ei.value.status == 502


async def test_decompose_non_json_plan_is_non_policy(provision_workload_env):
    make_runner, _ = _model_calls_submit_plan("this is not json {{")
    with patch.object(fanout, "Runner", make_runner):
        with pytest.raises(FanoutError) as ei:
            await decompose("do something")
    assert ei.value.kind is FanoutFailureKind.DECOMPOSE_NON_POLICY


async def test_decompose_missing_target_path_is_non_policy(provision_workload_env):
    payload = json.dumps({
        "slices": [{"goal": "create a bucket"}],  # no target_path
        "pr_title": "Bucket",
        "pr_body_intro": "intro",
    })
    make_runner, _ = _model_calls_submit_plan(payload)
    with patch.object(fanout, "Runner", make_runner):
        with pytest.raises(FanoutError) as ei:
            await decompose("bucket")
    assert ei.value.kind is FanoutFailureKind.DECOMPOSE_NON_POLICY


async def test_decompose_missing_pr_title_is_non_policy(provision_workload_env):
    payload = json.dumps({
        "slices": [{"goal": "create a bucket", "target_path": "iac/bucket.tf"}],
        # no pr_title
        "pr_body_intro": "intro",
    })
    make_runner, _ = _model_calls_submit_plan(payload)
    with patch.object(fanout, "Runner", make_runner):
        with pytest.raises(FanoutError) as ei:
            await decompose("bucket")
    assert ei.value.kind is FanoutFailureKind.DECOMPOSE_NON_POLICY


async def test_decompose_extra_field_in_slice_is_non_policy(provision_workload_env):
    """A stray field on a slice is rejected by SliceSpec's ``extra='forbid'``
    and surfaces as DECOMPOSE_NON_POLICY (a malformed plan, not a policy
    rejection)."""
    payload = json.dumps({
        "slices": [{
            "goal": "create a bucket",
            "target_path": "iac/bucket.tf",
            "second_path": "iac/sneaky.tf",  # forbidden extra
        }],
        "pr_title": "Bucket",
        "pr_body_intro": "intro",
    })
    make_runner, _ = _model_calls_submit_plan(payload)
    with patch.object(fanout, "Runner", make_runner):
        with pytest.raises(FanoutError) as ei:
            await decompose("bucket")
    assert ei.value.kind is FanoutFailureKind.DECOMPOSE_NON_POLICY


# --------------------------------------------------------------------------- #
# decompose() — session isolation + event_sink forwarding
# --------------------------------------------------------------------------- #


async def test_decompose_uses_a_fresh_dedicated_session(provision_workload_env):
    """A brand-new session id is created per decompose run (isolation: decompose
    chatter must not leak into later authoring sessions)."""
    make_runner, captured = _model_calls_submit_plan(_two_slice_payload())
    with patch.object(fanout, "Runner", make_runner):
        await decompose("add a bucket and a network")

    sid = captured["session_id"]
    assert isinstance(sid, str) and sid
    # The session service the runner got is a fresh InMemorySessionService
    # carrying exactly this one session.
    svc = captured["session_service"]
    from google.adk.sessions import InMemorySessionService
    assert isinstance(svc, InMemorySessionService)

    # Two runs use two DIFFERENT session ids (fresh per call).
    make_runner2, captured2 = _model_calls_submit_plan(_two_slice_payload())
    with patch.object(fanout, "Runner", make_runner2):
        await decompose("again")
    assert captured2["session_id"] != sid


async def test_decompose_forwards_events_to_event_sink(provision_workload_env):
    make_runner, _ = _model_calls_submit_plan(_two_slice_payload())
    seen: list = []
    with patch.object(fanout, "Runner", make_runner):
        await decompose("add a bucket and a network", event_sink=seen.append)
    assert len(seen) >= 1


async def test_decompose_respects_injected_read_tools(provision_workload_env):
    """When ``read_tools`` is passed explicitly, decompose uses those values
    (plus submit_plan) and does NOT fall back to the provision resolver."""
    sentinel_called = {}

    def fake_read_tool():  # a stand-in read tool callable
        sentinel_called["hit"] = True

    fake_read_tool.__name__ = "fake_read_tool"
    make_runner, captured = _model_calls_submit_plan(_two_slice_payload())
    with patch.object(fanout, "Runner", make_runner):
        await decompose(
            "add a bucket and a network",
            read_tools={"fake_read_tool": fake_read_tool},
        )
    tool_names = {getattr(t, "__name__", "") for t in captured["agent"].tools}
    assert "fake_read_tool" in tool_names
    assert "submit_plan" in tool_names
    # The editor callable must never be present (read tools only + submit).
    assert "open_infra_pr_tool" not in tool_names


# --------------------------------------------------------------------------- #
# RISK: OFFLINE proof ADK can build the submit_plan tool declaration
# --------------------------------------------------------------------------- #


def test_submit_plan_tool_declaration_builds_offline():
    """Prove ADK can construct a FunctionDeclaration for ``submit_plan``.

    The ``submit_plan`` param shape is a single ``plan_json: str`` — a plain
    primitive ADK maps to ``types.Type.STRING``. Wrapping the closure in
    ``FunctionTool`` and calling ``_get_declaration()`` is the same path the
    runtime uses to hand the tool schema to Gemini, so a green here means the
    live decompose agent's tool schema will build (no deep-nested-list schema
    gap). This catches schema-gen failures a mocked run would miss.
    """
    from google.adk.tools.function_tool import FunctionTool

    sink: dict = {}
    tool = make_submit_plan(sink)
    decl = FunctionTool(tool)._get_declaration()
    assert decl is not None
    assert decl.name == "submit_plan"
    # The single declared parameter is the JSON-string plan.
    props = decl.parameters.properties
    assert "plan_json" in props


async def test_decompose_agent_tools_all_build_declarations_offline(provision_workload_env):
    """Build the REAL decompose agent (no run) and assert ADK can construct a
    FunctionDeclaration for the submit_plan tool it carries — the offline
    schema-gen proof from the RISK note, exercised on the actual agent the
    factory wires."""
    from google.adk.tools.function_tool import FunctionTool

    captured: dict = {}

    class _CaptureRunner:
        def __init__(self, *, agent, app_name, session_service):
            captured["agent"] = agent

        async def run_async(self, *, user_id, session_id, new_message):
            submit = _find_submit_plan(captured["agent"])
            submit(plan_json=_two_slice_payload())
            yield _Ev([_P(text="done")], partial=False, final=True)

    with patch.object(fanout, "Runner", _CaptureRunner):
        await decompose("add a bucket and a network")

    submit = _find_submit_plan(captured["agent"])
    decl = FunctionTool(submit)._get_declaration()
    assert decl is not None
    assert decl.name == "submit_plan"
