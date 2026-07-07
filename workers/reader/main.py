"""Reader Agent — read-only Cloud Run state worker (Phase 11.3).

Worker #1 of 4 in the DriftScribe v3.1 multi-agent architecture. Returns the
live env block + active revision name for the *hardcoded* target service,
plus a short list of previous READY revisions (rollback candidates).

Safety layers in play here:

- **Layer 1 (IAM scoping):** ``reader-agent-sa`` has only ``roles/run.viewer``
  at the project — it cannot mutate anything, even if this code were buggy.
- **Layer 2 (payload-intent policy):** the request body is a closed schema
  (:class:`ReadRequest` with ``extra="forbid"``). The Reader's target service /
  region / project come from environment variables loaded at boot; the caller
  cannot influence them. Any extra field → 4xx from FastAPI's pydantic
  validation before our handler runs.
- **Layer 3 (inter-service auth):** :func:`driftscribe_lib.auth.verify_caller`
  validates the inbound Google ID token's audience claim and checks the
  caller's email against ``ALLOWED_CALLERS``.

Layers 0 (tool registry) and 3 (HITL approval) live in the coordinator and
are out of scope for this worker.
"""
import os

from fastapi import Depends, FastAPI, Request
from google.api_core import exceptions as gax
from google.cloud import run_v2
from pydantic import BaseModel, ConfigDict

from driftscribe_lib.auth import verify_caller
from driftscribe_lib.cloud_run import list_previous_ready_revisions, read_live_state
from driftscribe_lib.logging import install_trace_middleware, setup as setup_logging

log = setup_logging("reader-agent")

# Boot-time env resolution. ``TARGET_SERVICE`` / ``TARGET_REGION`` have sane
# defaults for the hackathon demo, but ``GCP_PROJECT`` / ``OWN_URL`` /
# ``ALLOWED_CALLERS`` MUST be set explicitly — KeyError here causes Cloud Run
# to fail the revision at startup, surfacing the misconfig immediately.
TARGET_SERVICE = os.environ.get("TARGET_SERVICE", "payment-demo")
TARGET_REGION = os.environ.get("TARGET_REGION", "asia-northeast1")
GCP_PROJECT = os.environ["GCP_PROJECT"]
OWN_URL = os.environ["OWN_URL"].rstrip("/")
ALLOWED_CALLERS = frozenset(
    e.strip() for e in os.environ["ALLOWED_CALLERS"].split(",") if e.strip()
)


def _verify_caller_dep(request: Request) -> str:
    """Thin wrapper around :func:`driftscribe_lib.auth.verify_caller` so tests
    can swap it via ``app.dependency_overrides`` without monkey-patching the
    shared library module."""
    return verify_caller(request, own_url=OWN_URL, allowed_callers=ALLOWED_CALLERS)


def _get_revisions_client() -> run_v2.RevisionsClient:
    """Indirection for testability (mirrors ``workers/rollback/main.py``) —
    tests monkeypatch this so no real gRPC channel is built. Called once per
    ``/read`` and the client is shared by both the live-state read and the
    previous-revisions listing, rather than letting each construct its own."""
    return run_v2.RevisionsClient()


class ReadRequest(BaseModel):
    """Empty by design — see module docstring, Layer 2.

    ``extra="forbid"`` makes pydantic raise ``ValidationError`` on any
    unexpected field; FastAPI converts that to HTTP 422 (which the tests
    accept as part of the 4xx class).
    """

    model_config = ConfigDict(extra="forbid")


app = FastAPI(title="DriftScribe Reader Agent")

# Phase 15.2: per-request trace id from inbound ``X-Trace-Id`` (or
# mint a fresh UUIDv4 hex). The id is bound to a ContextVar so every
# ``log.*`` call inside the request carries it in its JSON output.
install_trace_middleware(app)


@app.get("/healthz")
def healthz() -> dict[str, bool]:
    """Liveness probe — intentionally unauthenticated so Cloud Run's built-in
    health checks (and operator curl from outside the VPC) work without
    minting an ID token."""
    return {"ok": True}


@app.post("/read")
def read(
    _body: ReadRequest,
    caller: str = Depends(_verify_caller_dep),
) -> dict:
    """Return live env + active revision for the configured target service.

    ``previous_revisions`` (added for the rollback-candidate-discovery flow):
    up to 5 other READY revisions, newest first, so a caller that wants to
    roll back doesn't have to already know a revision name. Additive field —
    existing consumers reading only ``env`` / ``revision`` are unaffected.
    """
    log.info(
        "read request from %s target=%s/%s/%s",
        caller, TARGET_SERVICE, TARGET_REGION, GCP_PROJECT,
    )
    revisions_client = _get_revisions_client()
    state = read_live_state(
        TARGET_SERVICE, TARGET_REGION, GCP_PROJECT,
        revisions_client=revisions_client,
    )
    # Fail-soft, unlike the read_live_state call above: ``env`` / ``revision``
    # are the core contract every chat turn and recheck depends on (a failure
    # there SHOULD 5xx so the coordinator sees a real reader outage), whereas
    # previous_revisions is a best-effort supplement for rollback-candidate
    # discovery. A transient listing failure (quota, IAM propagation, backend
    # blip) must not take down the whole read that already succeeded.
    try:
        previous_revisions = list_previous_ready_revisions(
            TARGET_SERVICE, TARGET_REGION, GCP_PROJECT, state["revision"],
            revisions_client=revisions_client,
        )
    except gax.GoogleAPICallError as e:
        # PermissionDenied is a GoogleAPICallError subclass, so this covers
        # both the no-IAM case and generic transient backend failures (same
        # stance as workers/infra_reader/main.py's CAI soft-fail).
        log.warning("previous-revisions listing unavailable: %s", e)
        previous_revisions = []
    return {
        "service": TARGET_SERVICE,
        "region": TARGET_REGION,
        "project": GCP_PROJECT,
        "env": state["env"],
        "revision": state["revision"],
        "previous_revisions": previous_revisions,
    }
