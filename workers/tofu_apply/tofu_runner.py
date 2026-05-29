"""OpenTofu subprocess orchestration for the C4 apply worker.

The ONLY place the worker shells out to ``tofu``. Three responsibilities, each
fail-closed and unit-testable via the injectable ``run_tofu`` seam (so the test
suite exercises the full decision matrix with NO live ``tofu`` / GCP — design
§10 "no live apply in automated tests"):

1. **Fidelity guard** (``assert_fidelity``) — refuse before any apply unless the
   worker can faithfully reproduce the planned environment: the signed
   ``opentofu_version`` matches the baked binary, the signed
   ``provider_lockfile_sha256`` matches the baked ``.terraform.lock.hcl``, AND
   the fetched ``plan.json`` only touches resources the baked ``iac/`` declares
   (no creates, no module-nested or unknown addresses). The baked ``iac/`` is
   built from ``main``; plans are built from PR heads, so this guard is what
   keeps a baked-config apply correct rather than scoped-by-convention (the
   general resource-set-changing case is a C5 capability that delivers the head
   config). See docs/plans/2026-05-29-infra-iac-phase-c4-tofu-apply.md §0.1.

2. **Freshness gate** (``run_apply_sequence`` step 2) — a NON-mutating
   ``tofu plan -refresh-only -detailed-exitcode``: exit 0 = fresh (proceed),
   2 = drift (refuse), 1 = error (refuse). Load-bearing: tofu's built-in
   saved-plan staleness check only fires on a state serial/lineage change, so a
   pure out-of-band edit is caught ONLY here.

3. **Saved-plan apply** (step 3) — ``tofu apply plan.tfplan`` (the embedded,
   HMAC-verified config; never a re-plan). ``TF_VAR_tofu_state_kms_key`` is in
   the subprocess env on EVERY call (the iac/ encryption block is enforced).
"""
from __future__ import annotations

import json
import re
import subprocess  # noqa: S404 — the worker legitimately shells out to tofu
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# A run_tofu callable: (args, cwd, env) -> (returncode, stdout, stderr).
RunTofu = Callable[[list[str], str, dict[str, str]], tuple[int, str, str]]

# resource "<type>" "<name>" — top-level managed resource declarations in baked HCL.
_RESOURCE_DECL_RE = re.compile(r'(?m)^\s*resource\s+"([A-Za-z0-9_]+)"\s+"([A-Za-z0-9_-]+)"')


class FidelityError(Exception):
    """The worker cannot faithfully reproduce the planned environment — refuse
    before any tofu run (version/lockfile mismatch, or a plan that creates a
    resource / touches an address the baked iac/ does not declare)."""


class FreshnessDrift(Exception):
    """The non-mutating refresh-only gate detected out-of-band drift (exit 2)."""

    def __init__(self, stdout: str, stderr: str) -> None:
        self.stdout = stdout
        self.stderr = stderr
        super().__init__("refresh-only detected drift (exit 2); refusing apply")


class TofuStepError(Exception):
    """A tofu step failed (non-success, non-drift exit) — fail closed."""

    def __init__(self, step: str, exit_code: int, stderr: str) -> None:
        self.step = step
        self.exit_code = exit_code
        self.stderr = stderr
        super().__init__(f"tofu {step} failed (exit {exit_code})")


@dataclass(frozen=True)
class ApplyOutcome:
    freshness_exit: int
    apply_exit: int
    state_serial: int | None
    state_lineage: str | None


def _default_run_tofu(args: list[str], cwd: str, env: dict[str, str]) -> tuple[int, str, str]:
    """Real subprocess runner (the production seam). ``args`` is the tofu
    sub-command + flags (no leading 'tofu'); ``cwd`` is the iac working dir."""
    proc = subprocess.run(  # noqa: S603 — args are fixed worker-controlled flags, no shell
        ["tofu", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def extract_declared_addresses(iac_dir: Path) -> set[str]:
    """Parse baked ``*.tf`` for ``resource "<type>" "<name>"`` → ``{type.name}``.

    Module-nested resources are intentionally NOT extracted; the resource-set
    guard refuses ``module.*`` plan addresses outright (fail-closed) until
    module-aware extraction exists. There are no modules in iac/ today."""
    declared: set[str] = set()
    for tf in sorted(iac_dir.glob("*.tf")):
        text = tf.read_text(encoding="utf-8")
        for rtype, rname in _RESOURCE_DECL_RE.findall(text):
            declared.add(f"{rtype}.{rname}")
    return declared


def _normalize_address(address: str) -> str:
    """Strip a trailing ``count``/``for_each`` instance suffix
    (``google_x.y[0]`` / ``google_x.y["k"]`` → ``google_x.y``) so instance
    addresses compare against the declared ``type.name`` set."""
    return re.sub(r"\[[^\]]*\]$", "", address)


def resource_set_guard(plan_json: dict, declared: set[str]) -> str | None:
    """Return a refusal reason if the plan touches anything the baked iac/ can't
    faithfully reproduce, else ``None``.

    Refuse if any managed ``resource_changes`` entry: (a) has a ``create`` action
    (a new resource the baked config doesn't yet declare → init/refresh would be
    misled), (b) is a ``module.*`` address (no module-aware extraction), or
    (c) has a normalized address not in the baked declared set. ``no-op``/``read``
    entries are ignored (they touch nothing)."""
    rcs = plan_json.get("resource_changes")
    if not isinstance(rcs, list):
        return "plan.json has no resource_changes list"
    for rc in rcs:
        if not isinstance(rc, dict):
            return "malformed resource_changes entry"
        change = rc.get("change") if isinstance(rc.get("change"), dict) else {}
        actions = change.get("actions")
        if not isinstance(actions, list):
            return f"{rc.get('address', '<unknown>')}: malformed actions"
        if actions in (["no-op"], ["read"]):
            continue
        address = rc.get("address")
        if not isinstance(address, str):
            return "resource_changes entry has no address"
        if "create" in actions:
            return f"{address}: plan creates a resource (needs the head config — C5)"
        if address.startswith("module."):
            return f"{address}: module-nested address not supported by the baked-config guard"
        if _normalize_address(address) not in declared:
            return f"{address}: address not declared in the baked iac/ (needs the head config — C5)"
    return None


def assert_fidelity(
    *,
    signed_metadata: dict,
    baked_tofu_version: str,
    baked_lockfile_sha256: str,
    plan_json: dict,
    declared_addresses: set[str],
) -> None:
    """Fail-closed fidelity gate — raise :class:`FidelityError` on any mismatch.
    Runs before init/refresh/apply (``tofu version`` itself is a subprocess)."""
    want_version = signed_metadata.get("opentofu_version")
    if want_version != baked_tofu_version:
        raise FidelityError(
            f"opentofu_version mismatch: signed {want_version!r} != baked {baked_tofu_version!r}"
        )
    want_lock = signed_metadata.get("provider_lockfile_sha256")
    if want_lock != baked_lockfile_sha256:
        raise FidelityError(
            f"provider_lockfile_sha256 mismatch: signed {want_lock!r} != baked {baked_lockfile_sha256!r}"
        )
    reason = resource_set_guard(plan_json, declared_addresses)
    if reason is not None:
        raise FidelityError(f"resource-set guard: {reason}")


def _state_serial_lineage(
    run_tofu: RunTofu, cwd: str, env: dict[str, str]
) -> tuple[int | None, str | None]:
    """Best-effort: parse ``tofu state pull`` for serial + lineage ONLY (never
    log full state). Returns ``(None, None)`` if unavailable — the apply already
    succeeded, so this is audit, not control-flow."""
    try:
        rc, out, _err = run_tofu(["state", "pull"], cwd, env)
        if rc != 0:
            return None, None
        doc = json.loads(out)
        serial = doc.get("serial")
        lineage = doc.get("lineage")
        return (
            serial if isinstance(serial, int) else None,
            lineage if isinstance(lineage, str) else None,
        )
    except Exception:  # noqa: BLE001 — audit best-effort, never fail the apply on this
        return None, None


def run_apply_sequence(
    *,
    workdir: str,
    kms_key: str,
    base_env: dict[str, str],
    run_tofu: RunTofu = _default_run_tofu,
) -> ApplyOutcome:
    """init (readonly lock) → refresh-only freshness gate → saved-plan apply.

    ``workdir`` is the per-request temp iac dir containing the fetched, verified
    ``plan.tfplan``. ``kms_key`` is injected as ``TF_VAR_tofu_state_kms_key`` on
    every call (the iac/ encryption block is enforced; ``show``/``plan``/``apply``
    all must decrypt). Raises :class:`FreshnessDrift` on drift and
    :class:`TofuStepError` on any non-success step — both fail-closed."""
    env = {**base_env, "TF_VAR_tofu_state_kms_key": kms_key}

    rc, _out, err = run_tofu(["init", "-input=false", "-no-color", "-lockfile=readonly"], workdir, env)
    if rc != 0:
        raise TofuStepError("init", rc, err)

    rc, out, err = run_tofu(
        ["plan", "-refresh-only", "-detailed-exitcode", "-input=false", "-no-color",
         "-lock=true", "-lock-timeout=120s"],
        workdir, env,
    )
    if rc == 2:
        raise FreshnessDrift(out, err)
    if rc != 0:
        raise TofuStepError("refresh-only", rc, err)

    rc, _out, err = run_tofu(
        ["apply", "-input=false", "-no-color", "-lock=true", "-lock-timeout=120s",
         "-auto-approve", "plan.tfplan"],
        workdir, env,
    )
    if rc != 0:
        raise TofuStepError("apply", rc, err)

    serial, lineage = _state_serial_lineage(run_tofu, workdir, env)
    return ApplyOutcome(freshness_exit=0, apply_exit=0, state_serial=serial, state_lineage=lineage)
