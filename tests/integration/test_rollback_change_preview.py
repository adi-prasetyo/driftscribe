"""Integration tests for the approval page's "what this rollback will change"
section and its acknowledgment gate (ds-uwc).

A rollback reverts the target revision's ENTIRE env, and before ds-uwc nothing
anywhere read that revision's config — the operator approved a revision NAME and
nothing else. These tests cover the two operator-facing consequences:

- **GET** renders the change preview, and renders *unknown* as an explicit note
  rather than an empty table. An empty table reads as "nothing will change",
  which is a promise the page cannot make.
- **POST approve** re-derives, from the immutable approval doc, whether the
  target PROVABLY violates the contract, and refuses without the
  acknowledgment. The acknowledgment is a speed bump on a deliberate click, not
  an authorization control — authorization remains the single-use HMAC token the
  worker verifies — so the tests also pin where it deliberately does NOT apply.

Mocking mirrors ``tests/integration/test_approvals.py``: an in-memory approval
store plus fake ``call_execute`` / ``call_deny``, so no real token is minted and
no worker is contacted.
"""
from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent import approvals as approval_helpers
from agent import worker_client
from agent.config import get_settings
from agent.contract import contract_hash, load_contract
from agent.main import app
from driftscribe_lib.approvals import Approval

_CONTRACT_YAML = """
service: payment-demo
environment: production
cloud_run_service: payment-demo
region: asia-northeast1
github_repo: adi-prasetyo/driftscribe
expected_env:
  PAYMENT_MODE:
    value: "mock"
    docs:
      file: demo/docs/runbook.md
      section: Runtime Configuration
    allow_manual_change: false
  FEATURE_NEW_CHECKOUT:
    value: "false"
    docs:
      file: demo/docs/runbook.md
      section: Feature Flags
    allow_manual_change: true
    operator_note: "Operator-toggleable."
"""


class _FakeApprovalStore:
    """In-memory store. ``raises`` makes :meth:`get` blow up, which is how the
    always-200 GET guard and the fail-open ack read are exercised."""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.raises: Exception | None = None
        self.get_calls = 0

    def create_pending(
        self,
        *,
        target_revision: str = "payment-demo-00015-sgt",
        reason: str = "restore contract compliance",
        env_snapshot: dict[str, Any] | None = None,
        ttl_minutes: int = 15,
    ) -> Approval:
        approval_id = str(uuid.uuid4())
        now = dt.datetime.now(dt.timezone.utc)
        self.docs[approval_id] = {
            "status": "pending",
            "target_revision": target_revision,
            "reason": reason,
            "token_hmac": "fake-hmac",
            "expires_at": now + dt.timedelta(minutes=ttl_minutes),
            "created_at": now,
            "created_by": "coordinator@test",
            "env_snapshot": env_snapshot,
        }
        return Approval(approval_id=approval_id, **self.docs[approval_id])

    def get(self, approval_id: str) -> Approval | None:
        self.get_calls += 1
        if self.raises is not None:
            raise self.raises
        if approval_id not in self.docs:
            return None
        return Approval(approval_id=approval_id, **self.docs[approval_id])

    def claim_pending(self, approval_id: str) -> Approval | None:
        data = self.docs.get(approval_id)
        if not data or data.get("status") != "pending":
            return None
        data["status"] = "used"
        return Approval(approval_id=approval_id, **data)

    def claim_denied(self, approval_id: str) -> Approval | None:
        data = self.docs.get(approval_id)
        if not data or data.get("status") != "pending":
            return None
        data["status"] = "denied"
        return Approval(approval_id=approval_id, **data)


@pytest.fixture
def contract_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "ops-contract.yaml"
    p.write_text(_CONTRACT_YAML, encoding="utf-8")
    monkeypatch.setenv("CONTRACT_PATH", str(p))
    get_settings.cache_clear()
    yield p
    get_settings.cache_clear()


@pytest.fixture
def good_hash(contract_file: Path) -> str:
    return contract_hash(load_contract(contract_file))


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _FakeApprovalStore:
    s = _FakeApprovalStore()
    monkeypatch.setattr(approval_helpers, "get_approval_store", lambda: s)
    return s


@pytest.fixture
def executed(monkeypatch: pytest.MonkeyPatch, store: _FakeApprovalStore) -> list:
    calls: list[tuple[str, str]] = []

    def fake_execute(approval_id: str, approval_token: str) -> dict:
        calls.append((approval_id, approval_token))
        store.claim_pending(approval_id)
        return {
            "approval_id": approval_id,
            "target_revision": "payment-demo-00015-sgt",
            "status": "executed",
            "operation_name": "operations/fake-op",
        }

    monkeypatch.setattr(worker_client, "call_execute", fake_execute)
    return calls


@pytest.fixture
def denied(monkeypatch: pytest.MonkeyPatch, store: _FakeApprovalStore) -> list:
    calls: list[tuple[str, str]] = []

    def fake_deny(approval_id: str, approval_token: str) -> dict:
        calls.append((approval_id, approval_token))
        store.claim_denied(approval_id)
        return {"approval_id": approval_id, "status": "denied"}

    monkeypatch.setattr(worker_client, "call_deny", fake_deny)
    return calls


@pytest.fixture
def client(store, executed, denied, contract_file) -> TestClient:
    return TestClient(app)


def _snapshot(
    hash_: str,
    *,
    payment_matches: bool = True,
    feature_changed: bool = False,
    changed_names: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "source_revision": "payment-demo-00016-w9k",
        "target_revision": "payment-demo-00015-sgt",
        "observed_at": "2026-07-29T12:23:56+00:00",
        "contract_hash": hash_,
        "changed_names": changed_names if changed_names is not None else ["PAYMENT_MODE"],
        "contract_vars": {
            "PAYMENT_MODE": {"changed": True, "target_matches_contract": payment_matches},
            "FEATURE_NEW_CHECKOUT": {
                "changed": feature_changed,
                "target_matches_contract": True,
            },
        },
    }


# --------------------------------------------------------------------------- #
# GET — the preview
# --------------------------------------------------------------------------- #


def test_get_renders_the_change_table_for_a_recorded_snapshot(client, store, good_hash):
    approval = store.create_pending(env_snapshot=_snapshot(good_hash))
    r = client.get(f"/approvals/{approval.approval_id}?t=tok")
    assert r.status_code == 200
    body = r.text
    assert 'data-testid="change-row"' in body
    assert 'data-var="PAYMENT_MODE"' in body
    assert 'data-var="FEATURE_NEW_CHECKOUT"' in body
    # The two revisions being compared are named, so "rollback to X" is no
    # longer the only thing the operator has.
    assert "payment-demo-00016-w9k" in body
    assert 'data-testid="change-unknown"' not in body


def test_every_governed_row_states_where_the_rollback_lands(client, store, good_hash):
    """"will change" without a destination is half an answer. Each
    contract-governed var says which value the target holds — the contract's own
    literal, which is public — whether or not it matches."""
    approval = store.create_pending(env_snapshot=_snapshot(good_hash, payment_matches=False))
    body = client.get(f"/approvals/{approval.approval_id}?t=tok").text
    # PAYMENT_MODE misses the contract value; FEATURE_NEW_CHECKOUT holds it.
    assert 'data-testid="change-target-mismatch"' in body
    assert 'data-testid="change-target-match"' in body


def test_get_renders_unknown_as_a_note_never_as_an_empty_table(client, store):
    """The central property. An approval minted before ds-uwc has no snapshot;
    the page must SAY it could not work out the change, not render a clean
    empty table that reads as "nothing will change"."""
    approval = store.create_pending(env_snapshot=None)
    r = client.get(f"/approvals/{approval.approval_id}?t=tok")
    assert r.status_code == 200
    assert 'data-testid="change-unknown"' in r.text
    assert 'data-testid="change-row"' not in r.text
    assert "was not recorded" in r.text


def test_get_distinguishes_a_missing_contract_from_a_missing_snapshot(
    client, store, good_hash, tmp_path, monkeypatch
):
    """Two different unknowns send the operator to two different places, so the
    page must not collapse them into one message."""
    approval = store.create_pending(env_snapshot=_snapshot(good_hash))
    monkeypatch.setenv("CONTRACT_PATH", str(tmp_path / "gone.yaml"))
    get_settings.cache_clear()
    r = client.get(f"/approvals/{approval.approval_id}?t=tok")
    assert r.status_code == 200
    assert 'data-testid="change-unknown"' in r.text
    assert "contract could not be loaded" in r.text


def test_get_says_contract_changed_when_the_contract_moved(
    client, store, good_hash, contract_file
):
    approval = store.create_pending(env_snapshot=_snapshot(good_hash))
    contract_file.write_text(
        _CONTRACT_YAML.replace('value: "mock"', 'value: "sandbox"'), encoding="utf-8"
    )
    r = client.get(f"/approvals/{approval.approval_id}?t=tok")
    assert "contract has changed" in r.text


def test_get_shows_the_acknowledgment_only_for_a_violating_target(
    client, store, good_hash
):
    ok = store.create_pending(env_snapshot=_snapshot(good_hash))
    assert 'data-testid="violates-ack"' not in client.get(
        f"/approvals/{ok.approval_id}?t=tok"
    ).text

    bad = store.create_pending(env_snapshot=_snapshot(good_hash, payment_matches=False))
    body = client.get(f"/approvals/{bad.approval_id}?t=tok").text
    assert 'data-testid="violates-ack"' in body
    assert 'data-testid="change-violates"' in body
    assert 'name="ack_target_violates_contract"' in body


def test_get_flags_a_rollback_that_reverts_an_operator_managed_var(
    client, store, good_hash
):
    approval = store.create_pending(
        env_snapshot=_snapshot(good_hash, feature_changed=True)
    )
    body = client.get(f"/approvals/{approval.approval_id}?t=tok").text
    assert 'data-testid="change-operator-set"' in body


def test_get_lists_changed_vars_the_contract_does_not_govern(client, store, good_hash):
    approval = store.create_pending(
        env_snapshot=_snapshot(
            good_hash, changed_names=["PAYMENT_MODE", "SENTRY_DSN", "LOG_LEVEL"]
        )
    )
    body = client.get(f"/approvals/{approval.approval_id}?t=tok").text
    assert 'data-testid="change-other"' in body
    assert "SENTRY_DSN" in body and "LOG_LEVEL" in body


def test_get_stays_200_when_the_store_read_fails(client, store):
    """The handler is contractually always-200 so a probe cannot use the status
    code as a presence oracle. Until ds-uwc this read was unguarded and a
    Firestore blip produced a 500 — which is exactly that oracle."""
    store.raises = RuntimeError("firestore unavailable")
    r = client.get("/approvals/11111111-1111-1111-1111-111111111111?t=tok")
    assert r.status_code == 200
    assert "not found" in r.text.lower()
    assert 'value="approve"' not in r.text


@pytest.mark.parametrize(
    "snapshot_kind", ["ok", "violating", "absent"], ids=["ok", "violating", "absent"]
)
def test_japanese_renders_for_every_change_state(client, store, good_hash, snapshot_kind):
    """Every state the section can be in has JA copy — an operator who switched
    the page to Japanese must not fall back to an English sentence for the one
    state that matters most."""
    snap = {
        "ok": _snapshot(good_hash),
        "violating": _snapshot(good_hash, payment_matches=False),
        "absent": None,
    }[snapshot_kind]
    approval = store.create_pending(env_snapshot=snap)
    body = client.get(f"/approvals/{approval.approval_id}?t=tok&lang=ja").text
    assert "このロールバックで変わるもの" in body
    # No English leaks from the branch under test.
    assert "What this rollback will change" not in body
    if snapshot_kind == "violating":
        assert "チェック" in body


def test_no_observed_env_value_reaches_the_rendered_page(client, store, good_hash):
    """The page is reachable by anyone holding the link, so this is asserted on
    the HTML, not just on the view dict."""
    snap = _snapshot(good_hash)
    snap["contract_vars"]["PAYMENT_MODE"]["source_value"] = "sk_live_LEAKED"
    snap["source_env"] = {"PAYMENT_MODE": "sk_live_LEAKED"}
    approval = store.create_pending(env_snapshot=snap)
    body = client.get(f"/approvals/{approval.approval_id}?t=tok").text
    assert "sk_live_LEAKED" not in body
    # The contract's own literal IS shown — it is public, and it is the value
    # the rollback lands on, which is the question the operator is answering.
    assert 'data-testid="change-target-match"' in body
    assert "mock" in body


# --------------------------------------------------------------------------- #
# POST — the acknowledgment gate
# --------------------------------------------------------------------------- #


def test_approving_a_violating_target_without_the_tick_is_refused(
    client, store, good_hash, executed
):
    approval = store.create_pending(env_snapshot=_snapshot(good_hash, payment_matches=False))
    r = client.post(
        f"/approvals/{approval.approval_id}",
        data={"t": "tok", "decision": "approve"},
    )
    assert r.status_code == 409
    # The refusal happens BEFORE the token reaches the worker — no rollback was
    # dispatched and the single-use credential was not spent.
    assert executed == []
    assert store.docs[approval.approval_id]["status"] == "pending"


@pytest.mark.parametrize("value", ["", "0", "on", "true", " ", "yes"], ids=str)
def test_only_the_exact_acknowledgment_value_satisfies_the_gate(
    client, store, good_hash, executed, value
):
    """``!= "1"`` — a checkbox that posts anything else, or a hand-built POST
    guessing at the field, does not count."""
    approval = store.create_pending(env_snapshot=_snapshot(good_hash, payment_matches=False))
    r = client.post(
        f"/approvals/{approval.approval_id}",
        data={"t": "tok", "decision": "approve", "ack_target_violates_contract": value},
    )
    assert r.status_code == 409
    assert executed == []


def test_approving_a_violating_target_with_the_tick_proceeds(
    client, store, good_hash, executed
):
    approval = store.create_pending(env_snapshot=_snapshot(good_hash, payment_matches=False))
    r = client.post(
        f"/approvals/{approval.approval_id}",
        data={"t": "tok", "decision": "approve", "ack_target_violates_contract": "1"},
    )
    assert r.status_code == 200
    assert executed == [(approval.approval_id, "tok")]


def test_a_non_violating_target_needs_no_acknowledgment(client, store, good_hash, executed):
    approval = store.create_pending(env_snapshot=_snapshot(good_hash))
    r = client.post(
        f"/approvals/{approval.approval_id}", data={"t": "tok", "decision": "approve"}
    )
    assert r.status_code == 200
    assert executed == [(approval.approval_id, "tok")]


def test_an_unknown_snapshot_does_not_demand_the_acknowledgment(
    client, store, executed
):
    """Deliberate. Every approval minted before ds-uwc is unknown, as is every
    one from a worker that has not deployed yet. Requiring the tick there would
    put a new obstacle in front of the operator without a single new fact to
    justify it — only a snapshot that positively PROVES a violation asks for
    it."""
    approval = store.create_pending(env_snapshot=None)
    r = client.post(
        f"/approvals/{approval.approval_id}", data={"t": "tok", "decision": "approve"}
    )
    assert r.status_code == 200
    assert executed == [(approval.approval_id, "tok")]


def test_a_store_read_failure_does_not_block_the_rollback(
    client, store, good_hash, executed, monkeypatch
):
    """Pinned as DELIBERATE, not overlooked.

    The gate is a speed bump, not an authorization control — the authority is
    the single-use HMAC token the worker verifies. A predicate that BLOCKS may
    only block on positive proof, and a failed read proves nothing. Blocking a
    legitimate rollback mid-incident because Firestore blipped would be the
    worse failure, and the ds-b3m gate already stands between an ungrounded
    proposal and an approval existing at all.
    """
    approval = store.create_pending(env_snapshot=_snapshot(good_hash, payment_matches=False))

    real_get = store.get
    calls = {"n": 0}

    def flaky(approval_id: str):
        calls["n"] += 1
        # Fail only the ack-path read; the post-decision re-render still needs
        # a doc to render from.
        if calls["n"] == 1:
            raise RuntimeError("firestore unavailable")
        return real_get(approval_id)

    monkeypatch.setattr(store, "get", flaky)
    r = client.post(
        f"/approvals/{approval.approval_id}", data={"t": "tok", "decision": "approve"}
    )
    assert r.status_code == 200
    assert executed == [(approval.approval_id, "tok")]


def test_a_view_that_could_not_work_out_the_change_never_gates(
    client, store, monkeypatch, executed
):
    """The ``state == "ok"`` conjunct, pinned as an invariant rather than left
    as an accident of the current return shape.

    Today an unknown view simply carries no ``violates`` key, so the conjunct
    is redundant — which means a future change to
    :func:`agent.main._rollback_change_view` could start reporting a violation
    it did not prove, and the gate would begin demanding an acknowledgment on
    every approval that predates ds-uwc. Only a POSITIVE proof may gate.
    """
    import agent.main as agent_main

    approval = store.create_pending(env_snapshot=None)
    monkeypatch.setattr(
        agent_main,
        "_rollback_change_view",
        lambda _a: {"state": "unknown", "reason": "absent", "violates": True},
    )
    r = client.post(
        f"/approvals/{approval.approval_id}", data={"t": "tok", "decision": "approve"}
    )
    assert r.status_code == 200
    assert executed == [(approval.approval_id, "tok")]


def test_rejecting_a_violating_target_is_never_gated(client, store, good_hash, denied):
    """Reject is the SAFE direction. Making the operator acknowledge a
    violation in order to decline the rollback would be backwards, and would
    hand an availability bug to anyone who wanted the rollback to stick."""
    approval = store.create_pending(env_snapshot=_snapshot(good_hash, payment_matches=False))
    r = client.post(
        f"/approvals/{approval.approval_id}", data={"t": "tok", "decision": "reject"}
    )
    assert r.status_code == 200
    assert denied == [(approval.approval_id, "tok")]


def test_the_gate_reads_the_doc_not_the_form(client, store, good_hash, executed):
    """A POST that asserts its own innocence gets nowhere: the requirement is
    derived from the worker-written approval doc, and the only field the form
    contributes is the acknowledgment itself."""
    approval = store.create_pending(env_snapshot=_snapshot(good_hash, payment_matches=False))
    r = client.post(
        f"/approvals/{approval.approval_id}",
        data={
            "t": "tok",
            "decision": "approve",
            # Attempts to overwrite the server's own view of the snapshot.
            "violates": "false",
            "change_view": "ok",
            "env_snapshot": "{}",
        },
    )
    assert r.status_code == 409
    assert executed == []


def test_the_post_decision_page_keeps_the_change_record(client, store, good_hash, executed):
    """After the click, the record of what was approved stays on the page the
    operator is looking at — recomputed from the same immutable snapshot, so it
    cannot re-observe the service and rewrite history."""
    approval = store.create_pending(env_snapshot=_snapshot(good_hash))
    r = client.post(
        f"/approvals/{approval.approval_id}", data={"t": "tok", "decision": "approve"}
    )
    assert r.status_code == 200
    assert 'data-testid="change-row"' in r.text
    assert "payment-demo-00016-w9k" in r.text
