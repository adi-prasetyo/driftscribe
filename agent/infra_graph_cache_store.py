"""L2 (Firestore) cache store for ``GET /infra/graph``.

A single Cloud Asset Inventory enumeration takes ~25-35s, and the coordinator
runs ``--min-instances=0`` so the in-process L1 cache (agent/main.py
``_INFRA_INVENTORY_CACHE``) dies on every scale-to-zero recycle. This store
persists the (redacted) inventory to a single Firestore document so a
freshly-spun instance can serve a warm map instead of paying the live fetch.

Design notes (see docs/plans/2026-06-18-infra-graph-l2-firestore-cache.md):

* It is a **dumb singleton-document persistence layer**: ``get()`` /``set()`` of
  one record. TTL, ``format_version``, and freshness validation all live in the
  caller (``agent.main`` owns the wall-clock math), mirroring how the in-process
  L1 cache keeps its freshness logic in the request handler.
* **Fail-soft is the contract.** A cache must never turn ``GET /infra/graph``
  into a 5xx: every Firestore touch (including the lazy client construction) is
  wrapped so a read error degrades to a miss (``None`` → caller falls through to
  a live fetch) and a write error is logged and swallowed (the response DTO is
  already built).
* The Firestore client is constructed **lazily on first use** (not at
  ``__init__``) so the backend-selection branch can instantiate the store
  without GCP creds, and a cold instance pays no auth cost until the cache is
  actually consulted.

The record lives in the existing ``config`` collection (alongside the
``config/pause`` and ``config/autonomy`` singletons) — tiny write volume, and
the coordinator runtime SA already holds ``datastore.user``.
"""
from __future__ import annotations

import copy
import logging
from typing import Any, Protocol

log = logging.getLogger("driftscribe.agent.infra_graph_cache")

# Singleton document id within the ``config`` collection.
_CACHE_DOC_ID = "infra_graph_cache"


def _construct_client(project: str):
    """Build a real Firestore client. Indirected through a module function so
    tests can patch it and assert lazy/once construction without GCP creds."""
    from google.cloud import firestore

    return firestore.Client(project=project)


class InfraGraphCacheStore(Protocol):
    def get(self) -> dict[str, Any] | None: ...
    # Returns True iff the record was durably stored. The Firestore impl returns
    # False (not raise) on a swallowed write failure, so the pre-warm endpoint
    # can report whether the persistent layer was actually warmed rather than
    # falsely claiming success on an IAM/network error.
    def set(self, record: dict[str, Any]) -> bool: ...


class InMemoryInfraGraphCacheStore:
    """Process-local double. Used when no GCP project is configured (and, via the
    test injection seam, in unit/integration tests). Deep-copies on both set and
    get so a caller mutating the served record — including its nested ``payload``
    — can't corrupt the stored one."""

    def __init__(self) -> None:
        self._record: dict[str, Any] | None = None

    def get(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._record) if self._record is not None else None

    def set(self, record: dict[str, Any]) -> bool:
        self._record = copy.deepcopy(record)
        return True


class FirestoreInfraGraphCacheStore:
    """Firestore-backed store. ``client`` is injectable for tests; when omitted,
    a real client is constructed lazily on first ``get``/``set`` (inside the
    fail-soft guard, so a construction/auth error degrades to a miss too)."""

    def __init__(self, project: str, client: Any = None) -> None:
        self._project = project
        self._client = client
        self._config = None  # the ``config`` collection ref, built lazily

    def _collection(self):
        if self._config is None:
            if self._client is None:
                self._client = _construct_client(self._project)
            self._config = self._client.collection("config")
        return self._config

    def get(self) -> dict[str, Any] | None:
        try:
            snap = self._collection().document(_CACHE_DOC_ID).get()
            return snap.to_dict() if snap.exists else None
        except Exception as e:  # noqa: BLE001 — cache must never raise into the request
            # Log the exception TYPE, not str(e): a PermissionDenied's message
            # embeds the full document resource path (project id + collection),
            # which we'd rather not spray into WARNING logs / exported sinks.
            log.warning("infra_graph_l2_read_failed", extra={"error": type(e).__name__})
            return None

    def set(self, record: dict[str, Any]) -> bool:
        try:
            self._collection().document(_CACHE_DOC_ID).set(record)
            return True
        except Exception as e:  # noqa: BLE001 — a cache-write failure must not fail the request
            log.warning("infra_graph_l2_write_failed", extra={"error": type(e).__name__})
            return False
