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


@pytest.fixture(autouse=True)
def _clean_reconcile_cooldown():
    """Both ds-7j0's cooldown and ds-ihi's terminal-approval cache are
    process-global dicts keyed by approval_id, and every test here uses the same
    ``ap-1``. Without this reset one test's attempt suppresses every later one
    (cooldown), or one test's settled doc is served to the next without a read
    at all (terminal cache) — either of which looks exactly like the wiring
    having rotted out, the failure these tests exist to catch."""
    agent_main._reset_reconcile_state_for_tests()
    agent_main._reset_terminal_approval_cache_for_tests()
    yield
    agent_main._reset_reconcile_state_for_tests()
    agent_main._reset_terminal_approval_cache_for_tests()


def _approval(
    phase: str | None,
    *,
    operation_name: str | None = "operations/op-1",
    age_s: float = 600.0,
) -> Approval:
    """`age_s` defaults to well past `_RECONCILE_MIN_AGE_S` so the common
    fixture is ELIGIBLE. A fresh doc is deliberately not reconcilable — see
    test_does_not_reconcile_a_fresh_doc."""
    audit = None
    if phase is not None:
        detail = {"operation_name": operation_name} if operation_name else {}
        audit = {
            "phase": phase,
            "phase_at": dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=age_s),
            "detail": detail,
        }
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


def test_does_not_reconcile_a_fresh_doc(calls) -> None:
    """The latency guard, and it is load-bearing rather than cosmetic.

    The rollback worker is --concurrency=1, so reconciling a rollback that
    /execute is still working on queues behind that very call and blocks for its
    remaining LRO budget. The operator's focus-return refresh lands in exactly
    that window, and overviewStore awaits /decisions alongside the graph and
    pending-list fetches — so one avoidable round-trip stalls the whole desk.
    /execute terminalizes its own doc; reconcile is for what it leaves behind.
    """
    record = _approval(PHASE_APPLYING, age_s=5.0)
    assert _maybe_reconcile(_Store(), "ap-1", record, [3]) is record
    assert calls == []


def test_pre_ds2mc_doc_without_apply_audit_is_untouched(calls) -> None:
    record = _approval(None)
    assert _maybe_reconcile(_Store(), "ap-1", record, [3]) is record
    assert calls == []


def test_budget_is_consumed_and_then_enforced(calls) -> None:
    # DISTINCT ids on purpose. The budget caps round-trips across the ROWS of one
    # GET /decisions, which in reality are distinct approvals; reusing one id here
    # would instead be measuring ds-7j0's per-approval cooldown and would pass at
    # len(calls) == 1 no matter what the budget did.
    budget = [2]
    store = _Store(after=_approval(PHASE_APPLIED))
    for approval_id in ("ap-1", "ap-2", "ap-3"):
        _maybe_reconcile(store, approval_id, _approval(PHASE_OUTCOME_UNKNOWN), budget)
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


# --------------------------------------------------------------------------- #
# ds-7j0 — the brakes on a row that will never settle.
#
# /reconcile writes nothing on its three non-settling exits, which is right (a
# re-stamped phase_at would slide the staleness clock and hide a stuck rollback),
# but it left the row eligible forever with no counter and no ceiling. The
# rollback worker is --concurrency=1 --max-instances=1 and also serves Approve,
# so "every GET /decisions, every 45s poll, per open tab" is a real queue in
# front of the button a judge is about to press.
# --------------------------------------------------------------------------- #


def test_a_second_request_for_the_same_approval_is_held_off_by_the_cooldown(calls) -> None:
    """The load fix: N tabs polling every 45s cost ONE attempt per cooldown."""
    store = _Store(after=_approval(PHASE_OUTCOME_UNKNOWN))
    for _ in range(5):
        # A fresh per-request budget each time — as a real sequence of GETs has.
        _maybe_reconcile(store, "ap-1", _approval(PHASE_OUTCOME_UNKNOWN), [3])
    assert calls == ["ap-1"]


def test_the_cooldown_is_per_approval_not_global(calls) -> None:
    """One row backing off must not silence a different row's first attempt."""
    store = _Store(after=_approval(PHASE_OUTCOME_UNKNOWN))
    _maybe_reconcile(store, "ap-1", _approval(PHASE_OUTCOME_UNKNOWN), [3])
    _maybe_reconcile(store, "ap-2", _approval(PHASE_OUTCOME_UNKNOWN), [3])
    assert calls == ["ap-1", "ap-2"]


def test_a_failed_attempt_still_starts_the_cooldown(calls, monkeypatch) -> None:
    """The attempt is marked BEFORE the call. A worker that is timing out is
    exactly the one that must not be retried by every tab on the next poll —
    marking only on success would invert the fix."""
    def _boom(approval_id: str):
        raise RuntimeError("worker down")

    monkeypatch.setattr(agent_main.worker_client, "call_reconcile", _boom)
    store = _Store(after=_approval(PHASE_OUTCOME_UNKNOWN))
    record = _approval(PHASE_OUTCOME_UNKNOWN)
    first = _maybe_reconcile(store, "ap-1", record, [3])
    second = _maybe_reconcile(store, "ap-1", record, [3])
    # Both still serve the honest un-reconciled record.
    assert first is record
    assert second is record


def test_a_day_old_unsettled_row_is_given_up_on(calls) -> None:
    """The ceiling. Past a day the answer will not change; this needs an
    operator, not another round-trip against the Approve worker."""
    old = _approval(PHASE_OUTCOME_UNKNOWN, age_s=25 * 60 * 60)
    out = _maybe_reconcile(_Store(after=_approval(PHASE_APPLIED)), "ap-1", old, [3])
    assert out is old
    assert calls == []


def test_a_row_just_inside_the_ceiling_is_still_reconciled(calls) -> None:
    recent = _approval(PHASE_OUTCOME_UNKNOWN, age_s=23 * 60 * 60)
    _maybe_reconcile(_Store(after=_approval(PHASE_APPLIED)), "ap-1", recent, [3])
    assert calls == ["ap-1"]


# --- ds-mml(3): the age gate's own failure modes ---------------------------- #


def test_an_unreadable_phase_at_fails_CLOSED(calls) -> None:
    """It used to fail OPEN — a non-datetime phase_at skipped the check and
    reconciled anyway, dropping the latency guard exactly when the doc is
    malformed. The guard is what keeps a reconcile from queueing behind the
    /execute that still owns the rollback."""
    record = _approval(PHASE_OUTCOME_UNKNOWN)
    record.apply_audit["phase_at"] = "2026-07-28T00:00:00Z"  # a string, not a datetime
    out = _maybe_reconcile(_Store(after=_approval(PHASE_APPLIED)), "ap-1", record, [3])
    assert out is record
    assert calls == []


def test_a_naive_phase_at_is_read_as_utc_not_raised(calls) -> None:
    """A naive datetime used to raise TypeError out of _maybe_reconcile, through
    the reader's blanket except, memoizing None for the row — so the desk lost
    status AND phase for it and went SILENT rather than showing the unresolved
    card, which is strictly worse than the state it was hiding.

    Naive values are read as UTC, this codebase's convention everywhere (see
    _utcnow). This one is a naive UTC instant, so it resolves to a real age and
    the row reconciles normally.
    """
    record = _approval(PHASE_OUTCOME_UNKNOWN)
    record.apply_audit["phase_at"] = dt.datetime.now(dt.timezone.utc).replace(
        tzinfo=None
    ) - dt.timedelta(seconds=600)
    out = _maybe_reconcile(_Store(after=_approval(PHASE_APPLIED)), "ap-1", record, [3])
    assert out.apply_audit["phase"] == PHASE_APPLIED  # eligible, not an exception
    assert calls == ["ap-1"]


def test_a_phase_at_that_reads_as_the_future_degrades_quietly(calls) -> None:
    """The other half of the naive case: a naive LOCAL timestamp read as UTC can
    land in the future (this repo's operator machine is JST, +9). A negative age
    fails the freshness check and the row is simply served un-reconciled — the
    safe direction, and still no exception."""
    record = _approval(PHASE_OUTCOME_UNKNOWN)
    record.apply_audit["phase_at"] = dt.datetime.now() + dt.timedelta(hours=9)
    out = _maybe_reconcile(_Store(after=_approval(PHASE_APPLIED)), "ap-1", record, [3])
    assert out is record  # enrichment intact; the desk still shows the card
    assert calls == []


# --------------------------------------------------------------------------- #
# ds-ihi — the N+1 on GET /decisions.
#
# The per-request memo keys on approval_id and every proposal mints a fresh one,
# so it collapsed nothing: ?limit=50 made one SEQUENTIAL Firestore read per
# rollback row, on the route overviewStore polls every 45s and on every focus.
# Terminal docs can never change again (record_phase refuses to overwrite a
# terminal phase; a denial is terminal at flip time), so they are the safe half
# to cache across requests.
# --------------------------------------------------------------------------- #


def _counting_store(record):
    class _S:
        reads = 0

        def get(self, approval_id: str):  # noqa: ANN201
            type(self).reads += 1
            return record

    return _S


@pytest.mark.parametrize("phase", [PHASE_APPLIED, PHASE_FAILED])
def test_a_terminal_approval_is_read_once_across_requests(phase, monkeypatch) -> None:
    store_cls = _counting_store(_approval(phase))
    monkeypatch.setattr(agent_main.approval_helpers, "get_approval_store", store_cls)

    for _ in range(4):  # four separate GET /decisions
        agent_main._memoized_approval_reader()("ap-1")

    assert store_cls.reads == 1


def test_a_denied_approval_is_also_terminal(monkeypatch) -> None:
    denied = _approval(None)
    denied.status = "denied"
    store_cls = _counting_store(denied)
    monkeypatch.setattr(agent_main.approval_helpers, "get_approval_store", store_cls)

    agent_main._memoized_approval_reader()("ap-1")
    agent_main._memoized_approval_reader()("ap-1")

    assert store_cls.reads == 1


@pytest.mark.parametrize("phase", [PHASE_APPLYING, PHASE_OUTCOME_UNKNOWN, PHASE_CLAIMED])
def test_a_non_terminal_approval_is_re_read_every_request(phase, monkeypatch, calls) -> None:
    """The whole point of /reconcile is that these DO change. Caching one would
    park an `outcome_unknown` on the desk permanently — the ds-2mc defect with
    extra steps."""
    store_cls = _counting_store(_approval(phase, operation_name=None))
    monkeypatch.setattr(agent_main.approval_helpers, "get_approval_store", store_cls)

    agent_main._memoized_approval_reader()("ap-1")
    agent_main._memoized_approval_reader()("ap-1")

    assert store_cls.reads == 2


def test_a_failed_read_is_never_cached_across_requests(monkeypatch) -> None:
    """A transient Firestore error memoizes None for the REQUEST (honest
    degradation) but must not persist — the next poll has to try again."""
    class _S:
        reads = 0

        def get(self, approval_id: str):  # noqa: ANN201
            type(self).reads += 1
            raise RuntimeError("transient")

    monkeypatch.setattr(agent_main.approval_helpers, "get_approval_store", _S)
    first = agent_main._memoized_approval_reader()
    assert first("ap-1") is agent_main._APPROVAL_READ_FAILED
    assert first("ap-1") is agent_main._APPROVAL_READ_FAILED  # memoized per-request
    assert agent_main._memoized_approval_reader()("ap-1") is agent_main._APPROVAL_READ_FAILED
    # One read per REQUEST — memoized within, never across.
    assert _S.reads == 2


def test_a_failed_read_is_distinguishable_from_a_missing_doc(monkeypatch) -> None:
    """ds-mml. The frontend's absent-status-means-pending rule is compat for
    pre-enrichment rows; applying it to a read that THREW re-offers a live
    Approve CTA on a burned approval, and the click dead-ends at the worker."""
    class _S:
        def get(self, approval_id: str):  # noqa: ANN201
            raise RuntimeError("transient")

    monkeypatch.setattr(agent_main.approval_helpers, "get_approval_store", _S)
    row = {"approval": {"approval_id": "ap-1", "approval_url": "https://x/approvals/ap-1"}}
    out = agent_main.attach_approval_status(
        row, approval_reader=agent_main._memoized_approval_reader()
    )
    assert out["approval"]["status_unavailable"] is True
    # And it must NOT invent a status.
    assert "status" not in out["approval"]


def test_a_genuinely_missing_doc_is_served_un_enriched_as_before(monkeypatch) -> None:
    """The other half: no doc is not a read failure, and must stay a byte-identical
    passthrough so pre-enrichment rows keep rendering their CTA."""
    class _S:
        def get(self, approval_id: str):  # noqa: ANN201
            return None

    monkeypatch.setattr(agent_main.approval_helpers, "get_approval_store", _S)
    row = {"approval": {"approval_id": "ap-1", "approval_url": "https://x/approvals/ap-1"}}
    out = agent_main.attach_approval_status(
        row, approval_reader=agent_main._memoized_approval_reader()
    )
    assert out is row
