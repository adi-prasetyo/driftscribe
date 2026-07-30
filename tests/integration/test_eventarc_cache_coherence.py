"""ds-q38 — the Eventarc audit must not file a verdict under a world it never
analyzed, and must not lose a fresh action to a cached ``no_op``.

The production incident these pin, verified from prod logs and by reproducing
all three live event keys:

    05:28:23     eventarc audit starts
    05:28:28.553 model read_live_env_tool -> rev 00011, PAYMENT_MODE=mock
    05:28:30     rev 00012 CREATED with PAYMENT_MODE=live   <-- lands MID-AUDIT
    05:28:40.505 final_response no_op, "no configuration drift is present"
    05:28:40.810 coordinator post-turn read -> rev 00012, live -> DRIFTED key
    05:28:40.924 the no_op is persisted under the DRIFTED env's key

A ``no_op`` row has no TTL (only rollbacks expire), so from that moment every
correct rollback proposal for ``PAYMENT_MODE=live`` was answered with a
day-old "no drift is present" — which is what happened on 2026-07-30.

The key is hashed from a read taken AFTER the agent turn, while the decision was
reasoned out over what the agent saw during it. The coordinator now brackets the
turn with its own reads: equal ends prove the world held still throughout, so
whatever the agent read in between saw the same env.

Tests drive the skew the way production does — the mocked Reader returns a
different env on the pre-turn and post-turn calls, which is exactly what a
deploy landing mid-audit produces. Note the call order with ``_run_adk_agent``
mocked: call 1 is the pre-turn read, call 2 the post-turn read (the agent makes
none of its own).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import agent.main as agent_main

from agent.config import get_settings
from agent.contract import load_contract
from agent.main import (
    _cached_decision_is_contradicted,
    _event_key,
    _hash_contract,
    _cached_decision_is_stale,
    _observation_skew,
    _reset_state_for_tests,
    app,
    get_state,
)
from agent.models import ContractStatus, DecisionAction, DecisionProposal, EnvDiff
from agent.state_store import FirestoreStateStore


_CLEAN_ENV = {"PAYMENT_MODE": "mock", "FEATURE_NEW_CHECKOUT": "false"}
_DRIFTED_ENV = {"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "false"}


def _reader_sequence(*envs: dict[str, str] | Exception):
    """Dispatch successive ``worker_client.call("reader", ...)`` results.

    The LAST entry repeats, so a test that only cares about a stable world
    passes one env. An ``Exception`` entry is raised for that call.
    """
    calls = {"n": 0}

    def dispatch(worker: str, *a: Any, **k: Any) -> Any:
        if worker != "reader":
            raise AssertionError(f"unexpected worker call: {worker!r}")
        i = min(calls["n"], len(envs) - 1)
        calls["n"] += 1
        item = envs[i]
        if isinstance(item, Exception):
            raise item
        return _reader_envelope(item)

    return dispatch


def _reader_envelope(env: dict[str, str]) -> dict[str, Any]:
    return {
        "service": "payment-demo",
        "region": "asia-northeast1",
        "project": "test-project",
        "env": env,
        "revision": "payment-demo-00042-cur",
    }


def _noop_proposal(live: str = "mock") -> DecisionProposal:
    """The verdict the model reached on 2026-07-29: everything matches.

    ``live`` is what the MODEL saw. The reader is mocked separately, so a test
    can make the two disagree the way the real deploy did.
    """
    return DecisionProposal(
        action=DecisionAction.NO_OP,
        env_diffs=[
            EnvDiff(
                name="FEATURE_NEW_CHECKOUT",
                expected="false",
                live="false",
                contract_status=ContractStatus.MATCH,
            ),
            EnvDiff(
                name="PAYMENT_MODE",
                expected="mock",
                live=live,
                contract_status=ContractStatus.MATCH,
            ),
        ],
        target_docs_file=None,
        target_docs_section=None,
        target_revision=None,
        rationale=(
            "The live configuration matches the declared operational contract "
            "exactly. No configuration drift is present."
        ),
        confidence=0.95,
        requires_human_review=False,
    )


def _rollback_proposal() -> DecisionProposal:
    return DecisionProposal(
        action=DecisionAction.ROLLBACK,
        env_diffs=[
            EnvDiff(
                name="PAYMENT_MODE",
                expected="mock",
                live="live",
                contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,
            )
        ],
        target_docs_file=None,
        target_docs_section=None,
        target_revision="payment-demo-00020-5qn",
        rationale="PAYMENT_MODE is live but the contract declares mock.",
        confidence=0.95,
        requires_human_review=True,
    )


@pytest.fixture(autouse=True)
def _adk_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()


# --------------------------------------------------------------------------- #
# The predicate itself
# --------------------------------------------------------------------------- #


def test_snapshots_that_agree_are_not_skew() -> None:
    assert _observation_skew(_CLEAN_ENV, _CLEAN_ENV) == []


def test_skew_names_the_var_the_deploy_changed_mid_turn() -> None:
    """The 2026-07-29 shape: the world was ``mock`` before the turn and ``live``
    after it."""
    assert _observation_skew(_DRIFTED_ENV, _CLEAN_ENV) == ["PAYMENT_MODE"]


def test_skew_sees_added_and_removed_variables() -> None:
    """A var appearing or vanishing mid-turn is skew just as much as a changed
    value — and is invisible to any check driven by the model's diff list."""
    assert _observation_skew(dict(_CLEAN_ENV, NEW="1"), _CLEAN_ENV) == ["NEW"]
    assert _observation_skew({"PAYMENT_MODE": "mock"}, _CLEAN_ENV) == [
        "FEATURE_NEW_CHECKOUT"
    ]


def test_no_post_turn_observation_is_not_skew() -> None:
    """``observed_env is None`` means the post-turn read failed, so the key is
    hashed from the pre-turn observation instead — the two are the same world
    by construction, and there is nothing to disagree about."""
    assert _observation_skew(None, _CLEAN_ENV) == []


# --------------------------------------------------------------------------- #
# Cause: a skewed decision is never persisted
# --------------------------------------------------------------------------- #


def test_a_noop_reasoned_over_stale_env_is_refused_and_records_nothing() -> None:
    """THE REGRESSION. Reproduces 2026-07-29 exactly: the model concludes
    ``no_op`` from the pre-deploy env while the coordinator's own read already
    sees the drift. Before this fix that verdict was persisted under the
    DRIFTED key and permanently outranked correct rollback proposals."""
    dispatch = _reader_sequence(_CLEAN_ENV, _DRIFTED_ENV)

    with (
        patch("agent.main._run_adk_agent",
              AsyncMock(return_value=_noop_proposal(live="mock"))),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        r = TestClient(app).post("/recheck")

    assert r.status_code == 409, r.text
    assert "live state changed while the agent was reasoning" in r.text
    # The whole point: nothing was written, so no key is poisoned.
    assert get_state().list_decisions(limit=50) == []


def test_a_noop_with_no_diffs_is_also_refused_when_the_world_moved() -> None:
    """Same incident, but with the proposal shape a diff-list check cannot see.

    ``no_op`` + ``env_diffs=[]`` is what the deterministic classifier emits and
    what the validator accepts, so nothing in the model's own report
    contradicts anything. Only the captured snapshot shows the world moved."""
    dispatch = _reader_sequence(_CLEAN_ENV, _DRIFTED_ENV)

    empty = DecisionProposal(
        action=DecisionAction.NO_OP,
        env_diffs=[],
        rationale="No configuration drift is present.",
        confidence=0.95,
    )
    with (
        patch("agent.main._run_adk_agent", AsyncMock(return_value=empty)),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        r = TestClient(app).post("/recheck")

    assert r.status_code == 409, r.text
    assert get_state().list_decisions(limit=50) == []


def test_the_refusal_leaks_neither_env_names_nor_values() -> None:
    """``HTTPException.detail`` is echoed to the caller, and this is exactly the
    text class that has carried live env values before."""
    dispatch = _reader_sequence(_CLEAN_ENV, {"PAYMENT_MODE": "sk-live-not-a-real-secret"})

    with (
        patch("agent.main._run_adk_agent",
              AsyncMock(return_value=_noop_proposal(live="mock"))),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        r = TestClient(app).post("/recheck")

    assert r.status_code == 409
    assert "sk-live-not-a-real-secret" not in r.text
    assert "PAYMENT_MODE" not in r.text


def test_a_flaky_reader_that_loses_the_baseline_refuses_rather_than_assumes() -> None:
    """Pre-turn read fails, post-turn read succeeds: there is no before-state to
    prove stability against.

    Assuming stability here is exactly how the poisoned row was written, so this
    refuses. It is distinct from the reader being down for BOTH reads, which is
    the documented no-observation tolerance and is NOT refused."""
    dispatch = _reader_sequence(RuntimeError("reader down"), _CLEAN_ENV)

    with (
        patch("agent.main._run_adk_agent",
              AsyncMock(return_value=_noop_proposal(live="mock"))),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        r = TestClient(app).post("/recheck")

    assert r.status_code == 409, r.text
    assert "before the agent ran" in r.text
    assert get_state().list_decisions(limit=50) == []


def test_a_reader_down_for_both_reads_still_produces_a_decision() -> None:
    """The documented tolerance (a coordinator without run.services.get) is
    preserved: with NO observation at all there is nothing for the coherence
    gate to compare, so it is skipped rather than failed, and ds-b3m's gate
    still refuses any rollback because ``observed_env`` stays None."""
    dispatch = _reader_sequence(RuntimeError("reader down"))

    with (
        patch("agent.main._run_adk_agent",
              AsyncMock(return_value=_noop_proposal(live="mock"))),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        r = TestClient(app).post("/recheck")

    assert r.status_code == 200, r.text
    assert r.json()["action"] == "no_op"


def test_the_key_survives_a_post_turn_read_failure_without_using_model_diffs() -> None:
    """Pre-turn read succeeded, post-turn failed: the key is hashed from the
    real pre-turn observation, not from ``proposal.env_diffs``.

    The old reconstruction was its own incoherence — a SUBSET of the env that
    can legally be empty, so a decision about a populated service could land
    under the `{}`-env key and collide with a genuinely empty one later. Here
    the proposal carries NO diffs at all, so a diff-derived key would be the
    empty-env key; asserting the recorded key differs from that pins it."""
    empty = DecisionProposal(
        action=DecisionAction.NO_OP,
        env_diffs=[],
        rationale="No configuration drift is present.",
        confidence=0.95,
    )
    dispatch = _reader_sequence(_CLEAN_ENV, RuntimeError("post-turn read down"))

    with (
        patch("agent.main._run_adk_agent", AsyncMock(return_value=empty)),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        r = TestClient(app).post("/recheck")

    assert r.status_code == 200, r.text
    recorded = get_state().list_decisions(limit=50)
    assert len(recorded) == 1
    key_from_real_env = _event_key(
        "manual_recheck", "payment-demo", get_settings().contract_path,
        _hash_contract(load_contract(Path(get_settings().contract_path))), _CLEAN_ENV,
    )
    assert recorded[0]["event_key"] == key_from_real_env


def test_a_coherent_noop_is_still_recorded_normally() -> None:
    """The gate must not fire on the happy path — the ordinary clean audit."""
    dispatch = _reader_sequence(_CLEAN_ENV)

    with (
        patch("agent.main._run_adk_agent",
              AsyncMock(return_value=_noop_proposal(live="mock"))),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        r = TestClient(app).post("/recheck")

    assert r.status_code == 200, r.text
    assert r.json()["action"] == "no_op"
    assert len(get_state().list_decisions(limit=50)) == 1


# --------------------------------------------------------------------------- #
# Harm: a cached no_op never outranks a fresh action
# --------------------------------------------------------------------------- #


def test_a_cached_noop_is_contradicted_by_any_fresh_action() -> None:
    cached = {"action": "no_op", "decision_id": "d1"}
    assert _cached_decision_is_contradicted(cached, _rollback_proposal()) is True
    assert _cached_decision_is_contradicted(cached, _noop_proposal()) is False
    assert _cached_decision_is_contradicted(None, _rollback_proposal()) is False


def test_a_cached_non_noop_is_left_alone() -> None:
    """Every other action already left an artifact — a PR, an issue, an
    approval. Re-proposing over one would duplicate it, which is the idempotency
    this cache exists to provide. ``no_op`` is the only action that persists
    'nothing is wrong' while creating nothing an operator can see."""
    for action in ("docs_pr", "drift_issue", "escalation", "rollback"):
        cached = {"action": action, "decision_id": "d1"}
        assert _cached_decision_is_contradicted(cached, _rollback_proposal()) is False


def test_stale_folds_expiry_and_contradiction_and_tolerates_none() -> None:
    assert _cached_decision_is_stale(None, _rollback_proposal()) is False
    assert _cached_decision_is_stale(
        {"action": "no_op", "decision_id": "d1"}, _rollback_proposal()
    ) is True
    assert _cached_decision_is_stale(
        {"action": "no_op", "decision_id": "d1"}, _noop_proposal()
    ) is False


def test_a_poisoned_noop_does_not_defeat_a_fresh_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end, the 2026-07-30 failure: a ``no_op`` already cached under the
    DRIFTED key must not be handed back to an audit that just proposed a
    rollback for that same drift. This is the half that neutralises the row
    already sitting in production, which is why no Firestore surgery is needed.
    """
    state = get_state()

    def dispatch(worker: str, *a: Any, **k: Any) -> Any:
        if worker == "reader":
            return _reader_envelope(_DRIFTED_ENV)
        if worker == "rollback":
            return {
                "approval_id": "8f14e45f-ceea-467a-9a3c-2b1d5f6a7b8c",
                "approval_token": "t" * 43,
                "approval_url": "/approvals/8f14e45f-ceea-467a-9a3c-2b1d5f6a7b8c?t=" + "t" * 43,
                "expires_at": "2099-01-01T00:00:00+00:00",
            }
        if worker == "notifier":
            return {"status": "ok", "channel": "approval", "severity": "high",
                    "downstream_status": 200}
        raise AssertionError(f"unexpected worker call: {worker!r}")

    # Pass 1: a coherent audit of the DRIFTED world proposes a rollback, so we
    # learn the real event key rather than hand-computing it.
    with (
        patch("agent.main._run_adk_agent",
              AsyncMock(return_value=_rollback_proposal())),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        first = TestClient(app).post("/recheck")
    assert first.status_code == 200, first.text
    assert first.json()["action"] == "rollback"
    drifted_key = first.json()["event_key"]

    # Now poison exactly as production was: a no_op filed under the DRIFTED key.
    _reset_state_for_tests()
    state = get_state()
    # Labelled with the trigger this key actually belongs to. The cache logic
    # is namespace-independent, but calling a manual_recheck key "eventarc"
    # would be a fixture that quietly describes the wrong world.
    state.record_event(drifted_key, {"trigger": "manual_recheck"})
    state.record_decision(
        "c76b85c8-poisoned", drifted_key,
        {"decision_id": "c76b85c8-poisoned", "action": "no_op",
         "rationale": "No configuration drift is present."},
    )

    with (
        patch("agent.main._run_adk_agent",
              AsyncMock(return_value=_rollback_proposal())),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        r = TestClient(app).post("/recheck")

    assert r.status_code == 200, r.text
    assert r.json()["action"] == "rollback", "the poisoned no_op was served again"
    assert r.json()["decision_id"] != "c76b85c8-poisoned"
    assert r.json().get("approval", {}).get("approval_url")


def test_a_proposal_that_fails_validation_does_not_evict_the_cached_row() -> None:
    """Eviction must come AFTER the safety gate, or a bad proposal can destroy
    good cached state and leave nothing behind that can be repaired.

    The trap: the CAS deletes the event POINTER, but the decision document
    survives. The Firestore store's recovery query then keeps resurfacing that
    document while ``evict_cached_decision`` — a compare-and-delete on a
    pointer that no longer exists — can never remove it again, so every later
    request takes the CAS-loser branch and 409s forever. Here the fresh
    rollback contradicts the cached ``no_op`` but names a MATCH diff, so the
    validator refuses it; the cached row must be exactly where it was."""
    state = get_state()
    key = "eventarc-payment-demo-testkey"
    state.record_event(key, {"trigger": "eventarc"})
    state.record_decision(
        "cached-noop", key,
        {"decision_id": "cached-noop", "action": "no_op",
         "rationale": "No configuration drift is present."},
    )

    unvalidatable = DecisionProposal(
        action=DecisionAction.ROLLBACK,
        env_diffs=[
            EnvDiff(name="PAYMENT_MODE", expected="mock", live="mock",
                    contract_status=ContractStatus.MATCH)
        ],
        target_revision="payment-demo-00020-5qn",
        rationale="claims a rollback with no confirmed violation",
        confidence=0.9,
        requires_human_review=True,
    )

    def dispatch(worker: str, *a: Any, **k: Any) -> Any:
        if worker == "reader":
            return _reader_envelope(_CLEAN_ENV)
        if worker == "rollback":
            raise AssertionError("no approval may be minted for an invalid proposal")
        raise AssertionError(f"unexpected worker call: {worker!r}")

    with (
        patch("agent.main._event_key", return_value=key),
        patch("agent.main._run_adk_agent",
              AsyncMock(return_value=unvalidatable)),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        r = TestClient(app).post("/recheck")

    assert r.status_code == 502, r.text
    assert "hard contract violation" in r.text
    # The cached decision survived, pointer intact and still resolvable.
    assert state.find_decision_for_event(key)["decision_id"] == "cached-noop"


def test_a_resurrected_firestore_row_is_still_recognised_as_stale() -> None:
    """The ds-bej interaction, pinned where it is testable.

    ``evict_cached_decision`` compare-and-deletes the event POINTER, but the
    decision document survives and ``FirestoreStateStore.find_decision_for_event``
    falls back to querying ``decisions.where("event_key","==",key)``. So between
    a CAS-evict and the winner's re-claim — or permanently, if the winner aborts
    — a lookup can resurrect the very row that was just declined. An in-memory
    store cannot exercise this: it has no fallback query.

    This test asserts the resurrection really happens, so the premise is not
    imagined, and that the shared predicate classifies the resurrected row as
    stale. It does NOT by itself prove the call sites consult that predicate —
    ``test_a_claim_loser_refuses_a_resurrected_noop`` covers one of them
    directly. Fixing the resurrection itself is ds-bej."""
    poisoned = {
        "decision_id": "c76b85c8-poisoned",
        "action": "no_op",
        "event_key": "eventarc-payment-demo-drifted",
        "rationale": "No configuration drift is present.",
    }

    mock_db = MagicMock()
    events, decisions = MagicMock(), MagicMock()
    mock_db.collection.side_effect = lambda n: events if n == "events" else decisions

    missing_pointer = MagicMock()
    missing_pointer.exists = False
    events.document.return_value.get.return_value = missing_pointer

    resurrected = MagicMock()
    resurrected.to_dict.return_value = poisoned
    decisions.where.return_value.limit.return_value.stream.return_value = iter(
        [resurrected]
    )

    store = FirestoreStateStore(project="p", client=mock_db)
    found = store.find_decision_for_event("eventarc-payment-demo-drifted")

    assert found == poisoned, "premise: the pointer is gone but the row comes back"
    assert _cached_decision_is_stale(found, _rollback_proposal()) is True


def test_a_claim_loser_refuses_a_resurrected_noop() -> None:
    """The call-site half of the resurrection story.

    A request can pass the cache lookup, lose the ``record_event`` claim to a
    concurrent run, and then re-read — and (per ds-bej) that re-read can hand
    back the contradicted ``no_op`` the store resurrected. Answering a fresh
    rollback with "no configuration drift is present" is the ds-q38 failure one
    layer down, so the site 409s instead. Removing the guard makes this fail."""
    poisoned = {"decision_id": "c76b85c8-poisoned", "action": "no_op",
                "rationale": "No configuration drift is present."}
    state = get_state()
    dispatch = _reader_sequence(_DRIFTED_ENV)

    with (
        patch("agent.main._run_adk_agent",
              AsyncMock(return_value=_rollback_proposal())),
        patch("agent.main.worker_client.call") as m_call,
        patch.object(state, "record_event", return_value=False),
        patch.object(state, "find_decision_for_event", return_value=poisoned),
    ):
        m_call.side_effect = dispatch
        r = TestClient(app).post("/recheck")

    assert r.status_code == 409, r.text
    assert "c76b85c8-poisoned" not in r.text


async def test_concurrent_contradicted_audits_mint_exactly_one_approval() -> None:
    """The property the deferred CAS exists for: two audits seeing the SAME
    contradicted ``no_op`` must not both re-propose.

    A sequential test makes "exactly one" true for free — only a concurrent one
    can catch a fall-through that double-mints an approval for a single drift.
    """
    state = get_state()
    key = "eventarc-payment-demo-concurrent"
    state.record_event(key, {"trigger": "eventarc"})
    state.record_decision(
        "cached-noop", key,
        {"decision_id": "cached-noop", "action": "no_op",
         "rationale": "No configuration drift is present."},
    )

    propose_calls: list[int] = []

    def dispatch(worker: str, *a: Any, **k: Any) -> Any:
        if worker == "reader":
            return _reader_envelope(_DRIFTED_ENV)
        if worker == "rollback":
            propose_calls.append(1)
            return {
                "approval_id": "8f14e45f-ceea-467a-9a3c-2b1d5f6a7b8c",
                "approval_token": "t" * 43,
                "approval_url":
                    "/approvals/8f14e45f-ceea-467a-9a3c-2b1d5f6a7b8c?t=" + "t" * 43,
                "expires_at": "2099-01-01T00:00:00+00:00",
            }
        if worker == "notifier":
            return {"status": "ok", "channel": "approval", "severity": "high",
                    "downstream_status": 200}
        raise AssertionError(f"unexpected worker call: {worker!r}")

    with (
        patch("agent.main._event_key", return_value=key),
        patch("agent.main._run_adk_agent",
              AsyncMock(return_value=_rollback_proposal())),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        results = await asyncio.gather(
            agent_main._do_recheck("eventarc", workload="drift"),
            agent_main._do_recheck("eventarc", workload="drift"),
            return_exceptions=True,
        )

    assert len(propose_calls) == 1, (
        f"one drift must mint one approval, got {len(propose_calls)}"
    )
    # The loser either returns the winner's rollback or 409s; it must never be
    # handed the contradicted no_op.
    for r in results:
        if isinstance(r, dict):
            assert r["action"] == "rollback"
            assert r["decision_id"] != "cached-noop"
        else:
            assert isinstance(r, HTTPException) and r.status_code == 409


def test_a_cached_noop_is_still_served_to_a_fresh_noop() -> None:
    """Idempotency must survive the fix: a repeated delivery for an unchanged
    clean world still gets the cached row, not a second LLM-driven record."""
    dispatch = _reader_sequence(_CLEAN_ENV)

    with (
        patch("agent.main._run_adk_agent",
              AsyncMock(return_value=_noop_proposal(live="mock"))),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        client = TestClient(app)
        first = client.post("/recheck")
        second = client.post("/recheck")

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["decision_id"] == second.json()["decision_id"]
    assert len(get_state().list_decisions(limit=50)) == 1
