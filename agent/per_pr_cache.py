"""Generic Firestore-backed per-PR cache (one document per PR number).

A small generalisation of ``iac_pr_source_cache.py`` for the open-trace
follow-up (2026-06-27), which needs two more per-PR read-through caches:

* ``iac_pr_merge_status`` — the merge-status reconcile probe (is the PR merged
  at the as-applied head_sha?).
* ``iac_pr_body`` — the agent-authored PR body shown in the open-trace card.

Same contract as the source cache:

* A **dumb per-PR persistence layer**: ``get(pr)`` / ``set(pr, record)`` of one
  document per PR. TTL, ``format_version`` and head_sha freshness validation all
  live in the caller (``agent.main``), so a stale doc is simply re-fetched and
  overwritten.
* **Fail-soft is the contract**: a read error degrades to a miss (``None``); a
  write error is logged and swallowed (``False``). A cache must never turn an
  always-200 serve path into a 5xx.
* The Firestore client is constructed **lazily on first use** so the backend
  can be selected without GCP creds (tests / local).

Backend selection (Firestore when ``gcp_project`` is set, else the in-memory
double) is the caller's job, gated on ``gcp_project`` ALONE — these are
read-only caches that must persist through ``DRY_RUN=true`` and scale-to-zero.
"""
from __future__ import annotations

import copy
import logging
from typing import Any, Protocol

log = logging.getLogger("driftscribe.agent.per_pr_cache")


def _construct_client(project: str):
    """Build a real Firestore client. Indirected so tests can patch it and
    assert lazy/once construction without GCP creds."""
    from google.cloud import firestore

    return firestore.Client(project=project)


class PerPrCacheStore(Protocol):
    def get(self, pr_number: int) -> dict[str, Any] | None: ...
    # True iff durably stored (False on a swallowed write failure).
    def set(self, pr_number: int, record: dict[str, Any]) -> bool: ...


class InMemoryPerPrCacheStore:
    """Process-local double. Used when no GCP project is configured (and, via the
    test injection seam, in tests). Deep-copies on both set and get so a caller
    mutating the served record can't corrupt the stored one."""

    def __init__(self) -> None:
        self._records: dict[int, dict[str, Any]] = {}

    def get(self, pr_number: int) -> dict[str, Any] | None:
        rec = self._records.get(pr_number)
        return copy.deepcopy(rec) if rec is not None else None

    def set(self, pr_number: int, record: dict[str, Any]) -> bool:
        self._records[pr_number] = copy.deepcopy(record)
        return True


class FirestorePerPrCacheStore:
    """Firestore-backed store over a named collection. ``client`` is injectable
    for tests; when omitted a real client is constructed lazily on first
    ``get``/``set`` (inside the fail-soft guard, so a construction/auth error
    degrades to a miss too)."""

    def __init__(self, *, collection: str, project: str, client: Any = None) -> None:
        self._collection_name = collection
        self._project = project
        self._client = client
        self._collection_ref = None  # built lazily

    def _collection(self):
        if self._collection_ref is None:
            if self._client is None:
                self._client = _construct_client(self._project)
            self._collection_ref = self._client.collection(self._collection_name)
        return self._collection_ref

    def get(self, pr_number: int) -> dict[str, Any] | None:
        try:
            snap = self._collection().document(str(pr_number)).get()
            return snap.to_dict() if snap.exists else None
        except Exception as e:  # noqa: BLE001 — a cache read must never raise into the request
            # Log the exception TYPE, not str(e): a PermissionDenied's message
            # embeds the full document resource path.
            log.warning(
                "per_pr_cache_read_failed",
                extra={"error": type(e).__name__, "collection": self._collection_name},
            )
            return None

    def set(self, pr_number: int, record: dict[str, Any]) -> bool:
        try:
            self._collection().document(str(pr_number)).set(record)
            return True
        except Exception as e:  # noqa: BLE001 — a cache-write failure must not fail the request
            log.warning(
                "per_pr_cache_write_failed",
                extra={"error": type(e).__name__, "collection": self._collection_name},
            )
            return False
