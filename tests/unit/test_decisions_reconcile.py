"""GET /decisions' lazy reconcile hook (ds-2mc, Codex review round 2).

`/execute` terminalizes the approval doc itself on every path it survives, but
two residues escape it: an LRO that outran the 60s poll budget, and a container
death between persisting the operation handle and recording a result. Both leave
a doc in `applying`/`outcome_unknown` with a durable handle.

The rollback worker's `/reconcile` settles those — but an endpoint nothing calls
is exactly the defect ds-2mc was filed about (`operation_name` was written for a
poller that never existed, which is how a failed rollback stayed
indistinguishable from a successful one). These tests exist so that wiring
cannot silently rot back out.

Everything here is fail-soft by contract: this runs inside the operator's main
serve path, so no reconcile problem may ever break GET /decisions.
"""
from __future__ import annotations

import datetime as dt

import pytest

from agent import main as agent_main
from agent.main import _maybe_reconcile
from driftscribe_lib.approvals import (
    PHASE_APPLIED,
    PHASE_APPLYING,
    PHASE_CLAIMED,
    PHASE_FAILED,
    PHASE_OUTCOME_UNKNOWN,
    Approval,
)


def _approval(phase: str | None, *, operation_name: str | None = "operations/op-1") -> Approval:
    audit = None
    if phase is not None:
        detail = {"operation_name": operation_name} if operation_name else {}
        audit = {"phase": phase, "phase_at": dt.datetime.now(dt.timezone.utc), "detail": detail}
    return Approval(
        approval_id="ap-1",
        target_revision="payment-demo-00003-xyz",
        reason="r",
        token_hmac="deadbeef",
        expires_at=dt.datetime(2026, 7, 28, 12, 0, tzinfo=dt.timezone.utc),
        created_at=dt.datetime(2026, 7, 28, 11, 45, tzinfo=dt.timezone.utc),
        created_by="coordinator@x",
        status="used",
        apply_audit=audit,
    )


class _Store:
    """Returns `after` on the post-reconcile re-read, so a test can prove the
    served record is the REFRESHED one and not the stale one."""

    def __init__(self, after: Approval | None = None) -> None:
        self.after = after
        self.get_calls = 0

    def get(self, approval_id: str):  # noqa: ANN201
        self.get_calls += 1
        return self.after


@pytest.fixture
def calls(monkeypatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(
        agent_main.worker_client, "call_reconcile",
        lambda approval_id: seen.append(approval_id) or {"reconciled": True},
    )
    return seen


@pytest.mark.parametrize("phase", [PHASE_APPLYING, PHASE_OUTCOME_UNKNOWN])
def test_reconciles_a_non_terminal_doc_and_serves_the_refreshed_record(phase, calls) -> None:
    settled = _approval(PHASE_APPLIED)
    store = _Store(after=settled)

    out = _maybe_reconcile(store, "ap-1", _approval(phase), [3])

    assert calls == ["ap-1"]
    assert out is settled  # the refreshed record, not the stale one


@pytest.mark.parametrize("phase", [PHASE_APPLIED, PHASE_FAILED])
def test_does_not_reconcile_a_terminal_doc(phase, calls) -> None:
    record = _approval(phase)
    assert _maybe_reconcile(_Store(), "ap-1", record, [3]) is record
    assert calls == []


def test_does_not_reconcile_a_claimed_doc(calls) -> None:
    """Nothing was started, so there is no operation to ask about."""
    record = _approval(PHASE_CLAIMED, operation_name=None)
    assert _maybe_reconcile(_Store(), "ap-1", record, [3]) is record
    assert calls == []


def test_does_not_reconcile_without_an_operation_handle(calls) -> None:
    """A transport error around update_service records outcome_unknown with no
    handle. Spending a round-trip to be told there is nothing to look up helps
    nobody — and this case is why the desk copy says "verify in Cloud Run"."""
    record = _approval(PHASE_OUTCOME_UNKNOWN, operation_name=None)
    assert _maybe_reconcile(_Store(), "ap-1", record, [3]) is record
    assert calls == []


def test_pre_ds2mc_doc_without_apply_audit_is_untouched(calls) -> None:
    record = _approval(None)
    assert _maybe_reconcile(_Store(), "ap-1", record, [3]) is record
    assert calls == []


def test_budget_is_consumed_and_then_enforced(calls) -> None:
    budget = [2]
    store = _Store(after=_approval(PHASE_APPLIED))
    for _ in range(3):
        _maybe_reconcile(store, "ap-1", _approval(PHASE_OUTCOME_UNKNOWN), budget)
    # Third call finds the budget spent and does not reach the worker.
    assert len(calls) == 2
    assert budget == [0]


def test_worker_failure_degrades_to_the_stale_record(monkeypatch) -> None:
    """Fail-soft: this runs inside GET /decisions, the operator's main surface.
    Serving the un-reconciled record is honest on its own terms — an
    outcome_unknown renders as unconfirmed, never as success."""
    def _boom(approval_id: str):
        raise RuntimeError("worker down")

    monkeypatch.setattr(agent_main.worker_client, "call_reconcile", _boom)
    record = _approval(PHASE_OUTCOME_UNKNOWN)
    assert _maybe_reconcile(_Store(), "ap-1", record, [3]) is record


def test_the_reader_actually_invokes_the_reconcile_hook(calls, monkeypatch) -> None:
    """Pins the WIRING, not just the helper.

    Testing `_maybe_reconcile` in isolation cannot detect the failure that
    matters most here — the reader simply not calling it. That is precisely how
    the first cut of this shipped: the endpoint and client wrapper both existed,
    fully reviewable, with zero production callers. A mutation that reverts
    `_memoized_approval_reader` to a plain store read must fail HERE.
    """
    settled = _approval(PHASE_APPLIED)

    class _S:
        def get(self, approval_id: str):  # noqa: ANN201
            # First read (inside the reader) hands back the in-flight doc; the
            # post-reconcile re-read hands back the settled one.
            return settled if calls else _approval(PHASE_OUTCOME_UNKNOWN)

    monkeypatch.setattr(agent_main.approval_helpers, "get_approval_store", _S)

    read = agent_main._memoized_approval_reader()
    out = read("ap-1")

    assert calls == ["ap-1"], "the reader must invoke reconcile for a non-terminal doc"
    assert out.apply_audit["phase"] == PHASE_APPLIED


def test_the_reader_memoizes_and_does_not_reconcile_twice(calls, monkeypatch) -> None:
    class _S:
        def get(self, approval_id: str):  # noqa: ANN201
            return _approval(PHASE_OUTCOME_UNKNOWN)

    monkeypatch.setattr(agent_main.approval_helpers, "get_approval_store", _S)
    read = agent_main._memoized_approval_reader()
    read("ap-1")
    read("ap-1")
    assert calls == ["ap-1"]


def test_a_reconcile_that_resolves_nothing_still_serves_a_record(calls) -> None:
    """The worker legitimately answers "still running". The re-read returns a
    doc that is still non-terminal; that must be served, not dropped."""
    still_unknown = _approval(PHASE_OUTCOME_UNKNOWN)
    out = _maybe_reconcile(_Store(after=still_unknown), "ap-1", _approval(PHASE_OUTCOME_UNKNOWN), [3])
    assert out is still_unknown
    assert calls == ["ap-1"]
