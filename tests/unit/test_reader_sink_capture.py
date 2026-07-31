"""ds-q38 — the seam between ADK's event stream and the coherence gate.

Every OTHER test of the gate populates ``reader_sink`` from a double, which
proves the gate's logic but says nothing about whether the real ADK path ever
fills it. That distinction is not academic here: if this capture silently
stopped working, ``_do_recheck`` would see zero observations on every
production turn and refuse every audit with "the agent did not read live
state" — a self-inflicted outage of the autonomous lane, with the whole suite
still green.

So these drive real ``google.genai`` event shapes through the actual
``_emit_event_logs`` and assert on what lands in the sink.

Two layers, because the projection and the plumbing fail differently:

* most tests here call ``_emit_event_logs`` directly, pinning WHAT gets
  projected out of a reader response (and what deliberately does not);
* the last two drive real ``run_agent`` with only ADK's ``Runner.run_async``
  replaced, pinning that the caller loop still forwards the sink at all.

The second layer exists because the first cannot see the outage-shaped
failure: dropping ``reader_sink=`` from ``run_agent``'s emit call leaves every
projection test green while production refuses every audit. That was verified
by injecting exactly that change — 8 green, 1 red.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from google.adk.runners import Runner
from google.genai import types

from agent.adk_agent import _emit_event_logs, run_agent
from agent.adk_tools import read_live_env_tool


def _response_event(name: str, response: dict, *, partial=None) -> SimpleNamespace:
    """An event shaped like the one ADK yields for a tool result."""
    return SimpleNamespace(
        content=types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        name=name, response=response
                    )
                )
            ],
        ),
        partial=partial,
    )


_READER_RESPONSE = {
    "service": "payment-demo",
    "region": "asia-northeast1",
    "project": "driftscribe-hack-2026",
    "env": {"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "false"},
    "revision": "payment-demo-00021-t8k",
    "previous_revisions": ["payment-demo-00020-5qn"],
}


def test_a_real_reader_function_response_lands_in_the_sink() -> None:
    """The shape from the actual 2026-07-30 incident trace, captured verbatim
    from that turn's ``tool_result`` log line."""
    sink: list = []
    _emit_event_logs(_response_event(read_live_env_tool.__name__, _READER_RESPONSE),
                     reader_sink=sink)

    assert sink == [
        {
            "env": {"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "false"},
            "revision": "payment-demo-00021-t8k",
        }
    ]


def test_the_sink_is_keyed_on_the_real_callable_name() -> None:
    """Name-matched, not shape-matched. Pinning it against the callable's own
    ``__name__`` means renaming the tool cannot silently stop capture — the same
    grounding rule the prompts follow, for the same reason."""
    sink: list = []
    _emit_event_logs(_response_event("some_other_tool", _READER_RESPONSE),
                     reader_sink=sink)
    assert sink == []


def test_previous_revisions_are_deliberately_not_recorded() -> None:
    """Only ``(env, revision)`` — the key's actual subject — is projected.

    ``previous_revisions`` is best-effort and steers WHICH rollback target the
    model picks, so two reads differing only in candidates collapse to one
    observation here. That is intended and bounded: the invariant this supports
    is coherence of the key's ``(env, serving revision)`` subject, NOT of every
    input the action choice used. The candidate-evidence gap is tracked
    separately (ds-lfk) rather than smuggled in as if this closed it."""
    sink: list = []
    _emit_event_logs(_response_event(read_live_env_tool.__name__, _READER_RESPONSE),
                     reader_sink=sink)
    assert set(sink[0]) == {"env", "revision"}


def test_two_responses_merged_into_one_event_are_both_captured() -> None:
    """ADK merges parallel function responses into a single non-partial event,
    so one event can carry several parts. Missing the second would silently
    turn "the agent saw the world change" into "one clean observation"."""
    sink: list = []
    event = SimpleNamespace(
        content=types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        name=read_live_env_tool.__name__,
                        response=dict(_READER_RESPONSE, env={"PAYMENT_MODE": "mock"}),
                    )
                ),
                types.Part(
                    function_response=types.FunctionResponse(
                        name=read_live_env_tool.__name__, response=_READER_RESPONSE
                    )
                ),
            ],
        ),
        partial=None,
    )
    _emit_event_logs(event, reader_sink=sink)

    assert [o["env"]["PAYMENT_MODE"] for o in sink] == ["mock", "live"]


def test_a_malformed_env_block_is_not_recorded_as_an_observation() -> None:
    """Same unknown-vs-empty rule ``_observed_env_or_none`` keeps: a payload we
    cannot read is NOT an observation of an empty service. Recording it would
    manufacture agreement with a genuinely empty env."""
    for bad in ({"env": None}, {"env": []}, {"env": {"K": 1}}, {}):
        sink: list = []
        _emit_event_logs(_response_event(read_live_env_tool.__name__, bad),
                         reader_sink=sink)
        assert sink == [], bad


def test_an_empty_env_from_a_well_formed_payload_IS_an_observation() -> None:
    """A service really can have no env vars. Dropping this would make the
    coordinator refuse every audit of such a service."""
    sink: list = []
    _emit_event_logs(
        _response_event(read_live_env_tool.__name__,
                        {"env": {}, "revision": "payment-demo-00001-abc"}),
        reader_sink=sink,
    )
    assert sink == [{"env": {}, "revision": "payment-demo-00001-abc"}]


def test_an_empty_revision_is_recorded_as_unknown_not_as_empty() -> None:
    """``read_live_state`` documents ``""`` as a real state (every deploy
    failed). It must not compare equal to another ``""`` and read as "the same
    revision" — the gate treats unknown as a mismatch."""
    sink: list = []
    _emit_event_logs(
        _response_event(read_live_env_tool.__name__,
                        {"env": {"A": "1"}, "revision": ""}),
        reader_sink=sink,
    )
    assert sink == [{"env": {"A": "1"}, "revision": None}]


async def test_run_agent_itself_forwards_the_sink_through_its_event_loop(
    drift_workload_env,
) -> None:
    """The whole caller loop, not just the projection — Codex round 5.

    Everything above calls ``_emit_event_logs`` directly, which pins the
    projection but leaves the seam that actually breaks untested: ``run_agent``
    could stop passing ``reader_sink=``, stop iterating response events, or
    tighten its ``partial`` gate, and all eight would stay green while
    production refused EVERY audit with "the agent did not read live state".
    That is an outage with a green suite, so the loop is exercised end to end
    here with only the ADK runner replaced.

    This also subsumes the ``partial``-gate assertion this test replaced, which
    was tautological: it built its own event with ``partial=None`` and then
    asserted ``partial is not True``. It could never have failed. Driving the
    real loop tests the gate against events instead of against itself.
    """
    proposal_json = json.dumps({
        "action": "no_op",
        "env_diffs": [],
        "target_docs_file": None,
        "target_docs_section": None,
        "rationale": "Live configuration matches the contract.",
        "confidence": 0.95,
        "requires_human_review": False,
    })

    reader_event = _response_event(read_live_env_tool.__name__, _READER_RESPONSE)
    reader_event.is_final_response = lambda: False
    reader_event.usage_metadata = None

    final_event = SimpleNamespace(
        content=types.Content(role="model", parts=[types.Part(text=proposal_json)]),
        partial=None,
        usage_metadata=None,
        is_final_response=lambda: True,
    )

    async def _fake_run_async(self, **_kwargs):
        yield reader_event
        yield final_event

    sink: list = []
    with patch.object(Runner, "run_async", _fake_run_async):
        proposal = await run_agent(
            "check for drift", workload="drift", autonomy_mode="propose",
            reader_sink=sink,
        )

    assert proposal.action.value == "no_op", "premise: the turn completed"
    assert sink == [
        {
            "env": {"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "false"},
            "revision": "payment-demo-00021-t8k",
        }
    ], "run_agent must forward the sink into _emit_event_logs for reader responses"


async def test_run_agent_leaves_the_sink_empty_when_the_agent_never_reads(
    drift_workload_env,
) -> None:
    """The other half of the seam, and the one the coherence gate keys on: an
    agent that answers without consulting live state must produce NO
    observation, so the caller refuses rather than assuming it looked."""
    proposal_json = json.dumps({
        "action": "no_op", "env_diffs": [], "target_docs_file": None,
        "target_docs_section": None, "rationale": "Nothing to do.",
        "confidence": 0.9, "requires_human_review": False,
    })
    final_event = SimpleNamespace(
        content=types.Content(role="model", parts=[types.Part(text=proposal_json)]),
        partial=None,
        usage_metadata=None,
        is_final_response=lambda: True,
    )

    async def _fake_run_async(self, **_kwargs):
        yield final_event

    sink: list = []
    with patch.object(Runner, "run_async", _fake_run_async):
        await run_agent(
            "check for drift", workload="drift", autonomy_mode="propose",
            reader_sink=sink,
        )

    assert sink == []
