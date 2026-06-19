"""(Firestore) cache store for the adopted/changed ``.tf`` source shown on the
``GET /iac-approvals/{pr_number}`` page.

The approval page can show the actual OpenTofu source a PR adds/changes (the
"view source" affordance). Fetching it means a GitHub API round-trip per changed
``.tf`` file; without a cache that cost would be paid on every page load (and the
coordinator runs ``--min-instances=0``, so an in-process cache dies on every
scale-to-zero recycle). This store persists the fetched source to Firestore so a
warm load — and a freshly-spun instance — serves it without touching GitHub.

Design notes (mirrors ``infra_graph_cache_store.py``):

* It is a **dumb per-PR persistence layer**: ``get(pr_number)`` / ``set(pr_number,
  record)`` of one document per PR. TTL, ``format_version``, and head_sha
  freshness validation all live in the caller (``agent.main`` owns the wall-clock
  math + the "does the cached head_sha still match the PR's head" check), so a
  stale-sha doc is simply re-fetched and overwritten.
* **One document per PR** (not per ``{pr}@{sha}``): a new push overwrites the
  same doc, so docs never accumulate. Correctness comes from the caller validating
  ``record["head_sha"] == view.head_sha`` on read; a mismatch is a miss.
* **Fail-soft is the contract.** A cache must never turn the always-200 approval
  GET into a 5xx: every Firestore touch (including the lazy client construction)
  is wrapped so a read error degrades to a miss (``None`` → caller falls through
  to a live fetch) and a write error is logged and swallowed (``False``).
* The Firestore client is constructed **lazily on first use** so the
  backend-selection branch can instantiate the store without GCP creds.

The coordinator runtime SA already holds ``roles/datastore.user``.
"""
from __future__ import annotations

import copy
import logging
from typing import Any, Protocol

log = logging.getLogger("driftscribe.agent.iac_pr_source_cache")

# Dedicated collection (one doc per PR) — separate from the ``config`` singletons.
_COLLECTION = "iac_pr_source"


def _construct_client(project: str):
    """Build a real Firestore client. Indirected through a module function so
    tests can patch it and assert lazy/once construction without GCP creds."""
    from google.cloud import firestore

    return firestore.Client(project=project)


class IacPrSourceCacheStore(Protocol):
    def get(self, pr_number: int) -> dict[str, Any] | None: ...
    # Returns True iff the record was durably stored (False on a swallowed write
    # failure), so the refresh endpoint can report whether it actually persisted.
    def set(self, pr_number: int, record: dict[str, Any]) -> bool: ...


class InMemoryIacPrSourceCacheStore:
    """Process-local double. Used when no GCP project is configured (and, via the
    test injection seam, in tests). Deep-copies on both set and get so a caller
    mutating the served record — including its nested ``files`` — can't corrupt
    the stored one."""

    def __init__(self) -> None:
        self._records: dict[int, dict[str, Any]] = {}

    def get(self, pr_number: int) -> dict[str, Any] | None:
        rec = self._records.get(pr_number)
        return copy.deepcopy(rec) if rec is not None else None

    def set(self, pr_number: int, record: dict[str, Any]) -> bool:
        self._records[pr_number] = copy.deepcopy(record)
        return True


class FirestoreIacPrSourceCacheStore:
    """Firestore-backed store. ``client`` is injectable for tests; when omitted,
    a real client is constructed lazily on first ``get``/``set`` (inside the
    fail-soft guard, so a construction/auth error degrades to a miss too)."""

    def __init__(self, project: str, client: Any = None) -> None:
        self._project = project
        self._client = client
        self._collection_ref = None  # built lazily

    def _collection(self):
        if self._collection_ref is None:
            if self._client is None:
                self._client = _construct_client(self._project)
            self._collection_ref = self._client.collection(_COLLECTION)
        return self._collection_ref

    def get(self, pr_number: int) -> dict[str, Any] | None:
        try:
            snap = self._collection().document(str(pr_number)).get()
            return snap.to_dict() if snap.exists else None
        except Exception as e:  # noqa: BLE001 — cache must never raise into the request
            # Log the exception TYPE, not str(e): a PermissionDenied's message
            # embeds the full document resource path (project id + collection).
            log.warning("iac_pr_source_read_failed", extra={"error": type(e).__name__})
            return None

    def set(self, pr_number: int, record: dict[str, Any]) -> bool:
        try:
            self._collection().document(str(pr_number)).set(record)
            return True
        except Exception as e:  # noqa: BLE001 — a cache-write failure must not fail the request
            log.warning("iac_pr_source_write_failed", extra={"error": type(e).__name__})
            return False
