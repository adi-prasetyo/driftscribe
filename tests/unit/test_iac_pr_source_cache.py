"""Unit tests for the (Firestore) IaC PR-source cache store.

Mirrors ``test_infra_graph_cache_store.py``: the store is a dumb persistence
layer keyed PER PR (``get(pr_number)`` point-reads, ``set(pr_number, record)``
full-overwrites a single document per PR). TTL / format-version / head_sha
freshness logic lives in the CALLER (agent.main), not here — so these tests pin
only persistence + the fail-soft contract (a cache must NEVER raise into the
read-only approval-page request path).

Dict-backed fake Firestore injected via the store's ``client=`` kwarg, so no real
Firestore / GCP creds are touched.
"""
from __future__ import annotations

from agent.iac_pr_source_cache import (
    _COLLECTION,
    FirestoreIacPrSourceCacheStore,
    InMemoryIacPrSourceCacheStore,
)

_RECORD = {
    "format_version": 1,
    "written_at": 1000.0,
    "head_sha": "a" * 40,
    "files": [{"path": "iac/adopt_bucket_x.tf", "content": 'resource "x" {}\n'}],
    "truncated": False,
}


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
    """Records which collection was opened so the test can assert it."""

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
    assert InMemoryIacPrSourceCacheStore().get(42) is None


def test_inmemory_set_returns_true():
    assert InMemoryIacPrSourceCacheStore().set(42, _RECORD) is True


def test_inmemory_round_trips_a_deep_copy():
    store = InMemoryIacPrSourceCacheStore()
    store.set(42, _RECORD)
    got = store.get(42)
    assert got == _RECORD
    # Mutating the returned record — including its NESTED files — must not corrupt
    # the stored copy (deep-copy isolation, not just top-level).
    got["head_sha"] = "z" * 40
    got["files"][0]["content"] = "TAMPERED"
    again = store.get(42)
    assert again["head_sha"] == "a" * 40
    assert again["files"][0]["content"] == 'resource "x" {}\n'


def test_inmemory_keys_are_per_pr():
    store = InMemoryIacPrSourceCacheStore()
    store.set(42, _RECORD)
    assert store.get(43) is None
    store.set(43, {**_RECORD, "head_sha": "b" * 40})
    assert store.get(42)["head_sha"] == "a" * 40
    assert store.get(43)["head_sha"] == "b" * 40


def test_inmemory_set_overwrites_same_pr():
    store = InMemoryIacPrSourceCacheStore()
    store.set(42, _RECORD)
    store.set(42, {**_RECORD, "head_sha": "c" * 40})
    assert store.get(42)["head_sha"] == "c" * 40


# --------------------------------------------------------------------------- #
# Firestore-backed store (injected fake client)
# --------------------------------------------------------------------------- #


def test_firestore_set_writes_per_pr_doc_in_collection():
    fake = _FakeFirestore()
    store = FirestoreIacPrSourceCacheStore(project="p", client=fake)
    assert store.set(42, _RECORD) is True
    assert fake.collections_opened == [_COLLECTION]
    assert fake.docs["42"] == _RECORD


def test_firestore_get_returns_record_when_present():
    fake = _FakeFirestore()
    store = FirestoreIacPrSourceCacheStore(project="p", client=fake)
    store.set(42, _RECORD)
    assert store.get(42) == _RECORD


def test_firestore_get_returns_none_when_absent():
    store = FirestoreIacPrSourceCacheStore(project="p", client=_FakeFirestore())
    assert store.get(42) is None


def test_firestore_get_fail_soft_returns_none():
    store = FirestoreIacPrSourceCacheStore(project="p", client=_BoomFirestore())
    assert store.get(42) is None  # must not raise


def test_firestore_set_fail_soft_returns_false():
    store = FirestoreIacPrSourceCacheStore(project="p", client=_BoomFirestore())
    assert store.set(42, _RECORD) is False  # must not raise


def test_firestore_client_constructed_lazily_once(monkeypatch):
    import agent.iac_pr_source_cache as mod

    calls = {"n": 0}

    def _fake_construct(project):
        calls["n"] += 1
        return _FakeFirestore()

    monkeypatch.setattr(mod, "_construct_client", _fake_construct)

    store = FirestoreIacPrSourceCacheStore(project="p")  # client=None → lazy
    assert calls["n"] == 0, "no client built at __init__"
    store.set(42, _RECORD)
    store.get(42)
    assert calls["n"] == 1, "client built once on first use, then memoized"
