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

from driftscribe_lib import approvals as approvals_mod  # noqa: E402
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
