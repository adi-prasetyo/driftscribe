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

The coordinator builds its idempotency key from a SECOND, INDEPENDENT reader
read taken after the agent turn. Every test here drives that skew directly:
``_run_adk_agent`` is mocked to return a proposal reasoned over env A while the
mocked reader returns env B, which is exactly what a deploy landing mid-audit
produces.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

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
from agent.request_context import record_analyzed_env
from agent.state_store import FirestoreStateStore


_CLEAN_ENV = {"PAYMENT_MODE": "mock", "FEATURE_NEW_CHECKOUT": "false"}
_DRIFTED_ENV = {"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "false"}


def _agent_that_read(
    analyzed: dict[str, str] | None, proposal: DecisionProposal
) -> AsyncMock:
    """An ``_run_adk_agent`` stand-in that ALSO records what the agent observed.

    The real ``read_live_env_tool`` calls ``record_analyzed_env`` at the tool
    boundary, so a mock that skips it would silently exercise the weaker
    diff-list fallback instead of the snapshot comparison the fix relies on.
    ``analyzed=None`` models an agent that never read live env.
    """

    async def _run(*_a: Any, **_k: Any) -> DecisionProposal:
        if analyzed is not None:
            record_analyzed_env(analyzed)
        return proposal

    return AsyncMock(side_effect=_run)


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
    assert _observation_skew(_noop_proposal(), _CLEAN_ENV, _CLEAN_ENV) == []


def test_snapshot_comparison_names_the_var_the_deploy_changed() -> None:
    """The 2026-07-29 shape: the agent analyzed ``mock``, the key was hashed
    from ``live``."""
    assert _observation_skew(_noop_proposal(), _DRIFTED_ENV, _CLEAN_ENV) == [
        "PAYMENT_MODE"
    ]


def test_an_empty_diff_list_cannot_hide_skew_from_the_snapshot() -> None:
    """The hole a diff-list-only check leaves open, and the reason the snapshot
    is the primary subject.

    ``no_op`` with ``env_diffs=[]`` is legal — the deterministic classifier
    emits exactly that and the validator accepts it — so iterating the model's
    diffs finds nothing to disagree with and pronounces the world coherent no
    matter how far it has moved. Comparing observations catches it."""
    empty = DecisionProposal(
        action=DecisionAction.NO_OP,
        env_diffs=[],
        rationale="no drift",
        confidence=0.9,
    )
    # The fallback is blind to it...
    assert _observation_skew(empty, _DRIFTED_ENV, None) == []
    # ...the snapshot comparison is not.
    assert _observation_skew(empty, _DRIFTED_ENV, _CLEAN_ENV) == ["PAYMENT_MODE"]


def test_snapshot_comparison_sees_added_and_removed_variables() -> None:
    """A var appearing or vanishing mid-audit is skew just as much as a changed
    value — and is invisible to any check driven by the model's diff list."""
    assert _observation_skew(
        _noop_proposal(), dict(_CLEAN_ENV, NEW="1"), _CLEAN_ENV
    ) == ["NEW"]
    assert _observation_skew(
        _noop_proposal(), {"PAYMENT_MODE": "mock"}, _CLEAN_ENV
    ) == ["FEATURE_NEW_CHECKOUT"]


def test_a_failed_reader_read_is_not_a_contradiction() -> None:
    """``observed_env is None`` is the reader-failed path, where ``live_env`` is
    reconstructed FROM the proposal — so key and proposal are coherent by
    construction and there is nothing independent to disagree with. Judging the
    model's report against itself would manufacture agreement, and the action
    that actually needs ground truth is already refused by ds-b3m's gate."""
    assert _observation_skew(_noop_proposal(live="mock"), None, _CLEAN_ENV) == []


def test_the_diff_fallback_applies_only_when_nothing_was_analyzed() -> None:
    """When the agent never read live env there is no snapshot to compare, so
    the model's report is all that is left. Weaker, and documented as such."""
    assert _observation_skew(_noop_proposal(live="mock"), _DRIFTED_ENV, None) == [
        "PAYMENT_MODE"
    ]
    assert _observation_skew(_noop_proposal(live="live"), _DRIFTED_ENV, None) == []


def test_the_fallback_treats_an_absent_var_as_presence_not_value() -> None:
    """``EnvDiff.live is None`` means ABSENT, so ``None != observed.get(name)``
    would be the wrong test — a var the model reported absent and that really is
    absent must not read as skew."""
    absent = DecisionProposal(
        action=DecisionAction.NO_OP,
        env_diffs=[
            EnvDiff(name="GONE", expected=None, live=None,
                    contract_status=ContractStatus.MATCH)
        ],
        rationale="nothing to do",
        confidence=0.9,
    )
    assert _observation_skew(absent, {"PAYMENT_MODE": "mock"}, None) == []
    assert _observation_skew(absent, {"GONE": ""}, None) == ["GONE"]


def test_the_fallback_compares_only_reported_vars() -> None:
    """The model reports a SUBSET (contract vars + what drifted). An unreported
    var present in the observation is not a contradiction on this path —
    otherwise every service with an extra env var would look skewed forever."""
    observed = dict(_CLEAN_ENV, UNRELATED="whatever")
    assert _observation_skew(_noop_proposal(live="mock"), observed, None) == []


# --------------------------------------------------------------------------- #
# Cause: a skewed decision is never persisted
# --------------------------------------------------------------------------- #


def test_a_noop_reasoned_over_stale_env_is_refused_and_records_nothing() -> None:
    """THE REGRESSION. Reproduces 2026-07-29 exactly: the model concludes
    ``no_op`` from the pre-deploy env while the coordinator's own read already
    sees the drift. Before this fix that verdict was persisted under the
    DRIFTED key and permanently outranked correct rollback proposals."""
    def dispatch(worker: str, *a: Any, **k: Any) -> Any:
        if worker == "reader":
            return _reader_envelope(_DRIFTED_ENV)
        raise AssertionError(f"unexpected worker call: {worker!r}")

    with (
        patch("agent.main._run_adk_agent",
              _agent_that_read(_CLEAN_ENV, _noop_proposal(live="mock"))),
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
    def dispatch(worker: str, *a: Any, **k: Any) -> Any:
        if worker == "reader":
            return _reader_envelope(_DRIFTED_ENV)
        raise AssertionError(f"unexpected worker call: {worker!r}")

    empty = DecisionProposal(
        action=DecisionAction.NO_OP,
        env_diffs=[],
        rationale="No configuration drift is present.",
        confidence=0.95,
    )
    with (
        patch("agent.main._run_adk_agent", _agent_that_read(_CLEAN_ENV, empty)),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        r = TestClient(app).post("/recheck")

    assert r.status_code == 409, r.text
    assert get_state().list_decisions(limit=50) == []


def test_the_refusal_leaks_neither_env_names_nor_values() -> None:
    """``HTTPException.detail`` is echoed to the caller, and this is exactly the
    text class that has carried live env values before."""
    def dispatch(worker: str, *a: Any, **k: Any) -> Any:
        if worker == "reader":
            return _reader_envelope({"PAYMENT_MODE": "sk-live-not-a-real-secret"})
        raise AssertionError(f"unexpected worker call: {worker!r}")

    with (
        patch("agent.main._run_adk_agent",
              _agent_that_read(_CLEAN_ENV, _noop_proposal(live="mock"))),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        r = TestClient(app).post("/recheck")

    assert r.status_code == 409
    assert "sk-live-not-a-real-secret" not in r.text
    assert "PAYMENT_MODE" not in r.text


def test_a_coherent_noop_is_still_recorded_normally() -> None:
    """The gate must not fire on the happy path — the ordinary clean audit."""
    def dispatch(worker: str, *a: Any, **k: Any) -> Any:
        if worker == "reader":
            return _reader_envelope(_CLEAN_ENV)
        raise AssertionError(f"unexpected worker call: {worker!r}")

    with (
        patch("agent.main._run_adk_agent",
              _agent_that_read(_CLEAN_ENV, _noop_proposal(live="mock"))),
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
              _agent_that_read(_DRIFTED_ENV, _rollback_proposal())),
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
    state.record_event(drifted_key, {"trigger": "eventarc"})
    state.record_decision(
        "c76b85c8-poisoned", drifted_key,
        {"decision_id": "c76b85c8-poisoned", "action": "no_op",
         "rationale": "No configuration drift is present."},
    )

    with (
        patch("agent.main._run_adk_agent",
              _agent_that_read(_DRIFTED_ENV, _rollback_proposal())),
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
              _agent_that_read(_CLEAN_ENV, unvalidatable)),
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

    This test asserts BOTH halves: the resurrection really happens (so the
    premise is not imagined), and every guard in ``_do_recheck`` still refuses
    to serve it, because they all ask ``_cached_decision_is_stale``. Fixing the
    resurrection itself is ds-bej."""
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


def test_a_cached_noop_is_still_served_to_a_fresh_noop() -> None:
    """Idempotency must survive the fix: a repeated delivery for an unchanged
    clean world still gets the cached row, not a second LLM-driven record."""
    def dispatch(worker: str, *a: Any, **k: Any) -> Any:
        if worker == "reader":
            return _reader_envelope(_CLEAN_ENV)
        raise AssertionError(f"unexpected worker call: {worker!r}")

    with (
        patch("agent.main._run_adk_agent",
              _agent_that_read(_CLEAN_ENV, _noop_proposal(live="mock"))),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        client = TestClient(app)
        first = client.post("/recheck")
        second = client.post("/recheck")

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["decision_id"] == second.json()["decision_id"]
    assert len(get_state().list_decisions(limit=50)) == 1
