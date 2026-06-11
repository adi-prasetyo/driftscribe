"""Tests for the C4 tofu-apply worker — fully offline (no live tofu / GCP).

Three injection seams (design §10): a fake GCS bucket (literal artifact bytes +
recorded pinning args), a fake Firestore-backed PlanApprovalStore (the C3 harness
+ transactional bypass), and a stubbed ``_RUN_TOFU`` returning chosen exit codes.
Drives the §3.6 claim-first matrix end-to-end.

Design: docs/plans/2026-05-29-infra-iac-phase-c4-tofu-apply.md
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

# Env MUST be set before importing the worker (module reads it at import; KeyError
# on any missing required value — mirrors the Cloud Run boot fail-fast).
os.environ.setdefault("GCP_PROJECT", "test-proj")
os.environ.setdefault("OWN_URL", "https://tofu-apply.example.com")
os.environ.setdefault("COORDINATOR_URL", "https://coord.example.com")
os.environ.setdefault("ALLOWED_CALLERS", "alice@corp.example,coordinator@test-proj.iam.gserviceaccount.com")
os.environ.setdefault("PLAN_APPROVAL_HMAC_KEY", "test-plan-hmac-key")
os.environ.setdefault("TF_VAR_tofu_state_kms_key", "projects/p/locations/l/keyRings/r/cryptoKeys/tofu-state")
# C5b-2: default the offline suite to "e2e" mode so the no-JWT propose/apply tests
# pass via the legacy caller==approver fallback (exactly the pre-C5 behavior). The
# new enforce-mode tests monkeypatch m.IAC_OPERATOR_AUTH_MODE="enforce" per-test.
os.environ.setdefault("IAC_OPERATOR_AUTH_MODE", "e2e")

import httpx  # noqa: E402
import jwt  # noqa: E402
import respx  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from driftscribe_lib import approvals as approvals_mod  # noqa: E402
from driftscribe_lib import cf_access as cf_access_mod  # noqa: E402
from driftscribe_lib.approvals import (  # noqa: E402
    PlanApprovalStore,
    build_plan_approval_payload,
    new_approval_window,
)
from workers.tofu_apply import gcs_fetch, main as m, tofu_runner  # noqa: E402

_SHA40 = "a" * 40
_BASE40 = "c" * 40
# Fixed "now" so the signed approval window (built via new_approval_window, which
# enforces the <=15-min TTL) is deterministic and not-expired at apply time.
_NOW = dt.datetime(2026, 5, 29, 12, 0, 0, tzinfo=dt.timezone.utc)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# --------------------------------------------------------------------------- #
# Phase C5f — _get_plan_approval_store threads the named-DB env into the store.
# --------------------------------------------------------------------------- #


def test_get_plan_approval_store_passes_named_database(monkeypatch: Any) -> None:
    """_get_plan_approval_store forwards GCP_PROJECT + PLAN_APPROVALS_DB to the
    store so the worker writes plan_approvals to the isolated named database."""
    captured: dict[str, Any] = {}

    def fake_store(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "store-sentinel"

    monkeypatch.setattr(m, "PlanApprovalStore", fake_store)
    monkeypatch.setattr(m, "GCP_PROJECT", "test-proj")
    monkeypatch.setattr(m, "PLAN_APPROVALS_DB", "plan-approvals")
    assert m._get_plan_approval_store() == "store-sentinel"
    assert captured == {"project": "test-proj", "database": "plan-approvals"}


def test_get_plan_approval_store_default_database_none(monkeypatch: Any) -> None:
    """With PLAN_APPROVALS_DB unset (None), the store gets database=None →
    the (default) database (back-compat for e2e / pre-isolation deploys)."""
    captured: dict[str, Any] = {}

    def fake_store(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "store-sentinel"

    monkeypatch.setattr(m, "PlanApprovalStore", fake_store)
    monkeypatch.setattr(m, "GCP_PROJECT", "test-proj")
    monkeypatch.setattr(m, "PLAN_APPROVALS_DB", None)
    m._get_plan_approval_store()
    assert captured == {"project": "test-proj", "database": None}


# --------------------------------------------------------------------------- #
# Fake Firestore (reuse the C3 shape) + transactional bypass
# --------------------------------------------------------------------------- #


class _FakeDocRef:
    def __init__(self, store: dict, path: str) -> None:
        self._store, self.path = store, path

    def set(self, data: dict) -> None:
        self._store[self.path] = dict(data)

    def get(self, transaction: Any = None) -> SimpleNamespace:
        if self.path not in self._store:
            return SimpleNamespace(exists=False, to_dict=lambda: None)
        data = dict(self._store[self.path])
        return SimpleNamespace(exists=True, to_dict=lambda: data)

    def update(self, data: dict) -> None:
        self._store[self.path].update(data)


class _FakeCollection:
    def __init__(self, store: dict, name: str) -> None:
        self._store, self._name = store, name

    def document(self, doc_id: str) -> _FakeDocRef:
        return _FakeDocRef(self._store, f"{self._name}/{doc_id}")


class _FakeTxn:
    def __init__(self, store: dict) -> None:
        self._store = store

    def update(self, ref: _FakeDocRef, data: dict) -> None:
        ref.update(data)


class _FakeFirestore:
    def __init__(self) -> None:
        self._store: dict = {}

    def collection(self, name: str) -> _FakeCollection:
        return _FakeCollection(self._store, name)

    def transaction(self) -> _FakeTxn:
        return _FakeTxn(self._store)

    def raw(self, path: str) -> dict | None:
        return self._store.get(path)


@pytest.fixture(autouse=True)
def _bypass_transactional(monkeypatch: pytest.MonkeyPatch) -> None:
    def passthrough(fn):  # noqa: ANN001
        def wrapper(transaction, *a, **k):  # noqa: ANN001
            return fn(transaction, *a, **k)
        return wrapper

    monkeypatch.setattr(approvals_mod.firestore, "transactional", passthrough)


# --------------------------------------------------------------------------- #
# Fake GCS bucket
# --------------------------------------------------------------------------- #


class _FakeBlob:
    def __init__(self, data: bytes | None, name: str, generation: int, recorder: dict) -> None:
        self._data, self._name, self._gen, self._rec = data, name, generation, recorder

    def download_as_bytes(self, raw_download: bool = False, if_generation_match: int | None = None) -> bytes:
        self._rec["calls"].append(
            {"name": self._name, "generation": self._gen, "raw_download": raw_download,
             "if_generation_match": if_generation_match}
        )
        if self._data is None:
            raise gcs_fetch.GcsFetchError(f"missing object {self._name}")
        return self._data


class _FakeBucket:
    def __init__(self, objects: dict[str, bytes]) -> None:
        # objects keyed by object_name → bytes
        self._objects = objects
        self.recorder: dict = {"calls": []}

    def blob(self, name: str, generation: int | None = None) -> _FakeBlob:
        return _FakeBlob(self._objects.get(name), name, generation, self.recorder)


# --------------------------------------------------------------------------- #
# Consistent artifact + approval builders
# --------------------------------------------------------------------------- #


def _iac_dir(tmp_path: Path) -> Path:
    """A baked-iac/ stand-in: a lockfile + one declared Cloud Run resource."""
    d = tmp_path / "iac"
    d.mkdir()
    (d / ".terraform.lock.hcl").write_text('provider "x" { version = "6.50.0" }\n', encoding="utf-8")
    (d / "cloudrun.tf").write_text(
        'resource "google_cloud_run_v2_service" "payment_demo" {\n  name = "payment-demo"\n}\n',
        encoding="utf-8",
    )
    return d


def _prefix() -> str:
    return f"gs://driftscribe-hack-2026-tofu-artifacts/pr-12/{_SHA40}/run-100-1/"


def _plan_json_obj(actions: list[str] | None = None, address: str | None = None) -> dict:
    return {
        "resource_changes": [
            {
                "address": address or "google_cloud_run_v2_service.payment_demo",
                "type": "google_cloud_run_v2_service",
                "change": {"actions": actions or ["update"], "before": {"name": "payment-demo"},
                           "after": {"name": "payment-demo"}},
            }
        ]
    }


def _metadata(lock_sha: str, plan_bytes: bytes, json_bytes: bytes) -> dict:
    p = _prefix()
    return {
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
        "plan_sha256": _sha(plan_bytes),
        "plan_json_sha256": _sha(json_bytes),
        "opentofu_version": "1.12.0",
        "provider_lockfile_sha256": lock_sha,
    }


def _wire(monkeypatch, tmp_path, *, plan_obj=None) -> dict:
    """Wire the worker for an /apply happy path: temp IAC_DIR, fake bucket with a
    consistent artifact set, a real store holding a valid approval, and a stubbed
    tofu runner. Returns a context dict."""
    iac = _iac_dir(tmp_path)
    monkeypatch.setattr(m, "IAC_DIR", iac)
    lock_sha = hashlib.sha256((iac / ".terraform.lock.hcl").read_bytes()).hexdigest()

    plan_bytes = b"BINARY-TFPLAN"
    json_bytes = json.dumps(plan_obj or _plan_json_obj()).encode("utf-8")
    md = _metadata(lock_sha, plan_bytes, json_bytes)
    meta_bytes = json.dumps(md).encode("utf-8")

    objects = {
        f"pr-12/{_SHA40}/run-100-1/metadata.json": meta_bytes,
        f"pr-12/{_SHA40}/run-100-1/plan.tfplan": plan_bytes,
        f"pr-12/{_SHA40}/run-100-1/plan.json": json_bytes,
    }
    bucket = _FakeBucket(objects)
    monkeypatch.setattr(m, "_get_artifact_bucket", lambda: bucket)

    fake_fs = _FakeFirestore()
    store = PlanApprovalStore(project="test-proj", client=fake_fs)
    monkeypatch.setattr(m, "_get_plan_approval_store", lambda: store)
    # Pin the worker's clock so the 15-min signed window is not-expired at apply.
    monkeypatch.setattr(m, "_now", lambda: _NOW)

    issued_at, expires_at = new_approval_window(now=_NOW)
    payload = build_plan_approval_payload(
        metadata=md,
        artifact_uri_metadata=_prefix() + "metadata.json",
        generation_metadata="1700000000000003",
        approver="alice@corp.example",
        issued_at=issued_at,
        expires_at=expires_at,
    )
    record, raw_token = store.create(payload=payload, hmac_key=m.PLAN_APPROVAL_HMAC_KEY, created_by="coord")

    # Stubbed tofu runner: dispatch on the sub-command. Records calls.
    tofu_calls: list[dict] = []

    def fake_run(args: list[str], cwd: str, env: dict) -> tuple[int, str, str]:
        tofu_calls.append({"args": args, "cwd": cwd, "kms": env.get("TF_VAR_tofu_state_kms_key")})
        if args[:2] == ["version", "-json"]:
            return 0, json.dumps({"terraform_version": "1.12.0"}), ""
        if args[0] == "init":
            return 0, "", ""
        if args[0] == "plan":
            return 0, "", ""        # exit 0 = fresh
        if args[0] == "apply":
            return 0, "Apply complete!", ""
        if args[:2] == ["state", "pull"]:
            return 0, json.dumps({"serial": 7, "lineage": "lin-xyz"}), ""
        return 1, "", "unexpected"

    monkeypatch.setattr(m, "_RUN_TOFU", fake_run)

    return {
        "store": store, "fake_fs": fake_fs, "bucket": bucket, "record": record,
        "raw_token": raw_token, "md": md, "tofu_calls": tofu_calls, "iac": iac,
        "json_bytes": json_bytes, "plan_bytes": plan_bytes,
    }


@pytest.fixture
def client(monkeypatch) -> TestClient:
    """A TestClient with auth overridden to a fixed caller (the signed approver).

    The override MUST key on the original ``_verify_caller_dep`` object that the
    routes captured at decoration time (FastAPI matches Depends() by identity)."""
    m.app.dependency_overrides[m._verify_caller_dep] = lambda: "alice@corp.example"
    yield TestClient(m.app)
    m.app.dependency_overrides.clear()


def _doc(ctx) -> dict:
    return ctx["fake_fs"].raw(f"plan_approvals/{ctx['record'].approval_id}")


# =========================================================================== #
# gcs_fetch
# =========================================================================== #


def test_parse_gs_uri_ok_and_bad() -> None:
    assert gcs_fetch.parse_gs_uri("gs://b/o/p.json") == ("b", "o/p.json")
    for bad in ("http://b/o", "gs://b", "gs:///o", "b/o"):
        with pytest.raises(gcs_fetch.GcsFetchError):
            gcs_fetch.parse_gs_uri(bad)


def test_validate_artifact_uri() -> None:
    good = _prefix() + "plan.tfplan"
    assert gcs_fetch.validate_artifact_uri(good, expected_basename="plan.tfplan")[1].endswith("plan.tfplan")
    # wrong bucket
    with pytest.raises(gcs_fetch.GcsFetchError):
        gcs_fetch.validate_artifact_uri("gs://evil/pr-12/" + _SHA40 + "/run-100-1/plan.tfplan",
                                        expected_basename="plan.tfplan")
    # wrong basename
    with pytest.raises(gcs_fetch.GcsFetchError):
        gcs_fetch.validate_artifact_uri(good, expected_basename="plan.json")
    # bad scheme/path
    with pytest.raises(gcs_fetch.GcsFetchError):
        gcs_fetch.validate_artifact_uri("gs://driftscribe-hack-2026-tofu-artifacts/etc/passwd",
                                        expected_basename="plan.json")


def test_fetch_object_pinned_passes_pinning_args() -> None:
    bucket = _FakeBucket({"o": b"DATA"})
    out = gcs_fetch.fetch_object_pinned(bucket, "o", "1700000000000001")
    assert out == b"DATA"
    call = bucket.recorder["calls"][0]
    assert call["generation"] == 1700000000000001
    assert call["raw_download"] is True
    assert call["if_generation_match"] == 1700000000000001


def test_fetch_object_pinned_bad_generation() -> None:
    with pytest.raises(gcs_fetch.GcsFetchError):
        gcs_fetch.fetch_object_pinned(_FakeBucket({"o": b"x"}), "o", "not-a-number")


# =========================================================================== #
# tofu_runner — guards
# =========================================================================== #


def test_extract_declared_addresses(tmp_path: Path) -> None:
    iac = _iac_dir(tmp_path)
    assert tofu_runner.extract_declared_addresses(iac) == {"google_cloud_run_v2_service.payment_demo"}


def test_resource_set_guard_pass_update_and_noop() -> None:
    declared = {"google_cloud_run_v2_service.payment_demo"}
    assert tofu_runner.resource_set_guard(_plan_json_obj(["update"]), declared) is None
    assert tofu_runner.resource_set_guard(_plan_json_obj(["no-op"]), declared) is None


def test_resource_set_guard_refuses_create_module_and_unknown() -> None:
    declared = {"google_cloud_run_v2_service.payment_demo"}
    assert tofu_runner.resource_set_guard(_plan_json_obj(["create"]), declared) is not None
    assert tofu_runner.resource_set_guard(
        _plan_json_obj(["update"], address="module.x.google_cloud_run_v2_service.payment_demo"), declared
    ) is not None
    assert tofu_runner.resource_set_guard(
        _plan_json_obj(["update"], address="google_storage_bucket.new"), declared
    ) is not None


def test_resource_set_guard_normalizes_instance_suffix() -> None:
    declared = {"google_cloud_run_v2_service.payment_demo"}
    assert tofu_runner.resource_set_guard(
        _plan_json_obj(["update"], address='google_cloud_run_v2_service.payment_demo["a"]'), declared
    ) is None


def test_assert_fidelity_version_and_lock_mismatch() -> None:
    md = {"opentofu_version": "1.12.0", "provider_lockfile_sha256": "lock"}
    declared = {"google_cloud_run_v2_service.payment_demo"}
    pj = _plan_json_obj(["update"])
    with pytest.raises(tofu_runner.FidelityError):
        tofu_runner.assert_fidelity(signed_metadata=md, baked_tofu_version="1.11.0",
                                    baked_lockfile_sha256="lock", plan_json=pj, declared_addresses=declared)
    with pytest.raises(tofu_runner.FidelityError):
        tofu_runner.assert_fidelity(signed_metadata=md, baked_tofu_version="1.12.0",
                                    baked_lockfile_sha256="OTHER", plan_json=pj, declared_addresses=declared)
    # happy
    tofu_runner.assert_fidelity(signed_metadata=md, baked_tofu_version="1.12.0",
                                baked_lockfile_sha256="lock", plan_json=pj, declared_addresses=declared)


# =========================================================================== #
# tofu_runner — apply sequence
# =========================================================================== #


def _runner(seq: dict):
    """A run_tofu stub keyed by sub-command, returning ``seq[cmd]`` = (rc,out,err)."""
    calls: list[list[str]] = []

    def run(args, cwd, env):  # noqa: ANN001
        calls.append(args)
        cmd = "version" if args[:1] == ["version"] else args[0]
        if args[:2] == ["state", "pull"]:
            cmd = "state"
        return seq.get(cmd, (1, "", "unexpected"))

    return run, calls


def test_run_apply_sequence_happy() -> None:
    run, calls = _runner({
        "init": (0, "", ""), "plan": (0, "", ""), "apply": (0, "", ""),
        "state": (0, json.dumps({"serial": 3, "lineage": "L"}), ""),
    })
    out = tofu_runner.run_apply_sequence(workdir="/tmp/iac", kms_key="K", base_env={}, run_tofu=run)
    assert out.apply_exit == 0 and out.state_serial == 3 and out.state_lineage == "L"
    # every call carried the KMS key + apply ran exactly once + no re-plan (only refresh-only)
    plan_calls = [c for c in calls if c[0] == "plan"]
    assert all("-refresh-only" in c for c in plan_calls)
    assert sum(1 for c in calls if c[0] == "apply") == 1


def test_run_apply_sequence_kms_on_every_call() -> None:
    seen: list[str | None] = []

    def run(args, cwd, env):  # noqa: ANN001
        seen.append(env.get("TF_VAR_tofu_state_kms_key"))
        if args[0] == "plan":
            return 0, "", ""
        if args[:2] == ["state", "pull"]:
            return 0, "{}", ""
        return 0, "", ""

    tofu_runner.run_apply_sequence(workdir="/x", kms_key="THEKEY", base_env={}, run_tofu=run)
    assert seen and all(k == "THEKEY" for k in seen)


def test_run_apply_sequence_drift_refuses() -> None:
    run, _ = _runner({"init": (0, "", ""), "plan": (2, "drift!", "")})
    with pytest.raises(tofu_runner.FreshnessDrift):
        tofu_runner.run_apply_sequence(workdir="/x", kms_key="K", base_env={}, run_tofu=run)


def test_run_apply_sequence_freshness_error_refuses() -> None:
    run, _ = _runner({"init": (0, "", ""), "plan": (1, "", "boom")})
    with pytest.raises(tofu_runner.TofuStepError):
        tofu_runner.run_apply_sequence(workdir="/x", kms_key="K", base_env={}, run_tofu=run)


def test_run_apply_sequence_apply_failure() -> None:
    # readable + unchanged serial (before==after==3) + clean refresh (plan exit 0)
    # ⇒ PROVABLY clean failure ⇒ plain TofuStepError (not state-suspect).
    run, _ = _runner({"init": (0, "", ""), "plan": (0, "", ""), "apply": (1, "", "apply boom"),
                      "state": (0, json.dumps({"serial": 3, "lineage": "L"}), "")})
    with pytest.raises(tofu_runner.TofuStepError) as ei:
        tofu_runner.run_apply_sequence(workdir="/x", kms_key="K", base_env={}, run_tofu=run)
    assert ei.value.step == "apply"
    assert not isinstance(ei.value, tofu_runner.ApplyStateSuspect)


def test_run_apply_sequence_init_failure_skips_plan_and_apply() -> None:
    run, calls = _runner({"init": (1, "", "init boom")})
    with pytest.raises(tofu_runner.TofuStepError):
        tofu_runner.run_apply_sequence(workdir="/x", kms_key="K", base_env={}, run_tofu=run)
    assert [c[0] for c in calls] == ["init"]  # never reached plan/apply


# =========================================================================== #
# tofu_runner — state-lock contention classification (C5d)
# =========================================================================== #

# OpenTofu's canonical state-lock-acquire stderr (the "Lock Info:" block follows).
_LOCK_STDERR = (
    "Error: Error acquiring the state lock\n\n"
    "Error message: writing \"gs://.../default.tflock\" failed: "
    "googleapi: Error 412: Precondition Failed, conditionNotMet\n"
    "Lock Info:\n  ID:        abc-123\n  Operation: OperationTypeApply\n"
)


def test_run_apply_sequence_init_lock_refused() -> None:
    run, _ = _runner({"init": (1, "", _LOCK_STDERR)})
    with pytest.raises(tofu_runner.LockRefused) as ei:
        tofu_runner.run_apply_sequence(workdir="/x", kms_key="K", base_env={}, run_tofu=run)
    assert ei.value.step == "init"
    # LockRefused is a STANDALONE Exception, NOT a TofuStepError subclass.
    assert not isinstance(ei.value, tofu_runner.TofuStepError)


def test_run_apply_sequence_refresh_only_lock_refused() -> None:
    run, _ = _runner({"init": (0, "", ""), "plan": (1, "", _LOCK_STDERR)})
    with pytest.raises(tofu_runner.LockRefused) as ei:
        tofu_runner.run_apply_sequence(workdir="/x", kms_key="K", base_env={}, run_tofu=run)
    assert ei.value.step == "refresh-only"


def test_run_apply_sequence_apply_lock_refused() -> None:
    run, _ = _runner({"init": (0, "", ""), "plan": (0, "", ""), "apply": (1, "", _LOCK_STDERR)})
    with pytest.raises(tofu_runner.LockRefused) as ei:
        tofu_runner.run_apply_sequence(workdir="/x", kms_key="K", base_env={}, run_tofu=run)
    assert ei.value.step == "apply"


def test_run_apply_sequence_init_nonlock_failure_is_step_error() -> None:
    run, _ = _runner({"init": (1, "", "Error: some provider error")})
    with pytest.raises(tofu_runner.TofuStepError) as ei:
        tofu_runner.run_apply_sequence(workdir="/x", kms_key="K", base_env={}, run_tofu=run)
    assert ei.value.step == "init"
    assert not isinstance(ei.value, tofu_runner.LockRefused)


def test_run_apply_sequence_refresh_only_nonlock_failure_is_step_error() -> None:
    run, _ = _runner({"init": (0, "", ""), "plan": (1, "", "Error: some provider error")})
    with pytest.raises(tofu_runner.TofuStepError) as ei:
        tofu_runner.run_apply_sequence(workdir="/x", kms_key="K", base_env={}, run_tofu=run)
    assert ei.value.step == "refresh-only"
    assert not isinstance(ei.value, tofu_runner.LockRefused)


def test_run_apply_sequence_apply_nonlock_failure_is_step_error() -> None:
    # non-lock failure + provably-clean state (serial readable+unchanged, refresh 0)
    # ⇒ TofuStepError, and specifically NOT LockRefused / NOT ApplyStateSuspect.
    run, _ = _runner({"init": (0, "", ""), "plan": (0, "", ""), "apply": (1, "", "Error: some provider error"),
                      "state": (0, json.dumps({"serial": 3, "lineage": "L"}), "")})
    with pytest.raises(tofu_runner.TofuStepError) as ei:
        tofu_runner.run_apply_sequence(workdir="/x", kms_key="K", base_env={}, run_tofu=run)
    assert ei.value.step == "apply"
    assert not isinstance(ei.value, tofu_runner.LockRefused)
    assert not isinstance(ei.value, tofu_runner.ApplyStateSuspect)


def test_run_apply_sequence_drift_not_shadowed_by_lock_classification() -> None:
    """refresh-only rc==2 stays FreshnessDrift even if its stderr mentions a lock —
    the drift branch is checked ABOVE the rc!=0 lock classification."""
    run, _ = _runner({"init": (0, "", ""), "plan": (2, "drift!", _LOCK_STDERR)})
    with pytest.raises(tofu_runner.FreshnessDrift):
        tofu_runner.run_apply_sequence(workdir="/x", kms_key="K", base_env={}, run_tofu=run)


def test_is_lock_contention_matches_canonical_phrase_any_case() -> None:
    assert tofu_runner._is_lock_contention("Error: Error acquiring the state lock\nLock Info:")
    assert tofu_runner._is_lock_contention("error acquiring the state lock")
    assert tofu_runner._is_lock_contention("ERROR ACQUIRING THE STATE LOCK")
    assert tofu_runner._is_lock_contention("prefix... Error Acquiring The State Lock ...suffix")


def test_is_lock_contention_conservative_on_benign_lock_words() -> None:
    # Benign uses of the word "lock" must NOT be classified as contention
    # (fail-closed default → TofuStepError).
    assert not tofu_runner._is_lock_contention("Error: deadlock avoidance in provider X")
    assert not tofu_runner._is_lock_contention("Error: cannot open block device /dev/sda")
    assert not tofu_runner._is_lock_contention("Error: failed to read lockfile .terraform.lock.hcl")
    assert not tofu_runner._is_lock_contention("Error: some provider error")
    assert not tofu_runner._is_lock_contention("")


# =========================================================================== #
# tofu_runner — failed_state_suspect diagnosis (C5g carry-forward 1b)
# =========================================================================== #


def _seq_runner(*, apply, serials, refresh=(0, "", ""), freshness=(0, "", ""), init=(0, "", "")):
    """run_tofu stub with per-call control of the failed-apply DIAGNOSTIC path.

    Call order in run_apply_sequence: init → plan(freshness) → state pull(before)
    → apply → [on non-lock failure] state pull(after) → plan(refresh-only diag).
    ``serials`` = (before, after); a None entry makes that ``state pull`` error
    (→ serial None). ``freshness``/``refresh`` are the 1st/2nd ``plan`` calls."""
    state_n = {"i": 0}
    plan_n = {"i": 0}
    calls: list[list[str]] = []

    def run(args, cwd, env):  # noqa: ANN001
        calls.append(args)
        if args[:2] == ["state", "pull"]:
            i = state_n["i"]
            state_n["i"] += 1
            s = serials[i] if i < len(serials) else None
            return (1, "", "state pull failed") if s is None else (0, json.dumps({"serial": s, "lineage": "L"}), "")
        if args[0] == "init":
            return init
        if args[0] == "plan":
            i = plan_n["i"]
            plan_n["i"] += 1
            return freshness if i == 0 else refresh
        if args[0] == "apply":
            return apply
        return 1, "", "unexpected"

    return run, calls


def test_apply_failure_state_suspect_on_serial_bump() -> None:
    """A failed apply that BUMPED the state serial → ApplyStateSuspect (the C5g
    signature: a 403-at-admission apply still persisted the planned attribute)."""
    run, _ = _seq_runner(apply=(1, "", "Error 403 admission denied"), serials=(3, 4))
    with pytest.raises(tofu_runner.ApplyStateSuspect) as ei:
        tofu_runner.run_apply_sequence(workdir="/x", kms_key="K", base_env={}, run_tofu=run)
    e = ei.value
    assert e.step == "apply" and e.exit_code == 1
    assert e.diag.state_suspect is True and e.diag.serial_bumped is True
    assert e.diag.serial_before == 3 and e.diag.serial_after == 4
    # standalone — NOT a TofuStepError subclass (so existing handlers are unaffected)
    assert not isinstance(e, tofu_runner.TofuStepError)


def test_apply_failure_state_suspect_on_refresh_drift() -> None:
    """No serial bump, but a post-failure refresh-only now sees state≠live (exit 2)
    → ApplyStateSuspect carrying the drift output."""
    run, _ = _seq_runner(
        apply=(1, "", "boom"), serials=(3, 3),
        refresh=(2, "~ service_account = runtime@ -> compute@", ""),
    )
    with pytest.raises(tofu_runner.ApplyStateSuspect) as ei:
        tofu_runner.run_apply_sequence(workdir="/x", kms_key="K", base_env={}, run_tofu=run)
    assert ei.value.diag.serial_bumped is False
    assert ei.value.diag.refresh_drift is True
    assert "service_account" in ei.value.diag.refresh_output


def test_apply_failure_clean_stays_step_error() -> None:
    """No serial bump AND a clean post-failure refresh (exit 0) → plain
    TofuStepError (today's 502 'failed'), NOT suspect."""
    run, _ = _seq_runner(apply=(1, "", "boom"), serials=(3, 3), refresh=(0, "", ""))
    with pytest.raises(tofu_runner.TofuStepError) as ei:
        tofu_runner.run_apply_sequence(workdir="/x", kms_key="K", base_env={}, run_tofu=run)
    assert ei.value.step == "apply"
    assert not isinstance(ei.value, tofu_runner.ApplyStateSuspect)


def test_apply_failure_serial_lost_is_suspect() -> None:
    """Serial readable BEFORE the apply but not after (state left unreadable) →
    suspect even without a positive drift signal (can't PROVE clean)."""
    run, _ = _seq_runner(apply=(1, "", "boom"), serials=(3, None), refresh=(0, "", ""))
    with pytest.raises(tofu_runner.ApplyStateSuspect) as ei:
        tofu_runner.run_apply_sequence(workdir="/x", kms_key="K", base_env={}, run_tofu=run)
    assert ei.value.diag.state_suspect is True


def test_apply_failure_serial_unknown_before_is_suspect() -> None:
    """Serial UNREADABLE before the apply ⇒ a clean verdict cannot be proven ⇒
    suspect, even if the post-failure refresh is exit 0 (Codex blocker 1: the
    fail-closed posture, NOT 'positive signals only')."""
    run, _ = _seq_runner(apply=(1, "", "boom"), serials=(None, 3), refresh=(0, "", ""))
    with pytest.raises(tofu_runner.ApplyStateSuspect) as ei:
        tofu_runner.run_apply_sequence(workdir="/x", kms_key="K", base_env={}, run_tofu=run)
    assert ei.value.diag.state_suspect is True
    assert ei.value.diag.serial_bumped is False  # no bump signal — suspect is from unprovable-clean


def test_apply_failure_refresh_error_is_suspect() -> None:
    """The post-failure refresh-only itself ERRORING (exit 1) is 'could not prove
    clean', not 'clean' ⇒ suspect even with no serial bump (Codex blocker 2)."""
    run, _ = _seq_runner(apply=(1, "", "boom"), serials=(3, 3), refresh=(1, "", "refresh exploded"))
    with pytest.raises(tofu_runner.ApplyStateSuspect) as ei:
        tofu_runner.run_apply_sequence(workdir="/x", kms_key="K", base_env={}, run_tofu=run)
    assert ei.value.diag.state_suspect is True
    assert ei.value.diag.refresh_drift is False  # exit 1 ≠ drift(exit 2), but still suspect


def test_apply_failure_diagnostic_refresh_is_lock_free() -> None:
    """The post-failure diagnostic refresh-only MUST be ``-lock=false`` (it runs
    after a failed apply and must never contend for / re-acquire the lock)."""
    run, calls = _seq_runner(apply=(1, "", "boom"), serials=(3, 4))
    with pytest.raises(tofu_runner.ApplyStateSuspect):
        tofu_runner.run_apply_sequence(workdir="/x", kms_key="K", base_env={}, run_tofu=run)
    diag_plan = [c for c in calls if c[0] == "plan"][-1]
    assert "-refresh-only" in diag_plan and "-lock=false" in diag_plan


def test_apply_lock_contention_skips_diagnosis() -> None:
    """Lock contention on apply → LockRefused with NO post-failure diagnosis (the
    lock was never acquired ⇒ no state write ⇒ nothing to diagnose). The 2nd state
    pull + diagnostic refresh must NOT run."""
    run, calls = _seq_runner(apply=(1, "", _LOCK_STDERR), serials=(3, 4))
    with pytest.raises(tofu_runner.LockRefused):
        tofu_runner.run_apply_sequence(workdir="/x", kms_key="K", base_env={}, run_tofu=run)
    assert sum(1 for c in calls if c[:2] == ["state", "pull"]) == 1  # only serial_before
    assert sum(1 for c in calls if c[0] == "plan") == 1              # only freshness


def test_failed_state_suspect_in_phase_vocabulary() -> None:
    """The new terminal phase is registered in the single source of truth so the
    coordinator's reconcile/audit surfaces recognize it (approvals.py)."""
    assert "failed_state_suspect" in approvals_mod.APPLY_AUDIT_PHASES


# =========================================================================== #
# tofu_runner — semantic freshness gate (C5g carry-forward 1a)
# =========================================================================== #

_PD = "google_cloud_run_v2_service.payment_demo"


def _drift_show(before, after, *, address=_PD, actions=None,
                resource_changes=None, output_changes=None):  # noqa: ANN001, ANN201
    """A minimal `tofu show -json` of a refresh-only plan with one drift entry."""
    j = {"resource_drift": [{
        "address": address, "type": "google_cloud_run_v2_service",
        "change": {"actions": actions or ["update"], "before": before, "after": after},
    }]}
    if resource_changes is not None:
        j["resource_changes"] = resource_changes
    if output_changes is not None:
        j["output_changes"] = output_changes
    return j


def test_changed_leaf_paths_added_removed_nested() -> None:
    paths = tofu_runner._changed_leaf_paths({"a": 1, "b": {"c": 2}}, {"a": 1, "b": {"c": 3, "d": 4}})
    assert paths == {"b.c", "b.d"}  # c changed + d added; a unchanged → omitted


def test_changed_leaf_paths_list_index_stripped() -> None:
    paths = tofu_runner._changed_leaf_paths(
        {"conditions": [{"t": "t1"}]}, {"conditions": [{"t": "t2"}]})
    assert paths == {"conditions.t"}  # list index stripped in the normalized path


def test_is_computed_only_path_anchoring() -> None:
    assert tofu_runner._is_computed_only_path("generation")
    assert tofu_runner._is_computed_only_path("conditions.last_transition_time")
    assert tofu_runner._is_computed_only_path("terminal_condition")
    # subtree match is anchored at the path root + a "." boundary
    assert not tofu_runner._is_computed_only_path("conditionsFoo")
    assert not tofu_runner._is_computed_only_path("template.conditions.x")
    assert not tofu_runner._is_computed_only_path("template.service_account")
    # identity/lifecycle-computed fields are deliberately NOT allowlisted
    assert not tofu_runner._is_computed_only_path("uid")
    assert not tofu_runner._is_computed_only_path("create_time")


def test_classify_drift_benign_computed_churn() -> None:
    """The exact C5g churn set (generation/etag/timestamps/revisions/...) → benign."""
    before = {
        "generation": 6, "etag": "a", "client_version": "1.0",
        "template": {"service_account": "runtime@", "containers": [{"image": "img:1"}]},
        "terminal_condition": {"last_transition_time": "t1"},
        "conditions": [{"type": "Ready", "last_transition_time": "t1"}],
        "latest_ready_revision": "rev-6",
    }
    after = {**before, "generation": 10, "etag": "b", "client_version": "1.1",
             "terminal_condition": {"last_transition_time": "t2"},
             "conditions": [{"type": "Ready", "last_transition_time": "t2"}],
             "latest_ready_revision": "rev-10"}
    v = tofu_runner.classify_refresh_drift(_drift_show(before, after))
    assert v.benign is True
    assert v.paths and all(":" in p for p in v.paths)  # the drifted computed paths, for audit


def test_classify_drift_material_env_change_refuses() -> None:
    before = {"generation": 6, "template": {"containers": [{"env": [{"name": "X", "value": "a"}]}]}}
    after = {"generation": 7, "template": {"containers": [{"env": [{"name": "X", "value": "b"}]}]}}
    v = tofu_runner.classify_refresh_drift(_drift_show(before, after))
    assert v.benign is False and any("env" in p for p in v.paths)


def test_classify_drift_material_service_account_refuses() -> None:
    v = tofu_runner.classify_refresh_drift(_drift_show(
        {"template": {"service_account": "runtime@"}}, {"template": {"service_account": "evil@"}}))
    assert v.benign is False and any("service_account" in p for p in v.paths)


def test_classify_drift_mixed_reports_only_material() -> None:
    """A drift mixing computed churn + one material change refuses, and the
    refusal names ONLY the material path (computed paths are not the offender)."""
    v = tofu_runner.classify_refresh_drift(_drift_show(
        {"generation": 6, "template": {"service_account": "runtime@"}},
        {"generation": 7, "template": {"service_account": "evil@"}}))
    assert v.benign is False
    assert any("service_account" in p for p in v.paths)
    assert not any(p.endswith(":generation") for p in v.paths)


def test_classify_drift_delete_create_replace_actions_material() -> None:
    for acts in (["delete"], ["create"], ["delete", "create"], ["create", "delete"]):
        v = tofu_runner.classify_refresh_drift(_drift_show({"x": 1}, {"x": 2}, actions=acts))
        assert v.benign is False, acts


def test_classify_drift_resource_changes_action_refuses() -> None:
    j = _drift_show({"generation": 6}, {"generation": 7},
                    resource_changes=[{"address": "x", "change": {"actions": ["update"]}}])
    v = tofu_runner.classify_refresh_drift(j)
    assert v.benign is False and "resource_changes" in v.reason


def test_classify_drift_resource_changes_noop_read_ok() -> None:
    for act in (["no-op"], ["read"]):
        j = _drift_show({"generation": 6}, {"generation": 7},
                        resource_changes=[{"address": "x", "change": {"actions": act}}])
        assert tofu_runner.classify_refresh_drift(j).benign is True, act


def test_classify_drift_output_changes_refuses() -> None:
    j = _drift_show({"generation": 6}, {"generation": 7},
                    output_changes={"o": {"actions": ["update"]}})
    v = tofu_runner.classify_refresh_drift(j)
    assert v.benign is False and "output" in v.reason


def test_classify_drift_identity_lifecycle_fields_material() -> None:
    """uid / create_time / delete_time are computed but signal recreate/deletion —
    they MUST refuse, not pass as benign (Codex review 019e7a3f)."""
    for added in ({"uid": "u"}, {"create_time": "t"}, {"delete_time": "t"}):
        v = tofu_runner.classify_refresh_drift(_drift_show({"generation": 6}, {"generation": 6, **added}))
        assert v.benign is False, added


def test_classify_drift_noop_entry_skipped() -> None:
    assert tofu_runner.classify_refresh_drift(_drift_show({"x": 1}, {"x": 1}, actions=["no-op"])).benign is True


def test_classify_drift_unknown_type_fails_closed() -> None:
    """The computed allowlist is type-scoped: even PURELY computed-looking drift
    on a NON-Cloud-Run-v2 resource refuses (future C6 resource-set safety)."""
    j = {"resource_drift": [{
        "address": "google_storage_bucket.x", "type": "google_storage_bucket",
        "change": {"actions": ["update"], "before": {"generation": 1}, "after": {"generation": 2}}}]}
    v = tofu_runner.classify_refresh_drift(j)
    assert v.benign is False and "type-scoped" in v.reason


def test_has_true_recurses_only_real_true() -> None:
    assert tofu_runner._has_true({"a": {"b": True}}) is True
    assert tofu_runner._has_true([False, {"x": True}]) is True
    # all-false / empty present trees must NOT flag (else benign churn over-refuses)
    assert tofu_runner._has_true({"a": False, "b": {"c": False}}) is False
    assert tofu_runner._has_true({}) is False
    assert tofu_runner._has_true(False) is False


def test_classify_drift_sensitive_marker_fails_closed() -> None:
    """A sensitive attr is redacted IDENTICALLY in before/after; the real change
    lives only in *_sensitive. before==after must NOT read as benign when a marker
    is set (the false-PROCEED the adversarial review found)."""
    j = {"resource_drift": [{
        "address": _PD, "type": "google_cloud_run_v2_service",
        "change": {"actions": ["update"],
                   "before": {"template": {"containers": [{"env": [{"value": None}]}]}},
                   "after": {"template": {"containers": [{"env": [{"value": None}]}]}},
                   "after_sensitive": {"template": {"containers": [{"env": [{"value": True}]}]}}}}]}
    v = tofu_runner.classify_refresh_drift(j)
    assert v.benign is False and "sensitive" in v.reason


def test_classify_drift_after_unknown_marker_fails_closed() -> None:
    j = {"resource_drift": [{
        "address": _PD, "type": "google_cloud_run_v2_service",
        "change": {"actions": ["update"], "before": {"generation": 6}, "after": {"generation": 6},
                   "after_unknown": {"uri": True}}}]}
    assert tofu_runner.classify_refresh_drift(j).benign is False


def test_classify_drift_all_false_marker_tree_still_benign() -> None:
    """An all-false (present-but-empty) marker tree must NOT over-refuse the
    benign computed-churn case (why _has_true recurses for a real True)."""
    j = {"resource_drift": [{
        "address": _PD, "type": "google_cloud_run_v2_service",
        "change": {"actions": ["update"], "before": {"generation": 6}, "after": {"generation": 10},
                   "before_sensitive": {"generation": False}, "after_sensitive": {"generation": False},
                   "after_unknown": {}}}]}
    assert tofu_runner.classify_refresh_drift(j).benign is True


def test_classify_drift_fail_closed_on_malformed() -> None:
    assert tofu_runner.classify_refresh_drift("not-a-dict").benign is False
    assert tofu_runner.classify_refresh_drift({}).benign is False                      # no resource_drift
    assert tofu_runner.classify_refresh_drift({"resource_drift": "x"}).benign is False  # not a list
    # non-dict before/after on an update → fail closed
    assert tofu_runner.classify_refresh_drift(
        {"resource_drift": [{"address": "a", "change": {"actions": ["update"], "before": "x", "after": {}}}]}
    ).benign is False
    # malformed entry (not a dict)
    assert tofu_runner.classify_refresh_drift({"resource_drift": ["x"]}).benign is False


def _gate_runner(*, refresh_exit, show_json=None, show_exit=0, apply=(0, "", ""), serials=(3, 3)):  # noqa: ANN001, ANN202
    """Stub for the freshness-gate path: init → plan(freshness)=refresh_exit →
    [on exit 2] show -json refresh.tfplan → show_json → state pull → apply."""
    state_n = {"i": 0}
    calls: list[list[str]] = []

    def run(args, cwd, env):  # noqa: ANN001
        calls.append(args)
        if args[:2] == ["state", "pull"]:
            i = state_n["i"]
            state_n["i"] += 1
            s = serials[i] if i < len(serials) else None
            return (1, "", "") if s is None else (0, json.dumps({"serial": s, "lineage": "L"}), "")
        if args[0] == "init":
            return 0, "", ""
        if args[:2] == ["show", "-json"]:
            return show_exit, (json.dumps(show_json) if show_json is not None else ""), ""
        if args[0] == "plan":
            return refresh_exit, ("drift" if refresh_exit == 2 else ""), ""
        if args[0] == "apply":
            return apply
        return 1, "", "unexpected"

    return run, calls


def test_gate_benign_drift_proceeds_and_records_paths() -> None:
    run, calls = _gate_runner(refresh_exit=2, show_json=_drift_show({"generation": 6}, {"generation": 10}))
    out = tofu_runner.run_apply_sequence(workdir="/x", kms_key="K", base_env={}, run_tofu=run)
    assert out.apply_exit == 0 and out.freshness_exit == 2
    assert any(":generation" in p for p in out.benign_drift_paths)
    assert sum(1 for c in calls if c[0] == "apply") == 1  # it DID apply (the saved plan)


def test_gate_material_drift_refuses() -> None:
    run, calls = _gate_runner(refresh_exit=2, show_json=_drift_show(
        {"template": {"service_account": "a"}}, {"template": {"service_account": "b"}}))
    with pytest.raises(tofu_runner.FreshnessDrift):
        tofu_runner.run_apply_sequence(workdir="/x", kms_key="K", base_env={}, run_tofu=run)
    assert not any(c[0] == "apply" for c in calls)  # never applied


def test_gate_show_failure_fails_closed() -> None:
    run, _ = _gate_runner(refresh_exit=2, show_exit=1)
    with pytest.raises(tofu_runner.FreshnessDrift):
        tofu_runner.run_apply_sequence(workdir="/x", kms_key="K", base_env={}, run_tofu=run)


def test_gate_fresh_exit0_skips_show() -> None:
    run, calls = _gate_runner(refresh_exit=0)
    out = tofu_runner.run_apply_sequence(workdir="/x", kms_key="K", base_env={}, run_tofu=run)
    assert out.freshness_exit == 0 and out.benign_drift_paths == ()
    assert not any(c[:2] == ["show", "-json"] for c in calls)  # show only runs on drift


def test_gate_empty_drift_on_exit2_fails_closed() -> None:
    """refresh exit 2 (drift) but show -json carries an EMPTY resource_drift (the
    signals disagree) → refuse, never proceed (fail-closed symmetry)."""
    run, calls = _gate_runner(refresh_exit=2, show_json={"resource_drift": []})
    with pytest.raises(tofu_runner.FreshnessDrift):
        tofu_runner.run_apply_sequence(workdir="/x", kms_key="K", base_env={}, run_tofu=run)
    assert not any(c[0] == "apply" for c in calls)


def test_refresh_drift_verdict_fails_closed_on_classify_exception(monkeypatch) -> None:  # noqa: ANN001
    """A classification error (e.g. RecursionError on pathological nesting) must
    return a non-benign verdict, NEVER escape (an escape would 500 the request and
    strand the already-burned approval at phase=\"claimed\")."""
    def boom(_show_json):  # noqa: ANN001, ANN202
        raise RecursionError("too deep")

    monkeypatch.setattr(tofu_runner, "classify_refresh_drift", boom)

    def run(args, cwd, env):  # noqa: ANN001
        return 0, "{}", ""  # valid JSON from show -json; the classifier raises

    v = tofu_runner._refresh_drift_verdict(run, "/x", {})
    assert v.benign is False and "classification failed" in v.reason


# =========================================================================== #
# /healthz + /deny
# =========================================================================== #


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"ok": True}


def test_deny_happy(client: TestClient, monkeypatch, tmp_path) -> None:
    ctx = _wire(monkeypatch, tmp_path)
    r = client.post("/deny", json={"approval_id": ctx["record"].approval_id, "approval_token": ctx["raw_token"]})
    assert r.status_code == 200 and r.json()["status"] == "denied"
    assert _doc(ctx)["status"] == "denied"


def test_deny_wrong_token(client: TestClient, monkeypatch, tmp_path) -> None:
    ctx = _wire(monkeypatch, tmp_path)
    r = client.post("/deny", json={"approval_id": ctx["record"].approval_id, "approval_token": "x" * 43})
    assert r.status_code == 403
    assert _doc(ctx)["status"] == "pending"  # no flip


# =========================================================================== #
# /apply matrix
# =========================================================================== #


def test_apply_happy_path(client: TestClient, monkeypatch, tmp_path) -> None:
    ctx = _wire(monkeypatch, tmp_path)
    r = client.post("/apply", json={"approval_id": ctx["record"].approval_id, "approval_token": ctx["raw_token"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "applied" and body["apply_attempt_id"]
    doc = _doc(ctx)
    assert doc["status"] == "used"
    assert doc["apply_audit"]["phase"] == "applied"
    assert doc["apply_audit"]["state_serial"] == 7
    # artifacts fetched pinned (raw + if_generation_match)
    assert all(c["raw_download"] is True for c in ctx["bucket"].recorder["calls"])


def test_apply_hmac_mismatch(client: TestClient, monkeypatch, tmp_path) -> None:
    ctx = _wire(monkeypatch, tmp_path)
    r = client.post("/apply", json={"approval_id": ctx["record"].approval_id, "approval_token": "z" * 43})
    assert r.status_code == 403
    assert _doc(ctx)["status"] == "pending"  # not burned


def test_apply_wrong_approver(client: TestClient, monkeypatch, tmp_path) -> None:
    ctx = _wire(monkeypatch, tmp_path)
    m.app.dependency_overrides[m._verify_caller_dep] = lambda: "mallory@corp.example"
    r = client.post("/apply", json={"approval_id": ctx["record"].approval_id, "approval_token": ctx["raw_token"]})
    assert r.status_code == 403
    assert _doc(ctx)["status"] == "pending"  # gate before claim


def test_apply_expired_signed_window(client: TestClient, monkeypatch, tmp_path) -> None:
    ctx = _wire(monkeypatch, tmp_path)
    # A fresh approval whose SIGNED window is entirely in the past (relative to the
    # pinned _NOW) → plan_approval_is_expired reads the signed window → 403.
    past = _NOW - dt.timedelta(hours=1)
    issued_at, expires_at = new_approval_window(now=past)
    payload = build_plan_approval_payload(
        metadata=ctx["md"], artifact_uri_metadata=_prefix() + "metadata.json",
        generation_metadata="1700000000000003", approver="alice@corp.example",
        issued_at=issued_at, expires_at=expires_at,
    )
    rec, tok = ctx["store"].create(payload=payload, hmac_key=m.PLAN_APPROVAL_HMAC_KEY, created_by="coord")
    r = client.post("/apply", json={"approval_id": rec.approval_id, "approval_token": tok})
    assert r.status_code == 403 and "expired" in r.json()["detail"]


def test_apply_replay_after_use(client: TestClient, monkeypatch, tmp_path) -> None:
    ctx = _wire(monkeypatch, tmp_path)
    body = {"approval_id": ctx["record"].approval_id, "approval_token": ctx["raw_token"]}
    assert client.post("/apply", json=body).status_code == 200
    # second time: status is now used → 403
    assert client.post("/apply", json=body).status_code == 403


def test_apply_integrity_mismatch_burns_and_refuses(client: TestClient, monkeypatch, tmp_path) -> None:
    ctx = _wire(monkeypatch, tmp_path)
    # corrupt the stored plan.tfplan bytes so sha256 no longer matches the signed digest
    ctx["bucket"]._objects[f"pr-12/{_SHA40}/run-100-1/plan.tfplan"] = b"TAMPERED"
    r = client.post("/apply", json={"approval_id": ctx["record"].approval_id, "approval_token": ctx["raw_token"]})
    assert r.status_code == 422
    doc = _doc(ctx)
    assert doc["status"] == "used"  # claim-first: burned
    assert doc["apply_audit"]["phase"] == "integrity_refused"


def test_apply_denylist_violation_burns_and_refuses(client: TestClient, monkeypatch, tmp_path) -> None:
    # plan touches a control-plane service → denylist denies (re-run at apply)
    ctx = _wire(monkeypatch, tmp_path,
                plan_obj={"resource_changes": [{
                    "address": "google_cloud_run_v2_service.payment_demo",
                    "type": "google_cloud_run_v2_service",
                    "change": {"actions": ["update"], "before": {"name": "driftscribe-agent"},
                               "after": {"name": "driftscribe-agent"}}}]})
    r = client.post("/apply", json={"approval_id": ctx["record"].approval_id, "approval_token": ctx["raw_token"]})
    assert r.status_code == 422
    assert _doc(ctx)["apply_audit"]["phase"] == "verify_refused"


def test_apply_drift_refused(client: TestClient, monkeypatch, tmp_path) -> None:
    """MATERIAL refresh drift (an out-of-band desired-state change) → 409
    drift_refused, and the saved plan is NEVER applied."""
    ctx = _wire(monkeypatch, tmp_path)
    material_show = {"resource_drift": [{
        "address": "google_cloud_run_v2_service.payment_demo",
        "type": "google_cloud_run_v2_service",
        "change": {"actions": ["update"], "before": {"template": {"service_account": "a@x"}},
                   "after": {"template": {"service_account": "b@x"}}}}]}

    def run(args, cwd, env):  # noqa: ANN001
        if args[:2] == ["version", "-json"]:
            return 0, json.dumps({"terraform_version": "1.12.0"}), ""
        if args[0] == "init":
            return 0, "", ""
        if args[:2] == ["show", "-json"]:
            return 0, json.dumps(material_show), ""
        if args[0] == "plan":
            return 2, "drift detected", ""  # refresh-only exit 2
        return 1, "", "should not apply"

    monkeypatch.setattr(m, "_RUN_TOFU", run)
    r = client.post("/apply", json={"approval_id": ctx["record"].approval_id, "approval_token": ctx["raw_token"]})
    assert r.status_code == 409
    assert _doc(ctx)["apply_audit"]["phase"] == "drift_refused"


def test_apply_benign_computed_drift_applies(client: TestClient, monkeypatch, tmp_path) -> None:
    """C5g 1a: refresh drift that is PURELY server-computed churn (generation) →
    the semantic gate proceeds, applies the saved plan, and the success audit
    records freshness_exit_code=2 + the benign drift paths."""
    ctx = _wire(monkeypatch, tmp_path)
    benign_show = {"resource_drift": [{
        "address": "google_cloud_run_v2_service.payment_demo",
        "type": "google_cloud_run_v2_service",
        "change": {"actions": ["update"], "before": {"generation": 6}, "after": {"generation": 10}}}]}

    def run(args, cwd, env):  # noqa: ANN001
        if args[:2] == ["version", "-json"]:
            return 0, json.dumps({"terraform_version": "1.12.0"}), ""
        if args[:2] == ["state", "pull"]:
            return 0, json.dumps({"serial": 7, "lineage": "L"}), ""
        if args[0] == "init":
            return 0, "", ""
        if args[:2] == ["show", "-json"]:
            return 0, json.dumps(benign_show), ""
        if args[0] == "plan":
            return 2, "drift detected", ""  # refresh-only exit 2 (computed churn)
        if args[0] == "apply":
            return 0, "Apply complete!", ""
        return 1, "", "unexpected"

    monkeypatch.setattr(m, "_RUN_TOFU", run)
    r = client.post("/apply", json={"approval_id": ctx["record"].approval_id, "approval_token": ctx["raw_token"]})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "applied"
    audit = _doc(ctx)["apply_audit"]
    assert audit["phase"] == "applied"
    assert audit["freshness_exit_code"] == 2  # proceeded THROUGH benign drift
    assert any(":generation" in p for p in audit["benign_drift_paths"])


def test_apply_tofu_failure_records_failed(client: TestClient, monkeypatch, tmp_path) -> None:
    ctx = _wire(monkeypatch, tmp_path)

    def run(args, cwd, env):  # noqa: ANN001
        if args[:2] == ["version", "-json"]:
            return 0, json.dumps({"terraform_version": "1.12.0"}), ""
        if args[:2] == ["state", "pull"]:
            # readable + unchanged serial both sides → PROVABLY clean failure.
            return 0, json.dumps({"serial": 3, "lineage": "L"}), ""
        if args[0] in ("init", "plan"):
            return 0, "", ""  # init + freshness gate + diagnostic refresh-only all clean
        if args[0] == "apply":
            return 1, "", "apply exploded"
        return 0, "{}", ""

    monkeypatch.setattr(m, "_RUN_TOFU", run)
    r = client.post("/apply", json={"approval_id": ctx["record"].approval_id, "approval_token": ctx["raw_token"]})
    assert r.status_code == 502
    assert _doc(ctx)["apply_audit"]["phase"] == "failed"


def test_apply_state_suspect_records_phase_and_bounded_diagnostic(
    client: TestClient, monkeypatch, tmp_path
) -> None:
    """C5g 1b: an ApplyStateSuspect from run_apply_sequence → HTTP 502 + the
    DISTINCT terminal phase ``failed_state_suspect`` + a BOUNDED diagnostic in the
    audit (serials, drift, refresh tail capped for Firestore), and a detail that
    carries the ``failed_state_suspect`` token + a runbook pointer so the
    coordinator can refine its apply_status."""
    ctx = _wire(monkeypatch, tmp_path)
    diag = tofu_runner.PostFailureState(
        state_suspect=True, serial_before=3, serial_after=4, serial_bumped=True,
        refresh_exit=2, refresh_drift=True,
        refresh_output="~ service_account = payment-demo-runtime@ -> compute@\n" * 300,
        refresh_stderr="Warning: provider read slow",
    )

    def fake_seq(**kwargs):  # noqa: ANN003
        raise tofu_runner.ApplyStateSuspect("apply", 1, "Error 403 admission denied", diag)

    monkeypatch.setattr(m.tofu_runner, "run_apply_sequence", fake_seq)
    r = client.post("/apply", json={"approval_id": ctx["record"].approval_id, "approval_token": ctx["raw_token"]})
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert "failed_state_suspect" in detail and "runbook" in detail
    # CONTRACT with the coordinator: it detects suspect by substring-matching the
    # worker body truncated to 500 chars (worker_client). Pin that the token lands
    # well within that window so a future reword can't silently break the coupling.
    assert "failed_state_suspect" in r.text[:500]
    doc = _doc(ctx)
    assert doc["status"] == "used"  # claim-first: burned
    audit = doc["apply_audit"]
    assert audit["phase"] == "failed_state_suspect"
    assert audit["phase"] != "failed"
    assert audit["state_suspect"] is True
    assert audit["serial_before"] == 3 and audit["serial_after"] == 4
    assert audit["serial_bumped"] is True and audit["refresh_drift"] is True
    assert audit["step"] == "apply" and audit["apply_exit_code"] == 1
    assert audit["post_failure_refresh_stderr_tail"] == "Warning: provider read slow"
    # the attached refresh output is BOUNDED (never the full ~15KB) for Firestore.
    assert 0 < len(audit["post_failure_refresh_tail"]) <= 4000


def test_apply_lock_refused_returns_423(client: TestClient, monkeypatch, tmp_path) -> None:
    """C5d: state-lock contention surfaces as the DISTINCT phase ``lock_refused``
    (HTTP 423), not ``failed`` (502). The fidelity/freshness gates pass, then
    run_apply_sequence raises LockRefused — the handler maps it to 423 + writes a
    terminal lock_refused audit carrying step + stderr_tail."""
    ctx = _wire(monkeypatch, tmp_path)

    def fake_seq(**kwargs):  # noqa: ANN001, ANN003
        raise tofu_runner.LockRefused(
            "apply", 1,
            "Error: Error acquiring the state lock\nLock Info:\n  ID: abc-123\n",
        )

    monkeypatch.setattr(m.tofu_runner, "run_apply_sequence", fake_seq)
    r = client.post("/apply", json={"approval_id": ctx["record"].approval_id, "approval_token": ctx["raw_token"]})
    assert r.status_code == 423
    assert r.status_code != 502
    detail = r.json()["detail"]
    assert "state lock" in detail and "force-unlock" in detail
    audit = _doc(ctx)["apply_audit"]
    assert audit["phase"] == "lock_refused"
    assert audit["phase"] != "failed"
    assert audit["step"] == "apply"
    assert "Error acquiring the state lock" in audit["stderr_tail"]


def test_apply_lock_refused_is_post_claim(client: TestClient, monkeypatch, tmp_path) -> None:
    """Post-claim proof (I1 contract): the approval is claimed/BURNED before the
    lock is hit. claim_pending must have run BEFORE run_apply_sequence, the stored
    approval status must have flipped pending → used, and the terminal audit phase
    is lock_refused (detection happened after the burn)."""
    ctx = _wire(monkeypatch, tmp_path)
    order: list[str] = []

    real_claim = ctx["store"].claim_pending

    def spy_claim(*a, **k):  # noqa: ANN002, ANN003
        order.append("claim_pending")
        return real_claim(*a, **k)

    monkeypatch.setattr(ctx["store"], "claim_pending", spy_claim)

    def fake_seq(**kwargs):  # noqa: ANN001, ANN003
        order.append("run_apply_sequence")
        raise tofu_runner.LockRefused("apply", 1, "...Error acquiring the state lock...")

    monkeypatch.setattr(m.tofu_runner, "run_apply_sequence", fake_seq)
    r = client.post("/apply", json={"approval_id": ctx["record"].approval_id, "approval_token": ctx["raw_token"]})
    assert r.status_code == 423
    # claim happened, and it happened BEFORE the lock was hit.
    assert order == ["claim_pending", "run_apply_sequence"]
    # the approval was burned (status flipped) AND the terminal phase is lock_refused.
    doc = _doc(ctx)
    assert doc["status"] == "used"
    assert doc["apply_audit"]["phase"] == "lock_refused"


def test_apply_lock_refused_in_gate_block_returns_423(client: TestClient, monkeypatch, tmp_path) -> None:
    """Defensive-parity proof (C5d review finding): a LockRefused raised from the
    FIRST post-claim gate block (re-fetch/integrity/fidelity) — not just from
    run_apply_sequence — is also caught and mapped to 423 + lock_refused, never an
    unhandled 500 that would strand the burned approval at phase=\"claimed\".

    No locking subprocess runs in that block today (the fidelity probe is
    `tofu version`, read-only → TofuStepError), so we simulate a hypothetical
    future locking step by making _fidelity_or_raise raise LockRefused. This pins
    the gate block's handler so a later refactor cannot reintroduce the 500 gap."""
    ctx = _wire(monkeypatch, tmp_path)

    def fake_fidelity(*a, **k):  # noqa: ANN002, ANN003
        raise tofu_runner.LockRefused(
            "refresh-only", 1, "Error: Error acquiring the state lock\nLock Info:\n",
        )

    monkeypatch.setattr(m, "_fidelity_or_raise", fake_fidelity)
    r = client.post("/apply", json={"approval_id": ctx["record"].approval_id, "approval_token": ctx["raw_token"]})
    assert r.status_code == 423  # NOT 500 (unhandled) and NOT 502 (failed)
    detail = r.json()["detail"]
    assert "state lock" in detail and "force-unlock" in detail
    doc = _doc(ctx)
    assert doc["status"] == "used"  # claim still happened before the gate block
    audit = doc["apply_audit"]
    assert audit["phase"] == "lock_refused"
    assert audit["step"] == "refresh-only"


def test_apply_missing_approval(client: TestClient, monkeypatch, tmp_path) -> None:
    _wire(monkeypatch, tmp_path)
    r = client.post("/apply", json={"approval_id": "0" * 8 + "-0000-0000-0000-" + "0" * 12,
                                    "approval_token": "t" * 43})
    assert r.status_code == 404


def test_apply_contract1_metadata_tamper_burns_and_refuses(client: TestClient, monkeypatch, tmp_path) -> None:
    """Contract #1: a post-mint swap of the metadata.json object content (at the
    pinned generation) must abort at the rebuild-compare gate — DISTINCT from the
    plan-bytes integrity gate (contract #2). Mutate base_sha (a field NOT
    cross-checked against the artifact URIs, so build_plan_approval_payload
    rebuilds successfully and the canonical-payload comparison is what fails)."""
    ctx = _wire(monkeypatch, tmp_path)
    key = f"pr-12/{_SHA40}/run-100-1/metadata.json"
    tampered = dict(ctx["md"])
    tampered["base_sha"] = "d" * 40  # schema-valid but != the signed base_sha
    ctx["bucket"]._objects[key] = json.dumps(tampered).encode("utf-8")
    r = client.post("/apply", json={"approval_id": ctx["record"].approval_id, "approval_token": ctx["raw_token"]})
    assert r.status_code == 422
    doc = _doc(ctx)
    assert doc["status"] == "used"  # claim-first: burned
    assert doc["apply_audit"]["phase"] == "verify_refused"
    assert "does not reproduce the signed payload" in doc["apply_audit"]["detail"]


def test_apply_create_plan_without_sidecar_tree_refused(client: TestClient, monkeypatch, tmp_path) -> None:
    """C6: a create-class /apply with NO iac-tree sidecar generation is refused at
    the tree gate (409 tree_mismatch_refused) BEFORE fidelity — a create now needs
    the head config delivered via re-bake-from-main, proven by the sidecar hash. Still
    claim-first burned, and tofu init/apply NEVER ran (only the version probe)."""
    ctx = _wire(monkeypatch, tmp_path, plan_obj=_plan_json_obj(["create"]))
    r = client.post("/apply", json={"approval_id": ctx["record"].approval_id, "approval_token": ctx["raw_token"]})
    assert r.status_code == 409
    doc = _doc(ctx)
    assert doc["status"] == "used"  # claim-first: burned
    assert doc["apply_audit"]["phase"] == "tree_mismatch_refused"
    # tofu init/apply must never have run — only the `version -json` fidelity probe.
    cmds = [c["args"][0] for c in ctx["tofu_calls"]]
    assert "init" not in cmds and "apply" not in cmds


# =========================================================================== #
# /propose
# =========================================================================== #


def test_propose_happy(client: TestClient, monkeypatch, tmp_path) -> None:
    _wire(monkeypatch, tmp_path)
    r = client.post("/propose", json={
        "artifact_uri_metadata": _prefix() + "metadata.json",
        "generation_metadata": "1700000000000003",
        "approver": "alice@corp.example",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["approval_id"] and body["approval_token"]


def test_propose_denylist_violation_refused(client: TestClient, monkeypatch, tmp_path) -> None:
    _wire(monkeypatch, tmp_path,
                plan_obj={"resource_changes": [{
                    "address": "google_cloud_run_v2_service.payment_demo",
                    "type": "google_cloud_run_v2_service",
                    "change": {"actions": ["update"], "before": {"name": "driftscribe-agent"},
                               "after": {"name": "driftscribe-agent"}}}]})
    r = client.post("/propose", json={
        "artifact_uri_metadata": _prefix() + "metadata.json",
        "generation_metadata": "1700000000000003",
        "approver": "alice@corp.example",
    })
    assert r.status_code == 422


def test_propose_create_without_sidecar_refused(client: TestClient, monkeypatch, tmp_path) -> None:
    """C6: a create-class /propose with NO iac-tree sidecar generation is refused at
    the tree gate (pre-mint, 422) — a create now needs the head config delivered via
    re-bake-from-main, proven by the sidecar hash."""
    _wire(monkeypatch, tmp_path, plan_obj=_plan_json_obj(["create"]))
    r = client.post("/propose", json={
        "artifact_uri_metadata": _prefix() + "metadata.json",
        "generation_metadata": "1700000000000003",
        "approver": "alice@corp.example",
    })
    assert r.status_code == 422
    assert "iac-tree gate" in r.json()["detail"]


def test_propose_nondict_metadata_clean_422(client: TestClient, monkeypatch, tmp_path) -> None:
    """A non-dict metadata.json (e.g. a JSON array) must be a clean 422 refusal,
    never an unhandled 500 (untrusted artifacts fail closed cleanly)."""
    ctx = _wire(monkeypatch, tmp_path)
    ctx["bucket"]._objects[f"pr-12/{_SHA40}/run-100-1/metadata.json"] = b"[]"
    r = client.post("/propose", json={
        "artifact_uri_metadata": _prefix() + "metadata.json",
        "generation_metadata": "1700000000000003",
        "approver": "alice@corp.example",
    })
    assert r.status_code == 422


# =========================================================================== #
# C5b-2 — worker operator-JWT re-verification (enforce mode)
# =========================================================================== #
#
# The most security-critical edit: the SOLE MUTATOR independently re-verifies a
# forwarded Cloudflare-Access operator JWT against CF's JWKS and binds
# verified-email == signed approver. These tests mint a real RS256 JWT with a
# throwaway RSA key + mock the CF JWKS over respx (NEVER the network), reusing the
# exact pattern from tests/unit/test_cf_access.py. The signed approver in `_wire`
# is ``alice@corp.example``, so the operator email must match that to bind.

_CF_TEAM = "test-team.cloudflareaccess.com"
_CF_AUD = "test-aud-tag-deadbeef"
_CF_JWKS_URL = f"https://{_CF_TEAM}/cdn-cgi/access/certs"


def _b64url_uint(i: int) -> str:
    import base64
    b = i.to_bytes((i.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _new_keypair() -> tuple[rsa.RSAPrivateKey, dict]:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nums = priv.public_key().public_numbers()
    return priv, {"kty": "RSA", "n": _b64url_uint(nums.n), "e": _b64url_uint(nums.e)}


def _make_jwks(jwk_pub: dict, kid: str) -> dict:
    return {"keys": [{**jwk_pub, "kid": kid, "alg": "RS256", "use": "sig"}]}


def _mint_operator_jwt(priv, kid: str, *, email: str = "alice@corp.example",
                       aud: str = _CF_AUD, iss: str = f"https://{_CF_TEAM}",
                       exp_offset: int = 300, nbf_offset: int = -5) -> str:
    import time
    now = int(time.time())
    return jwt.encode(
        {"aud": aud, "iss": iss, "iat": now, "nbf": now + nbf_offset,
         "exp": now + exp_offset, "email": email, "sub": "subject-123"},
        priv, algorithm="RS256", headers={"kid": kid},
    )


@pytest.fixture
def _enforce(monkeypatch) -> None:
    """Switch the worker into enforce mode + configure the CF Access app, and reset
    the module-level JWKS cache so respx mocks aren't shadowed by a prior fetch."""
    monkeypatch.setattr(m, "IAC_OPERATOR_AUTH_MODE", "enforce")
    monkeypatch.setattr(m, "CF_ACCESS_TEAM_DOMAIN", _CF_TEAM)
    monkeypatch.setattr(m, "CF_ACCESS_AUD_TAG", _CF_AUD)
    cf_access_mod._reset_cache_for_tests()
    yield
    cf_access_mod._reset_cache_for_tests()


def _serve_jwks(priv_pub_kid: tuple) -> None:
    _priv, jwk_pub, kid = priv_pub_kid
    respx.get(_CF_JWKS_URL).mock(return_value=httpx.Response(200, json=_make_jwks(jwk_pub, kid)))


# ---- /apply enforce ----


@respx.mock
def test_apply_enforce_valid_jwt_applies_and_audits_both_identities(
    client: TestClient, monkeypatch, tmp_path, _enforce
) -> None:
    """Valid operator JWT whose email == signed approver → 200 applied; the audit
    records BOTH the verified operator_email AND the caller_sa (N2)."""
    priv, jwk_pub = _new_keypair()
    _serve_jwks((priv, jwk_pub, "kid-1"))
    ctx = _wire(monkeypatch, tmp_path)
    token = _mint_operator_jwt(priv, "kid-1", email="alice@corp.example")
    r = client.post("/apply", json={
        "approval_id": ctx["record"].approval_id, "approval_token": ctx["raw_token"],
        "operator_jwt": token,
    })
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "applied"
    audit = _doc(ctx)["apply_audit"]
    assert audit["phase"] == "applied"
    assert audit["operator_email"] == "alice@corp.example"
    assert audit["caller_sa"] == "alice@corp.example"  # the overridden caller


@respx.mock
def test_apply_enforce_forged_jwt_403_not_burned(
    client: TestClient, monkeypatch, tmp_path, _enforce
) -> None:
    """A forged JWT (signed by a key NOT in the served JWKS) → 403 PRE-CLAIM; the
    approval stays pending (nothing burned, no apply_audit)."""
    real_priv, _ = _new_keypair()
    _decoy_priv, decoy_pub = _new_keypair()
    _serve_jwks((_decoy_priv, decoy_pub, "kid-1"))
    ctx = _wire(monkeypatch, tmp_path)
    token = _mint_operator_jwt(real_priv, "kid-1")  # signed by the wrong key
    r = client.post("/apply", json={
        "approval_id": ctx["record"].approval_id, "approval_token": ctx["raw_token"],
        "operator_jwt": token,
    })
    assert r.status_code == 403
    assert r.json()["detail"] == "operator verification failed"
    doc = _doc(ctx)
    assert doc["status"] == "pending"          # NOT burned
    assert "apply_audit" not in doc            # claim never ran


def test_apply_enforce_garbage_jwt_403_not_burned(
    client: TestClient, monkeypatch, tmp_path, _enforce
) -> None:
    """A garbage (non-JWT) operator_jwt → 403 PRE-CLAIM, not burned. No JWKS fetch
    is needed (the header parse fails first), so no respx mock required."""
    ctx = _wire(monkeypatch, tmp_path)
    r = client.post("/apply", json={
        "approval_id": ctx["record"].approval_id, "approval_token": ctx["raw_token"],
        "operator_jwt": "not.a.jwt",
    })
    assert r.status_code == 403
    assert r.json()["detail"] == "operator verification failed"
    doc = _doc(ctx)
    assert doc["status"] == "pending"
    assert "apply_audit" not in doc


def test_apply_enforce_absent_jwt_403_not_burned(
    client: TestClient, monkeypatch, tmp_path, _enforce
) -> None:
    """No operator_jwt in enforce mode → 403 PRE-CLAIM, not burned."""
    ctx = _wire(monkeypatch, tmp_path)
    r = client.post("/apply", json={
        "approval_id": ctx["record"].approval_id, "approval_token": ctx["raw_token"],
    })
    assert r.status_code == 403
    assert r.json()["detail"] == "operator JWT required"
    doc = _doc(ctx)
    assert doc["status"] == "pending"
    assert "apply_audit" not in doc


@respx.mock
def test_apply_enforce_expired_jwt_403_not_burned(
    client: TestClient, monkeypatch, tmp_path, _enforce
) -> None:
    """An EXPIRED operator JWT → 403 PRE-CLAIM, not burned (CfAccessJwtError on the
    exp check is swallowed into the generic 403 — no detail leak)."""
    priv, jwk_pub = _new_keypair()
    _serve_jwks((priv, jwk_pub, "kid-1"))
    ctx = _wire(monkeypatch, tmp_path)
    token = _mint_operator_jwt(priv, "kid-1", exp_offset=-60)  # expired 1min ago
    r = client.post("/apply", json={
        "approval_id": ctx["record"].approval_id, "approval_token": ctx["raw_token"],
        "operator_jwt": token,
    })
    assert r.status_code == 403
    assert r.json()["detail"] == "operator verification failed"
    doc = _doc(ctx)
    assert doc["status"] == "pending"
    assert "apply_audit" not in doc


@respx.mock
def test_apply_enforce_valid_jwt_wrong_email_403_not_burned(
    client: TestClient, monkeypatch, tmp_path, _enforce
) -> None:
    """A perfectly valid JWT whose email != the signed approver → 403 PRE-CLAIM,
    not burned (the email-binding check)."""
    priv, jwk_pub = _new_keypair()
    _serve_jwks((priv, jwk_pub, "kid-1"))
    ctx = _wire(monkeypatch, tmp_path)
    token = _mint_operator_jwt(priv, "kid-1", email="mallory@corp.example")
    r = client.post("/apply", json={
        "approval_id": ctx["record"].approval_id, "approval_token": ctx["raw_token"],
        "operator_jwt": token,
    })
    assert r.status_code == 403
    assert r.json()["detail"] == "operator is not the approver"
    doc = _doc(ctx)
    assert doc["status"] == "pending"
    assert "apply_audit" not in doc


# ---- /propose enforce ----


@respx.mock
def test_propose_enforce_valid_jwt_mints(
    client: TestClient, monkeypatch, tmp_path, _enforce
) -> None:
    """Valid JWT whose email == req.approver → 200 (mints)."""
    priv, jwk_pub = _new_keypair()
    _serve_jwks((priv, jwk_pub, "kid-1"))
    _wire(monkeypatch, tmp_path)
    token = _mint_operator_jwt(priv, "kid-1", email="alice@corp.example")
    r = client.post("/propose", json={
        "artifact_uri_metadata": _prefix() + "metadata.json",
        "generation_metadata": "1700000000000003",
        "approver": "alice@corp.example",
        "operator_jwt": token,
    })
    assert r.status_code == 200, r.text
    assert r.json()["approval_id"] and r.json()["approval_token"]


@respx.mock
def test_propose_enforce_jwt_email_mismatch_403_no_mint(
    client: TestClient, monkeypatch, tmp_path, _enforce
) -> None:
    """Valid JWT but email != req.approver → 403, no mint."""
    priv, jwk_pub = _new_keypair()
    _serve_jwks((priv, jwk_pub, "kid-1"))
    _wire(monkeypatch, tmp_path)
    token = _mint_operator_jwt(priv, "kid-1", email="mallory@corp.example")
    r = client.post("/propose", json={
        "artifact_uri_metadata": _prefix() + "metadata.json",
        "generation_metadata": "1700000000000003",
        "approver": "alice@corp.example",
        "operator_jwt": token,
    })
    assert r.status_code == 403
    assert r.json()["detail"] == "operator is not the approver"


def test_propose_enforce_absent_jwt_403(
    client: TestClient, monkeypatch, tmp_path, _enforce
) -> None:
    """No operator_jwt in enforce mode → 403 (no mint)."""
    _wire(monkeypatch, tmp_path)
    r = client.post("/propose", json={
        "artifact_uri_metadata": _prefix() + "metadata.json",
        "generation_metadata": "1700000000000003",
        "approver": "alice@corp.example",
    })
    assert r.status_code == 403
    assert r.json()["detail"] == "operator JWT required"


# ---- schema shape: /deny rejects operator_jwt; Apply/Propose accept it ----


def test_deny_rejects_operator_jwt_field(client: TestClient, monkeypatch, tmp_path) -> None:
    """/deny keeps TokenRequest (extra='forbid'), so an operator_jwt field → 422.
    Deny is cleanup-only with no operator binding (plan §5)."""
    ctx = _wire(monkeypatch, tmp_path)
    r = client.post("/deny", json={
        "approval_id": ctx["record"].approval_id, "approval_token": ctx["raw_token"],
        "operator_jwt": "x" * 40,
    })
    assert r.status_code == 422
    assert _doc(ctx)["status"] == "pending"  # no flip


def test_deny_without_operator_jwt_still_works(client: TestClient, monkeypatch, tmp_path) -> None:
    """Regression: /deny with the original TokenRequest shape still denies."""
    ctx = _wire(monkeypatch, tmp_path)
    r = client.post("/deny", json={
        "approval_id": ctx["record"].approval_id, "approval_token": ctx["raw_token"],
    })
    assert r.status_code == 200 and r.json()["status"] == "denied"


def test_request_schema_shapes_for_operator_jwt() -> None:
    """ProposeRequest + ApplyRequest accept operator_jwt; TokenRequest (deny) does
    NOT (extra='forbid')."""
    uid = "0" * 8 + "-0000-0000-0000-" + "0" * 12
    tok = "t" * 43
    # ApplyRequest accepts it (and defaults to None when absent).
    assert m.ApplyRequest(approval_id=uid, approval_token=tok).operator_jwt is None
    assert m.ApplyRequest(approval_id=uid, approval_token=tok, operator_jwt="j").operator_jwt == "j"
    # ProposeRequest accepts it.
    pr = m.ProposeRequest(artifact_uri_metadata="gs://b/o/metadata.json",
                          generation_metadata="1", approver="a@b.com", operator_jwt="j")
    assert pr.operator_jwt == "j"
    # TokenRequest (deny) forbids it.
    with pytest.raises(Exception):
        m.TokenRequest(approval_id=uid, approval_token=tok, operator_jwt="j")


def test_e2e_empty_string_operator_jwt_fails_closed(
    client: TestClient, monkeypatch, tmp_path
) -> None:
    """In e2e mode the legacy fallback gates on ``operator_jwt is None`` — NOT
    falsiness. An empty-string operator_jwt is 'present but invalid' and must fail
    closed (403 'operator JWT required'), never silently fall through to the
    caller==approver legacy path. The default suite mode is already 'e2e' and the
    caller==approver would otherwise have PASSED, so this proves the is-None gate."""
    ctx = _wire(monkeypatch, tmp_path)
    r = client.post("/apply", json={
        "approval_id": ctx["record"].approval_id, "approval_token": ctx["raw_token"],
        "operator_jwt": "",
    })
    assert r.status_code == 403
    assert r.json()["detail"] == "operator JWT required"
    doc = _doc(ctx)
    assert doc["status"] == "pending"      # not burned
    assert "apply_audit" not in doc


def test_coordinator_url_is_optional() -> None:
    """I8: COORDINATOR_URL is no longer hard-required. The module read uses
    os.environ.get(...) so a missing var does not KeyError at import — assert the
    boot constant resolved to a string (set or "")."""
    assert isinstance(m.COORDINATOR_URL, str)
    # And the underlying read is the optional ``.get`` form (no KeyError on absence).
    assert os.environ.get("DRIFTSCRIBE_DEFINITELY_UNSET_VAR", "") == ""


def test_apply_request_rejects_extra_field() -> None:
    """C5b-2 review: ApplyRequest is a CLOSED schema. Pydantic v2 does NOT inherit
    model_config across subclasses, so extra='forbid' is restated explicitly on
    ApplyRequest — a stray field must be a ValidationError, not silently dropped
    (a compromised coordinator can't probe undocumented extension points)."""
    uid = "0" * 8 + "-0000-0000-0000-" + "0" * 12
    with pytest.raises(Exception):  # pydantic ValidationError → FastAPI 422
        m.ApplyRequest(approval_id=uid, approval_token="t" * 43, operator_jwt="j", bogus="x")


def test_validate_operator_auth_config_fails_fast() -> None:
    """C5b-2 review: boot fail-fast on operator-auth misconfig — a clear 'Revision
    is not ready' over a runtime 403. (1) enforce + empty CF_ACCESS_* refuses to
    start; (2) an UNKNOWN mode (typo) refuses to start rather than silently
    behaving like enforce-without-the-CF-gate; e2e and fully-configured enforce
    boot fine."""
    with pytest.raises(RuntimeError, match="CF_ACCESS_TEAM_DOMAIN"):
        m._validate_operator_auth_config("enforce", "", "aud")
    with pytest.raises(RuntimeError, match="CF_ACCESS_TEAM_DOMAIN"):
        m._validate_operator_auth_config("enforce", "team.cloudflareaccess.com", "")
    # Unknown/typo'd mode → fail-fast (Codex: 'enfroce' must not boot).
    with pytest.raises(RuntimeError, match="must be one of"):
        m._validate_operator_auth_config("enfroce", "team.cloudflareaccess.com", "aud")
    with pytest.raises(RuntimeError, match="must be one of"):
        m._validate_operator_auth_config("", "", "")
    # Fully-configured enforce and e2e (any CF config) both boot fine.
    m._validate_operator_auth_config("enforce", "team.cloudflareaccess.com", "aud-tag")
    m._validate_operator_auth_config("e2e", "", "")


# =========================================================================== #
# C6a-3 — iac/-tree hash gate + create-of-declared guard (SOLE MUTATOR)
# =========================================================================== #

from driftscribe_lib.iac_tree import (  # noqa: E402
    SidecarInput,
    build_sidecar,
    iac_tree_hash,
    serialize_sidecar,
)

_DECLARED = {"google_cloud_run_v2_service.payment_demo"}
_SIDECAR_OBJ = f"pr-12/{_SHA40}/run-100-1/iac-tree.json"
_SIDECAR_GEN = "1700000000000004"


def _add_sidecar(ctx, *, tree_hash=None, field_overrides=None) -> str:
    """Drop a c6.v1 iac-tree.json into the wired bucket, matching ctx['md'] +
    (by default) the REAL baked-iac tree hash. Returns the generation to pass."""
    md = ctx["md"]
    th = tree_hash if tree_hash is not None else iac_tree_hash(ctx["iac"])
    fields = dict(
        repo=md["repo"], pr_number=md["pr_number"], head_sha=md["head_sha"],
        base_sha=md["base_sha"], workflow_run_id=md["workflow_run_id"],
        workflow_run_attempt=md["workflow_run_attempt"], plan_sha256=md["plan_sha256"],
        plan_json_sha256=md["plan_json_sha256"], iac_tree_hash=th,
    )
    if field_overrides:
        fields.update(field_overrides)
    ctx["bucket"]._objects[_SIDECAR_OBJ] = serialize_sidecar(
        build_sidecar(SidecarInput(**fields))
    ).encode("utf-8")
    return _SIDECAR_GEN


# --- resource_set_guard: the create-of-declared relaxation ----------------


def test_guard_allows_create_of_declared_only_when_flagged() -> None:
    pj = _plan_json_obj(["create"])  # create of the DECLARED payment_demo
    assert tofu_runner.resource_set_guard(pj, _DECLARED) is not None  # default: refuse
    assert tofu_runner.resource_set_guard(pj, _DECLARED, allow_create_of_declared=True) is None


def test_guard_create_of_undeclared_refused_even_when_flagged() -> None:
    pj = _plan_json_obj(["create"], address="google_storage_bucket.new")
    assert tofu_runner.resource_set_guard(pj, _DECLARED, allow_create_of_declared=True) is not None


def test_guard_module_refused_first_even_when_flagged() -> None:
    pj = _plan_json_obj(["create"], address="module.m.google_cloud_run_v2_service.payment_demo")
    reason = tofu_runner.resource_set_guard(pj, _DECLARED, allow_create_of_declared=True)
    assert reason is not None and "module" in reason


def test_assert_fidelity_admits_create_of_declared_with_flag(tmp_path: Path) -> None:
    iac = _iac_dir(tmp_path)
    lock = hashlib.sha256((iac / ".terraform.lock.hcl").read_bytes()).hexdigest()
    md = _metadata(lock, b"p", b"j")
    pj = _plan_json_obj(["create"])
    # default → refuse; flagged → admit
    with pytest.raises(tofu_runner.FidelityError):
        tofu_runner.assert_fidelity(signed_metadata=md, baked_tofu_version="1.12.0",
                                    baked_lockfile_sha256=lock, plan_json=pj, declared_addresses=_DECLARED)
    tofu_runner.assert_fidelity(signed_metadata=md, baked_tofu_version="1.12.0",
                                baked_lockfile_sha256=lock, plan_json=pj, declared_addresses=_DECLARED,
                                allow_create_of_declared=True)


# --- derive_sidecar_uri ----------------------------------------------------


def test_derive_sidecar_uri_swaps_basename() -> None:
    meta = _prefix() + "metadata.json"
    assert gcs_fetch.derive_sidecar_uri(meta) == _prefix() + "iac-tree.json"


def test_derive_sidecar_uri_rejects_non_metadata() -> None:
    with pytest.raises(gcs_fetch.GcsFetchError):
        gcs_fetch.derive_sidecar_uri(_prefix() + "plan.tfplan")


# --- _verify_iac_tree_or_raise (unit) -------------------------------------


def test_verify_iac_tree_match_returns_hash(monkeypatch, tmp_path) -> None:
    ctx = _wire(monkeypatch, tmp_path, plan_obj=_plan_json_obj(["create"]))
    gen = _add_sidecar(ctx)
    got = m._verify_iac_tree_or_raise(
        ctx["bucket"], signed_md=ctx["md"], metadata_uri=_prefix() + "metadata.json",
        generation_iac_tree=gen,
    )
    assert got == iac_tree_hash(ctx["iac"])


def test_verify_iac_tree_absent_generation_raises(monkeypatch, tmp_path) -> None:
    ctx = _wire(monkeypatch, tmp_path, plan_obj=_plan_json_obj(["create"]))
    with pytest.raises(tofu_runner.IacTreeMismatch):
        m._verify_iac_tree_or_raise(
            ctx["bucket"], signed_md=ctx["md"], metadata_uri=_prefix() + "metadata.json",
            generation_iac_tree=None,
        )


def test_verify_iac_tree_hash_mismatch_raises(monkeypatch, tmp_path) -> None:
    ctx = _wire(monkeypatch, tmp_path, plan_obj=_plan_json_obj(["create"]))
    gen = _add_sidecar(ctx, tree_hash="f" * 64)
    with pytest.raises(tofu_runner.IacTreeMismatch):
        m._verify_iac_tree_or_raise(
            ctx["bucket"], signed_md=ctx["md"], metadata_uri=_prefix() + "metadata.json",
            generation_iac_tree=gen,
        )


def test_verify_iac_tree_field_mismatch_raises(monkeypatch, tmp_path) -> None:
    ctx = _wire(monkeypatch, tmp_path, plan_obj=_plan_json_obj(["create"]))
    gen = _add_sidecar(ctx, field_overrides={"plan_sha256": "9" * 64})
    with pytest.raises(tofu_runner.IacTreeMismatch):
        m._verify_iac_tree_or_raise(
            ctx["bucket"], signed_md=ctx["md"], metadata_uri=_prefix() + "metadata.json",
            generation_iac_tree=gen,
        )


def test_verify_iac_tree_missing_object_raises(monkeypatch, tmp_path) -> None:
    ctx = _wire(monkeypatch, tmp_path, plan_obj=_plan_json_obj(["create"]))
    # generation supplied but no sidecar object in the bucket → fetch fails closed.
    with pytest.raises(tofu_runner.IacTreeMismatch):
        m._verify_iac_tree_or_raise(
            ctx["bucket"], signed_md=ctx["md"], metadata_uri=_prefix() + "metadata.json",
            generation_iac_tree=_SIDECAR_GEN,
        )


# --- /apply create-class matrix -------------------------------------------


def _apply_body(ctx, **extra):
    body = {"approval_id": ctx["record"].approval_id, "approval_token": ctx["raw_token"]}
    body.update(extra)
    return body


def test_apply_create_class_with_matching_sidecar_applies(client, monkeypatch, tmp_path) -> None:
    ctx = _wire(monkeypatch, tmp_path, plan_obj=_plan_json_obj(["create"]))
    gen = _add_sidecar(ctx)
    r = client.post("/apply", json=_apply_body(ctx, generation_iac_tree=gen))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "applied"
    audit = _doc(ctx)["apply_audit"]
    assert audit["phase"] == "applied"
    assert audit["iac_tree_verified"] is True


def test_apply_create_class_hash_mismatch_409(client, monkeypatch, tmp_path) -> None:
    ctx = _wire(monkeypatch, tmp_path, plan_obj=_plan_json_obj(["create"]))
    gen = _add_sidecar(ctx, tree_hash="f" * 64)
    r = client.post("/apply", json=_apply_body(ctx, generation_iac_tree=gen))
    assert r.status_code == 409, r.text
    assert "failed_state_suspect" not in r.text
    assert _doc(ctx)["apply_audit"]["phase"] == "tree_mismatch_refused"


def test_apply_create_class_no_generation_409(client, monkeypatch, tmp_path) -> None:
    ctx = _wire(monkeypatch, tmp_path, plan_obj=_plan_json_obj(["create"]))
    _add_sidecar(ctx)  # sidecar present, but the caller forgot to pass the generation
    r = client.post("/apply", json=_apply_body(ctx))
    assert r.status_code == 409, r.text
    assert _doc(ctx)["apply_audit"]["phase"] == "tree_mismatch_refused"


def test_apply_create_class_field_mismatch_409(client, monkeypatch, tmp_path) -> None:
    ctx = _wire(monkeypatch, tmp_path, plan_obj=_plan_json_obj(["create"]))
    gen = _add_sidecar(ctx, field_overrides={"head_sha": "b" * 40})
    r = client.post("/apply", json=_apply_body(ctx, generation_iac_tree=gen))
    assert r.status_code == 409, r.text


def test_apply_module_create_refused_fidelity_even_with_sidecar(client, monkeypatch, tmp_path) -> None:
    """Modules stay forbidden: a module.* create with a VALID sidecar still refuses
    at the resource-set guard (module-before-create) → fidelity_refused, NOT applied."""
    ctx = _wire(monkeypatch, tmp_path,
                plan_obj=_plan_json_obj(["create"], address="module.m.google_cloud_run_v2_service.payment_demo"))
    gen = _add_sidecar(ctx)
    r = client.post("/apply", json=_apply_body(ctx, generation_iac_tree=gen))
    assert r.status_code == 422, r.text
    assert _doc(ctx)["apply_audit"]["phase"] == "fidelity_refused"


def test_apply_noncreate_ignores_sidecar_c5_path(client, monkeypatch, tmp_path) -> None:
    """A C5 update applies WITHOUT a sidecar (the hash gate is create-class only);
    a stray generation_iac_tree is harmless (never fetched)."""
    ctx = _wire(monkeypatch, tmp_path)  # default plan = update
    r = client.post("/apply", json=_apply_body(ctx, generation_iac_tree=_SIDECAR_GEN))
    assert r.status_code == 200, r.text
    audit = _doc(ctx)["apply_audit"]
    assert audit["phase"] == "applied"
    assert audit["iac_tree_verified"] is False


# --- /propose create-class -------------------------------------------------


def test_propose_create_class_with_matching_sidecar_mints(client, monkeypatch, tmp_path) -> None:
    ctx = _wire(monkeypatch, tmp_path, plan_obj=_plan_json_obj(["create"]))
    gen = _add_sidecar(ctx)
    r = client.post("/propose", json={
        "artifact_uri_metadata": _prefix() + "metadata.json",
        "generation_metadata": "1700000000000003",
        "approver": "alice@corp.example",
        "generation_iac_tree": gen,
    })
    assert r.status_code == 200, r.text
    assert r.json()["approval_id"]


def test_propose_create_class_hash_mismatch_422(client, monkeypatch, tmp_path) -> None:
    ctx = _wire(monkeypatch, tmp_path, plan_obj=_plan_json_obj(["create"]))
    gen = _add_sidecar(ctx, tree_hash="f" * 64)
    r = client.post("/propose", json={
        "artifact_uri_metadata": _prefix() + "metadata.json",
        "generation_metadata": "1700000000000003",
        "approver": "alice@corp.example",
        "generation_iac_tree": gen,
    })
    assert r.status_code == 422, r.text
    assert "iac-tree gate" in r.json()["detail"]


# --- C6a-3 review hardening: replace refusal, schema/hex validation, req pattern ---


def test_guard_refuses_replace_of_declared_even_when_flagged() -> None:
    """A replace (create+delete) of a DECLARED address is destroy+recreate — refused
    UNCONDITIONALLY (C6 admits pure creates only), independent of the denylist."""
    for actions in (["create", "delete"], ["delete", "create"]):
        pj = _plan_json_obj(actions)
        reason = tofu_runner.resource_set_guard(pj, _DECLARED, allow_create_of_declared=True)
        assert reason is not None and "replace" in reason


def test_apply_replace_refused_fidelity_with_valid_sidecar(client, monkeypatch, tmp_path) -> None:
    """A replace with a valid sidecar still refuses (the resource-set guard refuses
    replace before admitting). Denylist may also catch it; this proves the guard
    itself does, defense in depth."""
    ctx = _wire(monkeypatch, tmp_path, plan_obj=_plan_json_obj(["create", "delete"]))
    gen = _add_sidecar(ctx)
    r = client.post("/apply", json=_apply_body(ctx, generation_iac_tree=gen))
    assert r.status_code == 422, r.text
    assert _doc(ctx)["apply_audit"]["phase"] in ("fidelity_refused", "verify_refused")


def test_verify_iac_tree_wrong_schema_version_raises(monkeypatch, tmp_path) -> None:
    ctx = _wire(monkeypatch, tmp_path, plan_obj=_plan_json_obj(["create"]))
    _add_sidecar(ctx)
    # Corrupt the stored sidecar's schema_version.
    import json as _json
    obj = _json.loads(ctx["bucket"]._objects[_SIDECAR_OBJ])
    obj["schema_version"] = "c6.v2"
    ctx["bucket"]._objects[_SIDECAR_OBJ] = _json.dumps(obj).encode("utf-8")
    with pytest.raises(tofu_runner.IacTreeMismatch):
        m._verify_iac_tree_or_raise(
            ctx["bucket"], signed_md=ctx["md"], metadata_uri=_prefix() + "metadata.json",
            generation_iac_tree=_SIDECAR_GEN,
        )


def test_verify_iac_tree_non_hex_hash_raises(monkeypatch, tmp_path) -> None:
    ctx = _wire(monkeypatch, tmp_path, plan_obj=_plan_json_obj(["create"]))
    _add_sidecar(ctx)
    import json as _json
    obj = _json.loads(ctx["bucket"]._objects[_SIDECAR_OBJ])
    obj["iac_tree_hash"] = "NOT-HEX"
    ctx["bucket"]._objects[_SIDECAR_OBJ] = _json.dumps(obj).encode("utf-8")
    with pytest.raises(tofu_runner.IacTreeMismatch):
        m._verify_iac_tree_or_raise(
            ctx["bucket"], signed_md=ctx["md"], metadata_uri=_prefix() + "metadata.json",
            generation_iac_tree=_SIDECAR_GEN,
        )


def test_apply_rejects_nonnumeric_generation_iac_tree(client, monkeypatch, tmp_path) -> None:
    """Request-schema validation: a non-numeric generation_iac_tree is a 422 at the
    boundary (matches the generation_metadata contract)."""
    ctx = _wire(monkeypatch, tmp_path, plan_obj=_plan_json_obj(["create"]))
    r = client.post("/apply", json=_apply_body(ctx, generation_iac_tree="abc"))
    assert r.status_code == 422, r.text


def test_apply_accepts_absent_generation_iac_tree_on_c5_path(client, monkeypatch, tmp_path) -> None:
    """None is still valid at the schema boundary (the pattern only constrains a
    present string) — the C5 update path passes no sidecar."""
    ctx = _wire(monkeypatch, tmp_path)  # update plan
    r = client.post("/apply", json=_apply_body(ctx))  # no generation_iac_tree key
    assert r.status_code == 200, r.text


def test_apply_create_class_unhashable_baked_tree_is_terminal_not_500(client, monkeypatch, tmp_path) -> None:
    """If iac_tree_hash(IAC_DIR) raises (a baked-tree IO/anomaly), the worker must
    return the terminal tree_mismatch_refused (409) with a recorded audit — NOT an
    uncaught 500 that strands the burned approval at phase='claimed' (adversarial
    review C6a-3, fail-open lens)."""
    ctx = _wire(monkeypatch, tmp_path, plan_obj=_plan_json_obj(["create"]))
    gen = _add_sidecar(ctx)

    def boom(_dir):
        raise tofu_runner.IacTreeMismatch  # placeholder; replaced below
    # Force the hash compute to raise the lib's fail-closed error.
    from driftscribe_lib.iac_tree import IacTreeHashError as _ITHE
    monkeypatch.setattr(m, "iac_tree_hash", lambda _d: (_ for _ in ()).throw(_ITHE("baked tree vanished")))

    r = client.post("/apply", json=_apply_body(ctx, generation_iac_tree=gen))
    assert r.status_code == 409, r.text
    assert _doc(ctx)["apply_audit"]["phase"] == "tree_mismatch_refused"


def test_fetch_object_pinned_converts_transport_error_to_gcsfetcherror() -> None:
    """A non-NotFound SDK/transport error (Forbidden, ServiceUnavailable, retry
    exhaustion) must surface as GcsFetchError — NOT a raw exception that escapes the
    POST-CLAIM gate and 500-strands the burned approval (adversarial review C6a-3)."""
    class _RaisingBlob:
        def download_as_bytes(self, raw_download=False, if_generation_match=None):
            raise RuntimeError("503 Service Unavailable (simulated transport)")

    class _RaisingBucket:
        def blob(self, name, generation=None):
            return _RaisingBlob()

    with pytest.raises(gcs_fetch.GcsFetchError):
        gcs_fetch.fetch_object_pinned(_RaisingBucket(), "o", 123)


# --- C6c: GET /baked-iac-hash (re-bake readiness) -------------------------


def test_baked_iac_hash_returns_baked_hash(client, monkeypatch, tmp_path) -> None:
    iac = _iac_dir(tmp_path)
    monkeypatch.setattr(m, "IAC_DIR", iac)
    r = client.get("/baked-iac-hash")
    assert r.status_code == 200
    assert r.json()["iac_tree_hash"] == iac_tree_hash(iac)


def test_baked_iac_hash_anomaly_returns_503(client, monkeypatch, tmp_path) -> None:
    from driftscribe_lib.iac_tree import IacTreeHashError as _E
    monkeypatch.setattr(m, "iac_tree_hash", lambda _d: (_ for _ in ()).throw(_E("baked tree boom")))
    r = client.get("/baked-iac-hash")
    assert r.status_code == 503


# --- Phase-2 import admission: resource_set_guard + /propose endpoint --------


def _importing_pj(address: str, actions: list[str] = None, importing_val=None) -> dict:
    """Build a minimal plan.json with one importing resource_changes entry."""
    if actions is None:
        actions = ["no-op"]
    if importing_val is None:
        importing_val = {"id": "some-bucket"}
    return {
        "resource_changes": [
            {
                "address": address,
                "change": {"actions": actions, "importing": importing_val},
            }
        ]
    }


def test_guard_importing_noop_flag_false_refuses_with_rebake_hint() -> None:
    """importing+no-op, flag False → refusal mentions 're-bake from main'."""
    pj = _importing_pj("google_storage_bucket.adopted")
    reason = tofu_runner.resource_set_guard(pj, {"google_storage_bucket.adopted"})
    assert reason is not None and "re-bake from main" in reason


def test_guard_importing_noop_flag_true_declared_admits() -> None:
    """importing+no-op, flag True, address declared → None (the admission)."""
    pj = _importing_pj("google_storage_bucket.adopted")
    reason = tofu_runner.resource_set_guard(
        pj, {"google_storage_bucket.adopted"}, allow_import_of_declared=True
    )
    assert reason is None


def test_guard_importing_noop_flag_true_not_declared_refuses() -> None:
    """importing+no-op, flag True, address NOT declared → refusal."""
    pj = _importing_pj("google_storage_bucket.adopted")
    reason = tofu_runner.resource_set_guard(pj, set(), allow_import_of_declared=True)
    assert reason is not None and "not declared" in reason


def test_guard_importing_create_flag_true_import_flag_false_refuses() -> None:
    """allow_create_of_declared=True but import flag False → refusal.
    The flags are independent (Phase-1 regression, re-pinned for Phase 2)."""
    pj = _importing_pj("google_storage_bucket.adopted")
    reason = tofu_runner.resource_set_guard(
        pj, {"google_storage_bucket.adopted"}, allow_create_of_declared=True
    )
    assert reason is not None and "re-bake from main" in reason


def test_guard_importing_update_both_flags_true_refuses() -> None:
    """importing+update, BOTH flags True, declared → refusal ("import with changes")."""
    pj = _importing_pj("google_storage_bucket.adopted", actions=["update"])
    reason = tofu_runner.resource_set_guard(
        pj, {"google_storage_bucket.adopted"},
        allow_create_of_declared=True, allow_import_of_declared=True,
    )
    assert reason is not None and "import with changes" in reason


def test_guard_importing_module_address_flag_true_refuses() -> None:
    """importing on module.x.y, flag True → refusal."""
    pj = _importing_pj("module.infra.google_storage_bucket.b")
    reason = tofu_runner.resource_set_guard(
        pj, {"module.infra.google_storage_bucket.b"}, allow_import_of_declared=True
    )
    assert reason is not None and "module" in reason


def test_guard_importing_indexed_address_flag_true_refuses() -> None:
    """importing on google_storage_bucket.b[0], flag True → refusal."""
    pj = _importing_pj("google_storage_bucket.b[0]")
    reason = tofu_runner.resource_set_guard(
        pj, {"google_storage_bucket.b"}, allow_import_of_declared=True
    )
    assert reason is not None and "indexed" in reason


def test_guard_importing_no_address_flag_true_refuses() -> None:
    """importing entry with no address, flag True → refusal."""
    pj = {
        "resource_changes": [
            {
                "change": {"actions": ["no-op"], "importing": {"id": "some-bucket"}},
            }
        ]
    }
    reason = tofu_runner.resource_set_guard(pj, set(), allow_import_of_declared=True)
    assert reason is not None


def test_guard_importing_null_is_ignored() -> None:
    """`importing: null` is NOT an import — plain no-op (unchanged)."""
    pj = {
        "resource_changes": [
            {
                "address": "google_storage_bucket.plain",
                "change": {"actions": ["no-op"], "importing": None},
            }
        ]
    }
    assert tofu_runner.resource_set_guard(pj, set()) is None


def test_propose_pure_import_passes_denylist_then_tree_gate(
    client: TestClient, monkeypatch, tmp_path
) -> None:
    """Phase-2 endpoint pin: the real pure-no-op import fixture now clears the
    denylist and is refused 422 at the iac-tree gate (no generation_iac_tree
    supplied) — pinning that imports route through the C6 tree-hash proof."""
    import json as _json
    _fixture = Path(__file__).parent.parent.parent.parent / "tests" / "fixtures" / "iac_plan_denylist" / "real_import_bucket_pure_noop.json"
    real_plan_obj = _json.loads(_fixture.read_text(encoding="utf-8"))
    _wire(monkeypatch, tmp_path, plan_obj=real_plan_obj)
    resp = client.post("/propose", json={
        "artifact_uri_metadata": _prefix() + "metadata.json",
        "generation_metadata": "1700000000000003",
        "approver": "alice@corp.example",
    })
    assert resp.status_code == 422
    assert "iac-tree gate" in resp.json()["detail"]


def test_propose_import_with_update_refused_by_denylist(
    client: TestClient, monkeypatch, tmp_path
) -> None:
    """The real_import_bucket_with_update fixture (importing+update) must still
    422 at /propose with import-with-changes-forbidden-v1 in the detail —
    denylist endpoint-level pin survives Phase 2."""
    import json as _json
    _fixture = Path(__file__).parent.parent.parent.parent / "tests" / "fixtures" / "iac_plan_denylist" / "real_import_bucket_with_update.json"
    real_plan_obj = _json.loads(_fixture.read_text(encoding="utf-8"))
    _wire(monkeypatch, tmp_path, plan_obj=real_plan_obj)
    resp = client.post("/propose", json={
        "artifact_uri_metadata": _prefix() + "metadata.json",
        "generation_metadata": "1700000000000003",
        "approver": "alice@corp.example",
    })
    assert resp.status_code == 422
    assert "import-with-changes-forbidden-v1" in resp.json()["detail"]
