"""Multi-turn /chat persistence + the conversations HTTP surface (P1)."""
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
                        autonomy_mode="propose_apply", prior_turns=None):
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


def _post(client, prompt, workload="drift", conversation_id=None):
    body = {"prompt": prompt, "workload": workload}
    if conversation_id is not None:
        body["conversation_id"] = conversation_id
    return client.post("/chat", json=body)  # JSON path (no SSE Accept header)


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
