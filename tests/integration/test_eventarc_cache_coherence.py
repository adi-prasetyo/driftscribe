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
reasoned out over what the agent saw during it. ``run_agent`` now reports every
``read_live_env_tool`` response back through ``reader_sink``, so the coordinator
compares what the agent ACTUALLY observed against what it is about to key the
row on, and refuses when they disagree.

The doubles here read live state the same way the real agent does — through the
patched ``worker_client.call("reader", {})`` — so the sequencing matches
production: call 1 is the agent's own read, call 2 is the coordinator's
post-turn read. A double that returned a proposal WITHOUT reading would model an
agent that never looked, which the coordinator refuses; keeping the doubles
faithful is the point, because this incident survived two earlier fixes
precisely because the doubles did not behave like the pipeline.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import agent.main as agent_main

from agent import worker_client
from agent.config import get_settings
from agent.main import (
    _cached_decision_is_contradicted,
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


def _adk(proposal: DecisionProposal, times: int = 1) -> AsyncMock:
    """A ``_run_adk_agent`` double that reads live state like the real agent.

    ``times`` > 1 models an agent that called ``read_live_env_tool`` more than
    once, which is how a turn observes the world changing under itself.
    """

    async def _run(*_a: Any, reader_sink: list | None = None, **_k: Any) -> Any:
        for _ in range(times):
            try:
                payload = worker_client.call("reader", {})
            except Exception:
                # DELIBERATE SIMPLIFICATION — see the note in
                # test_rollback_e2e._adk. In production a Reader failure inside
                # the tool aborts the turn (ADK re-raises; build_agent installs
                # no on_tool_error callback), so it surfaces as 502 and never
                # reaches the coherence gate. Pinned by
                # test_a_reader_failure_inside_the_turn_is_a_502.
                payload = None
            if (
                reader_sink is not None
                and isinstance(payload, dict)
                and isinstance(payload.get("env"), dict)
            ):
                reader_sink.append(
                    {"env": dict(payload["env"]),
                     "revision": payload.get("revision") or None}
                )
        return proposal

    return AsyncMock(side_effect=_run)


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


def test_a_noop_reasoned_over_stale_env_is_refused_and_records_nothing() -> None:
    """THE REGRESSION. Reproduces 2026-07-29 exactly: the model concludes
    ``no_op`` from the pre-deploy env while the coordinator's own read already
    sees the drift. Before this fix that verdict was persisted under the
    DRIFTED key and permanently outranked correct rollback proposals."""
    dispatch = _reader_sequence(_CLEAN_ENV, _DRIFTED_ENV)

    with (
        patch("agent.main._run_adk_agent",
              _adk(_noop_proposal(live="mock"))),
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
        patch("agent.main._run_adk_agent", _adk(empty)),
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
              _adk(_noop_proposal(live="mock"))),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        r = TestClient(app).post("/recheck")

    assert r.status_code == 409
    assert "sk-live-not-a-real-secret" not in r.text
    assert "PAYMENT_MODE" not in r.text


def test_the_same_env_on_a_different_revision_is_still_skew() -> None:
    """Env equality alone is a weaker claim than it looks.

    Revisions A(mock) -> B(live) -> C(mock) inside one turn present IDENTICAL
    env at both ends while the world moved twice, so an env-only check would
    call that stable and cache the agent's reasoning about B under the mock
    key. The serving revision is compared too, which is why ``read_live_state``
    pairs env with the revision it came from."""
    def dispatch(worker: str, *a: Any, **k: Any) -> Any:
        if worker != "reader":
            raise AssertionError(f"unexpected worker call: {worker!r}")
        dispatch.n = getattr(dispatch, "n", 0) + 1
        rev = "payment-demo-00020-aaa" if dispatch.n == 1 else "payment-demo-00022-ccc"
        return dict(_reader_envelope(_CLEAN_ENV), revision=rev)

    with (
        patch("agent.main._run_adk_agent",
              _adk(_noop_proposal(live="mock"))),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        r = TestClient(app).post("/recheck")

    assert r.status_code == 409, r.text
    assert "serving revision changed" in r.text
    assert get_state().list_decisions(limit=50) == []


def test_an_unreadable_revision_refuses_rather_than_abstains() -> None:
    """A check that cannot see its subject must FAIL, not wave the request
    through. ``read_live_state`` documents an empty ``revision`` as a real state
    (all deploys failed), so it is unknown for comparison purposes, never
    equal-to-empty."""
    def dispatch(worker: str, *a: Any, **k: Any) -> Any:
        if worker != "reader":
            raise AssertionError(f"unexpected worker call: {worker!r}")
        return dict(_reader_envelope(_CLEAN_ENV), revision="")

    with (
        patch("agent.main._run_adk_agent", _adk(_noop_proposal(live="mock"))),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        r = TestClient(app).post("/recheck")

    assert r.status_code == 409, r.text
    assert "serving revision changed" in r.text
    assert get_state().list_decisions(limit=50) == []


def test_an_agent_that_never_read_live_state_is_refused() -> None:
    """The hole bracketing could not close.

    Comparing the coordinator's own before/after reads proves the WORLD was
    stable; it never proves the agent LOOKED. An agent that skipped the reader
    entirely can still emit a legal ``no_op`` (``env_diffs=[]`` passes the
    validator), and that verdict would then be filed under whatever the
    coordinator happened to see. Reading the agent's own tool results answers
    both questions, so this refuses. The workload prompt does require the call,
    but a prompt is not an enforcement boundary."""
    dispatch = _reader_sequence(_CLEAN_ENV)

    with (
        # times=0: the agent never called the reader at all. Modelled as a
        # CHOICE, not as a failed read — a read that fails aborts the turn in
        # production (502) and is covered separately.
        patch("agent.main._run_adk_agent",
              _adk(_noop_proposal(live="mock"), times=0)),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        r = TestClient(app).post("/recheck")

    assert r.status_code == 409, r.text
    assert "the agent did not read live state" in r.text
    assert get_state().list_decisions(limit=50) == []


def test_an_agent_that_saw_the_world_change_itself_is_refused() -> None:
    """Two DIFFERENT observations in one turn: the agent watched the change
    happen, and there is no honest answer to which reading its conclusion rests
    on. ADK may run function calls in parallel, so arrival order is not
    authority either — so this refuses rather than picking one."""
    dispatch = _reader_sequence(_CLEAN_ENV, _DRIFTED_ENV, _DRIFTED_ENV)

    with (
        patch("agent.main._run_adk_agent", _adk(_noop_proposal(live="mock"), times=2)),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        r = TestClient(app).post("/recheck")

    assert r.status_code == 409, r.text
    assert "saw live state change" in r.text
    assert get_state().list_decisions(limit=50) == []


def test_a_failed_post_turn_read_refuses_instead_of_hashing_model_diffs() -> None:
    """The agent read fine, but this request could not, so nothing corroborates
    that what the agent saw still holds.

    The historical behaviour hashed the key from ``proposal.env_diffs`` here.
    That is a SUBSET the model chooses and can legally be empty, so a populated
    service's decision could land under the ``{}``-env key and collide with a
    genuinely empty one later — a second way to file a row under the wrong
    world. Refusing loses one audit, which the next event re-discovers; a
    poisoned key is permanent."""
    empty = DecisionProposal(
        action=DecisionAction.NO_OP,
        env_diffs=[],
        rationale="No configuration drift is present.",
        confidence=0.95,
    )
    dispatch = _reader_sequence(_CLEAN_ENV, RuntimeError("post-turn read down"))

    with (
        patch("agent.main._run_adk_agent", _adk(empty)),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        r = TestClient(app).post("/recheck")

    assert r.status_code == 409, r.text
    assert "live state could not be read after the agent ran" in r.text
    assert get_state().list_decisions(limit=50) == []


def test_a_coherent_noop_is_still_recorded_normally() -> None:
    """The gate must not fire on the happy path — the ordinary clean audit."""
    dispatch = _reader_sequence(_CLEAN_ENV)

    with (
        patch("agent.main._run_adk_agent",
              _adk(_noop_proposal(live="mock"))),
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
              _adk(_rollback_proposal())),
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
              _adk(_rollback_proposal())),
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
              _adk(unvalidatable)),
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


def test_a_failed_repair_leaves_the_key_retryable() -> None:
    """Codex round 5 blocker: a repair that dies mid-flight must not wedge the
    key permanently.

    This is the ds-bej resurrection followed through to its consequence, on the
    exact path that repairs the poisoned production row:

    1. Audit A judges the cached ``no_op`` stale and WINS the eviction, so the
       event pointer is deleted.
    2. The Rollback Worker's /propose then fails, so A records no replacement.
       The stale decision doc is still there, still carrying ``event_key``.
    3. Audit B looks up the key, misses on the pointer, and resurrects the
       stale row through the ``event_key`` recovery query.
    4. B judges it stale — correctly — and tries to evict.

    Under the pre-fix contract step 4 compared against a pointer that no longer
    existed, so it returned False forever and every later audit 409'd. One
    transient worker blip during a repair would have made the poisoning
    permanent, which is strictly worse than the bug being fixed: a wrong answer
    became no answer, on the only path that could have healed it.

    So the property asserted is not "the repair succeeds" — it is "a failed
    repair leaves the key in a state the NEXT delivery can still pick up".
    """
    key = "eventarc-payment-demo-failed-repair"
    stale = {"decision_id": "cached-noop", "action": "no_op",
             "event_key": key, "rationale": "No configuration drift is present."}

    state = get_state()
    state.record_event(key, {"trigger": "eventarc"})
    state.record_decision("cached-noop", key, dict(stale))

    # Step 3's resurrection: the in-memory store has no recovery query, so the
    # Firestore-shaped fallback is modelled by answering every lookup with the
    # stale row — which is precisely what production does once the pointer is
    # gone but the decision doc survives (test_a_resurrected_firestore_row_...
    # pins that the store really behaves this way).
    propose_calls: list[int] = []
    fail_propose = True

    def dispatch(worker: str, *a: Any, **k: Any) -> Any:
        if worker == "reader":
            return _reader_envelope(_DRIFTED_ENV)
        if worker == "rollback":
            propose_calls.append(1)
            if fail_propose:
                raise worker_client.WorkerClientError(
                    503, "rollback worker unavailable", "rollback"
                )
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
        patch("agent.main._run_adk_agent", _adk(_rollback_proposal())),
        patch("agent.main.worker_client.call") as m_call,
        patch.object(state, "find_decision_for_event", return_value=dict(stale)),
    ):
        m_call.side_effect = dispatch
        first = TestClient(app).post("/recheck")

    assert first.status_code >= 500, (
        f"premise: the repair must FAIL, got {first.status_code}"
    )
    assert propose_calls == [1], "premise: the repair got as far as /propose"
    # The wedge state, asserted against the REAL store (the patch above is gone
    # now): the eviction landed so the pointer is deleted, while the stale
    # decision document itself survives. In Firestore those two facts together
    # are what the recovery query turns back into a live cache hit; in-memory
    # has no such query, which is why the resurrection is patched in rather
    # than expected here.
    assert state.find_decision_for_event(key) is None, "the pointer was evicted"
    assert state.get_decision("cached-noop") is not None, "the stale row survives"

    # Step 4: the next delivery, reader healthy and worker recovered. Under the
    # old contract this 409'd; it must now get through and mint the repair.
    fail_propose = False
    with (
        patch("agent.main._event_key", return_value=key),
        patch("agent.main._run_adk_agent", _adk(_rollback_proposal())),
        patch("agent.main.worker_client.call") as m_call,
        patch.object(state, "find_decision_for_event", return_value=dict(stale)),
    ):
        m_call.side_effect = dispatch
        second = TestClient(app).post("/recheck")

    assert second.status_code == 200, (
        "a failed repair must not wedge the key at 409 forever; got "
        f"{second.status_code}: {second.text}"
    )
    assert second.json()["action"] == "rollback"
    assert second.json()["decision_id"] != "cached-noop"
    assert propose_calls == [1, 1], "the retry minted exactly one new approval"


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
              _adk(_rollback_proposal())),
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

    ⚠️ READ THE LIMIT BEFORE TRUSTING THIS. ``asyncio.gather`` does NOT actually
    interleave the two runs here, so this is weaker than its name suggests: it
    proves the two runs compose, not that the CAS wins a race. The reason is the
    invariant pinned by ``test_the_lookup_to_claim_window_contains_no_await``
    below — the whole lookup -> gate -> evict-CAS -> claim window is
    ``await``-free, so the first coroutine runs it to completion before the
    second is scheduled. Within one event loop that window is atomic BY
    CONSTRUCTION, and no same-process test can exercise it.

    The genuine cross-INSTANCE race is what Firestore's compare-and-delete and
    the ``record_event`` claim exist for, and it is covered deterministically by
    the loser tests (``test_a_claim_loser_refuses_a_resurrected_noop``, the
    CAS-loser re-read) which force the losing branch instead of hoping to hit
    it. Kept because "two audits compose without double-minting" is still worth
    pinning; not kept as evidence that the CAS is race-safe.
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
              _adk(_rollback_proposal())),
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


def test_the_contradicted_cas_loser_never_mints_a_second_approval() -> None:
    """Forced CAS-loser on the CONTRADICTED path — Codex round 5.

    The concurrency test above cannot reach this branch (the window is
    ``await``-free, see below), and the existing loser coverage is for EXPIRED
    ROLLBACKS, which get there by a different predicate: expiry is a property of
    the cached row alone, while contradiction is proposal-sensitive — the same
    cached ``no_op`` is stale against a rollback and perfectly servable against
    a fresh ``no_op``. Testing one does not test the other, so the losing branch
    is forced directly rather than raced for.

    What must hold when this run loses: it does NOT fall through to mint a
    second approval for one drift, and it does NOT hand back the contradicted
    ``no_op`` it just declined.
    """
    key = "eventarc-payment-demo-cas-loser"
    state = get_state()
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
            raise AssertionError("the CAS loser must not mint an approval")
        raise AssertionError(f"unexpected worker call: {worker!r}")

    with (
        patch("agent.main._event_key", return_value=key),
        patch("agent.main._run_adk_agent", _adk(_rollback_proposal())),
        patch("agent.main.worker_client.call") as m_call,
        # Lose the compare-and-delete: the pointer now names a DIFFERENT
        # decision, i.e. a concurrent run already installed its replacement.
        patch.object(state, "evict_cached_decision", return_value=False),
    ):
        m_call.side_effect = dispatch
        r = TestClient(app).post("/recheck")

    assert r.status_code == 409, r.text
    assert propose_calls == [], "the loser must not re-propose"
    assert "cached-noop" not in r.text, "and must not serve the row it declined"


def test_the_lookup_to_claim_window_contains_no_await() -> None:
    """The invariant the test above LEANS ON, pinned so it cannot rot silently.

    ``_do_recheck`` reads the cached decision, validates, runs the coherence
    gate, compare-and-deletes a stale row, then claims the event — and does all
    of it without a single ``await``. That is what makes the window atomic
    inside one event loop: no other request in the same worker can observe the
    half-state where the stale row is gone but the claim is not yet taken.

    Nothing in the code SAYS so, though — it is an emergent property of the
    statements that happen to sit there. Add one innocuous ``await`` in the
    middle (make ``find_decision_for_event`` async, wrap a Firestore call in
    ``asyncio.to_thread``, await a notifier) and the atomicity is gone with no
    other visible change.

    **What was actually measured, so this docstring does not overclaim.** An
    ``await asyncio.sleep(0)`` injected at the most dangerous point — after the
    cache lookup, before the deferred evict-CAS — made THIS test fail (line
    reported correctly) and left the concurrency test above still passing, with
    no double-mint: ``evict_cached_decision``'s compare-and-delete serialized
    the loser exactly as designed. So the demonstrated consequence of breaking
    this invariant is "the atomicity the concurrency test leans on is gone",
    NOT "a drift mints two approvals". The CAS is the thing that would have to
    fail for that, and it held under injection.

    This is therefore a conservative tripwire on an assumption, not a
    proven-exploit guard. If it fails, do not relax it: re-derive whether the
    CAS still serializes losers across the new suspension point, and make the
    concurrency test genuinely interleave before deciding the await is safe.
    """
    source = Path(inspect.getsourcefile(agent_main)).read_text(encoding="utf-8")
    tree = ast.parse(source)

    fn = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_do_recheck"
        ),
        None,
    )
    assert fn is not None, "premise: _do_recheck is an async def in agent/main.py"

    def _first_call(pred) -> ast.Call:
        found = [
            n for n in ast.walk(fn) if isinstance(n, ast.Call) and pred(n.func)
        ]
        assert found, "premise: the anchoring call still exists in _do_recheck"
        return min(found, key=lambda n: n.lineno)

    key_call = _first_call(
        lambda f: isinstance(f, ast.Name) and f.id == "_event_key"
    )
    claim_call = _first_call(
        lambda f: isinstance(f, ast.Attribute) and f.attr == "record_event"
    )
    assert key_call.lineno < claim_call.lineno, (
        "premise: the key is built before the event is claimed"
    )

    offenders = sorted(
        n.lineno
        for n in ast.walk(fn)
        if isinstance(n, ast.Await) and key_call.lineno < n.lineno < claim_call.lineno
    )
    assert not offenders, (
        "agent/main.py:{}-{} (_event_key -> record_event) must stay await-free; "
        "found await at line(s) {}. Two concurrent audits in one instance can "
        "now interleave between the cache lookup, the stale-row eviction and "
        "the claim — the atomicity the concurrency test leans on is gone. "
        "Whether that is exploitable depends on the CAS still serializing the "
        "loser; re-derive it, do not assume it. See this test's docstring."
        .format(key_call.lineno, claim_call.lineno, offenders)
    )


def test_a_cached_noop_is_still_served_to_a_fresh_noop() -> None:
    """Idempotency must survive the fix: a repeated delivery for an unchanged
    clean world still gets the cached row, not a second LLM-driven record."""
    dispatch = _reader_sequence(_CLEAN_ENV)

    with (
        patch("agent.main._run_adk_agent",
              _adk(_noop_proposal(live="mock"))),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        client = TestClient(app)
        first = client.post("/recheck")
        second = client.post("/recheck")

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["decision_id"] == second.json()["decision_id"]
    assert len(get_state().list_decisions(limit=50)) == 1


def test_a_reader_failure_inside_the_turn_is_a_502() -> None:
    """Production behaviour the doubles above deliberately simplify away.

    ``read_live_env_tool`` does not catch ``WorkerClientError``, and ADK
    re-raises a tool exception when no ``on_tool_error`` callback handles it —
    ``build_agent`` installs none. So a Reader that is down during the agent's
    own read ABORTS the turn: ``_do_recheck`` maps it to ``502 adk agent
    failed`` and never reaches the coherence gate at all.

    Worth pinning because the doubles swallow that failure, and a reader of
    those tests could otherwise conclude the 409 "did not read live state" path
    covers a reader outage. It does not — that path is for an agent that had a
    working reader and did not use it.
    """
    async def _raising_agent(*_a: Any, **_k: Any) -> DecisionProposal:
        raise worker_client.WorkerClientError(503, "reader unreachable", "reader")

    with (
        patch("agent.main._run_adk_agent", AsyncMock(side_effect=_raising_agent)),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = _reader_sequence(_CLEAN_ENV)
        r = TestClient(app).post("/recheck")

    assert r.status_code == 502, r.text
    assert "adk agent failed" in r.text
    assert get_state().list_decisions(limit=50) == []
