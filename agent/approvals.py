"""Coordinator-side approval helpers (Phase 11.7).

This module is the coordinator's narrow window into the shared
``approvals/`` Firestore collection. The rollback worker owns the
``pending → used`` transition; the coordinator owns ``pending → denied``.
Both transitions go through transactional ``_claim`` helpers on
:class:`driftscribe_lib.approvals.ApprovalStore` so the state machine
stays closed:

    pending --[rollback worker /execute, HMAC-verified]--> used
    pending --[coordinator deny button]------------------> denied
    {used, denied, expired-by-time, missing} -----------> 403 from any side

The coordinator NEVER flips to ``used``. That's the rollback worker's
job — and the only reason the worker can do it is because it (and only
it) holds the HMAC key. Splitting authority this way means a compromised
coordinator cannot mint executions; it can only refuse them.
"""
from __future__ import annotations

import datetime as dt
import os
from functools import lru_cache

from driftscribe_lib.approvals import Approval, ApprovalStore


@lru_cache(maxsize=1)
def _store_singleton() -> ApprovalStore:
    """Process-wide :class:`ApprovalStore` for the coordinator.

    Lazy so tests can monkeypatch :func:`get_approval_store` to inject
    a fake without first triggering a real Firestore client construction.
    The cache is cleared via :func:`_reset_for_tests` in the integration
    suite — same pattern as :func:`agent.main.get_state`.
    """
    # Read project lazily so tests can set GCP_PROJECT after import.
    project = os.environ.get("GCP_PROJECT", "")
    return ApprovalStore(project=project)


def get_approval_store() -> ApprovalStore:
    """Public accessor — tests monkeypatch this to inject a fake store."""
    return _store_singleton()


def _reset_for_tests() -> None:
    """Test helper — drop the cached store singleton."""
    _store_singleton.cache_clear()


def deny(store: ApprovalStore, approval_id: str) -> Approval | None:
    """Transactionally flip the approval to ``denied``.

    Returns the updated :class:`Approval` on success, or ``None`` if:

    - the doc doesn't exist, OR
    - the doc's status was not ``"pending"``.

    The ``None`` cases collapse to a 403 at the HTTP layer — the
    operator gets the same response whether they replayed an
    already-denied request or tried to deny something that never
    existed. Distinguishing these would leak doc presence to an
    unauthenticated probe.
    """
    return store.claim_denied(approval_id)


def is_expired(approval: Approval, *, now: dt.datetime | None = None) -> bool:
    """Return True if the approval's TTL has lapsed.

    The store records ``expires_at`` but does NOT act on it — by design,
    so the coordinator can render countdowns on the approval page
    without the store needing to be clock-aware. The 403 is enforced
    at the worker's ``/execute`` handler; the page renders a "this has
    expired" notice using this helper.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    return approval.expires_at < now
