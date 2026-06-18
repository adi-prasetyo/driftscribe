"""Unit tests for the L2 (Firestore) infra-graph cache store.

The store is a dumb singleton-document persistence layer for the
``GET /infra/graph`` cache: ``get()`` point-reads the record, ``set()``
full-overwrites it. TTL / format-version / freshness logic lives in the CALLER
(agent.main.get_infra_graph), not here — so these tests pin only persistence +
the fail-soft contract (a cache must NEVER raise into the request path).

Mirrors the in-process Firestore fake style of test_approval_store.py: a
dict-backed fake client injected via the store's ``client=`` kwarg, so no real
Firestore / GCP creds are touched.
"""
from __future__ import annotations

from agent.infra_graph_cache_store import (
    _CACHE_DOC_ID,
    FirestoreInfraGraphCacheStore,
    InMemoryInfraGraphCacheStore,
)

_RECORD = {"format_version": 1, "written_at": 1000.0, "payload": {"total_resources": 3}}


# --------------------------------------------------------------------------- #
# Dict-backed fake Firestore (only the surface the store uses)
# --------------------------------------------------------------------------- #


class _FakeSnap:
    def __init__(self, data):
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _FakeDocRef:
    def __init__(self, store, key):
        self._store = store
        self._key = key

    def get(self):
        return _FakeSnap(self._store.get(self._key))

    def set(self, data):
        self._store[self._key] = dict(data)


class _FakeCollection:
    def __init__(self, store):
        self._store = store

    def document(self, key):
        return _FakeDocRef(self._store, key)


class _FakeFirestore:
    """Records which collection was opened so the test can assert it's ``config``."""

    def __init__(self):
        self.docs: dict[str, dict] = {}
        self.collections_opened: list[str] = []

    def collection(self, name):
        self.collections_opened.append(name)
        return _FakeCollection(self.docs)


class _BoomFirestore:
    """Every access raises — exercises the fail-soft contract."""

    def collection(self, name):
        raise RuntimeError("firestore unavailable")


# --------------------------------------------------------------------------- #
# InMemory double
# --------------------------------------------------------------------------- #


def test_inmemory_get_is_none_before_any_set():
    assert InMemoryInfraGraphCacheStore().get() is None


def test_inmemory_set_returns_true():
    assert InMemoryInfraGraphCacheStore().set(_RECORD) is True


def test_inmemory_round_trips_a_deep_copy():
    store = InMemoryInfraGraphCacheStore()
    store.set(_RECORD)
    got = store.get()
    assert got == _RECORD
    # Mutating the returned record — including its NESTED payload — must not
    # corrupt the stored copy (deep-copy isolation, not just top-level).
    got["written_at"] = 0.0
    got["payload"]["total_resources"] = 999
    again = store.get()
    assert again["written_at"] == 1000.0
    assert again["payload"]["total_resources"] == 3


def test_inmemory_set_overwrites():
    store = InMemoryInfraGraphCacheStore()
    store.set(_RECORD)
    store.set({"format_version": 2, "written_at": 2000.0, "payload": {}})
    assert store.get()["format_version"] == 2


# --------------------------------------------------------------------------- #
# Firestore-backed store (injected fake client)
# --------------------------------------------------------------------------- #


def test_firestore_set_writes_singleton_doc_in_config_collection():
    fake = _FakeFirestore()
    store = FirestoreInfraGraphCacheStore(project="p", client=fake)
    assert store.set(_RECORD) is True
    assert fake.collections_opened == ["config"]
    assert fake.docs[_CACHE_DOC_ID] == _RECORD


def test_firestore_get_returns_record_when_present():
    fake = _FakeFirestore()
    store = FirestoreInfraGraphCacheStore(project="p", client=fake)
    store.set(_RECORD)
    assert store.get() == _RECORD


def test_firestore_get_returns_none_when_absent():
    store = FirestoreInfraGraphCacheStore(project="p", client=_FakeFirestore())
    assert store.get() is None


def test_firestore_get_fail_soft_returns_none(caplog):
    """A Firestore read error must degrade to a miss, never raise into the
    request path (the panel falls through to a live fetch)."""
    store = FirestoreInfraGraphCacheStore(project="p", client=_BoomFirestore())
    assert store.get() is None


def test_firestore_set_fail_soft_returns_false(caplog):
    """A Firestore write error must never fail the request — the response DTO is
    already built; a cache-write failure is logged, swallowed, and reported as
    False so the pre-warm endpoint can tell it didn't actually persist."""
    store = FirestoreInfraGraphCacheStore(project="p", client=_BoomFirestore())
    assert store.set(_RECORD) is False  # must not raise


def test_firestore_client_constructed_lazily_once(monkeypatch):
    """Construction is deferred until the cache is first consulted (so the
    backend-selection branch can instantiate the store without GCP creds), and
    memoized across calls."""
    import agent.infra_graph_cache_store as mod

    calls = {"n": 0}

    def _fake_construct(project):
        calls["n"] += 1
        return _FakeFirestore()

    monkeypatch.setattr(mod, "_construct_client", _fake_construct)

    store = FirestoreInfraGraphCacheStore(project="p")  # client=None → lazy
    assert calls["n"] == 0, "no client built at __init__"
    store.set(_RECORD)
    store.get()
    assert calls["n"] == 1, "client built once on first use, then memoized"
