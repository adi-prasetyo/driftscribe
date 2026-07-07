"""Prior-turn seeding into the per-call ADK session (P1 multi-turn)."""
from types import SimpleNamespace

import pytest

import agent.adk_agent as adk_agent


def test_seed_event_user_turn_uses_user_author():
    ev = adk_agent._seed_event_from_turn(
        {"role": "user", "text": "hello"}, agent_name="driftscribe_chat_drift"
    )
    assert ev.author == "user"
    assert ev.content.role == "user"
    assert ev.content.parts[0].text == "hello"


def test_seed_event_crew_turn_uses_agent_name_and_model_role():
    # CRITICAL: crew turns MUST carry the agent's own name, else ADK rewrites
    # them into "For context: ... said" user messages.
    ev = adk_agent._seed_event_from_turn(
        {"role": "crew", "text": "all clear"}, agent_name="driftscribe_chat_drift"
    )
    assert ev.author == "driftscribe_chat_drift"
    assert ev.content.role == "model"
    assert ev.content.parts[0].text == "all clear"


@pytest.mark.asyncio
async def test_run_chat_stream_seeds_prior_turns_into_session(monkeypatch):
    """run_chat_stream appends prior turns (user, then crew-as-agent) before run."""
    appended = []

    class _RecordingSession:
        def __init__(self):
            self.events = []

    class _RecordingService:
        def __init__(self):
            self._session = _RecordingSession()

        async def create_session(self, **kw):
            return self._session

        async def append_event(self, session, event):
            appended.append(
                (event.author, event.content.role, event.content.parts[0].text)
            )
            return event

    async def _stub_run(*a, **k):
        yield SimpleNamespace(
            content=SimpleNamespace(
                parts=[SimpleNamespace(text="ok", thought=False)]
            ),
            partial=False,
            usage_metadata=None,
            is_final_response=lambda: True,
        )

    monkeypatch.setattr(adk_agent, "InMemorySessionService", _RecordingService)
    monkeypatch.setattr(adk_agent, "load_workload", lambda w: SimpleNamespace())
    monkeypatch.setattr(
        adk_agent, "build_chat_agent",
        lambda res, autonomy_mode, extra_instruction=None: SimpleNamespace(
            name="driftscribe_chat_drift"
        ),
    )

    class _Runner:
        def __init__(self, **kw):
            pass

        def run_async(self, **kw):
            return _stub_run()

    monkeypatch.setattr(adk_agent, "Runner", _Runner)

    prior = [
        {"role": "user", "text": "first q", "workload": "drift"},
        {"role": "crew", "text": "first a", "workload": "drift"},
    ]
    items = [
        it async for it in adk_agent.run_chat_stream(
            "second q", workload="drift", autonomy_mode="propose_apply",
            prior_turns=prior,
        )
    ]
    assert appended == [
        ("user", "user", "first q"),
        ("driftscribe_chat_drift", "model", "first a"),
    ]
    assert items[-1]["type"] == "result"


@pytest.mark.asyncio
async def test_run_chat_stream_caps_prior_turns_with_marker(monkeypatch):
    """Over MAX_SEED_TURNS, only the last N seed + one omission marker first."""
    appended = []

    class _Svc:
        def __init__(self):
            self._s = SimpleNamespace(events=[])

        async def create_session(self, **kw):
            return self._s

        async def append_event(self, session, event):
            appended.append((event.author, event.content.parts[0].text))
            return event

    async def _stub_run(*a, **k):
        yield SimpleNamespace(
            content=SimpleNamespace(parts=[SimpleNamespace(text="ok", thought=False)]),
            partial=False, usage_metadata=None, is_final_response=lambda: True,
        )

    monkeypatch.setattr(adk_agent, "InMemorySessionService", _Svc)
    monkeypatch.setattr(adk_agent, "load_workload", lambda w: SimpleNamespace())
    monkeypatch.setattr(
        adk_agent, "build_chat_agent",
        lambda res, autonomy_mode, extra_instruction=None: SimpleNamespace(name="ag"),
    )
    monkeypatch.setattr(
        adk_agent, "Runner",
        lambda **kw: SimpleNamespace(run_async=lambda **k: _stub_run()),
    )

    n = adk_agent.MAX_SEED_TURNS + 4
    prior = [{"role": "user", "text": f"t{i}", "workload": "drift"} for i in range(n)]
    _ = [it async for it in adk_agent.run_chat_stream(
        "q", workload="drift", autonomy_mode="propose_apply", prior_turns=prior,
    )]
    # 1 marker + MAX_SEED_TURNS seeded
    assert len(appended) == adk_agent.MAX_SEED_TURNS + 1
    assert "omitted" in appended[0][1]
    # the oldest kept turn is t4 (first 4 dropped)
    assert appended[1][1] == "t4"


def _seed_recorder(monkeypatch):
    """Wire run_chat_stream's session service + runner to capture seeded turn
    text. Returns the ``appended`` list of (author, role, text)."""
    appended = []

    class _RecordingService:
        def __init__(self):
            self._session = SimpleNamespace(events=[])

        async def create_session(self, **kw):
            return self._session

        async def append_event(self, session, event):
            appended.append(
                (event.author, event.content.role, event.content.parts[0].text)
            )
            return event

    async def _stub_run(*a, **k):
        yield SimpleNamespace(
            content=SimpleNamespace(parts=[SimpleNamespace(text="ok", thought=False)]),
            partial=False, usage_metadata=None, is_final_response=lambda: True,
        )

    monkeypatch.setattr(adk_agent, "InMemorySessionService", _RecordingService)
    monkeypatch.setattr(adk_agent, "load_workload", lambda w: SimpleNamespace())
    monkeypatch.setattr(
        adk_agent, "build_chat_agent",
        lambda res, autonomy_mode, extra_instruction=None: SimpleNamespace(
            name="driftscribe_chat_drift"
        ),
    )
    monkeypatch.setattr(
        adk_agent, "Runner",
        lambda **kw: SimpleNamespace(run_async=lambda **k: _stub_run()),
    )
    return appended


@pytest.mark.asyncio
async def test_run_chat_stream_scrubs_seeded_token_for_demo_anon(monkeypatch):
    """Defense-in-depth (C1): an anonymous caller resuming a conversation whose
    persisted operator turn carries a live ?t= approval link must NOT have that
    token re-seeded into the model context. With demo_anon=True the seeded turn
    text is token-scrubbed before it reaches the session."""
    appended = _seed_recorder(monkeypatch)
    prior = [
        {"role": "crew",
         "text": "Approve at https://c/approvals/id1?t=SECRETTOKEN",
         "workload": "drift"},
    ]
    _ = [it async for it in adk_agent.run_chat_stream(
        "q", workload="drift", autonomy_mode="propose_apply",
        prior_turns=prior, demo_anon=True,
    )]
    seeded_text = appended[0][2]
    assert "SECRETTOKEN" not in seeded_text
    assert "?t=<redacted>" in seeded_text


@pytest.mark.asyncio
async def test_run_chat_stream_keeps_seeded_token_for_operator(monkeypatch):
    """Operator path (demo_anon default False): the seeded turn is untouched, so
    an operator resuming their own conversation still gets the clickable link."""
    appended = _seed_recorder(monkeypatch)
    prior = [
        {"role": "crew",
         "text": "Approve at https://c/approvals/id1?t=SECRETTOKEN",
         "workload": "drift"},
    ]
    _ = [it async for it in adk_agent.run_chat_stream(
        "q", workload="drift", autonomy_mode="propose_apply", prior_turns=prior,
    )]
    assert "?t=SECRETTOKEN" in appended[0][2]
