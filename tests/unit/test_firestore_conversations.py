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
# sentinel matching firestore.DELETE_FIELD — the handoff burn REMOVES the
# pending_handoff field rather than writing a falsy tombstone, so the fake
# has to model deletion for the single-use assertions to mean anything.
_DELETE = object()


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
            if v is _DELETE:
                self._store.docs.get(self._path, {}).pop(k, None)
                continue
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
        DELETE_FIELD=_DELETE,
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


# --- Crew handoff over the fake client -------------------------------------
#
# The in-memory store is what every other test exercises, so these run the
# SAME scenarios against the Firestore implementation. The two share their
# decision ladder (``_evaluate_handoff_redemption``) but not their read/write
# mechanics, and the mechanics are where a burn can silently fail to burn.

import datetime as _dt  # noqa: E402

from agent.handoff import (  # noqa: E402
    build_pending_handoff,
    mint_handoff_nonce,
    validate_handoff_proposal,
)

_NOW = _dt.datetime(2026, 7, 28, 12, 0, tzinfo=_dt.timezone.utc)


def _fs_propose(store, cid, *, to="drift", frm="explore", create=True):
    nonce, digest = mint_handoff_nonce()
    store.append_turns(
        cid,
        [{"role": "user", "text": "q", "workload": frm},
         {"role": "crew", "text": "a", "workload": frm}],
        create_with={"workload": frm, "title": "q"} if create else None,
        pending_handoff=build_pending_handoff(
            validate_handoff_proposal(
                target=to, reason="needs a rollback", brief="b",
                current_workload=frm,
            ),
            digest=digest, now=_NOW,
        ),
    )
    return nonce


def test_firestore_proposal_commits_with_the_first_turns(store):
    _fs_propose(store, "c1")
    conv = store.get_conversation("c1")
    assert conv["turn_count"] == 2
    assert conv["pending_handoff"]["to"] == "drift"


def test_firestore_accept_flips_crew_burns_nonce_and_appends_transition(store):
    nonce = _fs_propose(store, "c1")
    out = store.redeem_handoff("c1", nonce=nonce, accept=True, now=_NOW)

    assert out["ok"] is True
    conv = store.get_conversation("c1")
    assert conv["workload"] == "drift"
    assert "pending_handoff" not in conv
    assert conv["turn_count"] == 3
    assert conv["turns"][-1]["role"] == "crew_change"
    assert conv["turns"][-1]["handoff"] == {"from": "explore", "to": "drift"}
    # Single use: the field is gone, so the same nonce can never verify again.
    assert store.redeem_handoff(
        "c1", nonce=nonce, accept=True, now=_NOW,
    )["error"] == "no_pending"


def test_firestore_decline_burns_without_flipping(store):
    nonce = _fs_propose(store, "c1")
    assert store.redeem_handoff("c1", nonce=nonce, accept=False, now=_NOW)["ok"]
    conv = store.get_conversation("c1")
    assert conv["workload"] == "explore"
    assert conv["turns"][-1]["role"] == "handoff_declined"


def test_firestore_supersede_burns_the_earlier_nonce(store):
    first = _fs_propose(store, "c1")
    second = _fs_propose(store, "c1", create=False)
    assert store.redeem_handoff(
        "c1", nonce=first, accept=True, now=_NOW,
    )["error"] == "invalid_nonce"
    assert store.redeem_handoff("c1", nonce=second, accept=True, now=_NOW)["ok"]


def test_firestore_expired_proposal_is_refused(store):
    nonce = _fs_propose(store, "c1")
    out = store.redeem_handoff(
        "c1", nonce=nonce, accept=True, now=_NOW + _dt.timedelta(minutes=15),
    )
    assert out["error"] == "expired"
    assert store.get_conversation("c1")["workload"] == "explore"


def test_firestore_run_lease_blocks_redemption_until_released(store):
    nonce = _fs_propose(store, "c1")
    assert store.begin_chat_run("c1", run_id="a", now=_NOW) is True
    assert store.begin_chat_run("c1", run_id="b", now=_NOW) is False
    assert store.redeem_handoff(
        "c1", nonce=nonce, accept=True, now=_NOW,
    )["error"] == "busy"

    store.finish_chat_run("c1", run_id="a")
    assert store.redeem_handoff("c1", nonce=nonce, accept=True, now=_NOW)["ok"]


def test_firestore_lease_on_an_absent_conversation_is_granted(store):
    assert store.begin_chat_run("never-created", run_id="a", now=_NOW) is True
