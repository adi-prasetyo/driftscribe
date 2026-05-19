"""Coordinator-side approval helpers (Phase 11.7, revised in 11.9).

This module is the coordinator's narrow window into the shared
``approvals/`` Firestore collection. As of Phase 11.9, the rollback
worker owns BOTH terminal transitions:

    pending --[rollback worker /execute, HMAC-verified]--> used
    pending --[rollback worker /deny,    HMAC-verified]--> denied
    {used, denied, expired-by-time, missing} -----------> 403 from any side

The coordinator is reduced to a read-only consumer of this collection
for rendering the approval page — it never flips status directly. The
pre-11.9 design had the coordinator owning the ``pending → denied``
flip without HMAC verification; Codex review of 11.7 flagged this as a
HITL availability bug (anyone with just an ``approval_id`` could deny a
pending rollback). See the module docstring of
``driftscribe_lib.approvals`` and the ``/deny`` handler in
``workers/rollback/main.py`` for the fix.

The ``ApprovalStore.claim_denied`` method is still used — but only from
the rollback worker, behind the same HMAC + token check that gates
``/execute``. The coordinator's previous ``deny()`` helper has been
deleted because it bypassed that check.
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
