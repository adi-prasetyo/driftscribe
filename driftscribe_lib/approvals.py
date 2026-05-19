"""Firestore-backed approval store for the HITL gate (Phase 11.5).

Used by:

- **Rollback Agent** (`workers/rollback/main.py`, Phase 11.5) — creates pending
  approvals on ``/propose``; transactionally flips ``pending → used`` on
  ``/execute``.
- **Coordinator** (Phase 11.7, future) — reads pending approvals to render
  the operator-facing approval page; writes the operator's approve/deny
  decision back into the doc. Sharing the data layer with the rollback
  worker keeps a single source of truth for the approval schema.

The "approval token" is a single-use credential the operator presents to
``/execute``. Its safety story has three parts:

1. **Server-side storage is HMAC, not plaintext.** The raw token is
   returned exactly once (from :meth:`ApprovalStore.create`) and never
   persisted anywhere. Only
   ``hmac(hmac_key, f"{token}|{approval_id}|{revision}")`` is written
   to Firestore. A Firestore exfiltration alone cannot mint an
   ``/execute`` request — the attacker would also need the HMAC key from
   Secret Manager.

2. **The HMAC binds both the approval_id and the target revision.**
   Mixing the revision into the HMAC input means a stolen-and-replayed
   approval for revision A cannot be redirected to roll back to revision
   B — the HMACs differ, and the constant-time comparison in the
   worker's ``/execute`` handler will fail. (See the negative test
   ``test_rollback.py::test_execute_rejects_wrong_revision_token``.)
   Mixing the ``approval_id`` in additionally forecloses cross-approval
   replay — token issued for approval A cannot be presented against
   approval B even if both share the same target revision. (Phase 11.9
   defense-in-depth, from Codex review of 11.7.)

3. **Transactional pending → used flip.** :meth:`ApprovalStore.claim_pending`
   uses a Firestore transaction so concurrent ``/execute`` calls race
   safely — at most one observes ``status == "pending"`` and wins the
   update; the others see ``status == "used"`` and bounce out. This is
   the canonical replay defense.

The 15-minute TTL is enforced in the worker (`/execute` rejects if
``expires_at < now``), not the store itself — the store records the
expiry but doesn't act on it. That asymmetry lets the coordinator
display countdowns on the approval page without the store needing to be
clock-aware.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from typing import Any

from google.cloud import firestore


@dataclass
class Approval:
    """A single approval record. Mirrors the Firestore doc shape 1:1 so that
    :meth:`ApprovalStore.get` and :meth:`ApprovalStore.claim_pending` can
    populate it directly from ``snap.to_dict()``."""

    approval_id: str
    target_revision: str
    reason: str
    token_hmac: str
    expires_at: dt.datetime
    created_at: dt.datetime
    created_by: str
    status: str  # "pending" | "approved" | "denied" | "used"


def compute_token_hmac(
    token: str, approval_id: str, target_revision: str, hmac_key: str
) -> str:
    """Return the HMAC-SHA-256 hex digest binding ``token`` to ``(approval_id,
    target_revision)``.

    Defense in depth (Phase 11.9 / Codex review of 11.7):

    1. **Revision-binding** — the original property. A stolen approval for
       revision A cannot be redirected to roll back to revision B; the
       HMACs differ and the constant-time compare in ``/execute`` fails.
    2. **Approval-ID binding** — added in 11.9. Even if an attacker
       correlates two approvals (say, via timing or out-of-band info),
       they cannot use approval A's token on approval B — different
       ``approval_id`` produces a different HMAC. This forecloses a
       theoretical cross-approval replay we did not previously close.

    The HMAC input is ``f"{token}|{approval_id}|{target_revision}"`` (UTF-8).
    The ``|`` delimiter is a U+007C ASCII pipe — neither
    :func:`secrets.token_urlsafe` nor UUID4 hex nor Cloud Run revision names
    emit U+007C, so the parse is unambiguous and there's no concatenation-
    ambiguity vector (e.g., ``"ab" + "cd" == "a" + "bcd"`` style attacks).

    Used both at ``create`` time (to store the HMAC) and at ``execute`` /
    ``deny`` time (to verify a presented token). Deterministic — same
    inputs produce the same output.

    Wire-breaking change in 11.9: in-flight approvals issued before this
    commit are invalidated. Acceptable pre-deploy (no real users yet).
    """
    msg = f"{token}|{approval_id}|{target_revision}".encode("utf-8")
    return hmac.new(hmac_key.encode("utf-8"), msg, hashlib.sha256).hexdigest()


class ApprovalStore:
    """Firestore wrapper for the ``approvals/`` collection.

    Single collection, single doc per approval. Keyed by ``approval_id``
    (UUID4 string). No secondary indexes; lookups are by primary key only
    in the current design.
    """

    def __init__(self, project: str, client: Any = None) -> None:
        # Lazy default so tests can inject a fake client without needing
        # GCP credentials. Same pattern as ``FirestoreStateStore`` in
        # ``agent/state_store.py``.
        self._client = client or firestore.Client(project=project)
        self._collection_name = "approvals"

    def _ref(self, approval_id: str):  # noqa: ANN202
        return self._client.collection(self._collection_name).document(approval_id)

    def create(
        self,
        *,
        target_revision: str,
        reason: str,
        hmac_key: str,
        created_by: str,
        ttl_minutes: int = 15,
    ) -> tuple[Approval, str]:
        """Create a pending approval; return ``(approval, raw_token)``.

        The ``raw_token`` is returned **only** here and is never stored
        anywhere by this code — the caller is responsible for handing it
        to the operator (typically via a one-time URL on the approval
        page). Only the HMAC lives in Firestore.

        ``ttl_minutes`` defaults to 15 per the Phase 11.5 plan. Bumping
        it requires a coordinated change to the operator-facing UI copy
        on the approval page.
        """
        approval_id = str(uuid.uuid4())
        raw_token = secrets.token_urlsafe(32)
        now = dt.datetime.now(dt.timezone.utc)
        expires_at = now + dt.timedelta(minutes=ttl_minutes)
        token_hmac = compute_token_hmac(
            raw_token, approval_id, target_revision, hmac_key
        )

        data = {
            "status": "pending",
            "target_revision": target_revision,
            "reason": reason,
            "token_hmac": token_hmac,
            "expires_at": expires_at,
            "created_at": now,
            "created_by": created_by,
        }
        self._ref(approval_id).set(data)
        return Approval(approval_id=approval_id, **data), raw_token

    def get(self, approval_id: str) -> Approval | None:
        """Read the approval doc. Returns ``None`` if the doc doesn't exist.

        Note: this is a plain non-transactional read. Callers that need
        the read-then-mutate semantics for executing a rollback should
        use :meth:`claim_pending`, which performs both inside a single
        Firestore transaction.
        """
        snap = self._ref(approval_id).get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        return Approval(approval_id=approval_id, **data)

    def claim_pending(self, approval_id: str) -> Approval | None:
        """Transactionally flip ``status: pending → used``.

        Returns the updated :class:`Approval` on success, or ``None`` if:

        - The doc doesn't exist, OR
        - The doc's status was not ``"pending"`` (already used, denied,
          revoked, etc.).

        Concurrent calls race safely — Firestore's optimistic concurrency
        guarantees at most one transaction commits the ``status`` write
        for a given doc version; the others retry, observe the new
        status, and return ``None``.
        """
        return self._claim(approval_id, new_status="used")

    def claim_denied(self, approval_id: str) -> Approval | None:
        """Transactionally flip ``status: pending → denied``.

        Mirrors :meth:`claim_pending` but for the coordinator-owned deny
        path (Phase 11.7). Used when the operator presses "Reject" on
        the approval page — the coordinator owns this state transition
        because the rollback worker only knows the ``pending → used``
        flip. A subsequent ``/execute`` against a denied approval will
        see ``status != "pending"`` and bounce out at the worker's
        explicit status check.

        Concurrency story matches ``claim_pending``: at most one
        transaction wins, others observe the new status and return
        ``None``. This makes the deny path replay-safe.
        """
        return self._claim(approval_id, new_status="denied")

    def _claim(self, approval_id: str, *, new_status: str) -> Approval | None:
        """Shared transactional flip helper for the two ``pending → X``
        transitions. Kept private — callers MUST go through
        :meth:`claim_pending` or :meth:`claim_denied` so the set of
        target statuses stays closed (no caller can flip to an arbitrary
        string)."""
        ref = self._ref(approval_id)

        @firestore.transactional
        def txn(transaction, ref):  # noqa: ANN001
            snap = ref.get(transaction=transaction)
            if not snap.exists:
                return None
            data = snap.to_dict() or {}
            if data.get("status") != "pending":
                return None
            transaction.update(ref, {"status": new_status})
            data["status"] = new_status
            return Approval(approval_id=approval_id, **data)

        return txn(self._client.transaction(), ref)
