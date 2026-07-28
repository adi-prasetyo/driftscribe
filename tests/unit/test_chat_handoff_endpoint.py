"""The crew-handoff HTTP surface: propose on /chat, redeem on /chat/handoff.

The load-bearing claim this module defends is that ``POST /chat`` still cannot
move a locked thread. The 409 crew-lock is scar tissue — a workload-less POST
once defaulted to the mutation-capable drift workload and an out-of-domain
probe prompt became fabricated docs PR #109 — so a crew CHANGE rides its own
single-use credential through a separate endpoint rather than relaxing it.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient

import agent.main as agent_main
from agent.auth import verify_token
from agent.state_store import InMemoryStateStore

PROPOSAL = {
    "from": "explore", "to": "drift",
    "reason": "fixing this needs a rollback",
    "brief": "ORDER_TIMEOUT is 90s; the contract pins 30s.",
}


@pytest.fixture
def state():
    return InMemoryStateStore()


@pytest.fixture
def client(monkeypatch, state):
    monkeypatch.setenv("USE_ADK", "true")
    agent_main.get_settings.cache_clear()
    monkeypatch.setattr(agent_main, "_state_singleton", state)
    monkeypatch.setattr(agent_main, "get_state", lambda: state)
    agent_main.app.dependency_overrides[verify_token] = lambda: None
    monkeypatch.setattr(agent_main, "load_workload", lambda w: object())
    monkeypatch.setattr(agent_main, "_eager_resolve_upgrade_contract", lambda r: None)

    # Records what the joining run was actually built with.
    calls: list[dict] = []

    async def _run_chat(prompt, session_id=None, *, workload="drift",
                        autonomy_mode="propose_apply", prior_turns=None,
                        demo_anon=False, denied_tools=None):
        calls.append({
            "prompt": prompt, "workload": workload, "prior_turns": prior_turns,
            "denied_tools": denied_tools, "autonomy_mode": autonomy_mode,
            "demo_anon": demo_anon,
        })
        out = {"reply": f"reply to {prompt}", "tool_calls": [], "session_id": "sid"}
        if getattr(_run_chat, "propose", None):
            out["handoff"] = dict(_run_chat.propose)
        return out

    monkeypatch.setattr("agent.adk_agent.run_chat", _run_chat)
    c = TestClient(agent_main.app)
    c.calls = calls
    c.run_chat = _run_chat
    yield c
    agent_main.app.dependency_overrides.pop(verify_token, None)
    agent_main.get_settings.cache_clear()


def _chat(client, prompt="the timeout looks wrong", workload="explore", cid=None,
          **extra):
    body = {"prompt": prompt, "workload": workload, **extra}
    if cid:
        body["conversation_id"] = cid
    return client.post("/chat", json=body)


def _propose(client, **kw):
    """Run one /chat turn whose crew proposes a handoff; return (cid, nonce)."""
    client.run_chat.propose = PROPOSAL
    try:
        r = _chat(client, **kw)
    finally:
        client.run_chat.propose = None
    body = r.json()
    return body["conversation_id"], body["handoff"]["nonce"]


# --- proposing -------------------------------------------------------------

def test_a_proposing_turn_returns_a_nonce_and_the_route(client):
    client.run_chat.propose = PROPOSAL
    body = _chat(client).json()

    assert body["handoff"]["to"] == "drift"
    assert body["handoff"]["from"] == "explore"
    assert body["handoff"]["reason"] == "fixing this needs a rollback"
    assert body["handoff"]["nonce"]
    assert body["handoff"]["expires_at"]


def test_a_proposal_survives_on_the_very_first_turn(client, state):
    """The case that breaks the obvious implementation: on turn 1 there is no
    conversation document yet, so the proposal has to commit WITH the turns."""
    cid, _ = _propose(client)
    conv = state.get_conversation(cid)
    assert conv["pending_handoff"]["to"] == "drift"
    assert len(conv["turns"]) == 2


def test_an_ordinary_turn_returns_no_handoff(client):
    assert "handoff" not in _chat(client).json()


def test_the_nonce_never_appears_in_persisted_state(client, state):
    """Only the digest is stored — reading the conversation must not hand a
    reader the credential."""
    cid, nonce = _propose(client)
    assert nonce not in json.dumps(state.get_conversation(cid), default=str)
    served = client.get(f"/conversations/{cid}").json()
    assert nonce not in json.dumps(served, default=str)
    assert "nonce_digest" not in json.dumps(served.get("pending_handoff", {}))


def test_an_ephemeral_turn_proposes_nothing(client):
    """Ephemeral probe turns persist nothing, so there is no conversation for a
    transition to land on — the nonce must not be minted at all."""
    client.run_chat.propose = PROPOSAL
    body = _chat(client, ephemeral=True).json()
    assert "handoff" not in body
    assert "conversation_id" not in body


def test_a_second_proposal_supersedes_the_first(client):
    cid, first = _propose(client)
    _, second = _propose(client, cid=cid)

    assert _redeem(client, cid, first).status_code == 403
    assert _redeem(client, cid, second).status_code == 200


# --- the lock this design refuses to relax ---------------------------------

def test_plain_chat_still_cannot_move_a_locked_thread(client):
    """The whole point. A crew change requires the nonce; ``ChatRequest`` stays
    a closed Literal and the 409 stays exactly as written."""
    cid, _ = _propose(client)
    assert _chat(client, workload="drift", cid=cid).status_code == 409


def test_chat_after_a_completed_handoff_locks_to_the_new_crew(client):
    cid, nonce = _propose(client)
    _redeem(client, cid, nonce)

    assert _chat(client, workload="drift", cid=cid).status_code == 200
    assert _chat(client, workload="explore", cid=cid).status_code == 409


# --- redeeming -------------------------------------------------------------

def _redeem(client, cid, nonce, accept=True):
    return client.post("/chat/handoff", json={
        "conversation_id": cid, "nonce": nonce, "accept": accept,
    })


def test_accepting_flips_the_crew_and_runs_the_joining_turn(client, state):
    """"Confirm IS the turn" — the chip submits on the new crew's behalf. A
    join that then waits for the operator to retype what they already said is
    the friction this design removes."""
    cid, nonce = _propose(client)
    client.calls.clear()

    r = _redeem(client, cid, nonce)
    assert r.status_code == 200
    assert state.get_conversation(cid)["workload"] == "drift"

    assert len(client.calls) == 1, "the confirm click must run exactly one turn"
    run = client.calls[0]
    assert run["workload"] == "drift"
    assert "ORDER_TIMEOUT is 90s" in run["prompt"], "the brief carries intent"


def test_the_joining_turn_cannot_merge_or_close_a_pull_request(client):
    """A turn the operator never typed must not reach the two tools that mutate
    an existing PR in one call. Enforced here, in code — the demo-anon denylist
    explicitly preserves ``upgrade_merge_pr`` and ``propose_apply`` filters
    nothing."""
    from agent.handoff import HANDOFF_FIRST_TURN_DENIED_TOOLS

    cid, nonce = _propose(client)
    client.calls.clear()
    _redeem(client, cid, nonce)

    denied = client.calls[0]["denied_tools"]
    assert denied is not None
    assert set(denied) >= {"upgrade_merge_pr", "upgrade_close_pr"}
    assert set(denied) == set(HANDOFF_FIRST_TURN_DENIED_TOOLS)


def test_an_ordinary_chat_turn_carries_no_denylist(client):
    """The denial belongs to the confirm click alone — a typed turn to Patch
    must still be able to merge."""
    _chat(client)
    assert client.calls[-1]["denied_tools"] in (None, frozenset())


def test_the_joining_crew_sees_the_prior_conversation(client):
    cid, nonce = _propose(client)
    client.calls.clear()
    _redeem(client, cid, nonce)

    prior = client.calls[0]["prior_turns"]
    roles = [t["role"] for t in prior]
    assert "crew_change" in roles, "the transition itself must be in the replay"
    assert any(t.get("text") == "the timeout looks wrong" for t in prior)


def test_the_transition_and_the_joining_reply_both_persist(client, state):
    cid, nonce = _propose(client)
    _redeem(client, cid, nonce)

    turns = state.get_conversation(cid)["turns"]
    assert [t["role"] for t in turns] == ["user", "crew", "crew_change", "crew"]
    # No synthetic "user" turn: the operator confirmed a suggestion, they did
    # not type the brief, and the transcript must not claim they did.
    assert turns[-1]["workload"] == "drift"


def test_declining_burns_the_nonce_and_runs_nothing(client, state):
    cid, nonce = _propose(client)
    client.calls.clear()

    r = _redeem(client, cid, nonce, accept=False)
    assert r.status_code == 200
    assert client.calls == [], "a decline must not spend an LLM turn"
    assert state.get_conversation(cid)["workload"] == "explore"
    assert state.get_conversation(cid)["turns"][-1]["role"] == "handoff_declined"
    assert _redeem(client, cid, nonce).status_code == 409


def test_a_nonce_is_single_use(client):
    cid, nonce = _propose(client)
    assert _redeem(client, cid, nonce).status_code == 200
    assert _redeem(client, cid, nonce).status_code == 409


def test_a_wrong_nonce_is_forbidden_and_leaves_the_chip_alive(client):
    from agent.handoff import mint_handoff_nonce

    cid, nonce = _propose(client)
    guess, _ = mint_handoff_nonce()

    assert _redeem(client, cid, guess).status_code == 403
    assert _redeem(client, cid, nonce).status_code == 200


def test_an_expired_proposal_is_gone(client, state, monkeypatch):
    cid, nonce = _propose(client)
    conv = state.get_conversation(cid)
    past = conv["pending_handoff"]["expires_at"] - dt.timedelta(minutes=30)
    state._conversations[cid]["pending_handoff"]["expires_at"] = past

    assert _redeem(client, cid, nonce).status_code == 410
    assert state.get_conversation(cid)["workload"] == "explore"


def test_redeeming_against_the_wrong_conversation_is_refused(client):
    cid_a, nonce_a = _propose(client)
    cid_b, _ = _propose(client)
    assert cid_a != cid_b
    # The body carries no target — ``from`` and ``to`` come from persisted
    # server state — so a nonce is bound to exactly one thread.
    assert _redeem(client, cid_b, nonce_a).status_code == 403


def test_an_unknown_conversation_is_not_found(client):
    from agent.handoff import mint_handoff_nonce

    nonce, _ = mint_handoff_nonce()
    assert _redeem(client, "does-not-exist", nonce).status_code == 404


def test_a_malformed_conversation_id_is_not_found(client):
    """Path-escape guard, same as ``GET /conversations/{id}``."""
    assert _redeem(client, "../../etc/passwd", "x").status_code == 404


def test_a_conversation_with_no_proposal_refuses(client):
    r = _chat(client)
    assert _redeem(client, r.json()["conversation_id"], "anything").status_code == 409


def test_redemption_is_refused_while_a_turn_is_in_flight(client, state):
    """The 409 crew-lock is a point-in-time check, not a lock: /chat validates
    the workload, returns, and only then runs — persisting with the CAPTURED
    workload. Flipping mid-run would attribute that turn to the wrong crew."""
    cid, nonce = _propose(client)
    now = dt.datetime.now(dt.timezone.utc)
    assert state.begin_chat_run(cid, run_id="inflight", now=now) is True

    assert _redeem(client, cid, nonce).status_code == 409

    state.finish_chat_run(cid, run_id="inflight")
    assert _redeem(client, cid, nonce).status_code == 200


def test_a_second_chat_turn_on_a_busy_thread_is_refused(client, state):
    cid, _ = _propose(client)
    now = dt.datetime.now(dt.timezone.utc)
    state.begin_chat_run(cid, run_id="inflight", now=now)

    assert _chat(client, cid=cid).status_code == 409


def test_a_completed_turn_releases_its_lease(client, state):
    cid, _ = _propose(client)
    assert _chat(client, cid=cid).status_code == 200
    assert _chat(client, cid=cid).status_code == 200


# --- pause gate ------------------------------------------------------------

def test_pause_blocks_redemption_and_keeps_the_chip(client, state):
    """An LLM turn IS agent activity, so a confirm click must not start one
    while paused — and the operator must still be able to confirm once they
    resume, so the credential survives the refusal unburned."""
    cid, nonce = _propose(client)
    state.set_pause(paused=True, reason="maintenance", actor="op")
    client.calls.clear()

    r = _redeem(client, cid, nonce)
    assert r.status_code == 200
    assert r.json()["paused"] is True
    assert client.calls == []
    assert state.get_conversation(cid)["workload"] == "explore"

    state.set_pause(paused=False, reason=None, actor="op")
    assert _redeem(client, cid, nonce).status_code == 200


# --- SSE transport ---------------------------------------------------------

def _sse_done(text):
    for block in text.split("\n\n"):
        block = block.strip()
        if not block.startswith("event: done"):
            continue
        for line in block.splitlines():
            if line.startswith("data:"):
                return json.loads(line[len("data:"):].strip())
    return None


def test_the_proposal_rides_the_terminal_done_frame(client, monkeypatch):
    """Not a dedicated mid-stream frame: the nonce does not exist until the
    proposal persists, and persistence happens at the end of the stream. A
    mid-stream frame could only advertise a credential that is not yet real."""
    async def _stream(prompt, session_id=None, *, workload="drift",
                      autonomy_mode="propose_apply", prior_turns=None,
                      demo_anon=False, denied_tools=None):
        yield {"type": "result", "reply": "r", "tool_calls": [],
               "session_id": "sid", "handoff": dict(PROPOSAL)}

    monkeypatch.setattr("agent.adk_agent.run_chat_stream", _stream)
    r = client.post(
        "/chat", json={"prompt": "q", "workload": "explore"},
        headers={"Accept": "text/event-stream"},
    )
    done = _sse_done(r.text)
    assert done["handoff"]["to"] == "drift"
    assert done["handoff"]["nonce"]


def test_redemption_streams_the_joining_turn_when_asked(client, monkeypatch):
    seen: list[dict] = []

    async def _stream(prompt, session_id=None, *, workload="drift",
                      autonomy_mode="propose_apply", prior_turns=None,
                      demo_anon=False, denied_tools=None):
        seen.append({"workload": workload, "denied_tools": denied_tools})
        yield {"type": "result", "reply": "anchor here", "tool_calls": [],
               "session_id": "sid"}

    cid, nonce = _propose(client)
    monkeypatch.setattr("agent.adk_agent.run_chat_stream", _stream)
    r = client.post(
        "/chat/handoff",
        json={"conversation_id": cid, "nonce": nonce, "accept": True},
        headers={"Accept": "text/event-stream"},
    )
    assert r.status_code == 200
    assert _sse_done(r.text)["reply"] == "anchor here"
    assert seen[0]["workload"] == "drift"
    assert set(seen[0]["denied_tools"]) == {"upgrade_merge_pr", "upgrade_close_pr"}


def test_a_declined_handoff_answers_on_the_same_transport(client):
    cid, nonce = _propose(client)
    r = client.post(
        "/chat/handoff",
        json={"conversation_id": cid, "nonce": nonce, "accept": False},
        headers={"Accept": "text/event-stream"},
    )
    assert r.status_code == 200
    assert _sse_done(r.text) is not None


# --- what the conversation endpoints may serve -----------------------------

def test_an_open_proposal_is_served_projected_so_the_chip_survives_reload(client):
    """The chip renders from persisted state, not from the one-shot SSE frame —
    that is what makes a reload or a ``?conversation=`` deep link restore it
    instead of stranding a dead nonce. So the field must be served, but only
    the parts a chip needs."""
    cid, _ = _propose(client)
    pending = client.get(f"/conversations/{cid}").json()["pending_handoff"]

    assert pending["to"] == "drift"
    assert pending["from"] == "explore"
    assert pending["reason"] == "fixing this needs a rollback"
    assert pending["expires_at"]
    # The digest is a hash of a live credential and the brief is written for
    # the joining crew, not for a reader. Neither has any business in a
    # response every browser polls.
    assert "nonce_digest" not in pending
    assert "brief" not in pending


def test_a_redeemed_proposal_disappears_from_the_conversation(client):
    cid, nonce = _propose(client)
    _redeem(client, cid, nonce)
    assert "pending_handoff" not in client.get(f"/conversations/{cid}").json()


def test_the_run_lease_is_never_served(client, state):
    """Concurrency plumbing with no reader outside the coordinator."""
    cid, _ = _propose(client)
    state.begin_chat_run(cid, run_id="x", now=dt.datetime.now(dt.timezone.utc))

    assert "chat_run_lease" not in client.get(f"/conversations/{cid}").json()
    rows = client.get("/conversations").json()["conversations"]
    assert all("chat_run_lease" not in r for r in rows)
    assert all("nonce_digest" not in json.dumps(r, default=str) for r in rows)


def test_both_transports_report_which_crew_took_over(client, monkeypatch):
    """The caller named no crew in its request — the route lives in server
    state — so this is the only place it learns which crew answered."""
    cid, nonce = _propose(client)
    assert _redeem(client, cid, nonce).json()["crew_change"] == {
        "from": "explore", "to": "drift",
    }

    async def _stream(prompt, session_id=None, *, workload="drift",
                      autonomy_mode="propose_apply", prior_turns=None,
                      demo_anon=False, denied_tools=None):
        yield {"type": "result", "reply": "r", "tool_calls": [],
               "session_id": "sid"}

    cid2, nonce2 = _propose(client)
    monkeypatch.setattr("agent.adk_agent.run_chat_stream", _stream)
    r = client.post(
        "/chat/handoff",
        json={"conversation_id": cid2, "nonce": nonce2, "accept": True},
        headers={"Accept": "text/event-stream"},
    )
    assert _sse_done(r.text)["crew_change"] == {"from": "explore", "to": "drift"}


def test_no_ordinary_turn_can_slip_between_the_burn_and_the_joining_run(
    client, state, monkeypatch
):
    """The confirmation must never refuse AFTER spending its credential.

    Reserving the joining run in the same transaction that burns the nonce is
    what closes this: a /chat racing the confirm click now loses the lease
    rather than winning it and leaving the operator with a flipped crew and a
    409.
    """
    cid, nonce = _propose(client)
    raced: list[int] = []
    real_redeem = state.redeem_handoff

    def _redeem_then_race(*a, **kw):
        out = real_redeem(*a, **kw)
        # The instant redemption commits, an ordinary turn tries to take over.
        raced.append(_chat(client, workload="drift", cid=cid).status_code)
        return out

    monkeypatch.setattr(state, "redeem_handoff", _redeem_then_race)
    assert _redeem(client, cid, nonce).status_code == 200
    assert raced == [409], "the racing turn must lose, not the confirmation"


def test_the_joining_run_releases_its_reserved_lease(client, state):
    cid, nonce = _propose(client)
    assert _redeem(client, cid, nonce).status_code == 200
    # Immediately usable — no waiting on the lease TTL, and no GC-timed release.
    assert _chat(client, workload="drift", cid=cid).status_code == 200


def test_a_declined_handoff_reserves_nothing(client):
    cid, nonce = _propose(client)
    assert _redeem(client, cid, nonce, accept=False).status_code == 200
    assert _chat(client, cid=cid).status_code == 200
