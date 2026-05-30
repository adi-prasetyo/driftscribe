"""Unit tests for the Phase C3 plan-bound approval schema in
``driftscribe_lib.approvals``.

Mirrors the rollback ``test_approval_store.py`` fake-Firestore harness — the
``PlanApprovalStore`` is exercised against an in-memory client (no live
Firestore, no GCP). The pure functions (payload builder, HMAC, canonicalizer,
artifact-integrity primitive) are exercised directly with in-memory dicts/bytes.

Design: docs/plans/2026-05-29-infra-iac-phase-c3-plan-approval.md
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest

from driftscribe_lib.approvals import (
    ArtifactIntegrityError,
    PlanApproval,
    PlanApprovalStore,
    build_plan_approval_payload,
    canonicalize_payload,
    compute_plan_approval_hmac,
    compute_token_hmac,
    new_approval_window,
    plan_approval_is_expired,
    signed_payload,
    verify_artifact_integrity,
    verify_plan_approval,
)

# --------------------------------------------------------------------------- #
# Fixtures / builders
# --------------------------------------------------------------------------- #

_SHA40 = "a" * 40
_BASE40 = "c" * 40
_H64_PLAN = "b" * 64
_H64_JSON = "d" * 64
_H64_LOCK = "e" * 64
_ISSUED = "2026-05-29T12:00:00+00:00"
_EXPIRES = "2026-05-29T12:15:00+00:00"


def _prefix(pr: int = 12, head: str = _SHA40, run_id: str = "100", attempt: str = "1") -> str:
    return f"gs://driftscribe-hack-2026-tofu-artifacts/pr-{pr}/{head}/run-{run_id}-{attempt}/"


def _metadata(**over: Any) -> dict[str, Any]:
    p = _prefix(over.get("pr_number", 12), over.get("head_sha", _SHA40))
    md = {
        "schema_version": "c2.v1",
        "repo": "adi-p/driftscribe",
        "pr_number": 12,
        "head_sha": _SHA40,
        "base_sha": _BASE40,
        "workflow_run_id": "100",
        "workflow_run_attempt": "1",
        "artifact_uri_plan": p + "plan.tfplan",
        "artifact_uri_json": p + "plan.json",
        "generation_plan": "1700000000000001",
        "generation_json": "1700000000000002",
        "plan_sha256": _H64_PLAN,
        "plan_json_sha256": _H64_JSON,
        "opentofu_version": "1.12.0",
        "provider_lockfile_sha256": _H64_LOCK,
    }
    md.update(over)
    return md


def _payload(**over: Any) -> dict[str, Any]:
    return build_plan_approval_payload(
        metadata=over.pop("metadata", _metadata()),
        artifact_uri_metadata=over.pop("artifact_uri_metadata", _prefix() + "metadata.json"),
        generation_metadata=over.pop("generation_metadata", "1700000000000003"),
        approver=over.pop("approver", "alice@corp.example"),
        issued_at=over.pop("issued_at", _ISSUED),
        expires_at=over.pop("expires_at", _EXPIRES),
    )


# --------------------------------------------------------------------------- #
# build_plan_approval_payload
# --------------------------------------------------------------------------- #


def test_build_payload_happy_path() -> None:
    p = _payload()
    assert p["approval_schema_version"] == "c3.v1"
    assert set(p) == {
        "approval_schema_version", "metadata", "artifact_uri_metadata",
        "generation_metadata", "approver", "issued_at", "expires_at",
    }
    assert len(p["metadata"]) == 15
    assert p["metadata"]["schema_version"] == "c2.v1"
    assert p["metadata"]["pr_number"] == 12  # stays an int
    assert p["approver"] == "alice@corp.example"
    assert p["artifact_uri_metadata"].endswith("/metadata.json")


def test_build_payload_rejects_non_dict_metadata() -> None:
    with pytest.raises(ValueError):
        _payload(metadata="not-a-dict")


def test_build_payload_rejects_wrong_key_set() -> None:
    md = _metadata()
    md["extra"] = "x"
    with pytest.raises(ValueError):
        _payload(metadata=md)
    md2 = _metadata()
    del md2["repo"]
    with pytest.raises(ValueError):
        _payload(metadata=md2)


def test_build_payload_rejects_bad_schema_version() -> None:
    with pytest.raises(ValueError):
        _payload(metadata=_metadata(schema_version="c2.v2"))


def test_build_payload_rejects_malformed_field() -> None:
    with pytest.raises(ValueError):
        _payload(metadata=_metadata(head_sha="nothex"))


def test_build_payload_rejects_wrong_typed_field() -> None:
    # pr_number as a string -> build_metadata's positive-int check fails (ValueError path).
    with pytest.raises(ValueError):
        _payload(metadata=_metadata(pr_number="12"))


def test_build_payload_folds_typeerror_to_valueerror() -> None:
    """A None on an UNGUARDED field (plan_sha256) makes re.fullmatch raise
    TypeError inside build_metadata; build_plan_approval_payload must fold it
    into ValueError so the builder never leaks a TypeError (fail-closed)."""
    with pytest.raises(ValueError):
        _payload(metadata=_metadata(plan_sha256=None))


def test_build_payload_rejects_bad_generation_metadata() -> None:
    with pytest.raises(ValueError):
        _payload(generation_metadata="not-numeric")
    with pytest.raises(ValueError):
        _payload(generation_metadata=12345)  # not a string


def test_build_payload_rejects_empty_approver() -> None:
    with pytest.raises(ValueError):
        _payload(approver="")


def test_build_payload_rejects_bad_window_format() -> None:
    with pytest.raises(ValueError):
        _payload(issued_at="2026-05-29T12:00:00.500000+00:00")  # microseconds
    with pytest.raises(ValueError):
        _payload(expires_at="2026-05-29T12:15:00Z")  # Z not +00:00
    with pytest.raises(ValueError):
        _payload(issued_at="2026-05-29 12:00:00+00:00")  # space, no T


def test_build_payload_rejects_locator_outside_run_dir() -> None:
    # metadata.json under a DIFFERENT run dir than the plan artifacts.
    bad_uri = _prefix(run_id="999") + "metadata.json"
    with pytest.raises(ValueError):
        _payload(artifact_uri_metadata=bad_uri)
    # right dir but wrong object name.
    with pytest.raises(ValueError):
        _payload(artifact_uri_metadata=_prefix() + "plan.tfplan")


def test_build_payload_rejects_inverted_window() -> None:
    with pytest.raises(ValueError):
        _payload(issued_at="2026-05-29T12:15:00+00:00", expires_at="2026-05-29T12:00:00+00:00")
    with pytest.raises(ValueError):  # equal is not "strictly after"
        _payload(issued_at="2026-05-29T12:00:00+00:00", expires_at="2026-05-29T12:00:00+00:00")


def test_build_payload_rejects_over_max_ttl() -> None:
    # 30-minute window exceeds the 15-minute max, even if format-valid.
    with pytest.raises(ValueError):
        _payload(issued_at="2026-05-29T12:00:00+00:00", expires_at="2026-05-29T12:30:00+00:00")


# --------------------------------------------------------------------------- #
# canonicalize_payload
# --------------------------------------------------------------------------- #


def test_canonicalize_is_deterministic_and_key_order_independent() -> None:
    p1 = _payload()
    # Rebuild the same logical dict with a different key insertion order.
    p2 = {k: p1[k] for k in reversed(list(p1))}
    assert canonicalize_payload(p1) == canonicalize_payload(p2)


def test_canonicalize_is_compact_and_round_trips() -> None:
    c = canonicalize_payload(_payload())
    assert ", " not in c and ": " not in c  # compact separators
    assert json.loads(c) == _payload()


# --------------------------------------------------------------------------- #
# new_approval_window
# --------------------------------------------------------------------------- #


def test_new_approval_window_frozen_format_and_ttl() -> None:
    now = dt.datetime(2026, 5, 29, 12, 0, 0, 500_000, tzinfo=dt.timezone.utc)
    issued, expires = new_approval_window(now=now, ttl_minutes=15)
    assert issued == "2026-05-29T12:00:00+00:00"  # microseconds dropped
    assert expires == "2026-05-29T12:15:00+00:00"


def test_new_approval_window_converts_to_utc() -> None:
    tz = dt.timezone(dt.timedelta(hours=9))  # JST
    now = dt.datetime(2026, 5, 29, 21, 0, 0, tzinfo=tz)  # 12:00 UTC
    issued, _ = new_approval_window(now=now, ttl_minutes=15)
    assert issued == "2026-05-29T12:00:00+00:00"


def test_new_approval_window_rejects_naive_now() -> None:
    with pytest.raises(ValueError):
        new_approval_window(now=dt.datetime(2026, 5, 29, 12, 0, 0))  # no tzinfo


def test_new_approval_window_rejects_bad_ttl() -> None:
    aware = dt.datetime(2026, 5, 29, 12, 0, 0, tzinfo=dt.timezone.utc)
    for bad in (0, -5, 16, 1440):
        with pytest.raises(ValueError):
            new_approval_window(now=aware, ttl_minutes=bad)


# --------------------------------------------------------------------------- #
# compute_plan_approval_hmac
# --------------------------------------------------------------------------- #


def test_plan_hmac_deterministic_64_hex() -> None:
    a = compute_plan_approval_hmac("tok", "aid", "f" * 64, "key")
    b = compute_plan_approval_hmac("tok", "aid", "f" * 64, "key")
    assert a == b
    assert len(a) == 64


def test_plan_hmac_differs_per_component() -> None:
    base = compute_plan_approval_hmac("tok", "aid", "f" * 64, "key")
    assert base != compute_plan_approval_hmac("tok2", "aid", "f" * 64, "key")
    assert base != compute_plan_approval_hmac("tok", "aid2", "f" * 64, "key")
    assert base != compute_plan_approval_hmac("tok", "aid", "e" * 64, "key")
    assert base != compute_plan_approval_hmac("tok", "aid", "f" * 64, "key2")


def test_plan_hmac_domain_separated_from_rollback() -> None:
    """The same (token, approval_id, target) must NOT collide with the rollback
    compute_token_hmac — the domain tag separates the two namespaces."""
    token, aid, target, key = "tok", "aid", "f" * 64, "key"
    assert compute_plan_approval_hmac(token, aid, target, key) != compute_token_hmac(
        token, aid, target, key
    )


# --------------------------------------------------------------------------- #
# verify_artifact_integrity
# --------------------------------------------------------------------------- #


def test_verify_artifact_integrity_pass() -> None:
    pb, jb = b"the-binary-plan", b'{"plan":"json"}'
    verify_artifact_integrity(
        plan_tfplan_bytes=pb,
        plan_json_bytes=jb,
        expected_plan_sha256=hashlib.sha256(pb).hexdigest(),
        expected_plan_json_sha256=hashlib.sha256(jb).hexdigest(),
    )  # returns None, no raise


def test_verify_artifact_integrity_raises_on_plan_mismatch() -> None:
    pb, jb = b"plan", b"json"
    with pytest.raises(ArtifactIntegrityError) as ei:
        verify_artifact_integrity(
            plan_tfplan_bytes=b"TAMPERED",
            plan_json_bytes=jb,
            expected_plan_sha256=hashlib.sha256(pb).hexdigest(),
            expected_plan_json_sha256=hashlib.sha256(jb).hexdigest(),
        )
    assert ei.value.artifact == "plan.tfplan"


def test_verify_artifact_integrity_raises_on_json_mismatch() -> None:
    pb, jb = b"plan", b"json"
    with pytest.raises(ArtifactIntegrityError) as ei:
        verify_artifact_integrity(
            plan_tfplan_bytes=pb,
            plan_json_bytes=b"TAMPERED",
            expected_plan_sha256=hashlib.sha256(pb).hexdigest(),
            expected_plan_json_sha256=hashlib.sha256(jb).hexdigest(),
        )
    assert ei.value.artifact == "plan.json"


def test_verify_artifact_integrity_rejects_non_hex_expected() -> None:
    with pytest.raises(ValueError):
        verify_artifact_integrity(
            plan_tfplan_bytes=b"x", plan_json_bytes=b"y",
            expected_plan_sha256="not-hex", expected_plan_json_sha256="d" * 64,
        )
    with pytest.raises(ValueError):
        verify_artifact_integrity(
            plan_tfplan_bytes=b"x", plan_json_bytes=b"y",
            expected_plan_sha256="A" * 64,  # uppercase rejected
            expected_plan_json_sha256="d" * 64,
        )


# --------------------------------------------------------------------------- #
# Fake Firestore (mirrors test_approval_store.py)
# --------------------------------------------------------------------------- #


class _FakeDocRef:
    def __init__(self, store: dict[str, dict[str, Any]], path: str) -> None:
        self._store = store
        self.path = path

    def set(self, data: dict[str, Any]) -> None:
        self._store[self.path] = dict(data)

    def get(self, transaction: Any = None) -> SimpleNamespace:
        if self.path not in self._store:
            return SimpleNamespace(exists=False, to_dict=lambda: None)
        data = dict(self._store[self.path])
        return SimpleNamespace(exists=True, to_dict=lambda: data)

    def update(self, data: dict[str, Any]) -> None:
        self._store[self.path].update(data)


class _FakeCollection:
    def __init__(self, store: dict[str, dict[str, Any]], name: str) -> None:
        self._store = store
        self._name = name

    def document(self, doc_id: str) -> _FakeDocRef:
        return _FakeDocRef(self._store, f"{self._name}/{doc_id}")


class _FakeTransaction:
    def __init__(self, store: dict[str, dict[str, Any]]) -> None:
        self._store = store

    def update(self, ref: _FakeDocRef, data: dict[str, Any]) -> None:
        ref.update(data)


class _FakeFirestore:
    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}

    def collection(self, name: str) -> _FakeCollection:
        return _FakeCollection(self._store, name)

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self._store)

    def raw(self, path: str) -> dict[str, Any] | None:
        return self._store.get(path)


@pytest.fixture(autouse=True)
def _bypass_transactional(monkeypatch: pytest.MonkeyPatch) -> None:
    from driftscribe_lib import approvals as approvals_mod

    def passthrough(fn):  # noqa: ANN001
        def wrapper(transaction, *args, **kwargs):  # noqa: ANN001
            return fn(transaction, *args, **kwargs)
        return wrapper

    monkeypatch.setattr(approvals_mod.firestore, "transactional", passthrough)


def _make_store() -> tuple[PlanApprovalStore, _FakeFirestore]:
    fake = _FakeFirestore()
    return PlanApprovalStore(project="test-proj", client=fake), fake


# --------------------------------------------------------------------------- #
# PlanApprovalStore named-database threading (Phase C5f) — the `database` kwarg
# selects a named Firestore DB to isolate plan_approvals from the coordinator.
# --------------------------------------------------------------------------- #


def test_store_threads_database_to_firestore_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no injected client, the `database` kwarg is passed through to
    firestore.Client(project=, database=) verbatim."""
    from driftscribe_lib import approvals as approvals_mod

    captured: dict[str, Any] = {}

    def fake_client(**kwargs: Any) -> _FakeFirestore:
        captured.update(kwargs)
        return _FakeFirestore()

    monkeypatch.setattr(approvals_mod.firestore, "Client", fake_client)
    PlanApprovalStore(project="test-proj", database="plan-approvals")
    assert captured == {"project": "test-proj", "database": "plan-approvals"}


def test_store_default_database_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Omitting `database` passes database=None — google.cloud.firestore treats
    that exactly as the (default) database (back-compat for existing deploys)."""
    from driftscribe_lib import approvals as approvals_mod

    captured: dict[str, Any] = {}

    def fake_client(**kwargs: Any) -> _FakeFirestore:
        captured.update(kwargs)
        return _FakeFirestore()

    monkeypatch.setattr(approvals_mod.firestore, "Client", fake_client)
    PlanApprovalStore(project="test-proj")
    assert captured == {"project": "test-proj", "database": None}


def test_injected_client_ignores_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """An injected client wins — firestore.Client is never called, so `database`
    is irrelevant (the fake already encodes whichever DB the test means)."""
    from driftscribe_lib import approvals as approvals_mod

    def boom(**kwargs: Any) -> None:
        raise AssertionError("firestore.Client must not be called with an injected client")

    monkeypatch.setattr(approvals_mod.firestore, "Client", boom)
    fake = _FakeFirestore()
    store = PlanApprovalStore(project="test-proj", client=fake, database="plan-approvals")
    assert store._client is fake


def _now() -> dt.datetime:
    return dt.datetime(2026, 5, 29, 12, 0, 0, tzinfo=dt.timezone.utc)


# --------------------------------------------------------------------------- #
# PlanApprovalStore.create
# --------------------------------------------------------------------------- #


def test_create_returns_approval_and_raw_token() -> None:
    store, _ = _make_store()
    approval, raw = store.create(payload=_payload(), hmac_key="k", created_by="coord@x")
    assert isinstance(approval, PlanApproval)
    assert len(raw) == 43  # secrets.token_urlsafe(32)
    assert approval.status == "pending"
    assert approval.created_by == "coord@x"
    assert approval.pr_number == 12
    assert approval.head_sha == _SHA40
    assert approval.artifact_uri_metadata.endswith("/metadata.json")
    assert approval.expires_at == dt.datetime(2026, 5, 29, 12, 15, 0, tzinfo=dt.timezone.utc)


def test_create_stores_hmac_not_raw_token() -> None:
    store, fake = _make_store()
    approval, raw = store.create(payload=_payload(), hmac_key="k", created_by="u")
    doc = fake.raw(f"plan_approvals/{approval.approval_id}")
    assert doc is not None
    assert "token_hmac" in doc
    assert raw not in doc.values()
    for v in doc.values():
        assert raw not in str(v)
    # token_hmac is the domain-separated plan HMAC over the stored canonical digest.
    digest = hashlib.sha256(doc["payload_canonical"].encode("utf-8")).hexdigest()
    assert doc["payload_sha256"] == digest
    assert doc["token_hmac"] == compute_plan_approval_hmac(raw, approval.approval_id, digest, "k")


def test_create_rejects_payload_without_c3_schema_version() -> None:
    store, _ = _make_store()
    bad = dict(_payload())
    del bad["approval_schema_version"]
    with pytest.raises(ValueError):
        store.create(payload=bad, hmac_key="k", created_by="u")


def test_create_rejects_malformed_payload_shape() -> None:
    """create() guards against a hand-built payload that bypassed
    build_plan_approval_payload — top-level keys must be exactly the c3.v1 set."""
    store, _ = _make_store()
    extra = dict(_payload())
    extra["surprise"] = "x"
    with pytest.raises(ValueError):
        store.create(payload=extra, hmac_key="k", created_by="u")
    missing = dict(_payload())
    del missing["approver"]
    with pytest.raises(ValueError):
        store.create(payload=missing, hmac_key="k", created_by="u")


def test_create_distinct_ids_and_tokens() -> None:
    store, _ = _make_store()
    a1, t1 = store.create(payload=_payload(), hmac_key="k", created_by="u")
    a2, t2 = store.create(payload=_payload(), hmac_key="k", created_by="u")
    assert a1.approval_id != a2.approval_id
    assert t1 != t2


# --------------------------------------------------------------------------- #
# get
# --------------------------------------------------------------------------- #


def test_get_hit_and_miss() -> None:
    store, _ = _make_store()
    created, _ = store.create(payload=_payload(), hmac_key="k", created_by="u")
    fetched = store.get(created.approval_id)
    assert fetched is not None
    assert fetched.approval_id == created.approval_id
    assert fetched.status == "pending"
    assert store.get("nope") is None


# --------------------------------------------------------------------------- #
# claim_pending / claim_denied
# --------------------------------------------------------------------------- #


def test_claim_pending_flips_once_with_audit() -> None:
    store, fake = _make_store()
    created, _ = store.create(payload=_payload(), hmac_key="k", created_by="u")
    used_at = _now()
    claimed = store.claim_pending(created.approval_id, used_by="alice@x", used_at=used_at)
    assert claimed is not None
    assert claimed.status == "used"
    assert claimed.used_by == "alice@x"
    assert claimed.used_at == used_at
    doc = fake.raw(f"plan_approvals/{created.approval_id}")
    assert doc["status"] == "used"
    assert doc["used_by"] == "alice@x"


def test_claim_pending_returns_none_on_second_call() -> None:
    store, _ = _make_store()
    created, _ = store.create(payload=_payload(), hmac_key="k", created_by="u")
    assert store.claim_pending(created.approval_id, used_by="a", used_at=_now()) is not None
    assert store.claim_pending(created.approval_id, used_by="b", used_at=_now()) is None


def test_claim_pending_none_for_missing() -> None:
    store, _ = _make_store()
    assert store.claim_pending("ghost", used_by="a", used_at=_now()) is None


def test_claim_pending_none_when_denied() -> None:
    store, fake = _make_store()
    created, _ = store.create(payload=_payload(), hmac_key="k", created_by="u")
    fake.raw(f"plan_approvals/{created.approval_id}")["status"] = "denied"
    assert store.claim_pending(created.approval_id, used_by="a", used_at=_now()) is None


def test_claim_denied_flips_once_with_audit() -> None:
    store, fake = _make_store()
    created, _ = store.create(payload=_payload(), hmac_key="k", created_by="u")
    denied = store.claim_denied(created.approval_id, denied_by="bob@x", denied_at=_now())
    assert denied is not None
    assert denied.status == "denied"
    assert denied.denied_by == "bob@x"
    assert fake.raw(f"plan_approvals/{created.approval_id}")["status"] == "denied"


def test_claim_denied_none_when_used() -> None:
    store, _ = _make_store()
    created, _ = store.create(payload=_payload(), hmac_key="k", created_by="u")
    store.claim_pending(created.approval_id, used_by="a", used_at=_now())
    assert store.claim_denied(created.approval_id, denied_by="b", denied_at=_now()) is None


def test_deny_then_pending_both_refuse() -> None:
    store, _ = _make_store()
    created, _ = store.create(payload=_payload(), hmac_key="k", created_by="u")
    assert store.claim_denied(created.approval_id, denied_by="b", denied_at=_now()) is not None
    assert store.claim_pending(created.approval_id, used_by="a", used_at=_now()) is None


# --------------------------------------------------------------------------- #
# verify_plan_approval
# --------------------------------------------------------------------------- #


def test_verify_plan_approval_true_for_valid_token() -> None:
    store, _ = _make_store()
    created, raw = store.create(payload=_payload(), hmac_key="k", created_by="u")
    assert verify_plan_approval(raw, created, "k") is True


def test_verify_plan_approval_false_for_wrong_token_or_key() -> None:
    store, _ = _make_store()
    created, raw = store.create(payload=_payload(), hmac_key="k", created_by="u")
    assert verify_plan_approval("wrong-token", created, "k") is False
    assert verify_plan_approval(raw, created, "wrong-key") is False


def test_verify_plan_approval_false_after_payload_tamper() -> None:
    """Editing payload_canonical in Firestore changes the recomputed digest, so
    the HMAC no longer matches the (unchanged) stored token_hmac."""
    store, fake = _make_store()
    created, raw = store.create(payload=_payload(), hmac_key="k", created_by="u")
    doc = fake.raw(f"plan_approvals/{created.approval_id}")
    tampered = json.loads(doc["payload_canonical"])
    tampered["approver"] = "attacker@evil.example"
    doc["payload_canonical"] = canonicalize_payload(tampered)
    refetched = store.get(created.approval_id)
    assert verify_plan_approval(raw, refetched, "k") is False


def test_verify_plan_approval_false_after_token_hmac_tamper() -> None:
    store, fake = _make_store()
    created, raw = store.create(payload=_payload(), hmac_key="k", created_by="u")
    doc = fake.raw(f"plan_approvals/{created.approval_id}")
    doc["token_hmac"] = "0" * 64
    refetched = store.get(created.approval_id)
    assert verify_plan_approval(raw, refetched, "k") is False


def test_payload_sha256_is_audit_only_not_trusted_by_verify() -> None:
    """The denormalized payload_sha256 must NEVER be an input to verification —
    editing it (leaving payload_canonical + token_hmac intact) must not change
    the result. Locks the 'recompute the digest from payload_canonical' invariant
    against a future refactor that 'optimizes' verify to read the stored digest."""
    store, fake = _make_store()
    created, raw = store.create(payload=_payload(), hmac_key="k", created_by="u")
    fake.raw(f"plan_approvals/{created.approval_id}")["payload_sha256"] = "0" * 64
    refetched = store.get(created.approval_id)
    assert verify_plan_approval(raw, refetched, "k") is True


# --------------------------------------------------------------------------- #
# plan_approval_is_expired (reads the SIGNED window, not the denormalized dt)
# --------------------------------------------------------------------------- #


def test_signed_payload_returns_the_bound_dict() -> None:
    store, _ = _make_store()
    p = _payload()
    created, _ = store.create(payload=p, hmac_key="k", created_by="u")
    assert signed_payload(created) == p
    assert signed_payload(created)["metadata"]["pr_number"] == 12


def test_plan_approval_is_expired_reads_signed_window() -> None:
    store, _ = _make_store()
    created, _ = store.create(payload=_payload(), hmac_key="k", created_by="u")  # signed expires 12:15
    before = dt.datetime(2026, 5, 29, 12, 10, tzinfo=dt.timezone.utc)
    after = dt.datetime(2026, 5, 29, 13, 0, tzinfo=dt.timezone.utc)
    assert plan_approval_is_expired(created, now=before) is False
    assert plan_approval_is_expired(created, now=after) is True


def test_plan_approval_is_expired_ignores_tampered_denormalized_dt() -> None:
    """TTL-bypass defense: an attacker who edits ONLY the denormalized (unsigned)
    stored.expires_at into the far future cannot resurrect an expired approval —
    the decision reads the HMAC-bound signed window."""
    store, fake = _make_store()
    created, _ = store.create(payload=_payload(), hmac_key="k", created_by="u")  # signed expires 12:15
    fake.raw(f"plan_approvals/{created.approval_id}")["expires_at"] = dt.datetime(
        2099, 1, 1, tzinfo=dt.timezone.utc
    )
    refetched = store.get(created.approval_id)
    assert refetched.expires_at.year == 2099  # the denormalized field IS tampered
    # ...but the decision uses the signed 12:15 window, so it is still expired at 13:00.
    after = dt.datetime(2026, 5, 29, 13, 0, tzinfo=dt.timezone.utc)
    assert plan_approval_is_expired(refetched, now=after) is True
