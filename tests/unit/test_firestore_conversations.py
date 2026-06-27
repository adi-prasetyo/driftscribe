"""FirestoreStateStore conversation methods over a fake client.

Verifies the transactional seq allocation + doc/subcollection shape without a
live Firestore. The fake implements just enough of the client surface the impl
touches (document/collection/get/set/update/stream/transaction).
"""
import itertools
import sys
import types as _t

import pytest

from agent.state_store import FirestoreStateStore

# sentinel matching firestore.SERVER_TIMESTAMP identity in the impl path
_SERVER_TS = object()


class _Snap:
    def __init__(self, data, create_time=None):
        self._data = data
        self.exists = data is not None
        self.create_time = create_time

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _DocRef:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    def collection(self, name):
        return _CollRef(self._store, f"{self._path}/{name}")

    def get(self, transaction=None):
        data = self._store.docs.get(self._path)
        return _Snap(data, self._store.create_times.get(self._path))

    def set(self, data, merge=False):
        if merge and self._path in self._store.docs:
            self._store.docs[self._path].update(self._resolve(data))
        else:
            self._store.docs[self._path] = self._resolve(data)
        self._store.create_times.setdefault(self._path, next(self._store._counter))

    def update(self, data):
        self._store.docs[self._path].update(self._resolve(data))

    def _resolve(self, data):
        out = {}
        for k, v in data.items():
            out[k] = next(self._store._counter) if v is _SERVER_TS else v
        return out


class _CollRef:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    def document(self, doc_id):
        return _DocRef(self._store, f"{self._path}/{doc_id}")

    def where(self, field, op, value):
        return _Query(self._store, self._path, [(field, op, value)])

    def stream(self):
        return _Query(self._store, self._path, []).stream()


class _Query:
    def __init__(self, store, path, filters):
        self._store = store
        self._path = path
        self._filters = filters

    def where(self, field, op, value):
        return _Query(self._store, self._path, self._filters + [(field, op, value)])

    def stream(self):
        for path, data in self._store.docs.items():
            parent, _, _ = path.rpartition("/")
            if parent != self._path:
                continue
            if all(data.get(f) == v for f, _op, v in self._filters):
                yield _Snap(data, self._store.create_times.get(path))


class _Txn:
    def __init__(self, store):
        self._store = store

    def get(self, ref, **kw):
        return ref.get()

    def set(self, ref, data):
        ref.set(data)

    def update(self, ref, data):
        ref.update(data)


class _FakeClient:
    def __init__(self):
        self.docs = {}
        self.create_times = {}
        self._counter = itertools.count(1)

    def collection(self, name):
        return _CollRef(self, name)

    def transaction(self):
        return _Txn(self)

    def batch(self):
        raise AssertionError("conversations must not use a batch")


@pytest.fixture
def store(monkeypatch):
    """FirestoreStateStore wired to the fake client + a stubbed firestore module.

    The impl does ``from google.cloud import firestore`` inside each method and
    uses ``firestore.SERVER_TIMESTAMP`` + ``@firestore.transactional``. Patch a
    fake module so those resolve to our sentinel + a pass-through decorator (the
    fake transaction needs no retry loop).
    """
    fake_fs = _t.SimpleNamespace(
        SERVER_TIMESTAMP=_SERVER_TS,
        transactional=lambda fn: fn,
    )
    monkeypatch.setitem(sys.modules, "google.cloud.firestore", fake_fs)
    import google.cloud as gc

    monkeypatch.setattr(gc, "firestore", fake_fs, raising=False)
    return FirestoreStateStore(project="p", client=_FakeClient())


def test_firestore_create_and_get(store):
    store.create_conversation("c1", workload="drift", title="t")
    conv = store.get_conversation("c1")
    assert conv["workload"] == "drift"
    assert conv["turn_count"] == 0
    assert conv["turns"] == []


def test_firestore_append_turn_transactional_seq(store):
    store.create_conversation("c1", workload="drift", title="t")
    assert store.append_turn("c1", role="user", text="a", workload="drift") == 0
    assert store.append_turn("c1", role="crew", text="b", workload="drift",
                             trace_id="tr") == 1
    conv = store.get_conversation("c1")
    assert [t["seq"] for t in conv["turns"]] == [0, 1]
    assert conv["turn_count"] == 2
    assert conv["last_trace_id"] == "tr"


def test_firestore_append_turns_create_with_in_one_transaction(store):
    seqs = store.append_turns(
        "new",
        [
            {"role": "user", "text": "q", "workload": "drift", "trace_id": "tr"},
            {"role": "crew", "text": "a", "workload": "drift", "trace_id": "tr"},
        ],
        create_with={"workload": "drift", "title": "q"},
    )
    assert seqs == [0, 1]
    conv = store.get_conversation("new")
    assert conv["turn_count"] == 2
    assert conv["title"] == "q"
    assert [t["seq"] for t in conv["turns"]] == [0, 1]
    assert conv["last_trace_id"] == "tr"


def test_firestore_append_turns_missing_without_create_raises(store):
    with pytest.raises(KeyError):
        store.append_turns("ghost", [{"role": "user", "text": "x",
                                      "workload": "drift"}])


def test_firestore_list_filters_and_limits(store):
    store.create_conversation("d", workload="drift", title="t")
    store.create_conversation("p", workload="provision", title="t")
    assert {c["conversation_id"] for c in store.list_conversations()} == {"d", "p"}
    assert [c["conversation_id"]
            for c in store.list_conversations(workload="provision")] == ["p"]


def test_firestore_get_unknown_returns_none(store):
    assert store.get_conversation("nope") is None
