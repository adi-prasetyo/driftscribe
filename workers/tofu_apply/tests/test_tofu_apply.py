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
    run, _ = _runner({"init": (0, "", ""), "plan": (0, "", ""), "apply": (1, "", "apply boom")})
    with pytest.raises(tofu_runner.TofuStepError) as ei:
        tofu_runner.run_apply_sequence(workdir="/x", kms_key="K", base_env={}, run_tofu=run)
    assert ei.value.step == "apply"


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
    run, _ = _runner({"init": (0, "", ""), "plan": (0, "", ""), "apply": (1, "", "Error: some provider error")})
    with pytest.raises(tofu_runner.TofuStepError) as ei:
        tofu_runner.run_apply_sequence(workdir="/x", kms_key="K", base_env={}, run_tofu=run)
    assert ei.value.step == "apply"
    assert not isinstance(ei.value, tofu_runner.LockRefused)


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
    ctx = _wire(monkeypatch, tmp_path)

    def run(args, cwd, env):  # noqa: ANN001
        if args[:2] == ["version", "-json"]:
            return 0, json.dumps({"terraform_version": "1.12.0"}), ""
        if args[0] == "init":
            return 0, "", ""
        if args[0] == "plan":
            return 2, "drift detected", ""  # refresh-only exit 2
        return 1, "", "should not apply"

    monkeypatch.setattr(m, "_RUN_TOFU", run)
    r = client.post("/apply", json={"approval_id": ctx["record"].approval_id, "approval_token": ctx["raw_token"]})
    assert r.status_code == 409
    assert _doc(ctx)["apply_audit"]["phase"] == "drift_refused"


def test_apply_tofu_failure_records_failed(client: TestClient, monkeypatch, tmp_path) -> None:
    ctx = _wire(monkeypatch, tmp_path)

    def run(args, cwd, env):  # noqa: ANN001
        if args[:2] == ["version", "-json"]:
            return 0, json.dumps({"terraform_version": "1.12.0"}), ""
        if args[0] in ("init", "plan"):
            return 0, "", ""
        if args[0] == "apply":
            return 1, "", "apply exploded"
        return 0, "{}", ""

    monkeypatch.setattr(m, "_RUN_TOFU", run)
    r = client.post("/apply", json={"approval_id": ctx["record"].approval_id, "approval_token": ctx["raw_token"]})
    assert r.status_code == 502
    assert _doc(ctx)["apply_audit"]["phase"] == "failed"


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


def test_apply_fidelity_refused_on_create_plan(client: TestClient, monkeypatch, tmp_path) -> None:
    """The fidelity/resource-set guard is the SOLE /apply gate for a plan that
    creates a resource (the denylist does NOT catch a plain create of a
    non-control-plane resource). Drive /apply with a create plan → 422,
    fidelity_refused, burned, and tofu init/apply NEVER ran (only the version probe)."""
    ctx = _wire(monkeypatch, tmp_path, plan_obj=_plan_json_obj(["create"]))
    r = client.post("/apply", json={"approval_id": ctx["record"].approval_id, "approval_token": ctx["raw_token"]})
    assert r.status_code == 422
    doc = _doc(ctx)
    assert doc["status"] == "used"  # claim-first: burned
    assert doc["apply_audit"]["phase"] == "fidelity_refused"
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


def test_propose_create_fidelity_refused(client: TestClient, monkeypatch, tmp_path) -> None:
    _wire(monkeypatch, tmp_path, plan_obj=_plan_json_obj(["create"]))
    r = client.post("/propose", json={
        "artifact_uri_metadata": _prefix() + "metadata.json",
        "generation_metadata": "1700000000000003",
        "approver": "alice@corp.example",
    })
    assert r.status_code == 422


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


def test_require_cf_config_if_enforce_fails_fast() -> None:
    """C5b-2 review: enforce mode with empty CF_ACCESS_* must fail-fast at boot
    (a clear 'Revision is not ready') rather than 403 every apply at runtime. e2e
    mode and a fully-configured enforce boot are fine."""
    with pytest.raises(RuntimeError, match="CF_ACCESS_TEAM_DOMAIN"):
        m._require_cf_config_if_enforce("enforce", "", "aud")
    with pytest.raises(RuntimeError, match="CF_ACCESS_TEAM_DOMAIN"):
        m._require_cf_config_if_enforce("enforce", "team.cloudflareaccess.com", "")
    # Fully-configured enforce and e2e (any CF config) both boot fine.
    m._require_cf_config_if_enforce("enforce", "team.cloudflareaccess.com", "aud-tag")
    m._require_cf_config_if_enforce("e2e", "", "")
