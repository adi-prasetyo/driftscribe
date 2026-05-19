"""Docs Agent — Worker #2 of 4 (Phase 11.4).

Patches the operator runbook on the *hardcoded* target repo and opens a
docs PR. Designed to be the smallest possible side-effect blast radius:
the only thing this worker can do, even if the coordinator is fully
compromised, is push a Markdown file under ``demo/docs/`` into a branch
prefixed ``driftscribe/`` and open a PR against ``main`` on
``adi-prasetyo/driftscribe``.

Safety layers in play here:

- **Layer 1 (IAM scoping):** ``docs-agent-sa`` has NO GCP-level grants.
  Its only privilege is access to the ``docs-agent-github-pat`` Secret
  Manager secret (per-secret binding), injected as the ``GITHUB_TOKEN``
  env var. The PAT itself is a fine-grained GitHub token scoped to a
  single repo with ``Contents: Read & write`` + ``Pull requests: Read &
  write`` only.
- **Layer 2 (payload-intent policy):**

    - Repo is hardcoded via ``TARGET_REPO`` env var. The request schema
      (:class:`PatchRequest` with ``extra="forbid"``) refuses any ``repo``
      field, so a caller cannot redirect this worker at another
      repository even if the PAT happened to have broader scope.
    - ``file_path`` must match :data:`ALLOWED_PATH`, survive
      ``os.path.normpath`` unchanged (no traversal), not be absolute,
      and not be a hidden file. See :func:`_check_path`.
    - ``branch`` must start with ``"driftscribe/"`` — prevents the
      worker from being tricked into pushing to a release / production
      branch name.
    - ``base`` must be ``"main"`` — prevents "open a PR FROM main INTO
      production" confused-deputy scenarios.

- **Layer 3 (inter-service auth):**
  :func:`driftscribe_lib.auth.verify_caller` validates the inbound Google
  ID token's audience claim against ``OWN_URL`` and the caller's email
  against ``ALLOWED_CALLERS``.

Layers 0 (tool registry) and 3 HITL approval live in the coordinator.
"""
import os
import re
from os.path import normpath

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from driftscribe_lib import github as ds_github
from driftscribe_lib.auth import verify_caller
from driftscribe_lib.logging import install_trace_middleware, setup as setup_logging

log = setup_logging("docs-agent")

# Boot-time env resolution. All five are REQUIRED — KeyError here causes
# the Cloud Run revision to fail at startup, surfacing the misconfig
# immediately rather than silently degrading at request time.
TARGET_REPO = os.environ["TARGET_REPO"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]  # injected from docs-agent-github-pat
OWN_URL = os.environ["OWN_URL"].rstrip("/")
ALLOWED_CALLERS = frozenset(
    e.strip() for e in os.environ["ALLOWED_CALLERS"].split(",") if e.strip()
)

# Allowlist: exactly one path component under demo/docs/ ending in .md.
# Combined with the normpath + hidden-file checks below, this means the
# Docs Agent can only touch files like ``demo/docs/runbook.md``.
#
# ``\Z`` (not ``$``) matches *only* end-of-string. Python's ``$`` would also
# match before a final newline — ``demo/docs/runbook.md\n`` would slip past
# the policy with ``$``, creating a visually deceptive filename even though
# it would not escape the directory. ``fullmatch`` is used below as a
# belt-and-suspenders measure (either alone would suffice).
ALLOWED_PATH = re.compile(r"demo/docs/[^/]+\.md\Z")
ALLOWED_BRANCH_PREFIX = "driftscribe/"
# Branch refs after the prefix may contain ASCII letters, digits, hyphen,
# underscore, dot, and slash. This deliberately excludes whitespace, control
# characters, ``..``, leading/trailing dots, and other characters Git itself
# rejects per ``git-check-ref-format(1)``. The cap at 200 chars is well under
# Git's practical limit and prevents pathological inputs. Combined with the
# explicit ``..`` rejection in ``_check_branch`` this means malformed refs
# fail the policy check (403) rather than reaching PyGithub as 500s.
_BRANCH_TAIL = re.compile(r"[A-Za-z0-9._/-]{1,200}\Z")
ALLOWED_BASE = "main"


def _check_path(file_path: str) -> None:
    """Raise ``HTTPException(403)`` if ``file_path`` is outside the allowlist.

    Order matters:

    1. Reject empty strings before the regex does (clearer error message).
    2. Reject absolute paths *before* normalization — ``normpath("/etc/passwd")``
       returns ``"/etc/passwd"`` unchanged, so the equality check below would
       pass it through.
    3. Reject path traversal via ``normpath`` differing from the input. This
       catches things like ``demo/docs/../infra/foo.md`` (normpath →
       ``infra/foo.md``) and ``demo/docs/./runbook.md`` (normpath →
       ``demo/docs/runbook.md``). The latter would actually be safe to allow
       but rejecting non-canonical inputs is simpler and removes a parsing
       attack surface.
    4. Apply the allowlist regex.
    5. Reject hidden files. The regex matches ``demo/docs/.runbook.md`` because
       the dot is inside the filename component — explicit basename check
       catches it. The worker has no business writing dotfiles.
    """
    if not file_path:
        raise HTTPException(status_code=403, detail="empty file_path")
    if file_path.startswith("/"):
        raise HTTPException(status_code=403, detail="absolute path not allowed")
    if file_path != normpath(file_path):
        raise HTTPException(
            status_code=403,
            detail=f"path not normalized: {file_path!r}",
        )
    if not ALLOWED_PATH.fullmatch(file_path):
        raise HTTPException(
            status_code=403,
            detail=f"path not in allowlist: {file_path!r}",
        )
    basename = os.path.basename(file_path)
    if basename.startswith("."):
        raise HTTPException(
            status_code=403,
            detail=f"hidden files not allowed: {basename!r}",
        )


def _check_branch(branch: str) -> None:
    """Reject malformed or out-of-allowlist branch refs *before* PyGithub
    sees them.

    Allowlist is conservative on purpose:

    - Must start with ``driftscribe/`` (Layer 2 anti-confused-deputy).
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
    without monkey-patching the shared lib (same pattern as the Reader)."""
    return verify_caller(
        request, own_url=OWN_URL, allowed_callers=ALLOWED_CALLERS
    )


def _get_repo():
    """Return a PyGithub ``Repository`` for the hardcoded ``TARGET_REPO``.

    Wrapped so tests can ``monkeypatch.setattr(workers.docs.main, "_get_repo",
    …)`` to skip the github.com round-trip.
    """
    return ds_github.get_repo(GITHUB_TOKEN, TARGET_REPO)


class PatchRequest(BaseModel):
    """Closed schema — see module docstring, Layer 2.

    Critically, there is NO ``repo`` field. The target repo is hardcoded
    in ``TARGET_REPO`` and cannot be influenced by the caller. ``extra="forbid"``
    makes pydantic raise on any unexpected field, which FastAPI converts to
    HTTP 422.
    """

    file_path: str
    new_content: str
    branch: str
    base: str
    title: str
    body: str

    model_config = ConfigDict(extra="forbid")


app = FastAPI(title="DriftScribe Docs Agent")

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
    """Patch ``req.file_path`` on the configured repo and open a PR.

    Status codes:

    - **200** on success — returns the PyGithub-derived dict from
      :func:`driftscribe_lib.github.open_docs_pr` (``url``, ``number``,
      ``labeled``, ``label_error``).
    - **403** on any policy violation: refused path / branch / base.
    - **401/403** on auth failure (raised by ``verify_caller`` upstream).
    - **422** on schema violation (extra field, missing field, bad type).
    """
    _check_path(req.file_path)
    _check_branch(req.branch)
    _check_base(req.base)
    log.info(
        "patch request: caller=%s file=%s branch=%s repo=%s",
        caller, req.file_path, req.branch, TARGET_REPO,
    )
    repo = _get_repo()
    result = ds_github.open_docs_pr(
        repo=repo,
        branch=req.branch,
        base=req.base,
        title=req.title,
        body=req.body,
        file_path=req.file_path,
        new_content=req.new_content,
        dry_run=False,
    )
    return result
