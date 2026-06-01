"""tofu-editor Agent — iac/-only multi-file PR opener (Phase D1-3).

Write-side worker of the DriftScribe Phase D agent-authoring flow. Takes a LIST
of ``iac/`` file writes the agent has composed, commits them on one ``infra/``
head branch off ``main``, and opens ONE pull request labeled
``driftscribe-infra``. It is the sole new WRITE SURFACE Phase D introduces, so
``/open-pr`` is fail-closed by construction: every policy check runs BEFORE any
GitHub call, and a rejected request leaves no side effect.

This worker authors HCL *text* only — it never runs ``tofu`` (plan/apply live
in the separate ``tofu-apply`` worker, the sole mutator) and it never touches
``main`` directly. Its blast radius is bounded to PRs on an ``infra/`` head
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

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from driftscribe_lib import github as ds_github
from driftscribe_lib.auth import verify_caller
from driftscribe_lib.iac_editor_policy import (
    MAX_BODY,
    MAX_TITLE,
    EditorPolicyError,
    validate_base,
    validate_branch,
    validate_file_writes,
)
from driftscribe_lib.logging import install_trace_middleware, setup as setup_logging

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
    - **422** on schema violation (extra/missing field, bad type) or any
      ``EditorPolicyError`` carrying ``status_code == 422`` (empty file list,
      empty/oversize content, oversize title/body).
    """
    # Re-validate request-body target_repo against the env-pinned allowlist
    # BEFORE any GitHub call. Defense in depth: a misconfigured coordinator
    # cannot redirect this worker even if it tried.
    if req.target_repo != TARGET_REPO:
        raise HTTPException(
            status_code=403,
            detail="target_repo does not match deployed allowlist",
        )

    # Layer 2 payload-intent policy. Each call maps EditorPolicyError onto an
    # HTTPException with the error's own status_code (403 policy / 422 schema).
    try:
        validate_base(req.base)
        validate_branch(req.branch)
        validate_file_writes([f.model_dump() for f in req.files])
    except EditorPolicyError as e:
        raise HTTPException(status_code=e.status_code, detail=e.reason) from e

    if len(req.title) > MAX_TITLE:
        raise HTTPException(status_code=422, detail="title too long")
    if len(req.body) > MAX_BODY:
        raise HTTPException(status_code=422, detail="body too long")

    # Do NOT log file contents — only counts/metadata.
    log.info(
        "open-pr request: caller=%s repo=%s branch=%s files=%d",
        caller, TARGET_REPO, req.branch, len(req.files),
    )

    repo = _get_repo()
    # ``base`` is pinned to "main" here even though ``validate_base`` already
    # enforced it — belt and suspenders, matches the plan.
    result = ds_github.open_iac_pr(
        repo,
        branch=req.branch,
        base="main",
        title=req.title,
        body=req.body,
        files=[f.model_dump() for f in req.files],
    )
    return {
        "status": "opened",
        "pr_number": result["number"],
        "pr_url": result["url"],
        "branch": result["branch"],
    }
