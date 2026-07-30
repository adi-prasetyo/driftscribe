"""ds-q38 — the seam between ADK's event stream and the coherence gate.

Every OTHER test of the gate populates ``reader_sink`` from a double, which
proves the gate's logic but says nothing about whether the real ADK path ever
fills it. That distinction is not academic here: if this capture silently
stopped working, ``_do_recheck`` would see zero observations on every
production turn and refuse every audit with "the agent did not read live
state" — a self-inflicted outage of the autonomous lane, with the whole suite
still green.

So these drive real ``google.genai`` event shapes through the actual
``_emit_event_logs`` and assert on what lands in the sink. The gate on the
caller's side (``event.content and event.content.parts and partial is not
True``) is asserted too, because a response that never passes it never reaches
this function at all.
"""
from __future__ import annotations

from types import SimpleNamespace

from google.genai import types

from agent.adk_agent import _emit_event_logs
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


def test_the_callers_partial_gate_would_admit_a_tool_response_event() -> None:
    """``run_agent`` only calls ``_emit_event_logs`` for events satisfying
    ``content and content.parts and partial is not True``. A tool-response event
    has content and parts, and ADK leaves ``partial`` unset (None) on it — so
    the gate admits it. Pinned because a response that never passes the gate
    never reaches the capture above, and the sink would be empty in production
    while every other test still passed."""
    event = _response_event(read_live_env_tool.__name__, _READER_RESPONSE)
    assert event.content and event.content.parts
    assert getattr(event, "partial", None) is not True
