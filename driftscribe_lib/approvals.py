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

----------------------------------------------------------------------------
**Phase C3** appended a SECOND, plan-bound approval family that sits ALONGSIDE
the rollback path above and does not modify it: :class:`PlanApproval` /
:class:`PlanApprovalStore` over a separate ``plan_approvals`` collection, the
``c3.v1`` canonical signed payload (:func:`build_plan_approval_payload` +
:func:`canonicalize_payload`), the domain-separated plan-bound HMAC
(:func:`compute_plan_approval_hmac`, :func:`verify_plan_approval`), the
artifact-integrity recompute primitive (:func:`verify_artifact_integrity`), and
the signed-window expiry check (:func:`plan_approval_is_expired`). See the C3
section comment further down and
docs/plans/2026-05-29-infra-iac-phase-c3-plan-approval.md. C3 is library/schema
only — the GCS fetch, denylist re-run, freshness check, and ``tofu apply`` are
the C4 ``tofu-apply`` worker's job.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import re
import secrets
import uuid
from dataclasses import dataclass
from typing import Any

from google.cloud import firestore

from driftscribe_lib.iac_plan_metadata import (
    METADATA_SCHEMA_VERSION,
    MetadataInput,
    build_metadata,
)


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


# =========================================================================== #
# Phase C3: plan-bound approval schema
#
# A typed, plan-bound approval that sits ALONGSIDE the rollback Approval above
# (the rollback path is untouched). It cryptographically binds ONE human
# approval to exactly ONE immutable c2.v1 plan artifact produced by the C2
# plan-builder.
#
# This is pure library/schema: NO GCS I/O, NO `tofu` subprocess. The GCS
# fetch-by-generation, the C1-denylist re-run on the fetched plan.json, the
# lockfile/OpenTofu-version freshness check, and `tofu apply` are the C4
# `tofu-apply` worker's job — it holds the HMAC key and runs BOTH `create` (at
# /propose, after independently verifying the artifact) and `claim` (at /apply),
# mirroring workers/rollback. The coordinator (C5) requests a proposal + renders
# the approval page but cannot mint a valid approval alone.
#
# Design + threat model + C4 consumer contract:
#   docs/plans/2026-05-29-infra-iac-phase-c3-plan-approval.md
# =========================================================================== #

PLAN_APPROVAL_SCHEMA_VERSION = "c3.v1"

# Domain-separation tag — the FIRST component of the plan-approval HMAC message.
# Guarantees a plan-approval HMAC can never collide with the rollback
# compute_token_hmac namespace even if the two ever shared an HMAC key (a
# rollback revision-token can never validate as a plan-approval token).
# WIRE-BREAKING if changed once any approval is minted live.
_PLAN_APPROVAL_DOMAIN = "driftscribe-plan-approval-v1"

# The 15 c2.v1 metadata keys, in the order build_metadata emits them. The signed
# payload nests these verbatim under "metadata".
_C2_METADATA_KEYS = (
    "schema_version", "repo", "pr_number", "head_sha", "base_sha",
    "workflow_run_id", "workflow_run_attempt", "artifact_uri_plan",
    "artifact_uri_json", "generation_plan", "generation_json",
    "plan_sha256", "plan_json_sha256", "opentofu_version",
    "provider_lockfile_sha256",
)

# RFC3339 UTC, no microseconds — the FROZEN datetime format for the signed
# approval window, so issued_at/expires_at are byte-reproducible across the
# signer (C5/propose) and the verifier (C4/apply). WIRE-BREAKING if changed.
_RFC3339_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")
# These intentionally MIRROR the (private) regexes in iac_plan_metadata rather
# than import that module's privates — keeps approvals.py self-contained.
_DIGITS = re.compile(r"^[0-9]+$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

# Max approval-window span. Fail-closed: a plan approval is an apply credential,
# so its validity window is short. Enforced both in new_approval_window (the
# happy path) AND in build_plan_approval_payload (so a caller that hand-crafts a
# payload, bypassing new_approval_window, cannot sign a year-2099 expiry).
_MAX_APPROVAL_TTL_MINUTES = 15

# The known terminal values of ``apply_audit.phase`` (written by the tofu-apply
# worker via claim_pending/set_apply_audit). This is DOCUMENTATION + a single
# source of truth for consumers (e.g. the C5 coordinator's reconcile/audit
# surfaces) — it is intentionally NOT enforced in set_apply_audit, so a future
# phase can be added at the worker without a lockstep library bump. Keep this in
# sync when a new phase is introduced.
#   - claimed:           burned but outcome-unknown (a crash between claim + terminal audit)
#   - applied:           tofu apply succeeded
#   - failed:               a tofu step failed (non-lock), state provably clean — HTTP 502
#   - failed_state_suspect: tofu apply failed AND state could not be proven clean
#                           (serial bump / post-failure refresh-only drift) — the
#                           failed apply may have persisted partial state; a state
#                           reconcile is required before retry — HTTP 502 (C5g)
#   - lock_refused:      a tofu step could not acquire the GCS state lock — HTTP 423 (C5d)
#   - drift_refused:     refresh-only detected out-of-band drift — HTTP 409
#   - integrity_refused: artifact fetch/integrity recompute failed — HTTP 422
#   - fidelity_refused:  version/lockfile/resource-set fidelity guard refused — HTTP 422
#   - verify_refused:    signed-payload re-derivation / parse refusal — HTTP 422
#   - tree_mismatch_refused: C6 — a create-class plan's baked iac/-tree hash != the
#                           approved head's iac_tree_hash (worker not re-baked from
#                           the merged main, or main advanced, or the sidecar failed
#                           its cross-check / was absent) — HTTP 409 (re-bake/re-plan,
#                           NOT a blind retry)
APPLY_AUDIT_PHASES = frozenset({
    "claimed", "applied", "failed", "failed_state_suspect", "lock_refused",
    "drift_refused", "integrity_refused", "fidelity_refused", "verify_refused",
    "tree_mismatch_refused",
})

# The exact top-level key set of a c3.v1 signed payload (from
# build_plan_approval_payload) — create() rejects anything else fail-closed.
_PLAN_PAYLOAD_TOP_KEYS = frozenset({
    "approval_schema_version", "metadata", "artifact_uri_metadata",
    "generation_metadata", "approver", "issued_at", "expires_at",
})


def _require(cond: bool, message: str) -> None:
    """Fail-closed validation helper — raise ValueError(message) unless cond."""
    if not cond:
        raise ValueError(message)


def new_approval_window(*, now: dt.datetime, ttl_minutes: int = 15) -> tuple[str, str]:
    """Compute the approval window. THE SINGLE place a window is produced.

    ``now`` is injected (no hidden clock) so it is deterministic + testable, and
    so the signed window and the stored window can never diverge — the signed
    ``expires_at`` is the one source of truth (:meth:`PlanApprovalStore.create`
    parses it back into the stored expiry).

    Returns ``(issued_at, expires_at)`` as frozen-format RFC3339 UTC strings
    (``+00:00``, no microseconds; see ``_RFC3339_UTC``).
    """
    # Reject naive datetimes — astimezone() would interpret them in the host's
    # LOCAL zone (Asia/Tokyo in this environment), silently shifting the window.
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("now: must be timezone-aware")
    if not (0 < ttl_minutes <= _MAX_APPROVAL_TTL_MINUTES):
        raise ValueError(
            f"ttl_minutes: must be in (0, {_MAX_APPROVAL_TTL_MINUTES}] (got {ttl_minutes})"
        )

    def _iso(d: dt.datetime) -> str:
        return d.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()

    return _iso(now), _iso(now + dt.timedelta(minutes=ttl_minutes))


def build_plan_approval_payload(
    *,
    metadata: dict[str, Any],
    artifact_uri_metadata: str,
    generation_metadata: str,
    approver: str,
    issued_at: str,
    expires_at: str,
) -> dict[str, Any]:
    """Build the canonical dict that gets HMAC-signed for a plan approval.

    Treats the (operator-supplied) ``metadata`` as UNTRUSTED: re-validates every
    c2.v1 field by round-tripping it through the canonical schema builder
    (:func:`driftscribe_lib.iac_plan_metadata.build_metadata`). This is what
    makes the signed payload tamper-evident — a malformed, renamed, or extra
    field can never be signed. Raises ``ValueError`` on any malformed input and
    NEVER returns a partial record.

    Shape (``c3.v1``):

    - the 15 c2.v1 fields nest verbatim under ``"metadata"`` so the C4 consumer
      can compare a re-fetched ``metadata.json`` against the signed copy in one
      shot (contract #1);
    - ``artifact_uri_metadata`` + ``generation_metadata`` are the out-of-band
      locator (``metadata.json`` cannot contain its own GCS generation), bound
      here so the consumer fetches EXACTLY that generation (contract #1);
    - ``approver`` is SIGNED (Decision B = D2) — see the module/plan docs for the
      enforcement caveat (its non-repudiation value depends on C4 verifying a
      trusted operator identity against this field);
    - ``issued_at`` / ``expires_at`` are the frozen-format window from
      :func:`new_approval_window`.
    """
    _require(isinstance(metadata, dict), "metadata: must be a dict")
    _require(
        tuple(sorted(metadata)) == tuple(sorted(_C2_METADATA_KEYS)),
        f"metadata: keys must be exactly the 15 c2.v1 keys (got {sorted(metadata)})",
    )
    _require(
        metadata["schema_version"] == METADATA_SCHEMA_VERSION,
        f"metadata.schema_version: must be {METADATA_SCHEMA_VERSION!r} "
        f"(got {metadata['schema_version']!r})",
    )

    # Re-validate every field through the canonical schema builder AND re-derive
    # the canonical metadata dict (single source of validation truth). Reject any
    # wrong-typed field (build_metadata raises ValueError; a bad python type for
    # the frozen dataclass would raise TypeError — fold both into ValueError).
    try:
        canonical_meta = build_metadata(MetadataInput(
            repo=metadata["repo"],
            pr_number=metadata["pr_number"],
            head_sha=metadata["head_sha"],
            base_sha=metadata["base_sha"],
            workflow_run_id=metadata["workflow_run_id"],
            workflow_run_attempt=metadata["workflow_run_attempt"],
            artifact_uri_plan=metadata["artifact_uri_plan"],
            artifact_uri_json=metadata["artifact_uri_json"],
            generation_plan=metadata["generation_plan"],
            generation_json=metadata["generation_json"],
            plan_sha256=metadata["plan_sha256"],
            plan_json_sha256=metadata["plan_json_sha256"],
            opentofu_version=metadata["opentofu_version"],
            provider_lockfile_sha256=metadata["provider_lockfile_sha256"],
        ))
    except TypeError as e:
        # A non-str value (e.g. None) for an unguarded field makes re.fullmatch
        # raise TypeError inside build_metadata; fold it into ValueError so the
        # builder NEVER leaks a TypeError (fail-closed on any malformed input).
        raise ValueError(f"metadata: malformed field ({e})")

    # The metadata locator must point at metadata.json in the SAME run dir as the
    # plan artifacts — ties the out-of-band locator to the signed artifact set.
    expected_metadata_uri = (
        canonical_meta["artifact_uri_plan"].rsplit("/", 1)[0] + "/metadata.json"
    )
    _require(
        artifact_uri_metadata == expected_metadata_uri,
        f"artifact_uri_metadata: must be exactly {expected_metadata_uri} "
        f"(got {artifact_uri_metadata!r})",
    )
    _require(
        isinstance(generation_metadata, str) and bool(_DIGITS.fullmatch(generation_metadata)),
        f"generation_metadata: must be a numeric string (got {generation_metadata!r})",
    )
    _require(
        isinstance(approver, str) and approver != "",
        "approver: must be a non-empty string",
    )
    _require(
        isinstance(issued_at, str) and bool(_RFC3339_UTC.fullmatch(issued_at)),
        f"issued_at: must be RFC3339 UTC '+00:00' with no microseconds (got {issued_at!r})",
    )
    _require(
        isinstance(expires_at, str) and bool(_RFC3339_UTC.fullmatch(expires_at)),
        f"expires_at: must be RFC3339 UTC '+00:00' with no microseconds (got {expires_at!r})",
    )

    # Semantic window validation — a caller that bypasses new_approval_window must
    # not be able to sign an inverted or arbitrarily-long window.
    issued_dt = _parse_rfc3339_utc(issued_at)
    expires_dt = _parse_rfc3339_utc(expires_at)
    _require(
        issued_dt < expires_dt,
        f"expires_at must be strictly after issued_at (issued={issued_at}, expires={expires_at})",
    )
    _require(
        expires_dt - issued_dt <= dt.timedelta(minutes=_MAX_APPROVAL_TTL_MINUTES),
        f"approval window must be <= {_MAX_APPROVAL_TTL_MINUTES} minutes "
        f"(issued={issued_at}, expires={expires_at})",
    )

    return {
        "approval_schema_version": PLAN_APPROVAL_SCHEMA_VERSION,
        "metadata": canonical_meta,
        "artifact_uri_metadata": artifact_uri_metadata,
        "generation_metadata": generation_metadata,
        "approver": approver,
        "issued_at": issued_at,
        "expires_at": expires_at,
    }


def canonicalize_payload(payload: dict[str, Any]) -> str:
    """Stable, compact canonical JSON for signing — sorted keys, no whitespace,
    ASCII-escaped. Deterministic pure function of ``payload``. WIRE-BREAKING if
    the form changes once any approval is minted live."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest_canonical(canonical: str) -> str:
    """SHA-256 hex of canonical-payload bytes. The SAME computation is used at
    create time and verify time, so a Firestore edit of ``payload_canonical``
    changes the digest and invalidates the HMAC."""
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_plan_approval_hmac(
    token: str, approval_id: str, payload_digest: str, hmac_key: str
) -> str:
    """Return the HMAC-SHA-256 hex binding a single-use ``token`` to ONE plan
    approval, identified by ``payload_digest`` (= SHA-256 of the canonical
    ``c3.v1`` payload).

    Mirrors :func:`compute_token_hmac` (the rollback path) with two upgrades:

    - the bound target is the digest of the WHOLE canonical payload (the 15
      c2.v1 fields + the two metadata locators + the approval window + the
      approver), so
      swapping to a different plan generation, head_sha, window, or approver
      changes the digest and the constant-time compare in C4's /apply fails;
    - a domain-separation tag (``_PLAN_APPROVAL_DOMAIN``) is mixed in first.

    Carried over: the raw ``token`` is returned once and never stored (only this
    HMAC is), and ``approval_id`` is bound (no cross-approval token reuse).

    The message is ``f"{domain}|{token}|{approval_id}|{payload_digest}"`` (UTF-8).
    The ``|`` is U+007C; ``token`` (urlsafe base64), ``approval_id`` (UUID4
    string), and ``payload_digest`` (64 lowercase hex) never emit it, so the
    parse is unambiguous. WIRE-BREAKING if the construction changes once live.
    """
    msg = f"{_PLAN_APPROVAL_DOMAIN}|{token}|{approval_id}|{payload_digest}".encode("utf-8")
    return hmac.new(hmac_key.encode("utf-8"), msg, hashlib.sha256).hexdigest()


class ArtifactIntegrityError(Exception):
    """Raised when a fetched artifact's recomputed SHA-256 does not match the
    digest bound in the signed approval. Carries which artifact + the expected
    and actual digests for the C4 deny log."""

    def __init__(self, artifact: str, expected_sha256: str, actual_sha256: str) -> None:
        self.artifact = artifact
        self.expected_sha256 = expected_sha256
        self.actual_sha256 = actual_sha256
        super().__init__(
            f"{artifact}: sha256 mismatch (expected {expected_sha256}, got {actual_sha256})"
        )


def verify_artifact_integrity(
    *,
    plan_tfplan_bytes: bytes,
    plan_json_bytes: bytes,
    expected_plan_sha256: str,
    expected_plan_json_sha256: str,
) -> None:
    """Recompute the SHA-256 of the two fetched artifacts and constant-time
    compare against the digests bound in the (already HMAC-verified) approval.

    PURE: operates only on in-memory bytes — NO network, NO files. The bytes
    MUST be the exact object bytes fetched from GCS (un-decoded, un-normalized)
    so the digest matches ``sha256sum plan.tfplan`` / ``plan.json`` computed by
    the C2 plan-builder (.github/workflows/iac.yml). Raises
    :class:`ArtifactIntegrityError` on the FIRST mismatch; returns ``None`` on a
    full match. (Contract #2 — the GCS fetch that supplies the bytes is the C4
    worker's job.)

    ``expected_*`` come from the signed metadata (already ``_HEX64``-shaped); we
    re-assert that shape so a corrupt record fails as a ValueError rather than a
    ``compare_digest`` TypeError on a non-ASCII string.
    """
    for name, value in (
        ("expected_plan_sha256", expected_plan_sha256),
        ("expected_plan_json_sha256", expected_plan_json_sha256),
    ):
        _require(
            isinstance(value, str) and bool(_HEX64.fullmatch(value)),
            f"{name}: must be 64 lowercase hex (got {value!r})",
        )

    actual_plan = hashlib.sha256(plan_tfplan_bytes).hexdigest()
    if not hmac.compare_digest(actual_plan, expected_plan_sha256):
        raise ArtifactIntegrityError("plan.tfplan", expected_plan_sha256, actual_plan)
    actual_json = hashlib.sha256(plan_json_bytes).hexdigest()
    if not hmac.compare_digest(actual_json, expected_plan_json_sha256):
        raise ArtifactIntegrityError("plan.json", expected_plan_json_sha256, actual_json)


@dataclass
class PlanApproval:
    """A single plan-bound approval record. Mirrors the Firestore doc shape 1:1
    so :meth:`PlanApprovalStore.get` / ``_claim`` populate it directly from
    ``snap.to_dict()``. ``payload_canonical`` is the source of truth; the
    denormalized fields below are for the C5 approval page and are all derivable
    from it. The terminal-transition audit fields are written atomically at
    claim time and are NOT HMAC inputs."""

    approval_id: str
    status: str  # "pending" | "used" | "denied"
    token_hmac: str
    payload_canonical: str
    payload_sha256: str             # audit/index ONLY — NEVER an input to verify_plan_approval
    expires_at: dt.datetime         # display/index ONLY — NOT HMAC-bound; the expiry DECISION must
                                    # read the signed window via plan_approval_is_expired() (the
                                    # denormalized dt is editable by a Firestore-write attacker)
    created_at: dt.datetime
    created_by: str
    pr_number: int
    head_sha: str
    artifact_uri_metadata: str
    generation_metadata: str
    used_at: dt.datetime | None = None
    used_by: str | None = None
    denied_at: dt.datetime | None = None
    denied_by: str | None = None
    operation_name: str | None = None
    # C4: a SINGLE nested audit map (apply_attempt_id, phase, freshness_exit_code,
    # apply_exit_code, apply_status, applied_at, state_serial, state_lineage, …).
    # One optional field (not many top-level keys) so a growing audit record never
    # breaks PlanApprovalStore.get()'s PlanApproval(approval_id=id, **data) on reads
    # of used docs. NOT an HMAC input — written at/after claim time only.
    apply_audit: dict[str, Any] | None = None


def _parse_rfc3339_utc(value: str) -> dt.datetime:
    """Parse a frozen-format (``_RFC3339_UTC``) string into a tz-aware UTC
    datetime. Validated upstream in :func:`build_plan_approval_payload`."""
    return dt.datetime.fromisoformat(value)


class PlanApprovalStore:
    """Firestore wrapper for the ``plan_approvals`` collection (Phase C3).

    Separate collection from the rollback ``approvals`` (different schema; lets
    the C4 worker hold the plan HMAC key without touching the rollback
    collection). Single doc per approval, keyed by ``approval_id`` (UUID4);
    lookups by primary key only. Same lazy/fake-injectable client pattern as
    :class:`ApprovalStore`.

    The C4 ``tofu-apply`` worker is the only service that calls
    :meth:`create` (at /propose, after independently verifying the artifact) and
    :meth:`claim_pending` (at /apply); the deny path goes through
    :meth:`claim_denied`, also behind the worker's HMAC check (never a direct
    coordinator flip).
    """

    def __init__(
        self, project: str, client: Any = None, *, database: str | None = None
    ) -> None:
        # ``database`` (Phase C5f) selects a NAMED Firestore database to isolate
        # ``plan_approvals`` from the coordinator's project-wide datastore.user.
        # ``None`` (the default) targets the project's ``(default)`` database,
        # exactly as before — so every existing caller and the fake-client tests
        # are unaffected. An injected ``client`` ignores ``database`` (the fake
        # already encodes whichever database the test means).
        self._client = client or firestore.Client(project=project, database=database)
        self._collection_name = "plan_approvals"

    def _ref(self, approval_id: str):  # noqa: ANN202
        return self._client.collection(self._collection_name).document(approval_id)

    def create(
        self,
        *,
        payload: dict[str, Any],
        hmac_key: str,
        created_by: str,
    ) -> tuple[PlanApproval, str]:
        """Create a pending plan approval; return ``(approval, raw_token)``.

        ``payload`` MUST come from :func:`build_plan_approval_payload` (already
        validated + canonical-shaped). The ``raw_token`` is returned ONLY here
        and is never persisted — only its HMAC lives in Firestore, so a
        Firestore exfiltration alone cannot mint an /apply request (the attacker
        also needs the Secret-Manager HMAC key the C4 worker holds).

        The stored ``expires_at`` is parsed from ``payload["expires_at"]`` so the
        signed window is the ONLY source of truth — there is no separate ttl knob
        that could diverge from the signed value.
        """
        _require(
            payload.get("approval_schema_version") == PLAN_APPROVAL_SCHEMA_VERSION,
            f"payload.approval_schema_version: must be {PLAN_APPROVAL_SCHEMA_VERSION!r}",
        )
        _require(
            frozenset(payload) == _PLAN_PAYLOAD_TOP_KEYS,
            "payload: must be the c3.v1 shape from build_plan_approval_payload "
            f"(top-level keys must be {sorted(_PLAN_PAYLOAD_TOP_KEYS)})",
        )
        approval_id = str(uuid.uuid4())
        raw_token = secrets.token_urlsafe(32)
        canonical = canonicalize_payload(payload)
        digest = _digest_canonical(canonical)
        token_hmac = compute_plan_approval_hmac(raw_token, approval_id, digest, hmac_key)

        now = dt.datetime.now(dt.timezone.utc)
        expires_at = _parse_rfc3339_utc(payload["expires_at"])

        data = {
            "status": "pending",
            "token_hmac": token_hmac,
            "payload_canonical": canonical,
            "payload_sha256": digest,
            "expires_at": expires_at,
            "created_at": now,
            "created_by": created_by,
            "pr_number": payload["metadata"]["pr_number"],
            "head_sha": payload["metadata"]["head_sha"],
            "artifact_uri_metadata": payload["artifact_uri_metadata"],
            "generation_metadata": payload["generation_metadata"],
        }
        self._ref(approval_id).set(data)
        return PlanApproval(approval_id=approval_id, **data), raw_token

    def get(self, approval_id: str) -> PlanApproval | None:
        """Read the approval doc; ``None`` if it doesn't exist. Plain
        non-transactional read — callers that mutate must use :meth:`claim_pending`
        / :meth:`claim_denied`."""
        snap = self._ref(approval_id).get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        return PlanApproval(approval_id=approval_id, **data)

    def claim_pending(
        self,
        approval_id: str,
        *,
        used_by: str,
        used_at: dt.datetime,
        apply_audit: dict[str, Any] | None = None,
    ) -> PlanApproval | None:
        """Transactionally flip ``status: pending -> used`` and record the actor
        that drove /apply (atomic with the flip). Returns the updated record, or
        ``None`` if the doc is missing or not pending (already used/denied) — the
        canonical single-use replay defense (mirrors :meth:`ApprovalStore.claim_pending`).

        ``apply_audit`` (C4) is an optional nested map written **atomically with the
        status flip** so a worker crash after the claim but before the terminal audit
        leaves a ``used`` doc whose ``apply_audit.phase`` records the outcome-unknown
        state (never a silent ``used``). It MUST be a plain dict; it lands only under
        the ``apply_audit`` key and so cannot collide with the control fields
        (``status``/``used_by``/``used_at``/``token_hmac``/``payload_canonical``/…)."""
        extra: dict[str, Any] = {"used_by": used_by, "used_at": used_at}
        if apply_audit is not None:
            if not isinstance(apply_audit, dict):
                raise TypeError("apply_audit must be a dict")
            extra["apply_audit"] = apply_audit
        return self._claim(approval_id, new_status="used", extra=extra)

    def set_apply_audit(self, approval_id: str, audit: dict[str, Any]) -> None:
        """Write the terminal ``apply_audit`` record (post-apply, non-transactional).

        Separate from the transactional claim because it runs AFTER the heavy
        re-checks + ``tofu apply`` — it is audit, not control-flow. Overwrites the
        ``apply_audit`` key only (never touches ``status`` or any HMAC-bound field).
        Used to record the terminal phase (see :data:`APPLY_AUDIT_PHASES` for the
        known vocabulary — ``applied``/``failed``/``lock_refused``/``drift_refused``/
        ``integrity_refused``/…), the tofu exit codes, and the observed state
        serial/lineage. A plain ``update`` (no transaction): the single-use claim
        already happened, so there is no concurrency to guard here. ``phase`` is
        deliberately NOT validated against :data:`APPLY_AUDIT_PHASES` here so the
        worker can introduce a new phase without a lockstep library change."""
        if not isinstance(audit, dict):
            raise TypeError("audit must be a dict")
        self._ref(approval_id).update({"apply_audit": audit})

    def claim_denied(
        self, approval_id: str, *, denied_by: str, denied_at: dt.datetime
    ) -> PlanApproval | None:
        """Transactionally flip ``status: pending -> denied`` and record the actor
        (atomic with the flip). Like the rollback deny path, the C4 worker calls
        this only behind its HMAC check — never a direct coordinator flip."""
        return self._claim(
            approval_id, new_status="denied", extra={"denied_by": denied_by, "denied_at": denied_at}
        )

    def _claim(
        self, approval_id: str, *, new_status: str, extra: dict[str, Any]
    ) -> PlanApproval | None:
        """Shared transactional flip helper. Kept private so the set of target
        statuses stays closed (callers go through claim_pending/claim_denied)."""
        ref = self._ref(approval_id)

        @firestore.transactional
        def txn(transaction, ref):  # noqa: ANN001
            snap = ref.get(transaction=transaction)
            if not snap.exists:
                return None
            data = snap.to_dict() or {}
            if data.get("status") != "pending":
                return None
            update = {"status": new_status, **extra}
            transaction.update(ref, update)
            data.update(update)
            return PlanApproval(approval_id=approval_id, **data)

        return txn(self._client.transaction(), ref)


def verify_plan_approval(
    presented_token: str, stored: PlanApproval, hmac_key: str
) -> bool:
    """Constant-time verify a presented raw token against a stored plan approval.

    Recomputes the payload digest FROM ``stored.payload_canonical`` (the source
    of truth — NOT the denormalized ``payload_sha256``, which is audit-only), so
    a Firestore edit of either ``payload_canonical`` or ``token_hmac`` fails this
    check. Then recomputes the HMAC and compares it to ``stored.token_hmac`` in
    constant time. Mirrors the rollback worker's /execute HMAC step.

    Returns a bool ONLY. The caller (C4 /apply) is responsible — in this order —
    for the status + expiry pre-checks, the ``current actor == signed approver``
    check, and the single-use claim. See the plan §3.6 consumer contract.
    """
    digest = _digest_canonical(stored.payload_canonical)
    expected = compute_plan_approval_hmac(
        presented_token, stored.approval_id, digest, hmac_key
    )
    return hmac.compare_digest(expected, stored.token_hmac)


def signed_payload(stored: PlanApproval) -> dict[str, Any]:
    """Return the HMAC-bound payload as a dict, parsed from ``payload_canonical``.

    EVERY apply-time decision in C4 MUST read its inputs from THIS dict (the
    signed source of truth) — ``generation_metadata``, ``artifact_uri_metadata``,
    ``head_sha``, ``pr_number``, the plan digests under ``metadata``, the window —
    NEVER from the denormalized :class:`PlanApproval` dataclass fields (those are
    display/index-only and are NOT HMAC-bound, so a Firestore-write attacker could
    edit them). Call :func:`verify_plan_approval` FIRST so these bytes are trusted.
    """
    return json.loads(stored.payload_canonical)


def plan_approval_is_expired(stored: PlanApproval, *, now: dt.datetime) -> bool:
    """Return True if the approval's SIGNED expiry has lapsed.

    Reads ``expires_at`` from the HMAC-bound payload (via :func:`signed_payload`),
    NOT the denormalized ``stored.expires_at`` datetime. The denormalized field is
    display/index-only and is NOT covered by the HMAC, so an attacker with
    Firestore write access (but without the HMAC key — C3's stated threat model)
    could push it into the future. Using the signed window closes that
    TTL-bypass replay. The caller (C4 /apply) MUST call :func:`verify_plan_approval`
    first so the bound bytes are trusted, THEN this expiry check.

    ``now`` is injected (no hidden clock) so the check is deterministic + testable.
    """
    signed_expires = _parse_rfc3339_utc(signed_payload(stored)["expires_at"])
    return signed_expires < now
