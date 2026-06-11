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
from typing import NoReturn

# A run_tofu callable: (args, cwd, env) -> (returncode, stdout, stderr).
RunTofu = Callable[[list[str], str, dict[str, str]], tuple[int, str, str]]

# resource "<type>" "<name>" — top-level managed resource declarations in baked HCL.
_RESOURCE_DECL_RE = re.compile(r'(?m)^\s*resource\s+"([A-Za-z0-9_]+)"\s+"([A-Za-z0-9_-]+)"')

# OpenTofu/Terraform's canonical state-lock-acquire failure: the GCS backend (and
# every other lockable backend) prints "Error acquiring the state lock" followed
# by a "Lock Info:" block. Match ONLY that canonical phrase (case-insensitive) —
# deliberately NOT the bare word "lock", which appears in unrelated errors
# ("deadlock", "block device", a provider's own "lock" wording) and would
# misclassify a genuine step failure as contention. When the phrase is absent we
# fall through to TofuStepError (fail-closed default).
_STATE_LOCK_RE = re.compile(r"error acquiring the state lock", re.IGNORECASE)


class FidelityError(Exception):
    """The worker cannot faithfully reproduce the planned environment — refuse
    before any tofu run (version/lockfile mismatch, or a plan that creates a
    resource / touches an address the baked iac/ does not declare)."""


class IacTreeMismatch(Exception):
    """C6 head-config-delivery gate: the worker's baked ``iac/``-tree hash does NOT
    equal the C2 sidecar's ``iac_tree_hash`` (or the sidecar failed its cross-check
    against the HMAC-signed metadata, or no sidecar generation was supplied for a
    create-class plan). For a create-class apply this means the worker was NOT
    re-baked from the approved head's merged config (or ``main`` advanced after the
    merge) — refuse. Distinct terminal outcome ``tree_mismatch_refused`` (HTTP 409 at
    ``/apply``; 422 at ``/propose``, pre-mint): the operator must re-bake from the
    current ``main`` (or re-plan if ``main`` advanced with another ``iac/`` change),
    NOT blind-retry.

    Standalone ``Exception`` (NOT a ``FidelityError`` subclass), mirroring
    :class:`LockRefused`/:class:`ApplyStateSuspect`, so ``except FidelityError``
    handlers don't capture it and it always surfaces as its own terminal phase."""


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


class LockRefused(Exception):
    """A tofu step could not acquire the GCS-backend state lock — distinct from a
    genuine step failure.

    Raised when a non-success step's stderr carries OpenTofu's canonical
    "Error acquiring the state lock" signature: the lock is held by a concurrent
    run OR was orphaned (e.g. an OOM-killed apply that never released it — this
    actually bit production). This is a self-describing terminal outcome so the
    operator knows to ``tofu force-unlock`` (operator-only — the worker NEVER
    auto-unlocks) and retry, rather than chasing a phantom apply failure.

    Intentionally a STANDALONE ``Exception`` subclass, NOT a subclass of
    :class:`TofuStepError`, so existing ``except TofuStepError`` handlers are
    unaffected and lock contention always surfaces as its own outcome."""

    def __init__(self, step: str, exit_code: int, stderr: str) -> None:
        self.step = step
        self.exit_code = exit_code
        self.stderr = stderr
        super().__init__(f"tofu {step} refused: state lock held (exit {exit_code})")


@dataclass(frozen=True)
class PostFailureState:
    """Diagnostic captured AFTER a failed ``tofu apply`` to decide whether the
    failure left state dirty. ``state_suspect`` is the fail-closed verdict: True
    unless the worker can PROVE state stayed clean (serial known + unchanged AND
    a read-only refresh-only plan reports no drift). ``refresh_output`` is the
    human-readable refresh-only plan text (bounded by the caller before audit)."""

    state_suspect: bool
    serial_before: int | None
    serial_after: int | None
    serial_bumped: bool
    refresh_exit: int | None
    refresh_drift: bool
    refresh_output: str
    refresh_stderr: str


class ApplyStateSuspect(Exception):
    """``tofu apply`` FAILED and the worker could NOT prove state stayed clean —
    the failed apply may have persisted (partial/desired) state into the backend
    even though the live update was rejected. This bit production during C5g: a
    403-at-admission apply still wrote ``service_account`` into state, so the next
    refresh-only 409'd on a phantom "drift". DISTINCT from :class:`TofuStepError`
    (a clean failure) so the operator runs the apply-failure recovery runbook (a
    state reconcile) instead of a futile blind retry.

    Standalone (NOT a TofuStepError subclass), mirroring :class:`LockRefused`, so
    existing ``except TofuStepError`` handlers are unaffected and a suspect-state
    failure always surfaces as its own outcome."""

    def __init__(self, step: str, exit_code: int, stderr: str, diag: "PostFailureState") -> None:
        self.step = step
        self.exit_code = exit_code
        self.stderr = stderr
        self.diag = diag
        super().__init__(f"tofu {step} failed (exit {exit_code}); state suspect")


def _is_lock_contention(stderr: str) -> bool:
    """True only if ``stderr`` carries tofu's canonical state-lock-acquire phrase.

    Conservative by design: a false negative (treat contention as a plain step
    failure) is merely a less-specific outcome, whereas a false positive (treat a
    real failure as contention) would mislead the operator into a force-unlock.
    So when uncertain this returns False and the caller raises
    :class:`TofuStepError` (fail-closed default)."""
    return bool(_STATE_LOCK_RE.search(stderr))


def _raise_step_failure(step: str, rc: int, stderr: str) -> NoReturn:
    """Classify a non-success tofu step: :class:`LockRefused` if the stderr is
    state-lock contention, else :class:`TofuStepError` (the fail-closed default).
    Both are terminal/fail-closed; the distinction only changes the operator's
    next action (force-unlock + retry vs. investigate the failure)."""
    if _is_lock_contention(stderr):
        raise LockRefused(step, rc, stderr)
    raise TofuStepError(step, rc, stderr)


@dataclass(frozen=True)
class ApplyOutcome:
    freshness_exit: int
    apply_exit: int
    state_serial: int | None
    state_lineage: str | None
    # The refresh-only drift paths that the semantic gate classified as benign
    # server-computed churn and PROCEEDED through (empty when freshness_exit == 0,
    # i.e. no drift at all). Recorded in the success audit for transparency.
    benign_drift_paths: tuple[str, ...] = ()


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


def resource_set_guard(
    plan_json: dict,
    declared: set[str],
    *,
    allow_create_of_declared: bool = False,
    allow_import_of_declared: bool = False,
) -> str | None:
    """Return a refusal reason if the plan touches anything the baked iac/ can't
    faithfully reproduce, else ``None``.

    Per managed ``resource_changes`` entry (``no-op``/``read`` ignored), in order:
    (0) an ``importing`` entry is handled by the adopt admission branch (Phase 2);
    (a) a ``module.*`` address is refused UNCONDITIONALLY and FIRST — there is no
    module-aware extraction, and re-baking does NOT unlock modules (C6); (b) a
    ``create`` action is refused UNLESS ``allow_create_of_declared`` is True; (c) any
    address (create or otherwise) not in the baked declared set is refused.

    ``allow_create_of_declared`` defaults to **False** (the C5 floor: every create
    refused). The worker sets it True ONLY after the C6 ``iac/``-tree hash gate has
    PROVEN the baked config equals the approved head's merged config — so a create is
    admitted only when (i) the baked ``main`` now declares the resource AND (ii) that
    baked config is provably the reviewed one. Decoupling these would let a saved
    plan's PR-authored create apply against a coincidentally-declaring config; the
    hash gate is the safety coupling (see the C6 plan §2)."""
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
        # Import admission (adopt design §4.5, Phase 2): an importing entry
        # writes a NEW address into state at apply even when its actions are
        # pure no-op. Admitted ONLY when (i) allow_import_of_declared — which
        # the worker sets ONLY after the C6 tree-hash proof — AND (ii) the
        # address is declared in the baked iac/. Anything else refuses:
        # import-with-changes and indexed/module addresses are refused even
        # WITH the flag (the denylist + static gate ban them; the guard must
        # not silently undo that — defense in depth). `importing: null` is
        # absent; a leftover-inert import block on a later plan carries no
        # `importing` and stays a plain no-op.
        if change.get("importing") is not None:
            address = rc.get("address")
            if not isinstance(address, str):
                return "importing resource_changes entry has no address"
            if actions != ["no-op"]:
                return (
                    f"{address}: import with changes (actions={actions}) — "
                    "only zero-change imports are admitted"
                )
            if address.startswith("module."):
                return f"{address}: module-nested import not supported by the baked-config guard"
            if "[" in address:
                return f"{address}: indexed import target not admitted (v1 adopts plain addresses)"
            if not allow_import_of_declared:
                return (
                    f"{address}: plan imports a resource into state "
                    "(needs the head config — re-bake from main, C6)"
                )
            if _normalize_address(address) not in declared:
                return f"{address}: imported address not declared in the baked iac/"
            continue
        if actions in (["no-op"], ["read"]):
            continue
        address = rc.get("address")
        if not isinstance(address, str):
            return "resource_changes entry has no address"
        # (a) module refusal FIRST — unconditional, before any create admission.
        if address.startswith("module."):
            return f"{address}: module-nested address not supported by the baked-config guard"
        # (a.5) a REPLACE (create+delete) is destroy-then-recreate — destructive and
        # OUT of C6 scope (pure creates only). Refuse UNCONDITIONALLY (even with the
        # create flag), independent of the denylist (defense in depth — Codex C6a-3).
        if "create" in actions and "delete" in actions:
            return f"{address}: replace (destroy+recreate) not supported by the baked-config guard (C6 admits pure creates)"
        # (b) creates need the head config delivered via re-bake-from-main (C6).
        if "create" in actions and not allow_create_of_declared:
            return f"{address}: plan creates a resource (needs the head config — re-bake from main, C6)"
        # (c) every touched address must be declared in the baked iac/.
        if _normalize_address(address) not in declared:
            return f"{address}: address not declared in the baked iac/ (needs the head config — C6)"
    return None


def assert_fidelity(
    *,
    signed_metadata: dict,
    baked_tofu_version: str,
    baked_lockfile_sha256: str,
    plan_json: dict,
    declared_addresses: set[str],
    allow_create_of_declared: bool = False,
    allow_import_of_declared: bool = False,
) -> None:
    """Fail-closed fidelity gate — raise :class:`FidelityError` on any mismatch.
    Runs before init/refresh/apply (``tofu version`` itself is a subprocess).
    ``allow_create_of_declared`` and ``allow_import_of_declared`` are forwarded to
    :func:`resource_set_guard` — the worker sets both True ONLY after the C6 hash
    gate proved baked == approved-head."""
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
    reason = resource_set_guard(
        plan_json, declared_addresses,
        allow_create_of_declared=allow_create_of_declared,
        allow_import_of_declared=allow_import_of_declared,
    )
    if reason is not None:
        raise FidelityError(f"resource-set guard: {reason}")


def _state_serial_lineage(
    run_tofu: RunTofu, cwd: str, env: dict[str, str]
) -> tuple[int | None, str | None]:
    """Best-effort: parse ``tofu state pull`` for serial + lineage ONLY (never
    log full state). Returns ``(None, None)`` if unavailable. Used both for the
    post-success audit AND as the pre-apply baseline that the failed-apply
    diagnosis compares against — an unreadable serial is treated fail-closed
    (a clean failure can't be PROVEN, so it's classified suspect)."""
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


def _diagnose_post_failure_state(
    run_tofu: RunTofu, cwd: str, env: dict[str, str],
    serial_before: int | None, lineage_before: str | None,
) -> PostFailureState:
    """After a failed ``tofu apply``, decide whether the failure dirtied state.

    Fail-closed: ``state_suspect`` is True UNLESS the worker can PROVE state
    stayed clean — the serial AND lineage were readable both before AND after,
    are UNCHANGED, AND a non-mutating refresh-only plan reports no drift
    (exit 0). Anything weaker is suspect: serial/lineage unreadable on either
    side, serial bumped, lineage changed, refresh drift (exit 2), the refresh
    erroring (exit 1 ⇒ "could not prove clean"), or the diagnosis itself raising.
    A failed apply that can't be proven clean must route the operator to a state
    reconcile, never a blind retry — the C5g signature was a rejected live update
    that still persisted the planned attribute and bumped the serial. (Lineage is
    read for free alongside the serial and closes the pathological "serial
    coincidentally equal but the state was replaced" gap.) ``serial_bumped`` /
    ``refresh_drift`` are kept as explanatory audit fields (NOT the sole verdict).

    Never raises — it is called on the already-failed apply branch, so any
    internal error fails closed to ``state_suspect=True`` rather than escaping
    and 500-ing the burned-approval request (which would strand phase="claimed").
    ``tofu state pull`` is swallowed by ``_state_serial_lineage``; the only other
    subprocess (the refresh-only plan) is wrapped below. The refresh-only plan is
    ``-lock=false`` (never persists, never acquires/contends the state lock,
    including one orphaned by the failed apply)."""
    serial_after, lineage_after = _state_serial_lineage(run_tofu, cwd, env)
    serial_bumped = (
        serial_before is not None
        and serial_after is not None
        and serial_after != serial_before
    )
    try:
        rc, out, err = run_tofu(
            ["plan", "-refresh-only", "-detailed-exitcode", "-no-color",
             "-lock=false", "-input=false"],
            cwd, env,
        )
    except Exception as exc:  # noqa: BLE001 — a diagnosis that can't run can't prove clean
        return PostFailureState(
            state_suspect=True, serial_before=serial_before, serial_after=serial_after,
            serial_bumped=serial_bumped, refresh_exit=None, refresh_drift=False,
            refresh_output="", refresh_stderr=f"refresh diagnosis raised: {exc}",
        )
    refresh_drift = rc == 2
    provably_clean = (
        serial_before is not None
        and serial_after is not None
        and serial_after == serial_before
        and lineage_before is not None
        and lineage_after is not None
        and lineage_after == lineage_before
        and rc == 0
    )
    return PostFailureState(
        state_suspect=not provably_clean,
        serial_before=serial_before,
        serial_after=serial_after,
        serial_bumped=serial_bumped,
        refresh_exit=rc,
        refresh_drift=refresh_drift,
        refresh_output=out,
        refresh_stderr=err,
    )


# --------------------------------------------------------------------------- #
# Semantic freshness gate (C5g carry-forward 1a)
# --------------------------------------------------------------------------- #
#
# The blunt freshness gate refused on ANY refresh-only drift (exit 2). That
# false-positived in prod (C5g): a redeploy froze gcs state at an old generation
# while live moved on, so refresh detected PURELY server-computed churn (no
# desired-state change) and the gate refused, forcing a manual state reconcile.
# The semantic gate classifies the drift: PROCEED (still applying the
# human-approved saved plan.tfplan — re-planning the head is C6, NOT here) only
# when EVERY drifted attribute is a known server-computed/status field; any
# material desired-state drift, or any unparseable/unexpected structure, fails
# CLOSED to drift_refused.
#
# SAFETY: the allowlist holds ONLY fields with no desired-state security meaning —
# server-computed readback PLUS the two gcloud-deploy metadata fields client /
# client_version (operator-settable but free-text deploy-tool tags, and already in
# iac/cloudrun.tf `ignore_changes`, so their out-of-band churn is tolerable by
# design). A material change to a real desired attribute (image, env,
# service_account, scaling, ingress, ...) is never allowlisted ⇒ always refuses.
# An incomplete allowlist can only OVER-refuse (= the old status quo) — it can
# never introduce a false-clean. Identity/lifecycle-computed fields (uid,
# create_time, delete_time) are deliberately NOT allowlisted: a changed uid /
# populated delete_time signals a recreate/deletion and MUST refuse. Sensitive /
# not-yet-known values are handled separately (the *_sensitive / after_unknown
# markers below) since their real value is redacted from before/after. (Codex
# review 019e7a3f + sole-mutator adversarial review.)

# List-index-stripped attribute paths tolerated as benign drift: server readback
# (generation, etag, ...) + the two ignore_changes'd gcloud-deploy metadata tags
# (client, client_version).
COMPUTED_ONLY_DRIFT_PATHS = frozenset({
    "generation", "observed_generation", "etag", "update_time", "last_modifier",
    "client", "client_version", "latest_created_revision", "latest_ready_revision",
    "reconciling",
})
# Whole subtrees that are status/readback only (never desired-state inputs).
COMPUTED_ONLY_DRIFT_SUBTREES = ("conditions", "terminal_condition", "traffic_statuses")
# The computed-field allowlist above is Cloud-Run-v2-specific (conditions /
# terminal_condition / traffic_statuses are Cloud Run status fields) and iac/
# manages exactly ONE resource today. Benign classification is therefore SCOPED
# to this type: refresh drift on any OTHER resource type fails closed (refuses)
# until the allowlist is extended for it (Codex review 019e7a3f — future-proofs
# the C6 resource-set expansion against a Cloud-Run allowlist misfiring).
_BENIGN_DRIFT_TYPES = frozenset({"google_cloud_run_v2_service"})

_LIST_INDEX_RE = re.compile(r"\[\d+\]")


@dataclass(frozen=True)
class RefreshDriftVerdict:
    """Result of classifying a refresh-only ``tofu show -json``.

    ``benign`` is True only if EVERY detected change is a known computed/status
    field. ``paths`` carries the offending material paths when not benign (for
    the refusal message) or the benign computed paths when benign (for the
    success audit). Fail-closed: malformed/unexpected input ⇒ ``benign=False``."""

    benign: bool
    paths: tuple[str, ...]
    reason: str


def _normalize_attr_path(path: str) -> str:
    """Strip list indices from a dotted attribute path
    (``conditions[0].last_transition_time`` → ``conditions.last_transition_time``)."""
    return _LIST_INDEX_RE.sub("", path)


def _changed_leaf_paths(before: object, after: object, prefix: str = "") -> set[str]:
    """Recursively diff two JSON values → the set of NORMALIZED leaf paths that
    differ. Added/removed keys (present on one side only) count as a change at
    that path. Lists are compared element-wise by index (indices are stripped in
    the normalized leaf path, so per-element churn collapses onto its subtree)."""
    if before == after:
        return set()
    if isinstance(before, dict) and isinstance(after, dict):
        out: set[str] = set()
        for key in set(before) | set(after):
            sub = f"{prefix}.{key}" if prefix else str(key)
            out |= _changed_leaf_paths(before.get(key), after.get(key), sub)
        return out
    if isinstance(before, list) and isinstance(after, list):
        out = set()
        for i in range(max(len(before), len(after))):
            b = before[i] if i < len(before) else None
            a = after[i] if i < len(after) else None
            out |= _changed_leaf_paths(b, a, f"{prefix}[{i}]")
        return out
    # scalar (or type-mismatch) leaf: a genuine value change.
    return {_normalize_attr_path(prefix)}


def _is_computed_only_path(path: str) -> bool:
    """True iff ``path`` is an exact computed leaf OR sits under a status subtree
    (anchored at the path root — never a same-named field deeper in the tree)."""
    if path in COMPUTED_ONLY_DRIFT_PATHS:
        return True
    return any(path == p or path.startswith(p + ".") for p in COMPUTED_ONLY_DRIFT_SUBTREES)


def _has_true(obj: object) -> bool:
    """True iff any leaf of a tofu sensitivity/unknown marker tree is ``True``.

    ``before_sensitive`` / ``after_sensitive`` / ``after_unknown`` mirror the
    attribute shape with ``true`` marking a sensitive or not-yet-known value (and
    ``false`` / ``{}`` / ``[]`` elsewhere). A plain truthiness test would
    over-refuse on the common all-false-but-present tree, so recurse for a real
    ``true``."""
    if obj is True:
        return True
    if isinstance(obj, dict):
        return any(_has_true(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_true(v) for v in obj)
    return False


def classify_refresh_drift(show_json: object) -> RefreshDriftVerdict:
    """Pure, fail-closed classifier of a refresh-only ``tofu show -json``.

    Benign ONLY when: no root ``output_changes``; every ``resource_changes``
    entry is no-op/read (a refresh-only plan carries no config-driven actions);
    and every ``resource_drift`` entry is either no-op/read or an ``update`` whose
    every changed leaf path is computed-only. Anything else (create/delete/replace
    drift, a config action, malformed/unexpected structure) ⇒ NOT benign."""
    if not isinstance(show_json, dict):
        return RefreshDriftVerdict(False, (), "show-json is not an object")
    # iac/ declares no outputs — any root output change is unexpected → refuse.
    if show_json.get("output_changes"):
        return RefreshDriftVerdict(False, (), "refresh changed root outputs")
    rcs = show_json.get("resource_changes")
    if rcs is not None:
        if not isinstance(rcs, list):
            return RefreshDriftVerdict(False, (), "resource_changes is not a list")
        for rc in rcs:
            change = rc.get("change") if isinstance(rc, dict) else None
            actions = change.get("actions") if isinstance(change, dict) else None
            if actions not in (["no-op"], ["read"]):
                return RefreshDriftVerdict(False, (), f"resource_changes carries action {actions!r}")
    drift = show_json.get("resource_drift")
    if not isinstance(drift, list):
        # exit-2 came with no parseable drift array → can't prove benign → refuse.
        return RefreshDriftVerdict(False, (), "resource_drift missing or not a list")
    computed: list[str] = []
    material: list[str] = []
    for entry in drift:
        if not isinstance(entry, dict):
            return RefreshDriftVerdict(False, (), "malformed resource_drift entry")
        addr = entry.get("address", "<unknown>")
        change = entry.get("change")
        if not isinstance(change, dict):
            return RefreshDriftVerdict(False, (), f"{addr}: malformed change object")
        actions = change.get("actions")
        if actions in (["no-op"], ["read"]):
            continue
        if actions != ["update"]:
            # create/delete/replace/unknown → resource appeared/vanished/recreated
            # out of band → material, refuse.
            return RefreshDriftVerdict(False, (), f"{addr}: drift action {actions!r} is material")
        if entry.get("type") not in _BENIGN_DRIFT_TYPES:
            # The computed-field allowlist is type-scoped; an unrecognized type's
            # drift cannot be proven benign by it → fail closed.
            return RefreshDriftVerdict(
                False, (), f"{addr}: computed-drift allowlist is type-scoped; "
                f"{entry.get('type')!r} not recognized")
        # A sensitive or not-yet-known value is redacted in before/after, so a
        # change to it can leave before==after with the real delta carried ONLY in
        # the marker trees. If any marker is set we cannot prove the drift benign
        # from before/after → fail closed (adversarial-review finding).
        if (_has_true(change.get("before_sensitive"))
                or _has_true(change.get("after_sensitive"))
                or _has_true(change.get("after_unknown"))):
            return RefreshDriftVerdict(
                False, (), f"{addr}: drift carries sensitive/unknown markers — cannot prove benign")
        before, after = change.get("before"), change.get("after")
        if not isinstance(before, dict) or not isinstance(after, dict):
            return RefreshDriftVerdict(False, (), f"{addr}: non-dict before/after on update")
        for p in sorted(_changed_leaf_paths(before, after)):
            (computed if _is_computed_only_path(p) else material).append(f"{addr}:{p}")
    if material:
        return RefreshDriftVerdict(False, tuple(material),
                                   "material refresh drift: " + ", ".join(material[:10]))
    return RefreshDriftVerdict(True, tuple(computed), "all refresh drift is computed-only churn")


def _refresh_drift_verdict(run_tofu: RunTofu, cwd: str, env: dict[str, str]) -> RefreshDriftVerdict:
    """Run ``tofu show -json refresh.tfplan`` + classify, fail-closed on any
    show/parse error (the saved refresh plan is encrypted; ``env`` carries the
    KMS key so ``show`` can decrypt)."""
    rc, out, err = run_tofu(["show", "-json", "refresh.tfplan"], cwd, env)
    if rc != 0:
        return RefreshDriftVerdict(False, (), f"refresh show -json failed (exit {rc}): {err[-200:]}")
    # Broad except: this guard runs POST-CLAIM (the approval is burned), so a
    # parse OR classification error (incl. RecursionError on pathologically nested
    # input — a RuntimeError, NOT caught by ValueError/TypeError) must fail CLOSED
    # to a refusal, never escape and 500 the request (which would strand the
    # approval at phase="claimed" with no terminal audit). Adversarial-review fix.
    try:
        show_json = json.loads(out)
        return classify_refresh_drift(show_json)
    except Exception as exc:  # noqa: BLE001 — any failure to classify ⇒ cannot prove benign ⇒ refuse
        return RefreshDriftVerdict(False, (), f"refresh drift classification failed: {type(exc).__name__}")


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
    all must decrypt). The refresh-only freshness gate is SEMANTIC: refresh drift
    that is purely server-computed churn proceeds (still applying the approved
    saved plan); only MATERIAL desired-state drift raises :class:`FreshnessDrift`.
    On any non-success step raises :class:`LockRefused` if the failure is
    state-lock contention (held/orphaned GCS lock) or :class:`TofuStepError`
    otherwise — all fail-closed (the classification applies to init, refresh-only,
    and apply)."""
    env = {**base_env, "TF_VAR_tofu_state_kms_key": kms_key}

    rc, _out, err = run_tofu(["init", "-input=false", "-no-color", "-lockfile=readonly"], workdir, env)
    if rc != 0:
        _raise_step_failure("init", rc, err)

    rc, out, err = run_tofu(
        ["plan", "-refresh-only", "-detailed-exitcode", "-out=refresh.tfplan",
         "-input=false", "-no-color", "-lock=true", "-lock-timeout=120s"],
        workdir, env,
    )
    freshness_exit = rc
    benign_drift_paths: tuple[str, ...] = ()
    if rc == 2:
        # Drift detected — classify SEMANTICALLY rather than blanket-refusing.
        # Proceed (still applying the human-approved saved plan.tfplan) ONLY when
        # every drifted attribute is known server-computed churn; any material
        # desired-state drift, or any unparseable/unexpected structure, fails
        # closed to FreshnessDrift → drift_refused (C5g carry-forward 1a).
        verdict = _refresh_drift_verdict(run_tofu, workdir, env)
        if not verdict.benign:
            raise FreshnessDrift(out, f"refusing apply: {verdict.reason}")
        if not verdict.paths:
            # Symmetry / fail-closed: refresh exited 2 (drift) yet the classifier
            # affirmatively identified NO benign computed change to explain it
            # (empty/all-no-op resource_drift) — the signals disagree, so we
            # cannot prove benign → refuse rather than proceed on an unexplained
            # exit-2 (adversarial-review nit).
            raise FreshnessDrift(out, "refusing apply: refresh exit 2 but no classifiable drift")
        benign_drift_paths = verdict.paths
    elif rc != 0:
        _raise_step_failure("refresh-only", rc, err)

    # State serial + lineage BEFORE the mutating apply — the baseline for
    # detecting whether a FAILED apply nonetheless persisted (partial) state.
    serial_before, lineage_before = _state_serial_lineage(run_tofu, workdir, env)

    rc, _out, err = run_tofu(
        ["apply", "-input=false", "-no-color", "-lock=true", "-lock-timeout=120s",
         "-auto-approve", "plan.tfplan"],
        workdir, env,
    )
    if rc != 0:
        # Lock contention never acquired the lock ⇒ no state write ⇒ clean
        # outcome (the existing 423 path). Classified FIRST.
        if _is_lock_contention(err):
            raise LockRefused("apply", rc, err)
        # Genuine apply failure: distinguish a clean failure (:class:`TofuStepError`
        # — today's 502) from a state-suspect one (:class:`ApplyStateSuspect`) by
        # inspecting state IN THIS workdir, which main.py tears down once the
        # exception leaves the worker (so the diagnosis must happen here).
        diag = _diagnose_post_failure_state(run_tofu, workdir, env, serial_before, lineage_before)
        if diag.state_suspect:
            raise ApplyStateSuspect("apply", rc, err, diag)
        raise TofuStepError("apply", rc, err)

    serial, lineage = _state_serial_lineage(run_tofu, workdir, env)
    return ApplyOutcome(
        freshness_exit=freshness_exit, apply_exit=0, state_serial=serial,
        state_lineage=lineage, benign_drift_paths=benign_drift_paths,
    )
