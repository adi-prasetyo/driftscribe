"""Unit tests for the serve-time approval-status enrichment (Task 3.0b,
2026-07-28).

``attach_approval_status(decision, approval_reader=...)`` is the read-path
sibling of ``reconcile_merge_state`` / ``scrub_decision_rationale``: for a
rollback row (one that carries an ``approval`` sub-object with an
``approval_id``) it joins the approval doc's ``status`` and its resolution
timestamp (``resolved_at``, Part A of this task) into the served copy.
COMPUTE-ONLY — never persists, never mutates the input.

The whole point of this transform is HONEST degradation: a missing approval
doc, a store failure, or a pre-Part-A doc without a timestamp key must never
synthesize a ``resolved_at`` value (and must never fall back to the
decision's own ``created_at``, which is when the proposal was MADE, not when
a human resolved it).

No network/Firestore: an in-memory fake reader (a plain dict keyed by
approval_id) plays the role of ``ApprovalStore.get``, injected via the
``approval_reader`` seam so the transform's join logic is tested independent
of ``agent.approvals.get_approval_store``. The store's own read failure mode
is exercised via a reader that raises.
"""
from __future__ import annotations

import datetime as dt

import pytest

from agent.main import attach_approval_status, _memoized_approval_reader
from driftscribe_lib.approvals import Approval


def _approval(**kw) -> Approval:
    base = dict(
        approval_id="ap-1",
        target_revision="payment-demo-00003-xyz",
        reason="hard contract violation",
        token_hmac="deadbeef",
        expires_at=dt.datetime(2026, 7, 28, 12, 0, tzinfo=dt.timezone.utc),
        created_at=dt.datetime(2026, 7, 28, 11, 45, tzinfo=dt.timezone.utc),
        created_by="coordinator@x",
        status="pending",
    )
    base.update(kw)
    return Approval(**base)


def _rollback_row(approval_id="ap-1", **extra) -> dict:
    d = {
        "decision_id": "d-1",
        "action": "rollback",
        "approval": {
            "approval_id": approval_id,
            "approval_url": "https://x/approvals/ap-1?t=tok",
            "expires_at": "2026-07-28T12:00:00Z",
        },
    }
    d.update(extra)
    return d


def _reader(table: dict[str, "Approval | Exception"]):
    def read(approval_id: str):
        v = table.get(approval_id)
        if isinstance(v, Exception):
            raise v
        return v
    return read


# --------------------------------------------------------------------------- #
# happy paths
# --------------------------------------------------------------------------- #


def test_used_approval_gets_status_and_resolved_at():
    resolved = dt.datetime(2026, 7, 28, 12, 5, tzinfo=dt.timezone.utc)
    reader = _reader({"ap-1": _approval(status="used", resolved_at=resolved)})
    out = attach_approval_status(_rollback_row(), approval_reader=reader)
    assert out["approval"]["status"] == "used"
    assert out["approval"]["resolved_at"] == resolved


def test_pending_approval_gets_status_and_none_resolved_at():
    reader = _reader({"ap-1": _approval(status="pending", resolved_at=None)})
    out = attach_approval_status(_rollback_row(), approval_reader=reader)
    assert out["approval"]["status"] == "pending"
    assert out["approval"]["resolved_at"] is None


def test_denied_approval_gets_status_and_resolved_at():
    resolved = dt.datetime(2026, 7, 28, 12, 3, tzinfo=dt.timezone.utc)
    reader = _reader({"ap-1": _approval(status="denied", resolved_at=resolved)})
    out = attach_approval_status(_rollback_row(), approval_reader=reader)
    assert out["approval"]["status"] == "denied"
    assert out["approval"]["resolved_at"] == resolved


def test_preserves_existing_approval_siblings():
    reader = _reader({"ap-1": _approval(status="used", resolved_at=dt.datetime.now(dt.timezone.utc))})
    out = attach_approval_status(_rollback_row(), approval_reader=reader)
    assert out["approval"]["approval_url"] == "https://x/approvals/ap-1?t=tok"
    assert out["approval"]["expires_at"] == "2026-07-28T12:00:00Z"


# --------------------------------------------------------------------------- #
# honest degradation (Part C) — never synthesize a timestamp
# --------------------------------------------------------------------------- #


def test_old_doc_with_no_resolved_at_gets_status_only():
    """A doc predating Part A: status is known (dataclass default None for
    resolved_at). Must surface status, but resolved_at stays None — NEVER
    falls back to the decision's created_at."""
    reader = _reader({"ap-1": _approval(status="used", resolved_at=None)})
    out = attach_approval_status(
        _rollback_row(created_at="2026-07-28T11:00:00Z"), approval_reader=reader
    )
    assert out["approval"]["status"] == "used"
    assert out["approval"]["resolved_at"] is None


def test_missing_approval_doc_is_identity():
    reader = _reader({})  # ap-1 not found -> None
    row = _rollback_row()
    out = attach_approval_status(row, approval_reader=reader)
    assert out is row
    assert "status" not in out["approval"]


def test_raw_reader_exception_propagates_by_contract():
    """``attach_approval_status`` itself does NOT swallow a reader exception —
    mirrors ``reconcile_merge_state``, where the fail-soft try/except lives in
    the resolver (``_resolve_pr_merged``), not the transform. Here the
    fail-soft guarantee is ``_memoized_approval_reader``'s job (see below);
    this test pins that the transform trusts its injected reader."""
    reader = _reader({"ap-1": RuntimeError("firestore down")})
    row = _rollback_row()
    with pytest.raises(RuntimeError):
        attach_approval_status(row, approval_reader=reader)


# --------------------------------------------------------------------------- #
# entry-gate identity cases
# --------------------------------------------------------------------------- #


def test_non_dict_input_is_returned_as_is():
    reader = _reader({})
    assert attach_approval_status(None, approval_reader=reader) is None
    assert attach_approval_status("nope", approval_reader=reader) == "nope"


def test_no_approval_subobject_is_identity():
    reader = _reader({})
    row = {"decision_id": "d-2", "action": "drift_issue"}
    assert attach_approval_status(row, approval_reader=reader) is row


def test_approval_without_approval_id_is_identity():
    reader = _reader({})
    row = {"decision_id": "d-2", "action": "rollback", "approval": {"expires_at": "x"}}
    assert attach_approval_status(row, approval_reader=reader) is row


@pytest.mark.parametrize("bad_id", [None, "", 1, True])
def test_invalid_approval_id_is_identity(bad_id):
    reader = _reader({})
    row = _rollback_row(approval_id=bad_id)
    assert attach_approval_status(row, approval_reader=reader) is row


def test_approval_not_a_dict_is_identity():
    reader = _reader({})
    row = {"decision_id": "d-2", "action": "rollback", "approval": "not-a-dict"}
    assert attach_approval_status(row, approval_reader=reader) is row


# --------------------------------------------------------------------------- #
# never-mutate / copy-on-change
# --------------------------------------------------------------------------- #


def test_does_not_mutate_input():
    resolved = dt.datetime.now(dt.timezone.utc)
    reader = _reader({"ap-1": _approval(status="used", resolved_at=resolved)})
    row = _rollback_row()
    attach_approval_status(row, approval_reader=reader)
    assert "status" not in row["approval"]
    assert "resolved_at" not in row["approval"]


def test_returns_new_dict_not_same_object():
    reader = _reader({"ap-1": _approval(status="used", resolved_at=dt.datetime.now(dt.timezone.utc))})
    row = _rollback_row()
    out = attach_approval_status(row, approval_reader=reader)
    assert out is not row
    assert out["approval"] is not row["approval"]


# --------------------------------------------------------------------------- #
# _memoized_approval_reader — Part D (perf) + fail-soft wrapping
# --------------------------------------------------------------------------- #


def test_memoized_reader_calls_store_get_once_per_id(monkeypatch):
    calls = {"n": 0}

    class _FakeStore:
        def get(self, approval_id):
            calls["n"] += 1
            return _approval(approval_id=approval_id, status="used")

    from agent import main as main_mod
    monkeypatch.setattr(main_mod.approval_helpers, "get_approval_store", lambda: _FakeStore())

    reader = _memoized_approval_reader()
    a = reader("ap-1")
    b = reader("ap-1")
    c = reader("ap-2")
    assert a.status == "used" and b.status == "used" and c.status == "used"
    assert calls["n"] == 2  # ap-1 read once (memoized), ap-2 read once


def test_memoized_reader_is_fail_soft_on_store_error(monkeypatch):
    class _BoomStore:
        def get(self, approval_id):
            raise RuntimeError("firestore down")

    from agent import main as main_mod
    monkeypatch.setattr(main_mod.approval_helpers, "get_approval_store", lambda: _BoomStore())

    reader = _memoized_approval_reader()
    assert reader("ap-1") is None  # never raises


def test_memoized_reader_is_fail_soft_on_store_construction_error(monkeypatch):
    def _boom():
        raise RuntimeError("no credentials")

    from agent import main as main_mod
    monkeypatch.setattr(main_mod.approval_helpers, "get_approval_store", _boom)

    reader = _memoized_approval_reader()
    assert reader("ap-1") is None
