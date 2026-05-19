"""End-to-end tests for the Rollback Agent (Phase 11.5).

The Rollback Agent is the only worker that mutates the live Cloud Run
service, so the negative-path coverage is large by design:

- Layer 2 (payload-intent policy): rejecting target_revision == active,
  target_revision not in service.
- Layer 3 inter-service auth: missing bearer, caller not in allowlist.
- HITL approval contract:
    * raw token returned exactly once on /propose; only HMAC stored
    * /execute rejects expired tokens, replayed tokens, mismatched HMAC
    * concurrent /execute races atomically (only one wins)

Mocking strategy mirrors workers/docs/tests/test_patch.py:

- Env is seeded at import time (the module reads env at import).
- ``app.dependency_overrides`` swaps the auth dependency per test.
- ``_get_approval_store`` and ``_list_revisions`` (the two helpers that
  reach external systems) are monkey-patched so we never touch
  Firestore or the Cloud Run admin API.
- The traffic-update side effect is replaced with a recording mock so
  tests can assert exactly which revision the worker tried to roll
  back to.
"""
from __future__ import annotations

import datetime as dt
import os
from typing import Any

import pytest
from fastapi import HTTPException, status
from fastapi.testclient import TestClient

# Env MUST be set before importing workers.rollback.main — the module reads
# OWN_URL / COORDINATOR_URL / ALLOWED_CALLERS / GCP_PROJECT / APPROVAL_HMAC_KEY
# at import time and KeyErrors if any are missing. This mirrors the
# fail-fast behavior the Cloud Run revision will have at boot.
os.environ.setdefault("GCP_PROJECT", "test-proj")
os.environ.setdefault("OWN_URL", "https://rollback.example.com")
os.environ.setdefault("COORDINATOR_URL", "https://coord.example.com")
os.environ.setdefault(
    "ALLOWED_CALLERS",
    "coordinator@test-proj.iam.gserviceaccount.com",
)
os.environ.setdefault("APPROVAL_HMAC_KEY", "test-hmac-key")

from driftscribe_lib.approvals import (  # noqa: E402
    Approval,
    compute_token_hmac,
)
from workers.rollback import main as rollback_main  # noqa: E402
from workers.rollback.main import _verify_caller_dep, app  # noqa: E402


# --------------------------------------------------------------------------- #
# In-memory ApprovalStore stand-in
# --------------------------------------------------------------------------- #


class FakeApprovalStore:
    """Minimal in-memory replacement that matches the ApprovalStore surface
    used by the worker. Keeping it test-local (rather than vending one from
    the lib) lets each test inspect/edit the stored docs directly when
    simulating exotic states like ``status == "denied"`` or an expired TTL.
    """

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}

    def create(
        self,
        *,
        target_revision: str,
        reason: str,
        hmac_key: str,
        created_by: str,
        ttl_minutes: int = 15,
    ) -> tuple[Approval, str]:
        import secrets
        import uuid

        approval_id = str(uuid.uuid4())
        raw_token = secrets.token_urlsafe(32)
        now = dt.datetime.now(dt.timezone.utc)
        expires_at = now + dt.timedelta(minutes=ttl_minutes)
        token_hmac = compute_token_hmac(raw_token, target_revision, hmac_key)
        data = {
            "status": "pending",
            "target_revision": target_revision,
            "reason": reason,
            "token_hmac": token_hmac,
            "expires_at": expires_at,
            "created_at": now,
            "created_by": created_by,
        }
        self.docs[approval_id] = data
        return Approval(approval_id=approval_id, **data), raw_token

    def get(self, approval_id: str) -> Approval | None:
        if approval_id not in self.docs:
            return None
        return Approval(approval_id=approval_id, **self.docs[approval_id])

    def claim_pending(self, approval_id: str) -> Approval | None:
        if approval_id not in self.docs:
            return None
        data = self.docs[approval_id]
        if data.get("status") != "pending":
            return None
        data["status"] = "used"
        return Approval(approval_id=approval_id, **data)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def store() -> FakeApprovalStore:
    return FakeApprovalStore()


@pytest.fixture
def traffic_calls() -> list[str]:
    """Records the target_revision arg of every call to ``_apply_traffic``.

    A list rather than a Mock so failing tests print the captured value in
    plain assertion output ("[] != ['rev-x']") instead of dumping the full
    Mock call list.
    """
    return []


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch,
    store: FakeApprovalStore,
    traffic_calls: list[str],
):
    """TestClient with Firestore + Cloud Run admin stubbed and auth bypassed.

    The two indirections we patch:

    - ``_get_approval_store``: returns our in-memory fake.
    - ``_list_revisions``: returns a fixed (revisions, active) tuple. The
      default is a service with three revisions where revision 3 is
      currently serving. Tests that need a different topology override
      via ``monkeypatch.setattr(rollback_main, "_list_revisions", ...)``.
    - ``_apply_traffic``: records the call instead of hitting Cloud Run.
    """
    monkeypatch.setattr(rollback_main, "_get_approval_store", lambda: store)
    monkeypatch.setattr(
        rollback_main,
        "_list_revisions",
        lambda: (
            ["payment-demo-00001-aaa", "payment-demo-00002-bbb", "payment-demo-00003-ccc"],
            "payment-demo-00003-ccc",
        ),
    )

    def fake_apply_traffic(target_revision: str) -> str:
        traffic_calls.append(target_revision)
        return "operations/fake-op-name"

    monkeypatch.setattr(rollback_main, "_apply_traffic", fake_apply_traffic)
    # Default: no tagged targets exist, so the /execute preflight is a no-op.
    # Tests that need to simulate a tagged service override this.
    monkeypatch.setattr(rollback_main, "_assert_no_tagged_targets", lambda: None)

    app.dependency_overrides[_verify_caller_dep] = (
        lambda: "coordinator@test-proj.iam.gserviceaccount.com"
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# /propose
# --------------------------------------------------------------------------- #


def test_propose_happy_path(client, store) -> None:
    r = client.post(
        "/propose",
        json={
            "target_revision": "payment-demo-00002-bbb",
            "reason": "rollback to last known good",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["approval_id"]
    assert body["approval_token"]
    # token_urlsafe(32) → 43 chars
    assert len(body["approval_token"]) == 43
    # The approval URL lives on the coordinator, not this worker.
    assert body["approval_url"].startswith("https://coord.example.com/approvals/")
    assert body["approval_url"].endswith(body["approval_id"])
    assert "expires_at" in body

    # The approval was actually persisted in the store.
    assert body["approval_id"] in store.docs
    doc = store.docs[body["approval_id"]]
    assert doc["status"] == "pending"
    assert doc["target_revision"] == "payment-demo-00002-bbb"
    assert doc["reason"] == "rollback to last known good"
    # Critical safety property: only HMAC is stored, never the raw token.
    assert "token_hmac" in doc
    raw_token_str = body["approval_token"]
    assert raw_token_str not in str(doc)


def test_propose_rejects_active_revision(client, store, traffic_calls) -> None:
    """Layer 2: rolling back to the currently-serving revision is a no-op
    masquerading as work — refuse it before creating the approval doc."""
    r = client.post(
        "/propose",
        json={
            "target_revision": "payment-demo-00003-ccc",  # active in fixture
            "reason": "redundant",
        },
    )
    assert r.status_code == 400
    assert "active" in r.json()["detail"].lower()
    # Layer 2 invariant: policy violation MUST short-circuit before the
    # store side effect runs.
    assert store.docs == {}
    assert traffic_calls == []


def test_propose_rejects_unknown_revision(client, store) -> None:
    r = client.post(
        "/propose",
        json={
            "target_revision": "payment-demo-99999-ghost",
            "reason": "nope",
        },
    )
    assert r.status_code == 404
    assert store.docs == {}


def test_propose_rejects_extra_field(client) -> None:
    """Layer 2: caller cannot supply target_service / region / project —
    those are hardcoded at boot."""
    r = client.post(
        "/propose",
        json={
            "target_revision": "payment-demo-00002-bbb",
            "reason": "x",
            "target_service": "attacker-controlled",
        },
    )
    assert 400 <= r.status_code < 500


def test_propose_missing_field_rejected(client) -> None:
    r = client.post("/propose", json={"target_revision": "payment-demo-00002-bbb"})
    assert r.status_code == 422


@pytest.mark.parametrize(
    "revision",
    [
        "Has-Uppercase",  # uppercase not allowed in Cloud Run revision names
        "starts-with-digit-x",  # actually fine
        "9starts-with-digit",  # starts with digit (regex requires letter)
        "ends-with-dash-",  # trailing hyphen
        "has spaces",  # whitespace
        "has/slash",  # path separator
        "has..dots",  # dot
        "a" * 65,  # too long
        "",  # empty string
    ],
)
def test_propose_rejects_malformed_revision(client, store, revision) -> None:
    """Schema-level regex catches gross malformations before the Cloud Run
    admin lookup runs. The ``starts-with-digit-x`` case is included to verify
    that valid (regex-passing) but service-list-failing names still hit the
    Layer 2 404, not a schema 422 — see below."""
    r = client.post(
        "/propose",
        json={"target_revision": revision, "reason": "x"},
    )
    if revision == "starts-with-digit-x":
        # Passes the regex but isn't in the fixture's revision list → 404.
        assert r.status_code == 404
    else:
        # Fails the schema → 422.
        assert r.status_code == 422
    assert store.docs == {}


@pytest.mark.parametrize(
    "approval_id",
    [
        # Right length (36) but not UUID-shaped.
        "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        # Path-traversal style.
        "../../../../etc/passwd-aaaa-aaaa-aaa",
        # Slashes inside.
        "00000000-0000/000-0000-000000000000",
        # Plain wrong length.
        "ghost-id",
    ],
)
def test_execute_rejects_malformed_approval_id(
    client, approval_id, traffic_calls
) -> None:
    """Schema-level UUID regex catches non-UUID approval IDs before they
    get passed to ``collection(...).document(approval_id)`` — guards
    against unexpected Firestore path construction."""
    r = client.post(
        "/execute",
        json={"approval_id": approval_id, "approval_token": "x" * 43},
    )
    assert r.status_code == 422
    assert traffic_calls == []


def test_propose_rejects_oversized_reason(client, store) -> None:
    r = client.post(
        "/propose",
        json={
            "target_revision": "payment-demo-00002-bbb",
            "reason": "x" * 501,  # 1 over the 500-char cap
        },
    )
    assert r.status_code == 422
    assert store.docs == {}


def test_apply_traffic_uses_update_mask_and_refuses_tagged_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test for Codex review findings #1 + #2 (Phase 11.5):

    1. ``update_service`` MUST be called with ``update_mask=[traffic]`` so a
       stale read can't clobber unrelated fields (env vars, scaling, etc.).
    2. If the service has any tagged traffic targets, the worker refuses
       the rollback rather than silently destroying them.

    Both are verified against a hand-rolled fake of ``ServicesClient`` —
    we don't go through the FastAPI handler because the other tests cover
    that path; here we want the assertions concentrated on the SDK shape.
    """
    from google.cloud import run_v2

    from workers.rollback import main as m

    # --- Property 1: update_mask=[traffic] ---
    class _FakeSvc:
        def __init__(self) -> None:
            self.traffic = []

    class _FakeOp:
        operation = type("O", (), {"name": "operations/fake-op"})()

    captured: dict = {}

    class _FakeSvcClient:
        def get_service(self, name: str):  # noqa: ANN201
            return _FakeSvc()

        def update_service(self, *, service, update_mask):  # noqa: ANN001
            captured["service"] = service
            captured["update_mask"] = update_mask
            return _FakeOp()

    monkeypatch.setattr(m, "_get_services_client", _FakeSvcClient)
    op_name = m._apply_traffic("payment-demo-00002-bbb")
    assert op_name == "operations/fake-op"
    assert list(captured["update_mask"].paths) == ["traffic"]
    # And the traffic block we sent was the expected single-target shape.
    assert len(captured["service"].traffic) == 1
    target = captured["service"].traffic[0]
    assert target.revision == "payment-demo-00002-bbb"
    assert target.percent == 100
    assert (
        target.type_
        == run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION
    )

    # --- Property 2: refuse if any existing target has a tag ---
    class _FakeSvcWithTag:
        def __init__(self) -> None:
            self.traffic = [
                run_v2.TrafficTarget(
                    type_=run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION,
                    revision="payment-demo-00003-ccc",
                    percent=100,
                    tag="canary",
                ),
            ]

    class _FakeSvcClientTagged:
        def get_service(self, name: str):  # noqa: ANN201
            return _FakeSvcWithTag()

        def update_service(self, *, service, update_mask):  # noqa: ANN001
            raise AssertionError("update_service must not be called when tags present")

    monkeypatch.setattr(m, "_get_services_client", _FakeSvcClientTagged)
    with pytest.raises(HTTPException) as exc:
        m._apply_traffic("payment-demo-00002-bbb")
    assert exc.value.status_code == 409
    assert "tag" in exc.value.detail.lower()


# --------------------------------------------------------------------------- #
# /execute
# --------------------------------------------------------------------------- #


def _propose(client, target_revision: str = "payment-demo-00002-bbb") -> dict:
    """Helper: create an approval via /propose, return the response body so
    the test can hand the approval_id + raw_token to /execute."""
    r = client.post(
        "/propose",
        json={"target_revision": target_revision, "reason": "rb"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_execute_happy_path(client, traffic_calls) -> None:
    proposed = _propose(client)
    r = client.post(
        "/execute",
        json={
            "approval_id": proposed["approval_id"],
            "approval_token": proposed["approval_token"],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "executed"
    assert body["target_revision"] == "payment-demo-00002-bbb"
    assert body["approval_id"] == proposed["approval_id"]
    # The traffic update was actually called with the right revision.
    assert traffic_calls == ["payment-demo-00002-bbb"]


def test_execute_missing_token_rejected(client, traffic_calls) -> None:
    proposed = _propose(client)
    r = client.post("/execute", json={"approval_id": proposed["approval_id"]})
    assert r.status_code == 422
    assert traffic_calls == []


def test_execute_missing_approval_id_rejected(client) -> None:
    r = client.post("/execute", json={"approval_token": "anything"})
    assert r.status_code == 422


def test_execute_unknown_approval_returns_404(client, traffic_calls) -> None:
    # Use a well-formed UUID + token-shaped string so the request passes
    # the schema check and reaches the handler's "approval not found" path.
    # Malformed inputs (caught by the schema layer) are covered separately.
    r = client.post(
        "/execute",
        json={
            "approval_id": "00000000-0000-0000-0000-000000000000",
            "approval_token": "x" * 43,
        },
    )
    assert r.status_code == 404
    assert traffic_calls == []


def test_execute_wrong_token_rejected(client, traffic_calls) -> None:
    proposed = _propose(client)
    # 43-char wrong token (matches the schema's min/max for token_urlsafe(32))
    # so we exercise the HMAC-mismatch path rather than the schema-rejection path.
    r = client.post(
        "/execute",
        json={
            "approval_id": proposed["approval_id"],
            "approval_token": "x" * 43,
        },
    )
    assert r.status_code == 403
    assert "invalid" in r.json()["detail"].lower()
    assert traffic_calls == []


def test_execute_rejects_wrong_revision_token(client, store, traffic_calls) -> None:
    """The HMAC binds the target revision. If an attacker steals an
    approval token for revision A and tries to use it to roll back to
    revision B, the HMACs won't match.

    Simulation: create the approval for revision A, then surreptitiously
    mutate the doc's ``target_revision`` to B (as a Firestore-level
    tamper would). The raw token still verifies against (token, A) but
    the doc claims (token, B) — the worker computes HMAC against the
    doc's stored revision (B) and finds it doesn't match the stored
    HMAC (which is over A)."""
    proposed = _propose(client, target_revision="payment-demo-00002-bbb")
    # Tamper with the doc — repoint to a different valid revision.
    store.docs[proposed["approval_id"]]["target_revision"] = "payment-demo-00001-aaa"
    r = client.post(
        "/execute",
        json={
            "approval_id": proposed["approval_id"],
            "approval_token": proposed["approval_token"],
        },
    )
    assert r.status_code == 403
    assert traffic_calls == []


def test_execute_expired_token_rejected(client, store, traffic_calls) -> None:
    proposed = _propose(client)
    # Fast-forward expiry to 1 minute in the past.
    store.docs[proposed["approval_id"]]["expires_at"] = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
    )
    r = client.post(
        "/execute",
        json={
            "approval_id": proposed["approval_id"],
            "approval_token": proposed["approval_token"],
        },
    )
    assert r.status_code == 403
    assert "expired" in r.json()["detail"].lower()
    assert traffic_calls == []


def test_execute_replay_rejected(client, traffic_calls) -> None:
    """The Firestore transaction in claim_pending must make replays
    impossible — second /execute sees status="used" and bounces."""
    proposed = _propose(client)
    payload = {
        "approval_id": proposed["approval_id"],
        "approval_token": proposed["approval_token"],
    }
    r1 = client.post("/execute", json=payload)
    assert r1.status_code == 200, r1.text
    r2 = client.post("/execute", json=payload)
    assert r2.status_code == 403
    # Only one traffic update was issued.
    assert traffic_calls == ["payment-demo-00002-bbb"]


def test_execute_preflight_tag_check_leaves_approval_pending(
    monkeypatch: pytest.MonkeyPatch,
    client,
    store,
    traffic_calls,
) -> None:
    """Codex review (operational finding #2, Phase 11.5): if the service
    has a tagged traffic target, ``/execute`` must refuse BEFORE flipping
    the approval to ``used``. Otherwise the operator clears the tag and
    retries, only to be told "already used" — the 409 is now recoverable
    because the approval is still pending."""
    proposed = _propose(client)

    def boom() -> None:
        raise HTTPException(
            status_code=409,
            detail="refusing rollback: service has a tagged traffic target",
        )

    monkeypatch.setattr(rollback_main, "_assert_no_tagged_targets", boom)
    r = client.post(
        "/execute",
        json={
            "approval_id": proposed["approval_id"],
            "approval_token": proposed["approval_token"],
        },
    )
    assert r.status_code == 409
    # Approval token MUST still be pending — the operator should be able
    # to clear the tag and retry the same approval.
    assert store.docs[proposed["approval_id"]]["status"] == "pending"
    # No traffic update happened either.
    assert traffic_calls == []


def test_execute_rejects_already_denied_approval(client, store, traffic_calls) -> None:
    """If the coordinator marked the approval as denied, /execute MUST refuse
    even if the token verifies. This exercises the explicit status check
    before the HMAC compare."""
    proposed = _propose(client)
    store.docs[proposed["approval_id"]]["status"] = "denied"
    r = client.post(
        "/execute",
        json={
            "approval_id": proposed["approval_id"],
            "approval_token": proposed["approval_token"],
        },
    )
    assert r.status_code == 403
    assert traffic_calls == []


# --------------------------------------------------------------------------- #
# Auth + healthz
# --------------------------------------------------------------------------- #


def test_missing_bearer_returns_401(client) -> None:
    def deny_401():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
        )

    app.dependency_overrides[_verify_caller_dep] = deny_401
    r = client.post(
        "/propose",
        json={"target_revision": "payment-demo-00002-bbb", "reason": "x"},
    )
    assert r.status_code == 401


def test_caller_not_in_allowlist_returns_403(client) -> None:
    def deny_caller():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="caller 'nope@example.com' not in allowed_callers",
        )

    app.dependency_overrides[_verify_caller_dep] = deny_caller
    r = client.post(
        "/propose",
        json={"target_revision": "payment-demo-00002-bbb", "reason": "x"},
    )
    assert r.status_code == 403


def test_healthz_does_not_require_auth(client) -> None:
    def boom():
        raise HTTPException(status_code=401, detail="should not be called")

    app.dependency_overrides[_verify_caller_dep] = boom
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_real_verify_caller_dep_wired_with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Layer 3 integration check (matches the reader / docs tests): without
    ``dependency_overrides`` the real ``_verify_caller_dep`` must forward
    OWN_URL + ALLOWED_CALLERS (read from env at boot) to
    ``driftscribe_lib.auth.verify_caller``.

    Module-level constants are monkeypatched directly because in a unified
    pytest run another worker's test module may have populated OWN_URL
    before this module was imported (``os.environ.setdefault`` would then
    be a no-op and the constant would carry the other worker's value).
    """
    seen: dict = {}

    def fake_verify(request, *, own_url, allowed_callers):
        seen["own_url"] = own_url
        seen["allowed_callers"] = set(allowed_callers)
        return "coordinator@test-proj.iam.gserviceaccount.com"

    monkeypatch.setattr(rollback_main, "verify_caller", fake_verify)

    # Stub the side-effect helpers so the handler can complete without
    # touching Firestore or Cloud Run.
    fake_store = FakeApprovalStore()
    monkeypatch.setattr(rollback_main, "_get_approval_store", lambda: fake_store)
    monkeypatch.setattr(
        rollback_main,
        "_list_revisions",
        lambda: (["payment-demo-00002-bbb", "payment-demo-00003-ccc"], "payment-demo-00003-ccc"),
    )
    monkeypatch.setattr(rollback_main, "OWN_URL", "https://rollback.example.com")
    monkeypatch.setattr(
        rollback_main,
        "ALLOWED_CALLERS",
        frozenset({"coordinator@test-proj.iam.gserviceaccount.com"}),
    )

    c = TestClient(app)
    r = c.post(
        "/propose",
        json={"target_revision": "payment-demo-00002-bbb", "reason": "x"},
        headers={"Authorization": "Bearer faketoken"},
    )
    assert r.status_code == 200
    assert seen["own_url"] == "https://rollback.example.com"
    assert seen["allowed_callers"] == {
        "coordinator@test-proj.iam.gserviceaccount.com",
    }
