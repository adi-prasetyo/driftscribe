"""tofu-editor Agent — iac/-only multi-file PR opener (Phase D1-3).

Write-side worker of the DriftScribe Phase D agent-authoring flow. Takes a LIST
of ``iac/`` file writes the agent has composed, commits them on one ``infra/``
head branch off ``main``, and opens ONE pull request labeled
``driftscribe-infra``. It is the sole new WRITE SURFACE Phase D introduces, so
``/open-pr`` is fail-closed by construction: every policy check runs BEFORE any
GitHub call, and a rejected request leaves no side effect.

This worker authors HCL *text*. The ONLY ``tofu`` subcommand it runs is
``tofu fmt`` — a purely syntactic, offline canonicalization of authored ``.tf``
before committing (so the required ``tofu`` CI check passes without a manual
fixup); it never runs plan/apply (those live in the separate ``tofu-apply``
worker, the sole mutator) and never touches ``main`` directly. Its blast radius
is bounded to PRs on an ``infra/`` head
branch targeting ``main`` in a single pinned repo.

Three safety layers stack here:

- **Layer 1 (IAM-scoped PAT):** the GitHub credential is a fine-grained,
  write-scoped PAT pinned to a single repository (``Contents`` +
  ``Pull requests`` write), injected from the ``tofu-editor-github-pat`` Secret
  Manager secret via ``GITHUB_TOKEN``. Even a fully compromised coordinator
  cannot reach any other repo or any other GitHub surface through this worker.

- **Layer 2 (payload-intent policy):** the file-write allowlist lives in
  :mod:`driftscribe_lib.iac_editor_policy` (which reuses CI's static-gate
  constants as the single source of truth): every write must be a normalized,
  traversal-free ``iac/``-prefixed ``.tf``/``.md`` path that is NOT one of the
  operator-only foundation files, deduplicated, non-empty, and size-bounded;
  ``branch`` must start with ``infra/``; ``base`` must be ``main``; ``title`` /
  ``body`` are length-bounded. The request schema is closed
  (``extra="forbid"``), and ``target_repo`` is re-validated against the
  deploy-pinned :data:`TARGET_REPO` so a coordinator misconfiguration cannot
  redirect the worker at another repository. The worker MUST NOT import any
  ``agent.*`` module — workers stay isolated from coordinator authority code
  (see ``agent/workloads/registry.py:429-440`` and the no-agent-import test).

- **Layer 3 (inter-service auth):**
  :func:`driftscribe_lib.auth.verify_caller` validates the inbound Google ID
  token's audience claim against ``OWN_URL`` and the caller's email against
  ``ALLOWED_CALLERS`` (the coordinator service account only).

Status-code convention (mirrors the other write-side workers): policy
violations from :class:`~driftscribe_lib.iac_editor_policy.EditorPolicyError`
carry their own ``status_code`` — 403 for policy/authorization failures, 422
for schema-shaped (empty/oversize/malformed) failures — and are mapped straight
onto :class:`fastapi.HTTPException` at the handler boundary.
"""
import os
import shutil
import subprocess
import time

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from driftscribe_lib import github as ds_github
from driftscribe_lib.auth import verify_caller
from driftscribe_lib.iac_editor_policy import (
    ALLOWED_BASE,
    EditorPolicyError,
    validate_base,
    validate_branch,
    validate_file_writes,
    validate_title_body,
)
from driftscribe_lib.logging import install_trace_middleware, setup as setup_logging
from tools.iac_static_gate import GateInput, GateMode, evaluate

log = setup_logging("tofu-editor")

# Boot-time env resolution. All four are REQUIRED — a KeyError here fails the
# Cloud Run revision at startup, surfacing the misconfig immediately rather
# than degrading silently at request time. Matches the fail-fast pattern in
# ``workers/upgrade_docs/main.py`` (~97-102).
#
# ``IAC_EDITOR_TARGET_REPO`` is the worker's only source of truth for the
# allowed repository slug — the request body's ``target_repo`` is re-validated
# against this value (see ``/open-pr`` handler below).
TARGET_REPO = os.environ["IAC_EDITOR_TARGET_REPO"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]  # injected from tofu-editor-github-pat
OWN_URL = os.environ["OWN_URL"].rstrip("/")
ALLOWED_CALLERS = frozenset(
    e.strip() for e in os.environ["ALLOWED_CALLERS"].split(",") if e.strip()
)
# An empty allowlist would let verify_caller accept no one (every request
# bounces) — but worse, it silently signals a misconfigured deploy. Fail the
# revision at boot rather than ship a worker that can never serve. Mirrors the
# upgrade-docs REQUIRED_CHECKS empty-set guard.
if not ALLOWED_CALLERS:
    raise RuntimeError(
        "ALLOWED_CALLERS resolved to an empty set — refusing to boot"
    )


def _verify_caller_dep(request: Request) -> str:
    """Thin wrapper so tests can swap auth via ``app.dependency_overrides``
    without monkey-patching the shared lib (same pattern as the upgrade-docs /
    docs / reader workers)."""
    return verify_caller(
        request, own_url=OWN_URL, allowed_callers=ALLOWED_CALLERS
    )


def _get_repo():
    """Return a PyGithub ``Repository`` for the env-pinned target.

    Wrapped so tests can monkeypatch this single seam instead of stubbing the
    entire ``driftscribe_lib.github`` surface (same idiom as upgrade-docs).
    """
    return ds_github.get_repo(GITHUB_TOKEN, TARGET_REPO)


# `tofu fmt` is a purely syntactic, OFFLINE transform — no providers, no state,
# no network, no filesystem writes (input on stdin, formatted output on stdout).
# It is the ONE tofu subcommand this authoring worker runs; the image bakes the
# pinned binary solely for this (see the Dockerfile).
#
# Resolve the binary to an ABSOLUTE path ONCE at import (not relying on PATH at
# each subprocess call): in the deployed image this is /usr/local/bin/tofu;
# locally/CI it's wherever `tofu` is installed. Falls back to the container path.
_TOFU_BIN = os.path.abspath(shutil.which("tofu") or "/usr/local/bin/tofu")
# Per-file cap AND an aggregate budget: a request may carry up to MAX_FILES (32)
# files, so a naive per-file timeout would let worst-case pre-commit time blow
# past the coordinator's ~30s worker HTTP timeout. The aggregate budget keeps the
# whole formatting pass comfortably under that; once it is exhausted the
# remaining files are committed UNFORMATTED (fail-soft, CI stays the backstop).
_TOFU_FMT_PER_FILE_TIMEOUT_S = 15
# Keep the whole pass well under the coordinator's ~30s worker HTTP timeout so
# the bulk of the budget stays available for the GitHub branch/file/PR calls.
# `tofu fmt` on authored files is milliseconds, so 5s is ample for ≤32 files.
_TOFU_FMT_TOTAL_BUDGET_S = 5.0


def _run_tofu_fmt(content: str, timeout_s: float = _TOFU_FMT_PER_FILE_TIMEOUT_S) -> str:
    """Return ``content`` formatted by ``tofu fmt`` (stdin → stdout).

    FAIL-SOFT by design: if the binary is missing, times out, errors, or rejects
    the input (non-zero exit), the ORIGINAL content is returned unchanged. The
    required ``tofu`` CI check (``tofu -chdir=iac fmt -check``) stays the
    authoritative backstop, so a fmt hiccup never turns a valid PR into a 500 —
    it just falls back to the pre-fmt behavior.
    """
    try:
        proc = subprocess.run(
            [_TOFU_BIN, "fmt", "-"],
            input=content,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:  # binary missing / timeout
        log.warning("tofu fmt unavailable — committing unformatted: %s", e)
        return content
    if proc.returncode != 0:
        log.warning(
            "tofu fmt rejected content (exit %d) — committing unformatted",
            proc.returncode,
        )
        return content
    return proc.stdout


def _format_tf_files(files: list[dict]) -> list[dict]:
    """Return committed file dicts with every ``.tf`` content run through
    :func:`_run_tofu_fmt`. Non-``.tf`` files (``.md``) pass through untouched —
    fmt only applies to HCL. Both authoring paths converge here, so formatting
    at this single point makes agent-authored HCL ``tofu fmt -check``-clean
    regardless of which orchestrator produced it.

    Bounded by an aggregate wall-clock budget (``_TOFU_FMT_TOTAL_BUDGET_S``): each
    file is formatted with whatever budget remains (capped per file); once the
    budget is spent the rest pass through unformatted. The caller MUST re-validate
    the returned dicts against the byte caps — ``tofu fmt`` can grow a file
    (``=`` alignment) past ``MAX_FILE_BYTES`` / ``MAX_TOTAL_BYTES``.
    """
    formatted: list[dict] = []
    deadline = time.monotonic() + _TOFU_FMT_TOTAL_BUDGET_S
    for f in files:
        if not f["path"].endswith(".tf"):
            formatted.append(f)
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.warning("tofu fmt budget exhausted — committing remaining files unformatted")
            formatted.append(f)
            continue
        timeout_s = min(remaining, _TOFU_FMT_PER_FILE_TIMEOUT_S)
        formatted.append({**f, "content": _run_tofu_fmt(f["content"], timeout_s)})
    return formatted


class FileWrite(BaseModel):
    """One file write — closed schema. ``path`` + ``content`` are validated by
    :func:`driftscribe_lib.iac_editor_policy.validate_file_writes` in the
    handler (iac/-prefix, suffix, foundation guard, traversal, size)."""

    model_config = ConfigDict(extra="forbid")
    path: str
    content: str


class OpenIacPrRequest(BaseModel):
    """Closed schema for ``/open-pr`` — see module docstring, Layer 2.

    All fields REQUIRED. ``target_repo`` is re-validated against the
    env-pinned :data:`TARGET_REPO`. ``branch`` / ``base`` / ``files`` pass
    through :mod:`driftscribe_lib.iac_editor_policy`. ``title`` / ``body`` are
    size-bounded against ``MAX_TITLE`` / ``MAX_BODY``. ``extra="forbid"`` makes
    pydantic raise on any unexpected field (FastAPI → HTTP 422).
    """

    model_config = ConfigDict(extra="forbid")
    target_repo: str
    branch: str
    base: str
    title: str
    body: str
    files: list[FileWrite]


app = FastAPI(title="DriftScribe tofu-editor Agent")

# Phase 15.2: per-request trace id propagation (see driftscribe_lib.logging).
install_trace_middleware(app)


@app.get("/healthz")
def healthz() -> dict[str, bool]:
    """Liveness probe — unauthenticated so Cloud Run's built-in health checks
    (and operator curl from outside the VPC) work without minting an ID
    token."""
    return {"ok": True}


@app.post("/open-pr")
def open_pr(
    req: OpenIacPrRequest,
    caller: str = Depends(_verify_caller_dep),
) -> dict:
    """Commit ``req.files`` onto an ``infra/`` branch and open ONE iac/-only PR.

    Fail-closed: every policy check below runs BEFORE any GitHub call, so a
    rejected request leaves no side effect.

    Status codes:

    - **200** on success — ``{status, pr_number, pr_url, branch}``.
    - **403** on policy violation: ``target_repo`` mismatch, or any
      :class:`~driftscribe_lib.iac_editor_policy.EditorPolicyError` carrying
      ``status_code == 403`` (path outside iac/, bad suffix, foundation file,
      traversal, duplicate path, bad branch / base).
    - **401/403** on auth failure (raised by ``verify_caller`` upstream).
    - **422** on schema violation (extra/missing field, bad type), any
      ``EditorPolicyError`` carrying ``status_code == 422`` (empty file list,
      empty/oversize content, oversize title/body), OR an AGENT-mode
      static-gate content violation — ``{"error": "static_gate", "violations":
      [...]}`` (NEW provider, module, provisioner, etc.; same ``evaluate`` as
      CI).
    """
    # Re-validate request-body target_repo against the env-pinned allowlist
    # BEFORE any GitHub call. Defense in depth: a misconfigured coordinator
    # cannot redirect this worker even if it tried.
    if req.target_repo != TARGET_REPO:
        raise HTTPException(
            status_code=403,
            detail="target_repo does not match deployed allowlist",
        )

    # Layer 2 payload-intent policy — the whole allowlist lives in
    # ``iac_editor_policy``. Each call maps EditorPolicyError onto an
    # HTTPException with the error's own status_code (403 policy / 422 schema).
    try:
        validate_base(req.base)
        validate_branch(req.branch)
        validate_file_writes([f.model_dump() for f in req.files])
        validate_title_body(req.title, req.body)
    except EditorPolicyError as e:
        raise HTTPException(status_code=e.status_code, detail=e.reason) from e

    # In-process AGENT-mode static gate (Phase D1-4). Runs AFTER the path /
    # title-body allowlist (so it only ever sees already-iac/-validated input)
    # and BEFORE any GitHub call — fail fast so a content-policy violation never
    # opens a junk PR. This is the SAME ``evaluate`` CI runs in AGENT mode, so
    # the worker's content policy is identical to CI's by construction: HCL the
    # worker accepts is HCL CI's static gate accepts (NEW providers, modules,
    # provisioners, arbitrary-execution / forbidden-data-source / dynamic
    # blocks, etc. are all rejected here). Fail-closed: any violation → 422 with
    # no GitHub side effect. Only ``.tf`` content is structurally analyzed (the
    # path checks above already pinned the suffix to ``.tf``/``.md``).
    paths = tuple(f.path for f in req.files)
    hcl = {f.path: f.content for f in req.files if f.path.endswith(".tf")}
    violations = evaluate(
        GateInput(mode=GateMode.AGENT, changed_paths=paths, hcl_files=hcl)
    )
    if violations:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "static_gate",
                "violations": [
                    {"rule": v.rule, "detail": v.detail} for v in violations
                ],
            },
        )

    # Do NOT log file contents — only counts/metadata.
    log.info(
        "open-pr request: caller=%s repo=%s branch=%s files=%d",
        caller, TARGET_REPO, req.branch, len(req.files),
    )

    # Canonicalize HCL with `tofu fmt` BEFORE committing (after the gate, which
    # sees the agent's raw intent — fmt is whitespace-only so it can't change
    # what the gate already approved). This makes agent-authored .tf files
    # `tofu fmt -check`-clean so the required `tofu` CI check passes without a
    # manual fixup commit (the friction seen on Phase 3 PR #66). Fail-soft.
    committed_files = _format_tf_files([f.model_dump() for f in req.files])
    # RE-VALIDATE the FORMATTED content against the byte caps before any GitHub
    # call: `tofu fmt` can grow a file (`=` alignment) past MAX_FILE_BYTES /
    # MAX_TOTAL_BYTES even when the raw payload was under them. Same 422 mapping
    # as the first pass — fail-closed, no side effect. (Path/suffix/foundation
    # checks are whitespace-invariant but re-running them is cheap and keeps this
    # a single authoritative gate over exactly what gets committed.)
    try:
        validate_file_writes(committed_files)
    except EditorPolicyError as e:
        raise HTTPException(status_code=e.status_code, detail=e.reason) from e

    repo = _get_repo()
    # ``base`` is pinned to the policy constant here even though
    # ``validate_base`` already enforced ``req.base == ALLOWED_BASE`` — belt and
    # suspenders, matches the plan.
    result = ds_github.open_iac_pr(
        repo,
        branch=req.branch,
        base=ALLOWED_BASE,
        title=req.title,
        body=req.body,
        files=committed_files,
    )
    return {
        "status": "opened",
        "pr_number": result["number"],
        "pr_url": result["url"],
        "branch": result["branch"],
    }
