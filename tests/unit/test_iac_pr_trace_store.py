"""ds-qua — the per-(repo, PR) authoring-trace store.

The one property that matters here is IDENTITY, not availability: a missing link is
fine, a link to reasoning that did not author the PR is not. Most of these tests are
therefore about refusing to serve the wrong trace rather than about serving the right
one.
"""
from __future__ import annotations

import logging

import pytest

from agent.iac_pr_trace_store import (
    FirestoreIacPrTraceStore,
    InMemoryIacPrTraceStore,
    _doc_id,
    is_replayable_trace_id,
)

_TRACE = "a" * 32
_OTHER_TRACE = "b" * 32
_REPO = "adi-prasetyo/driftscribe"
_OTHER_REPO = "owner/editor-target"


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value",
    [
        "A" * 32,  # uppercase — uuid4().hex is lowercase, and the SPA regex is too
        "a" * 31,
        "a" * 33,
        "a" * 32 + "\n",  # the fullmatch-vs-match trap
        "g" * 32,  # not hex
        "",
        None,
        123,
        b"a" * 32,
    ],
)
def test_a_trace_id_the_spa_could_not_replay_is_refused(value):
    assert is_replayable_trace_id(value) is False


def test_the_canonical_trace_id_shape_is_accepted():
    assert is_replayable_trace_id("0123456789abcdef" * 2) is True


def test_doc_id_is_unambiguous_across_repo_and_number_boundaries():
    """The NUL separator is load-bearing: plain concatenation would collide."""
    assert _doc_id("a/b", 12) != _doc_id("a/b1", 2)
    assert _doc_id(_REPO, 7) != _doc_id(_OTHER_REPO, 7)
    assert _doc_id(_REPO, 7) == _doc_id(_REPO, 7)


# --------------------------------------------------------------------------- #
# In-memory twin
# --------------------------------------------------------------------------- #
def test_in_memory_round_trips_and_is_repo_scoped():
    store = InMemoryIacPrTraceStore()
    assert store.set_if_absent(_REPO, 7, _TRACE) is True
    assert store.get(_REPO, 7) == _TRACE
    # THE finding that motivated repo scoping: PR numbers are repository-local, and
    # the authoring repo can diverge from the listing repo via
    # IAC_EDITOR_TARGET_REPO_OVERRIDE. #7 in one repo is not #7 in the other.
    assert store.get(_OTHER_REPO, 7) is None


def test_in_memory_first_writer_wins():
    store = InMemoryIacPrTraceStore()
    assert store.set_if_absent(_REPO, 7, _TRACE) is True
    assert store.set_if_absent(_REPO, 7, _OTHER_TRACE) is False
    assert store.get(_REPO, 7) == _TRACE


@pytest.mark.parametrize("bad", ["", "nope", "a" * 31])
def test_in_memory_refuses_an_unreplayable_trace(bad):
    """A value the read side would refuse must not burn the first-writer slot."""
    store = InMemoryIacPrTraceStore()
    assert store.set_if_absent(_REPO, 7, bad) is False
    assert store.get(_REPO, 7) is None
    assert store.set_if_absent(_REPO, 7, _TRACE) is True


def test_in_memory_refuses_a_blank_repo():
    store = InMemoryIacPrTraceStore()
    assert store.set_if_absent("", 7, _TRACE) is False


# --------------------------------------------------------------------------- #
# Firestore twin — doubles, no GCP
# --------------------------------------------------------------------------- #
class _Snap:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return self._data


class _Doc:
    def __init__(self, collection, doc_id):
        self._c = collection
        self._id = doc_id

    def get(self):
        if self._c.raise_on_get:
            raise self._c.raise_on_get
        return _Snap(self._c.docs.get(self._id))

    def create(self, data):
        if self._c.raise_on_create:
            raise self._c.raise_on_create
        if self._id in self._c.docs:
            from google.api_core.exceptions import AlreadyExists

            raise AlreadyExists("exists")
        self._c.docs[self._id] = dict(data)


class _Collection:
    def __init__(self):
        self.docs: dict[str, dict] = {}
        self.raise_on_get: Exception | None = None
        self.raise_on_create: Exception | None = None

    def document(self, doc_id):
        return _Doc(self, doc_id)


class _Client:
    def __init__(self, collection):
        self._collection = collection

    def collection(self, name):
        assert name == "iac_pr_trace"
        return self._collection


def _store():
    col = _Collection()
    return FirestoreIacPrTraceStore(project="p", client=_Client(col)), col


def test_firestore_round_trips_and_stores_its_own_identity():
    store, col = _store()
    assert store.set_if_absent(_REPO, 7, _TRACE) is True
    assert store.get(_REPO, 7) == _TRACE
    stored = col.docs[_doc_id(_REPO, 7)]
    # The body re-states its identity so `get` can re-check it. Schema integrity,
    # not authentication — see the module docstring.
    assert stored["repo"] == _REPO
    assert stored["pr_number"] == 7
    assert stored["trace_id"] == _TRACE


def test_firestore_first_writer_wins_via_create_not_set():
    store, col = _store()
    assert store.set_if_absent(_REPO, 7, _TRACE) is True
    assert store.set_if_absent(_REPO, 7, _OTHER_TRACE) is False
    assert store.get(_REPO, 7) == _TRACE


def test_firestore_is_repo_scoped_end_to_end():
    store, _ = _store()
    store.set_if_absent(_OTHER_REPO, 7, _OTHER_TRACE)
    assert store.get(_REPO, 7) is None
    assert store.get(_OTHER_REPO, 7) == _OTHER_TRACE


def test_a_document_whose_body_names_another_repo_is_refused():
    """Mutation guard for the identity re-check. Hand-write a document AT the correct
    key whose body disagrees — without the body check this would cross-serve."""
    store, col = _store()
    col.docs[_doc_id(_REPO, 7)] = {
        "repo": _OTHER_REPO,
        "pr_number": 7,
        "trace_id": _OTHER_TRACE,
    }
    assert store.get(_REPO, 7) is None


def test_a_document_whose_body_names_another_pr_is_refused():
    store, col = _store()
    col.docs[_doc_id(_REPO, 7)] = {
        "repo": _REPO,
        "pr_number": 8,
        "trace_id": _OTHER_TRACE,
    }
    assert store.get(_REPO, 7) is None


def test_a_stored_trace_the_spa_could_not_replay_is_refused_on_read():
    store, col = _store()
    col.docs[_doc_id(_REPO, 7)] = {
        "repo": _REPO,
        "pr_number": 7,
        "trace_id": "not-a-trace",
    }
    assert store.get(_REPO, 7) is None


def test_a_missing_document_is_simply_absent():
    store, _ = _store()
    assert store.get(_REPO, 7) is None


# --------------------------------------------------------------------------- #
# Fail-soft — evidence must never break the request
# --------------------------------------------------------------------------- #
def test_read_failure_degrades_to_absent_and_never_logs_the_message(caplog):
    store, col = _store()
    col.raise_on_get = PermissionError(
        "403 on projects/driftscribe-hack-2026/databases/(default)/documents/x"
    )
    with caplog.at_level(logging.WARNING):
        assert store.get(_REPO, 7) is None
    (record,) = caplog.records
    # The exception TYPE only: a PermissionDenied message embeds the full document
    # resource path (project id + collection). Assert over the WHOLE record, not
    # caplog.text — `extra=` lands in record attributes, which the formatted text
    # does not render, so a text-only check would pass even if the path leaked.
    assert record.error == "PermissionError"
    assert "driftscribe-hack-2026" not in repr(record.__dict__)


def test_write_failure_is_swallowed_and_never_logs_the_message(caplog):
    store, col = _store()
    col.raise_on_create = PermissionError("403 on projects/driftscribe-hack-2026/x")
    with caplog.at_level(logging.WARNING):
        assert store.set_if_absent(_REPO, 7, _TRACE) is False
    (record,) = caplog.records
    assert record.error == "PermissionError"
    assert "driftscribe-hack-2026" not in repr(record.__dict__)


def test_firestore_refuses_an_unreplayable_trace_without_touching_the_collection():
    store, col = _store()
    assert store.set_if_absent(_REPO, 7, "nope") is False
    assert col.docs == {}


# --------------------------------------------------------------------------- #
# record_authoring_trace — the shared entry point
# --------------------------------------------------------------------------- #
def test_record_uses_the_bound_trace_and_never_mints_one(monkeypatch):
    """⚠️ The `current_trace_id_or_new()` trap. That function MINTS a fresh id when
    the ContextVar is unset; recording it would persist an id with no logged reasoning
    behind it — a link that opens an empty timeline. No trace in context → no record."""
    from driftscribe_lib.logging import reset_trace_id, set_trace_id

    from agent import iac_pr_trace_store as mod

    store = InMemoryIacPrTraceStore()
    monkeypatch.setattr(mod, "get_iac_pr_trace_store", lambda: store)

    tok = set_trace_id(_TRACE)
    try:
        assert mod.record_authoring_trace(_REPO, 7) is True
    finally:
        reset_trace_id(tok)
    assert store.get(_REPO, 7) == _TRACE


def test_record_writes_nothing_when_no_trace_is_bound(monkeypatch):
    from agent import iac_pr_trace_store as mod

    store = InMemoryIacPrTraceStore()
    monkeypatch.setattr(mod, "get_iac_pr_trace_store", lambda: store)
    monkeypatch.setattr("driftscribe_lib.logging.get_trace_id", lambda: "")

    assert mod.record_authoring_trace(_REPO, 7) is False
    assert store.get(_REPO, 7) is None


def test_record_never_raises_into_the_caller(monkeypatch):
    """PR authoring must not fail because an evidence write did."""
    from driftscribe_lib.logging import reset_trace_id, set_trace_id

    from agent import iac_pr_trace_store as mod

    def _boom():
        raise RuntimeError("firestore is down")

    monkeypatch.setattr(mod, "get_iac_pr_trace_store", _boom)
    tok = set_trace_id(_TRACE)
    try:
        assert mod.record_authoring_trace(_REPO, 7) is False
    finally:
        reset_trace_id(tok)


# --------------------------------------------------------------------------- #
# The identity check is TYPE-strict — Python equality is not enough
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "stored_pr",
    [
        pytest.param(True, id="bool-True-equals-int-1"),
        pytest.param(1.0, id="float-equals-int-1"),
        pytest.param("1", id="str"),
        pytest.param(None, id="none"),
    ],
)
def test_a_non_int_pr_number_in_the_body_is_refused(stored_pr):
    """``True == 1`` and ``1.0 == 1`` in Python. A schema check that accepts the
    wrong TYPE is not checking the schema."""
    store, col = _store()
    col.docs[_doc_id(_REPO, 1)] = {
        "repo": _REPO,
        "pr_number": stored_pr,
        "trace_id": _TRACE,
    }
    assert store.get(_REPO, 1) is None


def test_a_body_missing_its_identity_fields_entirely_is_refused():
    store, col = _store()
    col.docs[_doc_id(_REPO, 7)] = {"trace_id": _TRACE}
    assert store.get(_REPO, 7) is None


def test_an_empty_document_is_refused():
    store, col = _store()
    col.docs[_doc_id(_REPO, 7)] = {}
    assert store.get(_REPO, 7) is None
