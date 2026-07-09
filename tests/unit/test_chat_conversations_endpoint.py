"""Multi-turn /chat persistence + the conversations HTTP surface (P1)."""
import json

import pytest
from fastapi.testclient import TestClient

import agent.main as agent_main
from agent.auth import verify_token
from agent.state_store import InMemoryStateStore


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("USE_ADK", "true")
    agent_main.get_settings.cache_clear()
    # fresh state singleton per test
    fresh = InMemoryStateStore()
    monkeypatch.setattr(agent_main, "_state_singleton", fresh)
    monkeypatch.setattr(agent_main, "get_state", lambda: fresh)
    agent_main.app.dependency_overrides[verify_token] = lambda: None

    async def _run_chat(prompt, session_id=None, *, workload="drift",
                        autonomy_mode="propose_apply", prior_turns=None, demo_anon=False):
        # echo how many prior turns were seeded so tests can assert resume
        return {"reply": f"reply to {prompt} (seeded={len(prior_turns or [])})",
                "tool_calls": [], "session_id": "sid"}

    monkeypatch.setattr("agent.adk_agent.run_chat", _run_chat)
    monkeypatch.setattr(agent_main, "load_workload", lambda w: object())
    monkeypatch.setattr(agent_main, "_eager_resolve_upgrade_contract",
                        lambda r: None)
    yield TestClient(agent_main.app)
    agent_main.app.dependency_overrides.pop(verify_token, None)
    agent_main.get_settings.cache_clear()


def _post(client, prompt, workload="drift", conversation_id=None, ephemeral=None):
    body = {"prompt": prompt, "workload": workload}
    if conversation_id is not None:
        body["conversation_id"] = conversation_id
    if ephemeral is not None:
        body["ephemeral"] = ephemeral
    return client.post("/chat", json=body)  # JSON path (no SSE Accept header)


def _sse_done(text):
    """Parse the terminal ``done`` frame dict from an SSE body."""
    import json as _json
    for block in text.split("\n\n"):
        block = block.strip()
        if not block.startswith("event: done"):
            continue
        for line in block.splitlines():
            if line.startswith("data:"):
                return _json.loads(line[len("data:"):].strip())
    return None


def test_new_chat_creates_conversation_and_returns_id(client):
    r = _post(client, "first question")
    assert r.status_code == 200
    cid = r.json()["conversation_id"]
    assert cid
    lst = client.get("/conversations").json()["conversations"]
    assert any(c["conversation_id"] == cid for c in lst)
    assert lst[0]["title"] == "first question"
    assert lst[0]["workload"] == "drift"


def test_resume_seeds_prior_turns(client):
    cid = _post(client, "q1").json()["conversation_id"]
    r2 = _post(client, "q2", conversation_id=cid)
    # turn1 = user q1 + crew; so q2 should see 2 prior turns seeded
    assert "seeded=2" in r2.json()["reply"]
    conv = client.get(f"/conversations/{cid}").json()
    assert [t["role"] for t in conv["turns"]] == ["user", "crew", "user", "crew"]
    assert conv["turns"][0]["text"] == "q1"


def test_unknown_conversation_id_404(client):
    assert _post(client, "x", conversation_id="ghost").status_code == 404


def test_crew_lock_mismatch_409(client):
    cid = _post(client, "q1", workload="drift").json()["conversation_id"]
    r = _post(client, "q2", workload="explore", conversation_id=cid)
    assert r.status_code == 409


def test_get_unknown_conversation_404(client):
    assert client.get("/conversations/ghost").status_code == 404


def test_get_conversation_malformed_id_404(client):
    # path-escape / bad chars are treated as not-found, never reaching .document()
    assert client.get("/conversations/has%20space").status_code == 404


def test_conversations_list_limit_bounds(client):
    assert client.get("/conversations?limit=0").status_code == 400
    assert client.get("/conversations?limit=500").status_code == 400


def test_persist_failure_omits_conversation_id(client, monkeypatch):
    # A write failure must not break the reply AND must not hand back an id
    # that resolves to nothing.
    def _boom(*a, **k):
        raise RuntimeError("firestore down")

    monkeypatch.setattr(agent_main._state_singleton, "append_turns", _boom)
    r = _post(client, "q")
    assert r.status_code == 200
    assert "conversation_id" not in r.json()


def _set_paused(monkeypatch, paused):
    from types import SimpleNamespace
    monkeypatch.setattr(
        agent_main, "_pause_state_fail_closed",
        lambda: SimpleNamespace(paused=paused),
    )


def test_paused_unknown_conversation_id_404(client, monkeypatch):
    _set_paused(monkeypatch, True)
    assert _post(client, "x", conversation_id="ghost").status_code == 404


def test_paused_crew_lock_mismatch_409(client, monkeypatch):
    cid = _post(client, "q", workload="drift").json()["conversation_id"]
    _set_paused(monkeypatch, True)
    assert _post(client, "q", workload="explore",
                 conversation_id=cid).status_code == 409


# --- Ephemeral (don't-persist) probe turns ---------------------------------

def test_ephemeral_chat_returns_reply_but_persists_nothing(client):
    # The probe gets a real answer, but no conversation_id and no rail entry —
    # so repeated verification checks don't pile up in the operator's history.
    r = _post(client, "health probe", workload="explore", ephemeral=True)
    assert r.status_code == 200
    assert r.json()["reply"]
    assert "conversation_id" not in r.json()
    assert client.get("/conversations").json()["conversations"] == []


def test_ephemeral_with_conversation_id_is_422(client):
    # Continuing a durable thread while persisting nothing is contradictory.
    cid = _post(client, "q1", workload="explore").json()["conversation_id"]
    r = _post(client, "q2", workload="explore", conversation_id=cid,
              ephemeral=True)
    assert r.status_code == 422


def test_ephemeral_sse_done_omits_conversation_id(client, monkeypatch):
    # The SPA uses SSE by default — pin that the ``done`` frame carries no
    # conversation_id (vs. a normal SSE turn, which does) and nothing persists.
    async def _stub_stream(prompt, session_id=None, *, workload="drift",
                           autonomy_mode="propose_apply", prior_turns=None, demo_anon=False):
        yield {"type": "result", "reply": f"reply to {prompt}",
               "tool_calls": [], "session_id": "sid"}

    monkeypatch.setattr("agent.adk_agent.run_chat_stream", _stub_stream)
    headers = {"Accept": "text/event-stream"}

    # Control: a normal SSE turn DOES echo conversation_id + persists.
    normal = client.post("/chat", json={"prompt": "real", "workload": "explore"},
                         headers=headers)
    done = _sse_done(normal.text)
    assert done["conversation_id"]
    assert len(client.get("/conversations").json()["conversations"]) == 1

    # Ephemeral SSE turn: reply, but no conversation_id and no new rail entry.
    eph = client.post(
        "/chat",
        json={"prompt": "probe", "workload": "explore", "ephemeral": True},
        headers=headers,
    )
    done = _sse_done(eph.text)
    assert done["reply"] == "reply to probe"
    assert "conversation_id" not in done
    assert len(client.get("/conversations").json()["conversations"]) == 1


def test_ephemeral_provision_fanout_persists_nothing(client, monkeypatch):
    # Provision routes through the fan-out stream (a structurally distinct path);
    # the same persist chokepoint must keep ephemeral probes out of history.
    async def _stub_fanout(prompt, session_id=None, *, autonomy_mode="propose_apply",
                           prior_turns=None, demo_anon=False):
        yield {"type": "result", "reply": f"provisioned {prompt}",
               "tool_calls": [], "session_id": "sid"}

    monkeypatch.setattr("agent.fanout.run_provision_fanout_stream", _stub_fanout)
    r = _post(client, "probe", workload="provision", ephemeral=True)
    assert r.status_code == 200
    assert r.json()["reply"] == "provisioned probe"
    assert "conversation_id" not in r.json()
    assert client.get("/conversations").json()["conversations"] == []


# --------------------------------------------------------------------------- #
# Conversations are shared "team memory" — shared-seat BY DESIGN in the public
# window (operator-seat reversal 2026-07-09, docs/plans/2026-07-09-operator-seat-
# demo-window.md — audit M1 reversed). A persisted crew turn can carry a live ?t=
# approval link, and an anonymous reader is now handed it intact, same as the
# operator, so the rail's approve CTA works from a reloaded conversation too.
# --------------------------------------------------------------------------- #

_TOKEN_URL = "https://c/approvals/id1?t=SECRETTOKEN"


def _seed_token_reply_conversation(client, monkeypatch):
    """Create a conversation whose persisted crew turn (the reply) carries a
    live approval link — the realistic operator-authored-token case."""
    async def _tok_run_chat(prompt, session_id=None, *, workload="drift",
                            autonomy_mode="propose_apply", prior_turns=None,
                            demo_anon=False):
        return {"reply": f"Approve at {_TOKEN_URL}", "tool_calls": [],
                "session_id": "sid"}

    monkeypatch.setattr("agent.adk_agent.run_chat", _tok_run_chat)
    return _post(client, "roll back").json()["conversation_id"]


def test_get_conversation_demo_anonymous_keeps_token(client, monkeypatch):
    # Operator-seat reversal: the anonymous reader gets the full turns with the
    # live link intact, same as the operator.
    cid = _seed_token_reply_conversation(client, monkeypatch)
    r = client.get(f"/conversations/{cid}",
                   headers={"X-DriftScribe-Demo-Anonymous": "1"})
    dumped = json.dumps(r.json())
    assert "?t=SECRETTOKEN" in dumped
    assert "?t=<redacted>" not in dumped


def test_get_conversation_operator_keeps_token(client, monkeypatch):
    cid = _seed_token_reply_conversation(client, monkeypatch)
    r = client.get(f"/conversations/{cid}")
    assert "?t=SECRETTOKEN" in json.dumps(r.json())


def test_list_conversations_demo_anonymous_keeps_token(client):
    # The list surfaces title (= first prompt); after the reversal the anonymous
    # metadata rows carry the token intact too, same as the operator.
    _post(client, "rb https://c/approvals/id1?t=LISTTOK123")
    r = client.get("/conversations", headers={"X-DriftScribe-Demo-Anonymous": "1"})
    dumped = json.dumps(r.json())
    assert "?t=LISTTOK123" in dumped
    assert "?t=<redacted>" not in dumped


def test_list_conversations_operator_keeps_token(client):
    _post(client, "rb https://c/approvals/id1?t=LISTTOK123")
    r = client.get("/conversations")
    assert "?t=LISTTOK123" in json.dumps(r.json())
