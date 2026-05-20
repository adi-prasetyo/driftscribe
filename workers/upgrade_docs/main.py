"""Upgrade Docs Agent — npm version bump + PR opener (Phase 17.C.3).

Worker #6 of the DriftScribe v3.2 multi-agent architecture. Write-side
worker of the new ``upgrade`` workload. Bumps a named package's pinned
version inside a *pinned* GitHub repo's ``package.json`` and opens a PR
against ``main``.

Tight blast radius by design: even if the coordinator is fully compromised,
the only side effect this worker can produce is a PR on
``adi-prasetyo/driftscribe`` that updates ``demo/upgrade-target/package.json``
on a branch prefixed ``upgrade/`` and targets ``main``.

Safety layers in play here:

- **Layer 1 (IAM scoping):** ``upgrade-docs-sa`` has no GCP write grants;
  its only privilege is access to the ``upgrade-docs-github-pat`` Secret
  Manager secret (per-secret binding). The PAT itself is a fine-grained
  GitHub token scoped to a single repo with ``Contents: Read & write`` +
  ``Pull requests: Read & write`` only — even if the worker were tricked
  into calling other endpoints, the token has no other surface.

- **Layer 2 (payload-intent policy):**

    - Repo allowlist is a SINGLE value pinned at deploy time via the env
      var ``UPGRADE_TARGET_REPO``. The request schema (:class:`PatchRequest`
      with ``extra="forbid"``) carries a ``target_repo`` field that is
      re-validated against this env value — defense in depth so a
      coordinator misconfiguration cannot redirect the worker at another
      repository. The worker MUST NOT import
      :mod:`agent.workloads.registry` or any other ``agent.*`` module —
      workers stay isolated from coordinator authority code (see comment
      at ``agent/workloads/registry.py:429-440``).
    - ``lockfile_path`` must match the regex :data:`_LOCKFILE_PATH_RE`
      exactly (``re.fullmatch``); it must also survive ``os.path.normpath``
      unchanged and contain no ``..`` segments. The traversal guard +
      regex pair is the same shape used by ``workers/upgrade_reader``.
    - ``branch`` must start with :data:`ALLOWED_BRANCH_PREFIX` (``upgrade/``)
      so all PRs from this worker are observability-scoped to the upgrade
      workload; the suffix is matched against
      :data:`_BRANCH_TAIL` which excludes whitespace, control chars,
      ``..``, and characters Git itself would reject.
    - ``base`` must be ``"main"`` — prevents "open a PR FROM main INTO
      production" confused-deputy scenarios.
    - The patch is scoped to a single key: ``dependencies[package_name]``
      is overwritten with ``target_version``; every other key in the
      lockfile (and every other file in the repo) is preserved as-is. If
      ``package_name`` is not already present in ``dependencies`` the
      worker refuses with 422 — adding new deps is out of scope and
      this is a minimal safety net so a malformed request cannot
      silently insert a new dependency. (Fuller semantic validation —
      semver no-downgrade, patch/minor only, GHSA URL shape — is the
      post-LLM validator's job, Task 17.C.3a.)

- **Layer 3 (inter-service auth):**
  :func:`driftscribe_lib.auth.verify_caller` validates the inbound Google
  ID token's audience claim against ``OWN_URL`` and the caller's email
  against ``ALLOWED_CALLERS``.

Status-code convention follows the Docs Agent (sibling write-side worker):
policy violations return **403** (not 400). This is deliberately different
from ``workers/upgrade_reader`` which uses 400 — the reader is read-only,
so a policy bounce there is closer to a request-validation issue; the
write-side worker treats every policy bounce as a deny.
"""
import json
import os
import re
from os.path import normpath

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from driftscribe_lib import github as ds_github
from driftscribe_lib.auth import verify_caller
from driftscribe_lib.logging import install_trace_middleware, setup as setup_logging

log = setup_logging("upgrade-docs")

# Boot-time env resolution. All four are REQUIRED — KeyError here causes the
# Cloud Run revision to fail at startup, surfacing the misconfig immediately
# rather than degrading silently at request time. Matches the fail-fast
# pattern in ``workers/docs/main.py`` and ``workers/upgrade_reader/main.py``.
#
# ``UPGRADE_TARGET_REPO`` is the worker's only source of truth for the
# allowed repository slug — the request body's ``target_repo`` is
# re-validated against this value (see ``/patch`` handler below). A CI
# guard (Task 17.C.5) compares this env-pinned value against
# ``UPGRADE_TARGET_REGISTRY["phase17_demo"].target_repo`` so coordinator
# authority and worker deploy intent cannot silently drift.
TARGET_REPO = os.environ["UPGRADE_TARGET_REPO"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]  # injected from upgrade-docs-github-pat
OWN_URL = os.environ["OWN_URL"].rstrip("/")
ALLOWED_CALLERS = frozenset(
    e.strip() for e in os.environ["ALLOWED_CALLERS"].split(",") if e.strip()
)

# Layer 2 path allowlist for Phase 17 — single ecosystem (npm), single demo
# target. Adding ecosystems is post-submission work. ``\Z`` (not ``$``)
# anchors at end-of-string only; combined with ``re.fullmatch`` below either
# alone would suffice, but the pair matches the Upgrade Reader's
# ``_LOCKFILE_PATH_RE`` byte-for-byte so reviewers can pattern-match the
# two workers against the same regex.
_LOCKFILE_PATH_RE = re.compile(r"demo/upgrade-target/package\.json\Z")

ALLOWED_BRANCH_PREFIX = "upgrade/"
# Branch refs after the prefix may contain ASCII letters, digits, hyphen,
# underscore, dot, and slash. Excludes whitespace, control characters,
# ``..``, and characters Git itself rejects per ``git-check-ref-format(1)``.
# The cap at 200 chars is well under Git's practical limit and prevents
# pathological inputs. Combined with the explicit ``..`` rejection in
# ``_check_branch`` malformed refs fail policy (403) rather than reaching
# PyGithub as 500s. Same shape as the Docs Agent's _BRANCH_TAIL.
_BRANCH_TAIL = re.compile(r"[A-Za-z0-9._/-]{1,200}\Z")
ALLOWED_BASE = "main"


def _check_lockfile_path(file_path: str) -> None:
    """Raise ``HTTPException(403)`` if ``file_path`` is outside the Phase 17
    allowlist.

    Order matters — traversal guards run BEFORE the regex so the rejection
    reason is precise and the regex never sees a normalized-different form
    that happens to slip through ``[^/]+``:

    1. Reject empty / absolute paths (cleaner error than a regex miss).
    2. Reject any ``..`` segment — catches ``demo/upgrade-target/../infra/X``.
    3. Reject inputs whose ``os.path.normpath`` form differs from the
       literal input — catches ``demo/upgrade-target/./package.json``,
       ``demo//upgrade-target/package.json``, trailing slashes, and other
       non-canonical forms.
    4. Apply :data:`_LOCKFILE_PATH_RE` via ``fullmatch``.

    Mirrors ``workers/upgrade_reader/main.py:_validate_lockfile_path`` but
    raises 403 instead of 400 — see module docstring on status-code
    convention.
    """
    if not file_path:
        raise HTTPException(status_code=403, detail="empty lockfile_path")
    if file_path.startswith("/"):
        raise HTTPException(
            status_code=403,
            detail="absolute lockfile_path not allowed",
        )
    if ".." in file_path.split("/"):
        raise HTTPException(
            status_code=403,
            detail=f"lockfile_path must not contain '..': {file_path!r}",
        )
    if normpath(file_path) != file_path:
        raise HTTPException(
            status_code=403,
            detail=f"lockfile_path not normalized: {file_path!r}",
        )
    if not _LOCKFILE_PATH_RE.fullmatch(file_path):
        raise HTTPException(
            status_code=403,
            detail=f"lockfile_path not in allowlist: {file_path!r}",
        )


def _check_branch(branch: str) -> None:
    """Reject malformed or out-of-allowlist branch refs *before* PyGithub
    sees them.

    Allowlist is conservative on purpose:

    - Must start with ``upgrade/`` (Layer 2 anti-confused-deputy +
      observability).
    - Suffix limited to ``[A-Za-z0-9._/-]{1,200}``: excludes whitespace,
      control chars, newlines, and any character ``git-check-ref-format(1)``
      itself would reject.
    - ``..`` substring rejected anywhere (Git itself forbids consecutive
      dots in ref components; we reject early so the failure is a clean
      policy 403 rather than a PyGithub 5xx).
    """
    if not branch.startswith(ALLOWED_BRANCH_PREFIX):
        raise HTTPException(
            status_code=403,
            detail=f"branch must start with {ALLOWED_BRANCH_PREFIX!r}",
        )
    tail = branch[len(ALLOWED_BRANCH_PREFIX):]
    if not tail:
        raise HTTPException(
            status_code=403,
            detail="branch suffix is empty",
        )
    if ".." in branch:
        raise HTTPException(
            status_code=403,
            detail=f"branch must not contain '..': {branch!r}",
        )
    if not _BRANCH_TAIL.fullmatch(tail):
        raise HTTPException(
            status_code=403,
            detail=f"branch has invalid characters or length: {branch!r}",
        )


def _check_base(base: str) -> None:
    if base != ALLOWED_BASE:
        raise HTTPException(
            status_code=403,
            detail=f"base must be {ALLOWED_BASE!r}",
        )


def _verify_caller_dep(request: Request) -> str:
    """Thin wrapper so tests can swap auth via ``app.dependency_overrides``
    without monkey-patching the shared lib (same pattern as the Reader /
    Docs / Upgrade Reader workers)."""
    return verify_caller(
        request, own_url=OWN_URL, allowed_callers=ALLOWED_CALLERS
    )


def _get_repo():
    """Return a PyGithub ``Repository`` for the env-pinned target.

    Wrapped so tests can monkeypatch this single seam instead of stubbing
    the entire ``driftscribe_lib.github`` surface.
    """
    return ds_github.get_repo(GITHUB_TOKEN, TARGET_REPO)


def _read_lockfile(repo, lockfile_path: str) -> dict:
    """Fetch ``lockfile_path`` from ``repo`` and parse it as JSON.

    Wrapped for the same reason as :func:`_get_repo` — single monkey-patch
    surface for tests.
    """
    contents = repo.get_contents(lockfile_path)
    return json.loads(contents.decoded_content.decode("utf-8"))


class PatchRequest(BaseModel):
    """Closed schema — see module docstring, Layer 2.

    All fields REQUIRED. ``target_repo`` is re-validated against the
    env-pinned :data:`TARGET_REPO` inside the handler. ``lockfile_path``
    passes through :func:`_check_lockfile_path` (traversal guard + regex).
    ``branch`` passes through :func:`_check_branch`. ``base`` passes through
    :func:`_check_base`. ``extra="forbid"`` makes pydantic raise on any
    unexpected field, which FastAPI converts to HTTP 422.
    """

    target_repo: str
    lockfile_path: str
    package_name: str
    target_version: str
    advisory_url: str
    branch: str
    base: str
    title: str
    body: str

    model_config = ConfigDict(extra="forbid")


app = FastAPI(title="DriftScribe Upgrade Docs Agent")

# Phase 15.2: per-request trace id propagation (see driftscribe_lib.logging).
install_trace_middleware(app)


@app.get("/healthz")
def healthz() -> dict[str, bool]:
    """Liveness probe — unauthenticated so Cloud Run's built-in health
    checks (and operator curl from outside the VPC) work without minting
    an ID token."""
    return {"ok": True}


@app.post("/patch")
def patch(
    req: PatchRequest,
    caller: str = Depends(_verify_caller_dep),
) -> dict:
    """Bump ``req.package_name`` to ``req.target_version`` in the lockfile
    on the configured repo and open a PR.

    Status codes:

    - **200** on success — returns the PyGithub-derived dict from
      :func:`driftscribe_lib.github.open_docs_pr` (``url``, ``number``,
      ``labeled``, ``label_error``).
    - **403** on policy violation: ``target_repo`` mismatch, refused
      ``lockfile_path`` / ``branch`` / ``base``.
    - **401/403** on auth failure (raised by ``verify_caller`` upstream).
    - **422** on schema violation (extra field, missing field, bad type)
      OR when ``package_name`` is not present in the current lockfile's
      ``dependencies`` block. The latter is a minimal safety net — the
      full semver/existence/range checks are the post-LLM validator
      (Task 17.C.3a).
    """
    # Re-validate request-body target_repo against the env-pinned allowlist
    # BEFORE any GitHub call. Defense in depth: a misconfigured coordinator
    # cannot redirect this worker even if it tried.
    if req.target_repo != TARGET_REPO:
        raise HTTPException(
            status_code=403,
            detail="target_repo does not match deployed allowlist",
        )
    _check_lockfile_path(req.lockfile_path)
    _check_branch(req.branch)
    _check_base(req.base)

    log.info(
        "patch request: caller=%s repo=%s lockfile=%s pkg=%s ver=%s branch=%s",
        caller, TARGET_REPO, req.lockfile_path,
        req.package_name, req.target_version, req.branch,
    )

    repo = _get_repo()
    lockfile = _read_lockfile(repo, req.lockfile_path)

    # Minimal safety net: the post-LLM validator (17.C.3a) does the full
    # semver / no-downgrade / range checks. Here we only enforce that the
    # package already exists in ``dependencies`` so a malformed request
    # cannot silently *add* a new dependency — that's not in scope for
    # this worker's action surface.
    deps = lockfile.get("dependencies") or {}
    if req.package_name not in deps:
        raise HTTPException(
            status_code=422,
            detail=(
                f"package_name {req.package_name!r} not present in "
                f"lockfile dependencies (cannot add new deps; worker "
                f"only bumps existing ones)"
            ),
        )

    # Mutate ONLY dependencies[package_name]. Every other key is preserved
    # as-is — top-level metadata (name, version, scripts, …) and other
    # dependencies must round-trip byte-identically modulo the JSON
    # serialization step below.
    lockfile["dependencies"][req.package_name] = req.target_version

    # Pin indent=2 + trailing newline. npm's own ``npm install`` writes
    # ``package.json`` with two-space indent and a trailing newline; matching
    # that convention keeps the PR diff minimal and avoids whitespace-only
    # churn against the upstream baseline.
    new_content = json.dumps(lockfile, indent=2) + "\n"

    # Always cite the advisory URL in the PR body. The agent-supplied
    # ``body`` is the prelude (rationale, links, etc.); we append the
    # canonical ``Advisory:`` line so the URL is present regardless of
    # what the agent wrote. The validator (17.C.3a) will additionally
    # enforce the URL's shape (GHSA-…) so a future caller can't slip
    # arbitrary URLs through.
    pr_body = f"{req.body}\n\nAdvisory: {req.advisory_url}\n"

    return ds_github.open_docs_pr(
        repo=repo,
        branch=req.branch,
        base=req.base,
        title=req.title,
        body=pr_body,
        file_path=req.lockfile_path,
        new_content=new_content,
        dry_run=False,
    )
