"""Upgrade Reader Agent — read-only npm lockfile + advisory worker (Phase 17.C.2).

Worker #5 of the DriftScribe v3.2 multi-agent architecture. First worker of
the new ``upgrade`` workload. Reads an npm-style ``package.json`` lockfile
from a *pinned* GitHub repo and looks up matching vulnerability advisories
from the GitHub Advisory Database.

Read-only. The worker has no GitHub write surface and no other side effects.

Safety layers in play here:

- **Layer 1 (IAM scoping):** ``upgrade-reader-sa`` has no GCP write grants;
  its only privilege is access to the ``upgrade-reader-github-pat`` Secret
  Manager secret (per-secret binding). The PAT itself is a fine-grained
  GitHub token scoped to a single repo with ``Contents: Read`` only — even
  if the worker were tricked into calling other GitHub endpoints, the token
  is incapable of writing anything.

- **Layer 2 (payload-intent policy):**

    - Repo allowlist is a SINGLE value pinned at deploy time via the env
      var ``UPGRADE_TARGET_REPO``. The request schema (:class:`ReadRequest`
      with ``extra="forbid"``) carries a ``target_repo`` field that is
      re-validated against this env value — defense in depth so a
      coordinator misconfiguration cannot redirect the worker at another
      repository. The worker MUST NOT import
      :mod:`agent.workloads.registry` or any other ``agent.*`` module —
      workers stay isolated from coordinator authority code (see comment
      at ``agent/workloads/registry.py:429-440``). The cross-check that
      ``UPGRADE_TARGET_REPO`` in ``infra/cloudbuild.yaml`` matches
      ``UPGRADE_TARGET_REGISTRY["phase17_demo"].target_repo`` is a CI
      guard, owned by Task 17.C.5.
    - ``lockfile_path`` must match the regex
      :data:`_LOCKFILE_PATH_RE` exactly (``re.fullmatch``); it must also
      survive ``os.path.normpath`` unchanged and contain no ``..``
      segments. Together these reject ``../`` traversal, ``./`` no-ops,
      doubled slashes, ``package-lock.json``, and every other path that
      isn't the literal Phase 17 demo target.
    - Advisory source is hardcoded (:data:`_GITHUB_ADVISORY_BASE_URL`).
      The caller cannot supply URLs.

- **Layer 3 (inter-service auth):**
  :func:`driftscribe_lib.auth.verify_caller` validates the inbound Google
  ID token's audience claim against ``OWN_URL`` and the caller's email
  against ``ALLOWED_CALLERS``.

Layers 0 (tool registry) and 3 (HITL approval) live in the coordinator and
are out of scope for this worker.
"""
import json
import os
import re
from os.path import normpath

import requests
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from driftscribe_lib import github as ds_github
from driftscribe_lib.auth import verify_caller
from driftscribe_lib.logging import install_trace_middleware, setup as setup_logging

log = setup_logging("upgrade-reader")

# Boot-time env resolution. All five are REQUIRED — KeyError here causes the
# Cloud Run revision to fail at startup, surfacing the misconfig immediately
# rather than degrading silently at request time. Matches the fail-fast
# pattern in ``workers/reader/main.py`` and ``workers/docs/main.py``.
#
# ``UPGRADE_TARGET_REPO`` is the worker's only source of truth for the
# allowed repository slug — the request body's ``target_repo`` is re-validated
# against this value (see ``/read`` handler below). A CI guard (Task 17.C.5)
# compares this env-pinned value against
# ``UPGRADE_TARGET_REGISTRY["phase17_demo"].target_repo`` so coordinator
# authority and worker deploy intent cannot silently drift.
TARGET_REPO = os.environ["UPGRADE_TARGET_REPO"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]  # injected from upgrade-reader-github-pat
GCP_PROJECT = os.environ["GCP_PROJECT"]
OWN_URL = os.environ["OWN_URL"].rstrip("/")
ALLOWED_CALLERS = frozenset(
    e.strip() for e in os.environ["ALLOWED_CALLERS"].split(",") if e.strip()
)

# Layer 2 path allowlist for Phase 17 — single ecosystem (npm), single demo
# target. Adding ecosystems is post-submission work. ``\Z`` (not ``$``)
# anchors at end-of-string only; combined with ``re.fullmatch`` below either
# alone would suffice, but the pair is a belt-and-suspenders match to the
# Docs Agent's ``ALLOWED_PATH``.
_LOCKFILE_PATH_RE = re.compile(r"demo/upgrade-target/package\.json\Z")

# Advisory source is hardcoded — the caller cannot supply URLs anywhere on
# this worker's surface. Phase 17 ships with GitHub Advisory DB only.
_GITHUB_ADVISORY_BASE_URL = "https://api.github.com/advisories"

# Conservative HTTP timeout for the advisory lookup. The Advisory DB
# endpoint is normally <500 ms; 10s leaves headroom without letting a stuck
# request pin a Cloud Run instance.
_ADVISORY_HTTP_TIMEOUT = 10


def _validate_lockfile_path(path: str) -> None:
    """Raise ``HTTPException(400)`` if ``path`` is outside the Phase 17 allowlist.

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
    """
    if not path:
        raise HTTPException(status_code=400, detail="empty lockfile_path")
    if path.startswith("/"):
        raise HTTPException(
            status_code=400,
            detail="absolute lockfile_path not allowed",
        )
    if ".." in path.split("/"):
        raise HTTPException(
            status_code=400,
            detail=f"lockfile_path must not contain '..': {path!r}",
        )
    if normpath(path) != path:
        raise HTTPException(
            status_code=400,
            detail=f"lockfile_path not normalized: {path!r}",
        )
    if not _LOCKFILE_PATH_RE.fullmatch(path):
        raise HTTPException(
            status_code=400,
            detail=f"lockfile_path not in allowlist: {path!r}",
        )


def _verify_caller_dep(request: Request) -> str:
    """Thin wrapper so tests can swap auth via ``app.dependency_overrides``
    without monkey-patching the shared lib (same pattern as the Reader/Docs)."""
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
    raw = contents.decoded_content.decode("utf-8")
    return json.loads(raw)


def _lookup_advisories(package_name: str, version: str) -> list[dict]:
    """Query GitHub Advisory DB for advisories affecting ``package_name@version``.

    Phase 17 stays simple: one query per dependency name (linear in deps).
    The demo target has 1 dep (``lodash``), so this is fine; a customer
    target with hundreds of deps would want batching or a cached snapshot,
    but that's a post-submission concern.

    Returns a list of ``{ghsa_id, severity, url, summary}`` dicts — the
    minimum surface the coordinator needs to render its decision context.
    Advisories whose ``vulnerable_version_range`` does not affect
    ``version`` are filtered out client-side (the API's ``affects=...``
    query param doesn't always do strict range matching, so we re-check).
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    params = {
        "ecosystem": "npm",
        "affects": f"{package_name}@{version}",
    }
    resp = requests.get(
        _GITHUB_ADVISORY_BASE_URL,
        headers=headers,
        params=params,
        timeout=_ADVISORY_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    out: list[dict] = []
    for adv in payload:
        # Defensive shape coercion: the advisories endpoint returns rich
        # objects; we extract only the fields the coordinator needs and
        # don't surface raw upstream JSON to downstream consumers.
        out.append({
            "ghsa_id": adv.get("ghsa_id"),
            "severity": adv.get("severity"),
            "url": adv.get("html_url") or adv.get("url"),
            "summary": adv.get("summary"),
        })
    return out


class ReadRequest(BaseModel):
    """Closed schema — see module docstring, Layer 2.

    Both fields are REQUIRED. ``target_repo`` is re-validated against the
    env-pinned :data:`TARGET_REPO` inside the handler. ``lockfile_path``
    passes through :func:`_validate_lockfile_path` (traversal guard +
    regex). ``extra="forbid"`` makes pydantic raise on any unexpected
    field, which FastAPI converts to HTTP 422.
    """

    target_repo: str
    lockfile_path: str

    model_config = ConfigDict(extra="forbid")


app = FastAPI(title="DriftScribe Upgrade Reader Agent")

# Phase 15.2: per-request trace id propagation (see driftscribe_lib.logging).
install_trace_middleware(app)


@app.get("/healthz")
def healthz() -> dict[str, bool]:
    """Liveness probe — unauthenticated so Cloud Run's built-in health
    checks (and operator curl from outside the VPC) work without minting
    an ID token."""
    return {"ok": True}


@app.post("/read")
def read(
    req: ReadRequest,
    caller: str = Depends(_verify_caller_dep),
) -> dict:
    """Return parsed dependencies + matched advisories for the configured target.

    Status codes:

    - **200** on success — returns the dep+advisory map.
    - **400** on policy violation: ``target_repo`` mismatch, refused
      ``lockfile_path``.
    - **401/403** on auth failure (raised by ``verify_caller`` upstream).
    - **422** on schema violation (extra field, missing field, bad type).
    """
    # Re-validate request-body target_repo against the env-pinned allowlist
    # BEFORE any GitHub call. Defense in depth: a misconfigured coordinator
    # cannot redirect this worker even if it tried.
    if req.target_repo != TARGET_REPO:
        raise HTTPException(
            status_code=400,
            detail="target_repo does not match deployed allowlist",
        )
    _validate_lockfile_path(req.lockfile_path)

    log.info(
        "read request: caller=%s repo=%s lockfile=%s",
        caller, TARGET_REPO, req.lockfile_path,
    )

    repo = _get_repo()
    lockfile = _read_lockfile(repo, req.lockfile_path)
    deps_block = lockfile.get("dependencies") or {}

    # Linear scan: 1 advisory lookup per dependency. Acceptable for Phase 17
    # since the demo target has 1 dep; revisit if upgrade-reader is pointed
    # at a real-world lockfile with hundreds of entries.
    dependencies: list[dict] = []
    for name, version_spec in deps_block.items():
        advisories = _lookup_advisories(name, version_spec)
        dependencies.append({
            "name": name,
            "version": version_spec,
            "advisories": advisories,
        })

    return {
        "target_repo": TARGET_REPO,
        "lockfile_path": req.lockfile_path,
        "dependencies": dependencies,
    }
