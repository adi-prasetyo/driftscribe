# agent/main.py
import asyncio
import contextlib
import datetime as dt
import hashlib
import hmac
import json
import re
import secrets
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutureTimeout
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from google.auth import exceptions as google_auth_exceptions
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.id_token import verify_oauth2_token
from pydantic import BaseModel, ConfigDict

from agent import approvals as approval_helpers
from agent import iac_artifacts
from agent import iac_csrf
from agent import worker_client
from agent.auth import require_cf_operator, verify_token
from agent.classifier import ClassificationInput, classify
from agent.config import Settings, artifacts_bucket, get_settings
from agent.worker_client import WorkerClientError
from agent.contract import OpsContract, load_contract
from agent.github_actions import (
    get_repo,
    open_docs_pr,
    open_drift_issue,
    open_escalation_issue,
)
from agent.mcp.developer_knowledge import MissingDeveloperKnowledgeApiKeyError
from agent.models import DecisionAction, DecisionProposal
from agent.renderer import (
    render_docs_pr_body,
    render_drift_issue_body,
    render_escalation_issue_body,
    render_rollback_body,
)
from agent.runbook_patcher import patch_runbook
from agent.secret_guard import redact_event
from agent.state_store import FirestoreStateStore, InMemoryStateStore, StateStore
from agent.trace_fetcher import (
    CloudLoggingFetcher,
    StubTraceFetcher,
    TraceFetcher,
    _HEX32_RE,
)
from agent.validator import ValidationError as ProposalValidationError
from agent.validator import validate
from agent.workloads import (
    MissingWorkerEnvError,
    ReservedToolNotImplementedError,
    UnknownUpgradeTargetError,
    WorkloadResolution,
    load_workload,
    reset_workload,
    set_workload,
)
from pydantic import ValidationError as PydanticValidationError
from driftscribe_lib import github
from driftscribe_lib.github import PrMergeBlockedError, PrNotEligibleError
from driftscribe_lib.logging import (
    current_trace_id_or_new,
    install_trace_middleware,
    reset_trace_id,
    set_trace_id,
    setup as setup_logging,
)

# Configure structured JSON logging for this service. Module-level so the
# root logger has its JSON handler before any per-module ``logging.getLogger()``
# call (or import-time log emission) goes out. Idempotent — repeated imports
# in a pytest session don't double-attach handlers.
log = setup_logging("driftscribe-agent")

# Match git refspec rules (https://git-scm.com/docs/git-check-ref-format):
# allow ASCII letters/digits/`_`/`-`; collapse runs of disallowed chars to `-`.
_BRANCH_SLUG = re.compile(r"[^a-z0-9_-]+")


def _branch_slug(name: str) -> str:
    """Sanitize an env-var name for use inside a git branch name."""
    slug = _BRANCH_SLUG.sub("-", name.lower()).strip("-")
    return slug or "var"


def _eager_resolve_upgrade_contract(resolution: WorkloadResolution) -> None:
    """Eagerly parse the upgrade workload's ``contract.yaml`` at request entry.

    Phase 17.C.4 (Codex 2026-05-20 follow-up — step 4 of task 17.C.4):
    ``load_workload("upgrade")`` already resolves the manifest's
    ``contract_file`` *path* but does NOT parse the contract YAML. The
    contract parser (:func:`agent.upgrade_contract.load_upgrade_contract`)
    is what surfaces :class:`UnknownUpgradeTargetError` for an unknown
    ``target_name``, and pydantic ValidationError for any schema
    violation. We invoke it here so a bad contract becomes a clean 503
    at request entry, not a mid-conversation runtime error after the
    LLM has already started reasoning.

    No-op for non-upgrade workloads — drift's contract is parsed by
    :func:`agent.contract.load_contract` later in :func:`_do_recheck`.

    Maps the parser's failure modes to a single 503 with the original
    error message preserved so the operator can self-diagnose:

    - :class:`UnknownUpgradeTargetError`: contract's ``target_name``
      isn't in :data:`UPGRADE_TARGET_REGISTRY` — a deploy bug, but
      structurally the same "workload not deployed" condition as a
      missing worker URL from the operator's POV.
    - :class:`pydantic.ValidationError`: schema violation (unknown
      decision key, missing field, bad type). Same 503 surface.
    - :class:`FileNotFoundError`: ``contract_file`` declared in the
      manifest but the file is missing on disk. Deploy bug, 503.
    - :class:`ValueError`: malformed YAML. ``load_upgrade_contract``
      re-raises ``yaml.YAMLError`` as ``ValueError`` with the
      contract path in the message (see
      :func:`agent.upgrade_contract.load_upgrade_contract`). Codex
      post-merge review caught this gap — without it, a malformed
      YAML would 500 instead of the intended 503.
    """
    if resolution.spec.name != "upgrade":
        return
    if resolution.contract_path is None:
        # The upgrade workload's manifest declares
        # ``contract_file: contract.yaml`` (pinned by 17.C.1 tests), so
        # this branch is unreachable in a well-formed deploy. Belt-and-
        # suspenders for a future YAML refactor that drops the field.
        raise HTTPException(
            status_code=503,
            detail=(
                "upgrade workload manifest is missing contract_file; "
                "cannot validate upgrade contract"
            ),
        )
    # Lazy import — keeps the upgrade-contract module out of the drift
    # request path's import graph.
    from agent.upgrade_contract import load_upgrade_contract

    try:
        load_upgrade_contract(resolution.contract_path)
    except (
        UnknownUpgradeTargetError,
        PydanticValidationError,
        FileNotFoundError,
        ValueError,
    ) as e:
        raise HTTPException(
            status_code=503,
            detail=(
                f"upgrade contract not loadable: {e}. See Phase 17.C.1 "
                f"for the contract schema and UPGRADE_TARGET_REGISTRY "
                f"for the allowed target names."
            ),
        ) from e


def _read_runbook_content(s: Settings, target_in_repo: str) -> str:
    """Return the current runbook content.

    Currently reads from the local filesystem under ``DOCS_ROOT``. Phase 9 will
    swap this to fetch from the base branch via the GitHub Contents API so the
    Eventarc handler doesn't depend on the deployed container's filesystem
    being in sync with main. Keeping this as a function boundary so the swap
    only touches one site.
    """
    target_path = Path(s.docs_root) / target_in_repo
    if not target_path.exists():
        raise HTTPException(
            status_code=500,
            detail=(
                f"runbook not found at {target_path} "
                f"(check DOCS_ROOT and the contract's docs.file)"
            ),
        )
    return target_path.read_text()

app = FastAPI(title="DriftScribe Agent")

# Phase 15.2: bind a per-request trace id (UUIDv4 hex) from inbound
# ``X-Trace-Id`` (or mint one), echo on the response, and surface in
# every log line via the ContextVar in ``driftscribe_lib.logging``.
# Worker calls in ``agent.worker_client`` read the same ContextVar to
# propagate the trace id downstream.
install_trace_middleware(app)


# Jinja2 templates for the HITL approval page (Phase 11.7). Mounted at
# import time so a typo in the directory path fails fast at boot rather
# than on the first /approvals GET. The template directory lives inside
# the agent package so a single ``pip install -e .`` or Cloud Build
# COPY ships it alongside the Python sources.
_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# Endpoints that handle the HITL approval token MUST set these headers
# on every response (GET render + POST decision). The token may appear
# in the URL (?t=<raw_token>) and in the form body; the headers below
# minimize the surfaces where it could leak.
#
# - ``Cache-Control: no-store``: no proxy / browser cache holds a
#   response that contained the token in the URL.
# - ``Referrer-Policy: no-referrer``: a link followed from this page
#   does NOT include the token-bearing URL in the Referer header.
# - ``X-Frame-Options: DENY``: prevents clickjacking — an attacker
#   cannot iframe the approval page in a phishing site to trick the
#   operator into clicking "Approve".
#
# Configured per-response (not as global middleware) so other routes
# (/healthz, /chat, /recheck) get FastAPI's default header set unchanged.
def _apply_approval_security_headers(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    return response


# Strict Content-Security-Policy for the C5e ``/iac-approvals`` pages (Phase
# C5e-2). The page is fully self-contained: inline ``<style>`` only, a same-origin
# form, no scripts, no images, no remote anything. We pin the CSP accordingly so a
# stored-XSS-style injection into the rendered plan/diff text cannot exfiltrate or
# escalate:
# - ``default-src 'none'``  — deny everything not explicitly allowed.
# - ``style-src 'unsafe-inline'`` — the only inline content we ship is the
#   ``<style>`` block (Jinja autoescaping covers the dynamic plan/diff text).
# - ``form-action 'self'`` — the Approve/Reject POST may only target this origin
#   (a CSP-level companion to the POST handler's exact-Origin check in C5e-3).
# - ``base-uri 'none'`` / ``frame-ancestors 'none'`` — no ``<base>`` hijack, no
#   framing (defense-in-depth alongside ``X-Frame-Options: DENY``).
def _apply_iac_csp(response: Response) -> Response:
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )
    return response


_state_singleton: StateStore | None = None


def get_state() -> StateStore:
    """Return the process-wide StateStore singleton.

    Picks InMemoryStateStore in DRY_RUN / no-project mode so tests and demos
    don't touch GCP; otherwise FirestoreStateStore.
    """
    global _state_singleton
    if _state_singleton is None:
        s = get_settings()
        if s.dry_run or not s.gcp_project:
            _state_singleton = InMemoryStateStore()
        else:
            _state_singleton = FirestoreStateStore(project=s.gcp_project)
    return _state_singleton


def _reset_state_for_tests() -> None:
    """Test helper — drop the cached state singleton.

    Not exposed to production callers. The integration test conftest uses
    this so each test starts with an empty in-memory store.
    """
    global _state_singleton
    _state_singleton = None


_trace_fetcher_singleton: TraceFetcher | None = None


def get_trace_fetcher() -> TraceFetcher:
    """Return the process-wide TraceFetcher singleton.

    Picks StubTraceFetcher in DRY_RUN / no-project mode so tests and demos
    don't touch GCP; otherwise CloudLoggingFetcher backed by
    google-cloud-logging.

    NOTE: per-process, best-effort. Not a correctness boundary —
    multi-process workers each have their own singleton. Acceptable because
    /trace's source of truth is Cloud Logging; the singleton just amortizes
    client construction.
    """
    global _trace_fetcher_singleton
    if _trace_fetcher_singleton is None:
        s = get_settings()
        if s.dry_run or not s.gcp_project:
            _trace_fetcher_singleton = StubTraceFetcher()
        else:
            _trace_fetcher_singleton = CloudLoggingFetcher(project=s.gcp_project)
    return _trace_fetcher_singleton


def _reset_trace_fetcher_for_tests() -> None:
    """Test helper — drop the cached TraceFetcher singleton.

    Mirrors ``_reset_state_for_tests``. The integration conftest calls this
    on setup and teardown so each test gets a fresh StubTraceFetcher.
    """
    global _trace_fetcher_singleton
    _trace_fetcher_singleton = None


# --------------------------------------------------------------------------- #
# /trace/{trace_id} — completion-aware caching + redact-at-render
# --------------------------------------------------------------------------- #
#
# Module-level — NOT per-request — so threads are reused. Single worker
# would suffice (each ``get_trace`` runs on FastAPI's own threadpool
# because the route is ``def``, not ``async def``), but ``max_workers=4``
# lets a small burst of concurrent operator polls each get their own
# fetch in flight rather than serializing through one worker. The only
# reason this nested executor exists is to provide a real
# ``Future.result(timeout=...)`` boundary that the sync
# google-cloud-logging client lacks natively (its ``list_entries`` has
# no timeout kwarg in 3.15.x — see CloudLoggingFetcher's docstring).
#
# Lifetime: created at import time, never shut down. Acceptable for
# Cloud Run process-lifetime — the container exits when the request
# stops flowing and the OS reclaims the threads. Avoiding ``atexit``
# keeps pytest from hanging on an executor that thinks a slow fetch
# is still in progress at test teardown.
_TRACE_FETCH_EXECUTOR = ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="trace-fetch"
)
_TRACE_FETCH_TIMEOUT_S = 5.0

# In-process completion cache. Keyed by trace_id; value is (written_at,
# payload). Only completed-AND-stable timelines are cached (see
# ``_observe_and_check_stability`` below) — in-flight traces refetch
# every poll so the UI sees fresh events.
_TRACE_CACHE: dict[str, tuple[float, dict]] = {}
_TRACE_CACHE_TTL_S = 300.0

# Observed-stability: how long the SAME timeline signature has held in
# our own observations. Required because Cloud Logging documents a
# 0-60s live-tail buffer where entries can arrive out of order — using
# log-event timestamps to decide "the timeline has settled" fails when
# a late-arriving ``final_response`` carries a 30-second-old timestamp
# and we'd otherwise return ``complete=True`` on the first poll
# (Codex v2 review CRITICAL). Tracking stability in PROCESS state
# (monotonic clock + signature of the events) closes that hole.
_STABILITY_GRACE_S = 30.0
_TRACE_OBSERVATIONS: dict[str, tuple[float, str]] = {}

# Soft cap on observation state. A trace polled once with
# ``final_response`` but never polled again leaves an observation
# entry forever — under operator-burst patterns (many traces, each
# observed exactly once) this is an unbounded slow leak. FIFO
# eviction by insertion order (dict iteration order is insertion
# order since Python 3.7) keeps the dict bounded with negligible
# per-insert cost. Sizing: 1024 entries × ~1 KiB/entry ≈ 1 MiB
# ceiling, well below any realistic operator burst the coordinator
# would see in a single Cloud Run process lifetime.
_OBSERVATIONS_SOFT_CAP = 1024


def _signature_of(events: list[dict]) -> str:
    """Hash over every event's identity tuple.

    Codex v3 IMPORTANT: a previous cheap signature of
    ``(count, last_(timestamp, insert_id))`` missed rare same-count
    replacement cases (e.g. ``max_results`` clipping the tail or a
    re-ordering of same-count results that swaps two entries without
    changing the count). Hashing every event's
    ``(timestamp, insert_id, event)`` tuple catches any reordering or
    swap without growing the count.

    Codex v3.1 MINOR: JSON-encoded tuples eliminate delimiter
    ambiguity — a timestamp containing ``|`` could otherwise produce
    the same digest as two adjacent shifted fields if we used a
    sentinel separator. ``json.dumps`` with
    ``separators=(",", ":")`` produces a stable, unambiguous encoding.
    """
    h = hashlib.sha256()
    for e in events:
        h.update(
            json.dumps(
                [
                    e.get("timestamp", ""),
                    e.get("insert_id", ""),
                    e.get("event", ""),
                ],
                separators=(",", ":"),
            ).encode()
        )
    return h.hexdigest()


def _observe_and_check_stability(trace_id: str, events: list[dict]) -> bool:
    """Decide whether the timeline is complete via OBSERVED stability.

    Two conditions both required for ``complete=True``:

    1. A ``final_response`` event is present. The agent emits this
       near the end of every run (``_emit_llm_usage`` follows it for
       token accounting, so ``final_response`` is not strictly the
       very last entry — the 30-second grace window catches the
       usage emit and any other tail events).
    2. The signature (over every event) has been the SAME for at
       least :data:`_STABILITY_GRACE_S` of WALL-CLOCK time in OUR
       observations. NOT the log entry timestamps — those can arrive
       out of order from Cloud Logging.

    On a signature change, the observation resets — the new timeline
    has to hold steady for another full grace window before we'd cache
    it. On a "no final_response" poll, the observation is dropped
    entirely so a transient empty fetch doesn't pollute the next
    poll's stability check.
    """
    if not any(e.get("event") == "final_response" for e in events):
        _TRACE_OBSERVATIONS.pop(trace_id, None)
        return False

    sig = _signature_of(events)
    obs = _TRACE_OBSERVATIONS.get(trace_id)
    if obs is None or obs[1] != sig:
        # First observation of this signature. Record and refuse to
        # mark complete — the next poll will measure elapsed grace.
        #
        # FIFO eviction at the soft cap: a trace polled once with
        # ``final_response`` but never polled again would otherwise
        # leak an observation entry forever. dict iteration order is
        # insertion order (3.7+), so ``next(iter(...))`` is the
        # oldest. Eviction is best-effort under concurrency (two
        # racing inserts may both observe ``len < cap`` and push the
        # dict one over the cap for a moment) — acceptable, the cap
        # is a soft ceiling, not a security boundary.
        if len(_TRACE_OBSERVATIONS) >= _OBSERVATIONS_SOFT_CAP:
            oldest_key = next(iter(_TRACE_OBSERVATIONS))
            _TRACE_OBSERVATIONS.pop(oldest_key, None)
        _TRACE_OBSERVATIONS[trace_id] = (time.monotonic(), sig)
        return False

    first_seen_at, _sig = obs
    return (time.monotonic() - first_seen_at) >= _STABILITY_GRACE_S


def _cache_get(trace_id: str) -> dict | None:
    """Return the cached payload for ``trace_id`` or None.

    Best-effort under concurrent expiry: two concurrent requests on
    the same expired entry will both pop, both refetch, and both may
    ``_cache_put`` the resulting payload. Not a correctness boundary
    — last writer wins and the cached payload is a deterministic
    function of the trace_id once the timeline is stable. Documenting
    so a future reader doesn't mistake this for an atomicity
    guarantee.
    """
    hit = _TRACE_CACHE.get(trace_id)
    if hit is None:
        return None
    written_at, payload = hit
    if time.monotonic() - written_at > _TRACE_CACHE_TTL_S:
        _TRACE_CACHE.pop(trace_id, None)
        return None
    return payload


def _cache_put(trace_id: str, payload: dict) -> None:
    """Write a completed-AND-stable payload into the in-process cache.

    Best-effort under concurrent inserts: two concurrent requests
    that both observed the same expired/missing entry will both
    write; last writer wins. See :func:`_cache_get` for the full
    concurrency note.
    """
    _TRACE_CACHE[trace_id] = (time.monotonic(), payload)


def _reset_trace_state_for_tests() -> None:
    """Test helper — drop the /trace cache + observation state.

    Wired into the integration conftest's autouse fixture alongside the
    other reset hooks so each test gets a clean slate (no stability
    history carrying over from a sibling test).
    """
    _TRACE_CACHE.clear()
    _TRACE_OBSERVATIONS.clear()


def _event_key(
    trigger: str,
    service: str,
    contract_path: str,
    contract_hash: str,
    live_env: dict[str, str],
) -> str:
    """Derive a stable event key from the inputs that define a decision.

    Including ``live_env`` (normalized by sorted-key order) is the fix for the
    v1 bug where Beats B and C of the demo collided on a service-only hash.

    Including ``contract_hash`` (not just contract_path) means a contract edit
    while live env stays the same still invalidates the prior cached decision.
    """
    payload = {
        "trigger": trigger,
        "service": service,
        "contract_path": contract_path,
        "contract_hash": contract_hash,
        "live_env": dict(sorted(live_env.items())),
    }
    h = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    return f"{trigger}-{service}-{h}"


def _hash_contract(contract: OpsContract) -> str:
    """Stable hash of the contract's *content* (not just its path).

    Used as a component of the event key so editing the contract invalidates
    cached decisions even when the file path is unchanged.
    """
    blob = contract.model_dump_json()
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _cached_rollback_is_expired(cached: dict) -> bool:
    """Phase 13 Codex W2: a cached rollback decision past its 15-min TTL
    must be treated as a cache miss so ``/recheck`` re-proposes a fresh
    approval. Returning the stale URL would surface a dead link to the
    operator without any way to recover short of ``force=true``.

    Returns False for non-rollback cached decisions (their cache contract
    is unchanged) and for any malformed/missing ``expires_at`` (fail-safe
    toward "return the cached decision"; the worker's own /execute will
    refuse on its second-pass expiry check).
    """
    if cached.get("action") != "rollback":
        return False
    expires_at = cached.get("approval", {}).get("expires_at")
    if not expires_at:
        return False
    try:
        when = dt.datetime.fromisoformat(expires_at)
    except (TypeError, ValueError):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return when < dt.datetime.now(dt.timezone.utc)


@app.get("/healthz")
@app.get("/health")
def healthz():
    # `/health` is the externally reachable alias. Cloud Run's GFE reserves
    # paths ending in `z` (Cloud Run "Known issues") and intercepts `/healthz`
    # with its own 404 before the request reaches FastAPI — so any external
    # uptime check or runbook smoke must hit `/health` instead. Keep
    # `/healthz` for in-cluster / unit-test callers that already wired to it.
    return {"ok": True}


def _render_for(action: DecisionAction, proposal: DecisionProposal) -> str:
    if action == DecisionAction.NO_OP:
        return "(no action)"
    if action == DecisionAction.DOCS_PR:
        return render_docs_pr_body(proposal)
    if action == DecisionAction.DRIFT_ISSUE:
        return render_drift_issue_body(proposal)
    if action == DecisionAction.ESCALATION:
        return render_escalation_issue_body(proposal)
    if action == DecisionAction.ROLLBACK:
        # The ROLLBACK control flow is structurally different from the other
        # actions: propose-worker → render → notify. The approval URL is
        # minted by the worker's /propose response and is not derivable from
        # the proposal alone, so the renderer is called out-of-band from
        # _perform_action with the URL in hand (see Task 13.3).
        raise ValueError(
            "ROLLBACK is rendered out-of-band via render_rollback_body(p, "
            "approval_url); _render_for has no access to the approval URL"
        )
    raise ValueError(f"no renderer for action {action!r}")


def _perform_action(
    s: Settings, contract: OpsContract, proposal: DecisionProposal, rendered: str
) -> dict:
    """Execute the side effect for ``proposal.action``.

    Honors ``s.dry_run`` — when true, no GitHub calls are made and a preview
    dict is returned instead. Returns a structured result the caller exposes
    under the ``github`` field of the API response.
    """
    if proposal.action == DecisionAction.NO_OP:
        return {"dry_run": s.dry_run, "url": None, "action": "no_op"}

    repo = None if s.dry_run else get_repo(s.github_token, s.github_repo)
    diffs_str = ", ".join(d.name for d in proposal.env_diffs)

    if proposal.action == DecisionAction.DRIFT_ISSUE:
        return open_drift_issue(
            repo=repo,  # type: ignore[arg-type]
            title=f"[DriftScribe] Drift: {diffs_str}",
            body=rendered,
            dry_run=s.dry_run,
        )

    if proposal.action == DecisionAction.ESCALATION:
        return open_escalation_issue(
            repo=repo,  # type: ignore[arg-type]
            title=f"[DriftScribe] Review: {diffs_str}",
            body=rendered,
            dry_run=s.dry_run,
        )

    # DOCS_PR. The validator has already guaranteed target_docs_file +
    # target_docs_section are set, so we can read them confidently.
    assert proposal.target_docs_file is not None  # validator-enforced
    assert proposal.target_docs_section is not None
    target_in_repo = proposal.target_docs_file
    current = _read_runbook_content(s, target_in_repo)
    new_content = patch_runbook(current, proposal.env_diffs, contract)

    # Timestamp + random suffix so retries / parallel deliveries don't collide
    branch = (
        f"driftscribe/{_branch_slug(proposal.env_diffs[0].name)}"
        f"-{int(time.time())}-{secrets.token_hex(2)}"
    )
    return open_docs_pr(
        repo=repo,  # type: ignore[arg-type]
        branch=branch,
        base="main",
        title=f"docs(driftscribe): update {proposal.env_diffs[0].name}",
        body=rendered,
        file_path=target_in_repo,
        new_content=new_content,
        dry_run=s.dry_run,
    )


async def _run_adk_agent(
    user_msg: str, *, workload: str = "drift"
) -> DecisionProposal:
    """Thin wrapper so integration tests have a stable patch target.

    Lazy-imports `agent.adk_agent` so the Google ADK SDK doesn't load on the
    non-ADK code path. Patching `agent.main._run_adk_agent` (rather than
    `agent.adk_agent.run_agent`) preserves the lazy-load benefit AND keeps
    the test patch site stable across spec evolution.

    ``workload`` selects the workload-scoped agent. Defaults to ``"drift"``
    so any pre-17.A.3 patch site that calls this with a positional
    ``user_msg`` only still works.
    """
    from agent.adk_agent import run_agent

    return await run_agent(user_msg, workload=workload)


def _do_rollback(
    s: Settings,
    proposal: DecisionProposal,
    event_key: str,
    trigger: str,
) -> dict:
    """ROLLBACK control flow: propose-via-worker → render → notify-via-worker.

    Returns the same shape as the other ``_do_recheck`` actions, EXCEPT the
    ``github`` key is replaced with ``approval`` — rollback's side effect is
    an HMAC-bound approval URL minted by the Rollback Worker, not a GitHub
    object. The schema divergence is intentional: ``github`` would be a lie
    here (no PR/issue was opened), and unioning it with ``approval`` would
    invite "the github field is null but maybe set" branchy reader code.

    Ordering vs. the non-rollback path:

    - Other actions: ``render → claim_event → perform_action``. The render is
      a pure function of the proposal, so it runs first to fail-fast on a
      bad proposal without touching state.
    - ROLLBACK: ``claim_event → propose → render → notify``. Render REQUIRES
      the approval URL from the worker's response, so it cannot run until
      the propose call has succeeded. Claiming the event BEFORE propose means
      a concurrent retry can't double-mint approval docs. On any worker
      failure the claim is released so retries can proceed.

    Phase 13 HITL safety property (Phase 11.9 carry-over #3): there is NO
    code path in this function that calls Cloud Run's admin API. The
    coordinator only mints an approval doc + URL and asks the Notifier to
    deliver it. Cloud Run traffic only shifts when the operator clicks
    Approve and the existing ``/approvals/{id}`` POST handler routes through
    ``worker_client.call_execute``. The integration test in
    ``tests/integration/test_rollback_e2e.py`` pins this explicitly.

    ``dry_run`` semantics (intentional, not a bug): even with ``DRY_RUN=true``
    we still call the rollback worker's ``/propose`` so the approval URL
    exists and the demo flow shows the operator-facing payoff. The actual
    Cloud Run mutation lives behind the worker's ``/execute`` endpoint
    (operator-triggered), so dry-run-ness at the coordinator can't gate it
    from here; it's the rollback worker's responsibility to decide whether
    ``/execute`` should be a no-op in a dry-run-target deployment. Out of
    scope for Task 13.3 — worker code is not modified in this task.
    """
    # Defensive: the classifier never produces ROLLBACK (no rollback branch
    # exists in agent/classifier.py); only the ADK path can emit it. If we
    # ever reach this with USE_ADK=false, the deploy is broken — a 500 is
    # the right surface so the on-call sees it as a coordinator bug, not an
    # upstream failure.
    if not s.use_adk:
        raise HTTPException(
            status_code=500,
            detail=(
                "rollback action emitted on non-ADK path — only the ADK "
                "agent should produce rollback decisions"
            ),
        )

    state = get_state()
    claimed = state.record_event(event_key, {"trigger": trigger})
    if not claimed:
        existing = state.find_decision_for_event(event_key)
        if existing:
            return existing
        raise HTTPException(status_code=409, detail="event in-progress, retry")

    # Side effect #1: mint the approval via the Rollback Worker. The worker
    # owns the HMAC key, the Firestore approvals collection write, and the
    # TTL; the coordinator only receives the resulting URL.
    try:
        propose_result = worker_client.call(
            "rollback",
            {
                "target_revision": proposal.target_revision,
                "reason": proposal.rationale,
            },
        )
    except WorkerClientError as e:
        # Worker propose failed (auth, schema, or transport). Release the
        # claim so a retry can mint a fresh approval; the prior doc (if the
        # worker partially wrote one before failing) is bounded by its 15-min
        # TTL and was never surfaced to the operator (no notification sent).
        state.release_event(event_key)
        raise HTTPException(
            status_code=502, detail=f"rollback propose failed: {e}"
        ) from e

    approval_url = propose_result.get("approval_url")
    approval_id = propose_result.get("approval_id")
    expires_at = propose_result.get("expires_at")
    if not approval_url or not approval_id:
        # Malformed worker response — bail rather than render a broken body.
        # Release the claim so the operator can retry once the worker is fixed.
        state.release_event(event_key)
        raise HTTPException(
            status_code=502,
            detail=(
                "rollback worker response missing approval_url/approval_id; "
                "refusing to render incomplete approval body"
            ),
        )

    # render_rollback_body is a pure function over the proposal + URL, so it
    # *shouldn't* raise — but if a future renderer change introduces a code
    # path that does, we must release the claim. Without this, a renderer
    # exception would leave the event claimed and perma-409 subsequent retries.
    try:
        rendered = render_rollback_body(proposal, approval_url)
    except Exception as e:
        state.release_event(event_key)
        raise HTTPException(
            status_code=500, detail=f"rollback render failed: {e}"
        ) from e

    # Side effect #2: ask the Notifier worker to deliver the rendered body
    # to the operator-facing channel. severity="high" tracks the approval-
    # required nature; channel="approval" routes to the operator inbox.
    #
    # On notifier failure we release the claim and 502. The orphan approval
    # doc in Firestore (now invisible to the operator) is bounded by its
    # 15-min TTL — at-least-once semantics, with the next retry minting a
    # fresh approval. Operationally: an operator who already received the
    # webhook before the worker reported failure could still see both the
    # original and the retry approval as pending; that's HITL-safe (the
    # operator can deny either) but worth knowing about.
    try:
        worker_client.call(
            "notifier",
            {"channel": "approval", "severity": "high", "body": rendered},
        )
    except WorkerClientError as e:
        state.release_event(event_key)
        raise HTTPException(
            status_code=502, detail=f"rollback notify failed: {e}"
        ) from e

    decision_id = str(uuid.uuid4())
    # Schema divergence vs. other actions: "approval" replaces "github". The
    # ``approval_token`` is intentionally NOT echoed here — it's already
    # embedded in approval_url as ``?t=<token>``, and exposing it as a
    # separate field would double the leak surface. See Phase 13.3 task spec.
    response = {
        "decision_id": decision_id,
        "event_key": event_key,
        # 19.A.4: stamp the inbound (or freshly-minted) trace_id onto the
        # decision document so the past-decisions UI (19.B.6) can deep-link
        # to ``/trace/{trace_id}``. Read from the ContextVar bound by the
        # FastAPI middleware on this request; ``current_trace_id_or_new``
        # mints a fresh hex32 if for any reason the binding is missing or
        # malformed, so the field is never empty in the persisted doc.
        "trace_id": current_trace_id_or_new(),
        "action": "rollback",
        # Hardcoded "adk" — the classifier doesn't emit rollback (see the
        # defensive guard above). When we eventually add a classifier branch
        # for rollback, swap to the same conditional as _do_recheck.
        "decision_path": "adk",
        "rendered_body": rendered,
        "rationale": proposal.rationale,
        "diffs": [d.model_dump(mode="json") for d in proposal.env_diffs],
        "target_revision": proposal.target_revision,
        "requires_human_review": True,
        # ``dry_run`` reflects the coordinator setting, BUT for the rollback
        # action it does NOT suppress the worker calls — propose + notify
        # both run so the demo can show the approval URL. The actual Cloud
        # Run mutation is gated by the operator clicking /approvals/{id}.
        # ``dry_run_effective`` is the unambiguous "did any side effect
        # happen?" answer: False because workers were called and a real
        # approval doc was minted in Firestore.
        "dry_run": s.dry_run,
        "dry_run_effective": False,
        "approval": {
            "approval_id": approval_id,
            "approval_url": approval_url,
            "expires_at": expires_at,
        },
        "trigger": trigger,
    }
    state.record_decision(decision_id, event_key, response)
    return response


# Workloads with no autonomous /recheck pipeline — chat-only by design.
# ``explore`` is strictly read-only and exists only as a free-form /chat
# surface; it has no DecisionProposal renderer / observation pass, so
# /recheck refuses it early (see the guard at the top of _do_recheck).
# Kept as an explicit set (not a schema flag) to mirror the inline
# upgrade /recheck refusal below — both are routing facts owned here.
CHAT_ONLY_WORKLOAD_NAMES: frozenset[str] = frozenset({"explore"})


async def _do_recheck(
    trigger: str, force: bool = False, *, workload: str = "drift"
) -> dict:
    """Run a recheck under the trigger label, with idempotency.

    Idempotency contract:
    - Computes ``event_key`` from trigger + service + contract_path +
      contract_hash + live_env. The contract hash means edits to the contract
      invalidate cached decisions even when the file path stays the same.
    - If the key is already known and ``force`` is false, returns the cached
      decision (so retries don't spawn duplicate PRs/issues).
    - Claims the event_key BEFORE invoking GitHub side effects. If the claim
      is refused (concurrent recheck won the race), returns the recorded
      decision if available, else 409.
    - On side-effect failure, releases the claim so a subsequent retry can
      proceed. The patcher's atomic pre-check + the github branch random
      suffix mean a retry doesn't create duplicate state.
    - ``force=true`` derives a brand-new event_key (suffixed with a random
      shortuuid) so the fresh decision is cached under a distinct key. Later
      unforced retries still compute the base key and find the prior base-key
      decision if one exists; the forced decision is only retrievable via its
      own decision_id.

    Async on the outer frame only — the ADK agent's `run_agent` is async, but
    `classify`, `validate`, `_render_for`, and `_perform_action` stay sync.
    """
    # Chat-only workloads have NO autonomous /recheck path. This guard
    # fires FIRST — before settings load and before load_workload — on
    # purpose: explore's manifest lists read workers whose URL env vars
    # may be unset in a given deploy, so resolving it first would surface
    # a misleading "workload not deployed" 503 instead of the honest
    # "chat-only, no autonomous path" reason. The invariant is "no
    # /recheck for chat-only, regardless of deploy wiring" — so it must
    # not depend on settings or resolution. (Codex review 2026-05-25.)
    # /chat is the only surface for these.
    if workload in CHAT_ONLY_WORKLOAD_NAMES:
        raise HTTPException(
            status_code=503,
            detail=(
                f"/recheck workload={workload!r} is not available: it is a "
                f"chat-only workload with no autonomous /recheck path. "
                f"Use /chat for {workload}."
            ),
        )

    s = get_settings()

    # Phase 17.A.3 (Codex review): workload pre-resolve runs BEFORE
    # contract load, BEFORE the USE_ADK branch, BEFORE any worker
    # call. The earlier Codex review caught a leak where
    # ``/recheck`` with ``workload=upgrade`` while ``USE_ADK=false``
    # silently fell through to the classifier path and ran drift's
    # logic. Pre-resolving here means BOTH paths surface 503 on an
    # undeployed workload, with a single uniform message.
    #
    # The resolution is also useful for surfacing "this workload's
    # contract file lives at X" once 17.C wires non-drift contracts
    # — out of scope for 17.A.3, but the seam is here. For drift,
    # ``s.contract_path`` is still the source of truth.
    try:
        resolution = load_workload(workload)
    except (
        MissingWorkerEnvError,
        ReservedToolNotImplementedError,
        MissingDeveloperKnowledgeApiKeyError,
    ) as e:
        # ``MissingDeveloperKnowledgeApiKeyError`` is a "deploy not
        # wired" condition (the Secret Manager binding for the
        # Developer Knowledge API key is missing), structurally
        # identical to the worker-env case above — same operator
        # surface, same 503. Kept as an explicit tuple addition
        # rather than inheriting from ``MissingWorkerEnvError``
        # because the developer-knowledge key is NOT a worker env
        # var; collapsing the hierarchies would muddy the exception
        # taxonomy for one shared status code.
        raise HTTPException(
            status_code=503,
            detail=(
                f"workload {workload!r} is not deployed: {e}. "
                f"See Phase 17.B/17.C/17.E for the wiring that lands "
                f"upgrade's tools and worker URLs."
            ),
        ) from e

    # Phase 17.C.4 (Codex 2026-05-20 follow-up): eagerly parse the
    # upgrade contract on every request so a bad contract surfaces as
    # a clean 503 at request entry rather than a mid-conversation
    # runtime error after the LLM has begun reasoning. No-op for
    # drift; see :func:`_eager_resolve_upgrade_contract`.
    _eager_resolve_upgrade_contract(resolution)

    # Phase 17.A (Codex review, Fix Important #1): the classifier-path
    # non-drift refusal must fire BEFORE the drift contract load below.
    # The contract is drift-specific (``s.contract_path`` is co-designed
    # with the drift classifier); reading it on a non-drift request that
    # we're about to refuse anyway would 500 on a broken/missing contract
    # before the 503 fires — masking the real "wrong path for this
    # workload" diagnosis with a misleading "contract load failed".
    #
    # The previous ordering happened to be safe today because the drift
    # contract is always present in the test/prod deploy, but the moment
    # ``load_workload("upgrade")`` starts succeeding (17.E) a broken
    # drift contract would surface as 500 here instead of the intended
    # 503. See the matching test in
    # tests/integration/test_workload_routing.py.
    #
    # The ADK path doesn't fire this guard — :func:`build_agent`/
    # :func:`build_chat_agent` already select the workload-specific tool
    # set, so an upgrade request on USE_ADK=true is routed correctly.
    if not s.use_adk and workload != "drift":
        raise HTTPException(
            status_code=503,
            detail=(
                f"workload {workload!r} requires the ADK path (USE_ADK=true). "
                f"The classifier path is drift-only by design — see "
                f"agent.classifier.classify, which is co-designed with "
                f"the drift contract+live-env shape."
            ),
        )

    # Phase 17.C.4 (Codex post-merge review — blocker): explicit 503 on
    # ``/recheck workload=upgrade``. The upgrade /recheck execution
    # path is intentionally NOT implemented in Phase 17: today's
    # _do_recheck post-agent plumbing below (drift OpsContract load,
    # drift validator with its env_diffs-required rule, drift
    # _render_for / _perform_action with no UPGRADE_PR branch, drift
    # reader for live_env hashing) would reject or crash on any upgrade
    # DecisionProposal even though /chat already routes upgrade cleanly
    # via the ADK runner. Failing fast here keeps the routing invariant
    # "upgrade excludes drift reader / rollback surfaces" honest —
    # without this guard, /recheck would build a drift-shaped user_msg,
    # call the drift Reader Worker, and then bounce inside the drift
    # validator with a misleading message. /chat is the supported
    # upgrade surface in this build; a workload-specific /recheck (with
    # an upgrade-shaped DecisionProposal renderer and an upgrade-side
    # observation pipeline) is post-Phase-17 work.
    #
    # Ordered AFTER the classifier-path refusal above so the more-
    # specific "use ADK" message still fires for USE_ADK=false.
    if workload == "upgrade":
        raise HTTPException(
            status_code=503,
            detail=(
                "/recheck workload='upgrade' is not implemented in this "
                "build: the post-agent plumbing (contract load, validator, "
                "renderer, perform_action) is drift-specific. Use /chat "
                "for upgrade; a workload-specific /recheck pipeline is "
                "post-Phase-17 work."
            ),
        )

    try:
        contract = load_contract(Path(s.contract_path))
    except Exception as e:
        # Bad contract = our deploy is broken, not GCP. 500, not 502.
        raise HTTPException(status_code=500, detail=f"contract load failed: {e}")

    if s.use_adk:
        # ADK path: the agent's own tool calls do the Cloud Run read, so we
        # don't pre-fetch live_env. We still need a live_env-shaped dict for
        # the idempotency hash, so we attempt one read here and fall back to
        # deriving it from the proposal's diffs if Cloud Run refuses us.
        user_msg = (
            f"Detect drift for Cloud Run service `{s.target_service}` in "
            f"region `{s.target_region}` (GCP project `{s.gcp_project}`). "
            f"The contract path is `{s.contract_path}`. "
            f"GitHub repo for PR history is `{s.github_repo}`. "
            f"/debug/config URL: `{s.debug_config_url or 'not provided'}`."
        )
        # COST NOTE: on USE_ADK=true we run the agent BEFORE the idempotency-
        # cache lookup further down — every retry pays the Gemini cost even if
        # the prior decision was already cached. This is because the cache key
        # includes live_env, which the agent itself produces. Two cheaper
        # designs — (a) cache on (trigger, service, contract_hash) only and
        # accept weaker idempotency, or (b) pre-call read_live_env even on the
        # ADK path to compute the key first — are deferred to Phase 9 along
        # with the Eventarc handler so retry storms don't break the bank.
        #
        # Phase 17.B.4 follow-up: bind the *caller* workload identity to
        # the ContextVar read by the Developer Knowledge MCP wrapper's
        # structured log. Distinct from ``mcp_server`` (which MCP we
        # called) — ``workload`` is who asked us to call it. Together
        # they let the operator dashboards slice latency/failures by
        # caller. The inner ``try/finally`` keeps the binding scoped to
        # the agent call so a concurrent ``/recheck`` running another
        # workload on the same event loop sees its own ContextVar
        # snapshot per :pep:`567`. The outer ``try/except`` catches
        # whatever propagates out of ``_run_adk_agent`` (the reset
        # already ran in the finally). Pin in
        # ``tests/integration/test_workload_contextvar_propagation.py``.
        _workload_token = set_workload(workload)
        try:
            try:
                proposal = await _run_adk_agent(user_msg, workload=workload)
            finally:
                reset_workload(_workload_token)
        except (
            MissingWorkerEnvError,
            ReservedToolNotImplementedError,
            MissingDeveloperKnowledgeApiKeyError,
        ) as e:
            # Workload's wiring isn't complete in this build (e.g.
            # upgrade before 17.B/17.C/17.E). The request is
            # structurally valid; the system isn't deployed for that
            # workload. 503 with a clear message so the operator can
            # self-diagnose. See the matching catch on /chat below for
            # the rationale on the split between this and
            # :class:`UnknownToolError` (which stays 500-shaped: a
            # drift YAML typo is a deploy bug, not a deploy ordering
            # issue).
            raise HTTPException(
                status_code=503,
                detail=(
                    f"workload {workload!r} is not deployed: {e}. "
                    f"See Phase 17.B/17.C/17.E for the wiring that lands "
                    f"upgrade's tools and worker URLs."
                ),
            ) from e
        except Exception as e:
            # LLM produced no parseable JSON, or schema-validation failed.
            # Distinct from a side-effect failure — surface as upstream-dep
            # failure (502) so the caller knows to retry rather than fix.
            raise HTTPException(status_code=502, detail=f"adk agent failed: {e}")
        try:
            # Reader Worker enforces TARGET_SERVICE/region/project via its own
            # boot config (Layer 2); the coordinator no longer passes them.
            live_env = worker_client.call("reader", {})["env"]
        except Exception:
            # Trade-off: when the Reader Worker read fails on the ADK path we
            # hash the diffs the LLM reported instead of the actual live env.
            # That's weaker idempotency (the LLM's tool call already saw the
            # live state, but we can't observe that here), but it lets the
            # demo proceed even when /run.services.get permission is missing.
            # Sentinel `<ABSENT>` keeps live=None distinct from live="" so the
            # event_key doesn't bucket two genuinely-different states together
            # (Cloud Run treats empty-string-as-value as a valid live state).
            live_env = {
                d.name: "<ABSENT>" if d.live is None else d.live
                for d in proposal.env_diffs
            }
    else:
        try:
            # Reader Worker enforces TARGET_SERVICE/region/project via its own
            # boot config (Layer 2); the coordinator no longer passes them and
            # no longer holds project-wide roles/run.viewer (Phase 13 trim).
            live_env = worker_client.call("reader", {})["env"]
        except WorkerClientError as e:
            # Same 502 semantics as before — a Reader Worker failure is still
            # an upstream-dep failure from the operator's POV. The classifier
            # path has no fallback; without live_env we cannot classify.
            raise HTTPException(status_code=502, detail=f"reader worker failed: {e}")
        proposal = classify(
            ClassificationInput(contract=contract, live_env=live_env, recent_prs=[])
        )

    contract_hash = _hash_contract(contract)
    event_key = _event_key(
        trigger, s.target_service, s.contract_path, contract_hash, live_env
    )
    if force:
        # Distinct key so the forced decision is cached under its own slot
        # without overwriting the base key's record.
        event_key = f"{event_key}-force-{uuid.uuid4().hex[:8]}"

    state = get_state()
    if not force:
        existing = state.find_decision_for_event(event_key)
        if existing:
            if _cached_rollback_is_expired(existing):
                # Phase 14 (Codex Phase 13 second-pass W2): compare-and-
                # delete instead of unconditional release. Two concurrent
                # retries seeing the same expired decision would otherwise
                # both release+re-claim, double-minting approval docs.
                # The CAS only deletes when the cached decision_id still
                # matches; the loser re-reads and returns the winner's
                # fresh decision rather than re-proposing.
                cached_decision_id = existing.get("decision_id")
                if cached_decision_id and state.evict_cached_decision(
                    event_key, cached_decision_id
                ):
                    pass  # CAS won — fall through to re-propose
                else:
                    # Phase 15.3: CAS-loser short-circuit (Codex carry-over
                    # from Phase 14). If the re-read finds the winner's
                    # fresh decision, return it. Otherwise the winner is
                    # mid-flight: do NOT fall through to record_event —
                    # that path could succeed (event slot transiently
                    # empty between winner's evict and re-claim) and
                    # mint a duplicate /propose. Surface 409 so the
                    # caller retries cleanly.
                    existing = state.find_decision_for_event(event_key)
                    if existing and not _cached_rollback_is_expired(existing):
                        return existing
                    raise HTTPException(
                        status_code=409,
                        detail="event in-progress, retry",
                    )
            else:
                return existing

    try:
        validate(proposal, contract)
    except ProposalValidationError as e:
        # ADK path: the LLM produced a proposal that violates the safety
        # rules (e.g. docs_pr for a SECRET-named var, allow_manual_change
        # violation). Surface as 502 with a distinguishable detail so logs
        # disambiguate from a Cloud Run / ADK transport failure.
        # Deterministic-classifier path: this should never happen — the
        # classifier and validator are co-designed. If it does, the deploy
        # is broken (500).
        if s.use_adk:
            # Hint at non-retryability in the detail: the model responded, but
            # the deterministic safety gate refused the proposal. Mechanical
            # retry without prompt/model changes is unlikely to fix it.
            raise HTTPException(
                status_code=502,
                detail=f"adk proposal rejected by safety gate: {e}",
            )
        raise HTTPException(status_code=500, detail=f"validator rejected proposal: {e}")

    # ROLLBACK branches out before render because the render needs the
    # approval URL minted by the Rollback Worker's /propose. The Phase 11.9
    # carry-over #3 safety property — no rollback executes without operator
    # approval — lives in _do_rollback: it only proposes + notifies, never
    # mutates Cloud Run.
    if proposal.action == DecisionAction.ROLLBACK:
        return _do_rollback(s, proposal, event_key, trigger)

    rendered = _render_for(proposal.action, proposal)

    # Claim the event BEFORE any side effects so retries don't spawn duplicate
    # PRs/issues. If the claim is refused (race), look up the recorded
    # decision; if no decision yet, surface 409 so the caller can retry.
    claimed = state.record_event(event_key, {"trigger": trigger})
    if not claimed:
        existing = state.find_decision_for_event(event_key)
        if existing:
            return existing
        raise HTTPException(status_code=409, detail="event in-progress, retry")

    try:
        github_result = _perform_action(s, contract, proposal, rendered)
    except HTTPException:
        # Side effect failed — release the claim so retries can proceed.
        # The patcher's atomic pre-check + branch random suffix mean a retry
        # won't create duplicate partial state.
        state.release_event(event_key)
        raise
    except Exception as e:
        state.release_event(event_key)
        raise HTTPException(status_code=502, detail=f"side effect failed: {e}")

    decision_id = str(uuid.uuid4())
    response = {
        "decision_id": decision_id,
        "event_key": event_key,
        # 19.A.4: stamp the inbound (or freshly-minted) trace_id onto the
        # decision document so the past-decisions UI (19.B.6) can deep-link
        # to ``/trace/{trace_id}``. Read from the ContextVar bound by the
        # FastAPI middleware on this request; ``current_trace_id_or_new``
        # mints a fresh hex32 if for any reason the binding is missing or
        # malformed, so the field is never empty in the persisted doc.
        "trace_id": current_trace_id_or_new(),
        "action": proposal.action.value,
        # Tells demo viewers / on-call which engine produced this proposal.
        # The deterministic validator gates BOTH paths the same way, so this
        # is purely a provenance label, not a safety boundary.
        "decision_path": "adk" if s.use_adk else "classifier",
        "rendered_body": rendered,
        "rationale": proposal.rationale,
        "diffs": [d.model_dump(mode="json") for d in proposal.env_diffs],
        "target_docs_file": proposal.target_docs_file,
        "target_docs_section": proposal.target_docs_section,
        "requires_human_review": proposal.requires_human_review,
        "dry_run": s.dry_run,
        "github": github_result,
        "trigger": trigger,
    }
    state.record_decision(decision_id, event_key, response)
    return response


class RecheckRequest(BaseModel):
    """Optional request body for /recheck.

    Phase 17.A.3 adds a ``workload`` selector so an operator can target
    drift vs. upgrade per call. Pre-17 callers (curl in the demo, every
    existing integration test) POSTed without a body — the model is
    fully optional via the ``RecheckRequest | None = None`` body
    declaration on the route below. ``extra="forbid"`` so a typo'd
    field surfaces as 422 rather than silently dropping to defaults.

    ``force`` stays as a query param (its pre-17 location) to keep the
    integration tests' ``client.post("/recheck?force=true")`` form
    working without a body shape change.
    """

    workload: Literal["drift", "upgrade", "explore"] = "drift"

    model_config = ConfigDict(extra="forbid")


@app.post("/recheck")
async def recheck(
    req: RecheckRequest | None = None,
    force: bool = False,
    _: None = Depends(verify_token),
):
    # ``verify_token`` runs first and raises 401/403/503 before _do_recheck.
    # The unused-parameter underscore is the standard FastAPI convention for
    # auth deps that only matter for their side effect (raising on failure).
    workload = (req or RecheckRequest()).workload
    return await _do_recheck("manual_recheck", force=force, workload=workload)


# Module-level Google auth transport: verify_oauth2_token needs a transport
# instance to fetch Google's signing-key JWKS. Constructing it once at import
# time avoids allocating a new ``requests.Session`` per /eventarc call.
_GOOGLE_AUTH_TRANSPORT = GoogleAuthRequest()


@app.post("/eventarc")
async def eventarc(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict:
    """Eventarc auto-trigger entrypoint (Phase 14.2).

    Cloud Run audit logs flow:
    ``audit log → Eventarc trigger → POST /eventarc with CloudEvent body``.

    Auth model (Layer 1, per ``docs/architecture/multi-agent-design.md``):
    Eventarc mints an ID token against
    ``eventarc-trigger-sa@<gcp_project>.iam.gserviceaccount.com``, audience-
    bound to this Cloud Run service's URL. We verify the token via
    ``google.oauth2.id_token.verify_oauth2_token`` and require the verified
    ``email`` claim to match the expected trigger SA. This is defense-in-depth
    on top of the IAM ``roles/run.invoker`` binding: even if the binding
    accidentally widened, only Eventarc-trigger-SA-signed tokens get past
    this handler.

    Status-code contract:

    - **401** — Authorization header missing, not Bearer-shaped, or
      ``verify_oauth2_token`` raises (bad signature, wrong audience,
      expired). Eventarc will retry on 401, which is the right behavior
      for a transient JWKS / clock-skew issue.
    - **403** — token verifies but the ``email`` claim is not the
      eventarc-trigger SA. Detail does NOT echo the presented email.
    - **503** — server-side config missing (``EVENTARC_AUDIENCE`` or
      ``GCP_PROJECT`` unset). Fail-closed canary, same pattern as
      ``agent/auth.py``'s ``DRIFTSCRIBE_TOKEN`` check.
    - **200 ignored (malformed-payload)** — body cannot be parsed, or
      ``resource.labels`` is missing / empty. Phase 15.3 (Codex carry-over
      from Phase 14): we previously returned 400 here, but Eventarc retries
      on 4xx in some paths and a future audit-log schema change could
      trigger a retry storm. Acknowledge delivery with 200 + a short
      ``{"ignored": "malformed-payload", "reason": "<tag>"}`` body. The
      reason tag is a fixed short string (no echo of attacker-controlled
      payload content), so the response body stays bounded and leak-free.
    - **200 ignored (non-target-service)** — body parses but
      ``(service, region)`` is off-target. Eventarc retries on non-2xx,
      so we explicitly 200 here to acknowledge delivery; the body carries
      ``{"ignored": "non-target-service", ...}``.
    - **200** — recheck dispatched; body is the standard ``_do_recheck``
      response with ``trigger="eventarc"``.
    - **5xx from _do_recheck** — propagated unchanged (worker outage = 502,
      contract-load failure = 500, etc.). The handler does NOT swallow
      these — Eventarc retries them, which is the correct behavior.

    Payload-blindness: the handler only reads ``(service, region)`` from
    ``resource.labels`` and intentionally does NOT branch on the audit log's
    methodName or actor. The audit log doesn't carry the post-mutation env
    anyway; the Reader Worker is what reads it. See
    ``docs/architecture/eventarc-payload.md`` for the full contract.
    """
    s = get_settings()

    # 503 canaries — fail-closed if the deploy didn't wire these.
    if not s.eventarc_audience:
        raise HTTPException(
            status_code=503,
            detail="auth not configured: EVENTARC_AUDIENCE unset",
        )
    if not s.gcp_project:
        raise HTTPException(
            status_code=503,
            detail="auth not configured: GCP_PROJECT unset (cannot build expected SA email)",
        )

    # 401: Authorization header presence + Bearer shape. We check both
    # before token verification so a missing/malformed header returns
    # without ever invoking the JWKS fetch.
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="missing Authorization header",
        )
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authorization header must be Bearer-shaped",
        )
    token = authorization[len("Bearer ") :].strip()
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Authorization Bearer token is empty",
        )

    # 401: verify_oauth2_token raises:
    # - ``ValueError`` on bad signature, wrong audience, expired, or
    #   malformed JWT (documented in its docstring).
    # - ``google.auth.exceptions.GoogleAuthError`` on wrong issuer
    #   (also documented).
    # - ``google.auth.exceptions.TransportError`` (subclass of
    #   GoogleAuthError) if the JWKS fetch over HTTP fails — e.g. Google's
    #   certs endpoint is briefly unreachable. Strictly this is a 503-shaped
    #   condition (upstream availability), but we collapse to 401 so the
    #   auth-failure response is uniform: a probe cannot distinguish "your
    #   token is bad" from "the JWKS fetch transiently failed". Eventarc's
    #   at-least-once retry will re-attempt on its own; we don't claim a
    #   warmer cache on the retry — google-auth's default Request transport
    #   does NOT cache JWKS responses across calls, so each verification
    #   refetches the certs. (Adding CacheControl is out of scope here.)
    # Collapsing all three to 401 is intentional — a token-leak probe
    # shouldn't be able to distinguish "expired" from "wrong audience" from
    # "garbage" from "issuer mismatch".
    try:
        claims = verify_oauth2_token(
            token, _GOOGLE_AUTH_TRANSPORT, audience=s.eventarc_audience
        )
    except (ValueError, google_auth_exceptions.GoogleAuthError):
        # Don't echo the verifier's message — internal detail might
        # disclose which check failed.
        raise HTTPException(
            status_code=401,
            detail="invalid Eventarc token",
        )

    # 403: principal check. Defense-in-depth: even if IAM widened, only
    # the dedicated trigger SA is honored here. Detail deliberately does
    # NOT echo the presented email.
    # Phase 15.3: constant-time comparison via hmac.compare_digest (Codex
    # carry-over from Phase 14). Threat model is mild — the expected SA
    # name isn't secret — but constant-time string comparison is correct
    # hygiene for any auth-claim check.
    # Phase 15.4 (Codex review of Phase 15): the ``isinstance(..., str)``
    # short-circuit BEFORE compare_digest is load-bearing. OIDC says
    # ``email`` is a string, but a (verified) token whose ``email`` claim
    # was an int or list — off-spec but technically possible if an
    # upstream malformed the JWT and Google still signed it (or in test
    # paths where the verifier is mocked) — would feed a non-str into
    # compare_digest, which requires str+str and raises ``TypeError``
    # on a mismatch. FastAPI would surface that as 500. The correct
    # outcome is 403: same as any other principal mismatch, "this
    # verified token's email claim isn't acceptable here". Empty-string
    # emails still 403 because ``compare_digest("", expected)`` is False.
    expected_email = f"eventarc-trigger-sa@{s.gcp_project}.iam.gserviceaccount.com"
    presented_email = claims.get("email")
    if not isinstance(presented_email, str) or not hmac.compare_digest(
        presented_email, expected_email
    ):
        raise HTTPException(
            status_code=403,
            detail="Eventarc token from unexpected service account principal",
        )

    # Phase 15.3: post-auth malformed payloads → 200 ignored, not 400
    # (Codex carry-over from Phase 14). Avoids the Eventarc retry-storm
    # risk if Google ever ships an audit-log schema change. Reason tags
    # are short fixed strings — the exception message (which may embed
    # attacker-controlled JSON fragments) is intentionally NOT echoed.
    try:
        data = await request.json()
    except Exception:
        # Do NOT include the exception message: it can quote raw bytes
        # from the request body (info leak / response inflation).
        return {"ignored": "malformed-payload", "reason": "invalid_json"}
    if not isinstance(data, dict):
        return {"ignored": "malformed-payload", "reason": "body_not_object"}
    resource = data.get("resource")
    if not isinstance(resource, dict):
        return {"ignored": "malformed-payload", "reason": "missing_resource"}
    labels = resource.get("labels")
    if not isinstance(labels, dict):
        return {"ignored": "malformed-payload", "reason": "missing_labels"}
    # Phase 15.4 (Codex review of Phase 15): isinstance(..., str) guards
    # are intentional. ``labels.get("service_name")`` could be a truthy
    # non-string like ``["payment-demo"]`` or ``{"name": "x"}`` (off-spec
    # for Cloud Run audit logs, but technically possible if a future
    # schema change or upstream bug wrapped the values). Without the
    # type check, those values would pass the existence check below and
    # flow into the ``non-target-service`` return — where they'd be
    # echoed in the response body, partially defeating the "fixed short
    # reason, no payload echo" intent of the 15.3 ignored-200 hardening.
    # Falsy non-strings (``[]``, ``{}``) would be caught by the
    # ``not service`` clause anyway, but only by accident of truthiness;
    # the explicit isinstance pins the type contract against a future
    # refactor that uses ``is None``. Both shapes share the same reason
    # tag — they fail the same contract ("we can't safely whitelist-
    # check this label").
    service = labels.get("service_name", "")
    region = labels.get("location", "")
    if (
        not isinstance(service, str)
        or not isinstance(region, str)
        or not service
        or not region
    ):
        return {
            "ignored": "malformed-payload",
            "reason": "missing_service_or_region",
        }

    # Service/region whitelist. 200 (not 4xx) so Eventarc doesn't retry the
    # off-target event indefinitely. Body carries the observed values so the
    # operator can see what was filtered in logs.
    if service != s.target_service or region != s.target_region:
        return {
            "ignored": "non-target-service",
            "service": service,
            "region": region,
        }

    # In-scope event: dispatch through the same recheck pipeline as the
    # manual /recheck path. ``trigger="eventarc"`` lets ``/runs/{id}`` and
    # the e2e smoke test identify decisions produced by the auto-trigger.
    # _do_recheck's HTTPExceptions (worker 502, contract-load 500, claim
    # 409) propagate unchanged — Eventarc will retry on those, which is
    # the correct behavior.
    #
    # Phase 17.A.3 (Codex blocker): the workload is HARDCODED to "drift"
    # server-side. Cloud Run audit-log events are drift's input source by
    # definition. The caller-presented payload does NOT extend authority
    # to workload selection — any ``workload`` field in the body is
    # ignored. An event-triggered upgrade workload, if ever added, will
    # get its own endpoint with its own server-side binding (e.g.
    # ``/eventarc-upgrade`` against a dependabot-style trigger).
    return await _do_recheck("eventarc", workload="drift")


@app.get("/runs/{decision_id}")
def get_run(decision_id: str):
    # Sync on purpose — this only reads from the StateStore singleton, no
    # I/O that benefits from async.
    d = get_state().get_decision(decision_id)
    if not d:
        raise HTTPException(status_code=404, detail="decision not found")
    return d


@app.get("/decisions")
def list_decisions_endpoint(
    response: Response,
    limit: int = 50,
    _: None = Depends(verify_token),
    state: StateStore = Depends(get_state),
) -> dict:
    """List past decisions, newest first, for the operator transparency UI.

    Phase 19.A.7 — backs the ``/ui/transparency`` decision history
    panel. Bounded by the ``limit`` query parameter (1..200) so a
    misconfigured caller can't pull the entire collection in one
    request. Token-guarded via :func:`verify_token` like /recheck.

    Implementation notes (delegated to ``StateStore.list_decisions``):

    * **Client-side sort** on ``DocumentSnapshot.create_time`` — a
      server-side ``order_by("created_at")`` would EXCLUDE pre-Phase-19
      docs that lack the field. (Codex review IMPORTANT.)
    * **Fetch-all-then-trim** — ``.limit(N)`` on the unordered
      stream would pick an arbitrary subset by doc ID, possibly
      missing the newest. (Codex review IMPORTANT.)

    ``headers={"Cache-Control": "no-store"}`` on the 400 HTTPException
    mirrors 19.A.6's pattern: FastAPI builds a fresh response for raised
    HTTPExceptions and does NOT inherit mutations made to the injected
    ``response`` argument, so an operator-surface no-cache guarantee
    requires the header on both the success and error paths.
    """
    if limit < 1 or limit > 200:
        raise HTTPException(
            status_code=400,
            detail="limit must be 1..200",
            headers={"Cache-Control": "no-store"},
        )
    response.headers["Cache-Control"] = "no-store"
    return {"decisions": state.list_decisions(limit=limit)}


@app.get("/trace/{trace_id}")
def get_trace(
    trace_id: str,
    response: Response,
    _: None = Depends(verify_token),
    fetcher: TraceFetcher = Depends(get_trace_fetcher),
    state: StateStore = Depends(get_state),
) -> dict:
    """Return the redacted reasoning timeline for a trace.

    Sync ``def`` on purpose — FastAPI runs sync routes on a threadpool
    (anyio's ``run_in_threadpool``), which is the right shape for the
    SYNC google-cloud-logging client used by
    :class:`CloudLoggingFetcher`. An ``async def`` here would block the
    event loop on every fetch.

    Response shape::

        { "trace_id": "<hex32>",
          "events": [<redacted event dicts, sorted ascending>],
          "decision": { ... } | None,
          "complete": bool,
          "fetched_from_cache": bool }

    Errors:

    * **400** on a non-hex32 ``trace_id`` (fail-closed before any
      Cloud Logging filter is built — same defense-in-depth as
      :class:`CloudLoggingFetcher.fetch`).
    * **401 / 403** from :func:`verify_token` (token guard, Phase 11.1).
    * **503** if the Cloud Logging fetch exceeds
      :data:`_TRACE_FETCH_TIMEOUT_S` — surfaced via a real
      ``Future.result(timeout=...)`` boundary because the
      google-cloud-logging client has no native timeout kwarg.

    Caching: only completed-AND-stable timelines land in the in-process
    cache (see :func:`_observe_and_check_stability`). In-flight traces
    refetch on every poll so the operator UI sees fresh events; the
    cache exists purely to short-circuit repeat polls AFTER the agent
    has finished reasoning.
    """
    # ``fullmatch`` (not ``match``) so a trailing-newline injection
    # can't slip past the guard — see CloudLoggingFetcher's docstring
    # for the full story. Carried forward from 19.A.5.
    #
    # ``headers={"Cache-Control": "no-store"}`` on the HTTPException
    # because FastAPI builds a fresh response for raised
    # HTTPExceptions and does NOT inherit any mutations we made to
    # the injected ``response`` argument. The same pattern repeats on
    # the 503 timeout path below.
    if not _HEX32_RE.fullmatch(trace_id):
        raise HTTPException(
            status_code=400,
            detail="trace_id must be 32-char lowercase hex",
            headers={"Cache-Control": "no-store"},
        )

    # Operator surface — never cache in the browser. The in-process
    # cache above is server-side only; a browser cache would defeat
    # the "refetch in-flight traces" property and let a stale view
    # outlive its server-side TTL.
    response.headers["Cache-Control"] = "no-store"

    # Decision is ALWAYS re-read from StateStore — not pulled from the
    # cache. Codex 19.A.6 review MEDIUM: ``_observe_and_check_stability``
    # can return True (and we'd cache the payload) before
    # ``record_decision`` lands in Firestore, because the ADK's
    # ``final_response`` event is emitted during execution but the
    # decision document is persisted later in ``_do_recheck``/
    # ``_do_rollback``. Caching a payload with ``decision: None`` would
    # freeze the null for the full 300s TTL. Re-reading on every
    # request — including cache hits — is cheap (single doc lookup) and
    # closes the staleness window.
    decision = state.find_decision_by_trace_id(trace_id)

    cached = _cache_get(trace_id)
    if cached is not None:
        return {**cached, "decision": decision, "fetched_from_cache": True}

    # Real timeout via a Future boundary. The google-cloud-logging
    # client's ``list_entries`` has no timeout parameter in 3.15.x —
    # without this wrapper, a hung fetch would tie up the request
    # threadpool slot indefinitely. ``fut.cancel()`` on timeout is
    # best-effort (Python can't kill a thread mid-call) but it at
    # least prevents the Future from being awaited again.
    fut = _TRACE_FETCH_EXECUTOR.submit(fetcher.fetch, trace_id, limit=500)
    try:
        events = fut.result(timeout=_TRACE_FETCH_TIMEOUT_S)
    except _FutureTimeout:
        fut.cancel()
        # Same as the 400 path: carry ``no-store`` on the exception
        # response so the operator's browser doesn't cache a transient
        # timeout view.
        raise HTTPException(
            status_code=503,
            detail="trace fetch timed out",
            headers={"Cache-Control": "no-store"},
        ) from None

    # Stable tie-breaker: same-millisecond events would otherwise
    # shuffle without ``insert_id`` to disambiguate. The fetcher
    # already orders by ``timestamp asc`` (Cloud Logging) but doesn't
    # break ties.
    events.sort(key=lambda e: (e.get("timestamp", ""), e.get("insert_id", "")))

    # Defense-in-depth: redact again at render. Phase 19.A.3 already
    # redacts at emit, but historical entries (pre-Phase-19) and any
    # future emit site that forgets ``redact_event`` are caught here.
    # ``redact_event`` returns ``object`` per signature but yields a
    # dict for dict inputs — every entry is a dict from
    # ``_entry_to_dict``, so the cast is sound.
    events = [redact_event(e) for e in events]  # type: ignore[misc]

    complete = _observe_and_check_stability(trace_id, events)
    if complete:
        # Cache the timeline-only view; the decision is re-read on
        # every response above. Drop the observation entry — once the
        # timeline is cached, future polls hit the cache and never
        # call ``_observe_and_check_stability`` again, so leaving the
        # observation around would just be unbounded growth.
        _cache_put(
            trace_id,
            {"trace_id": trace_id, "events": events, "complete": True},
        )
        _TRACE_OBSERVATIONS.pop(trace_id, None)

    return {
        "trace_id": trace_id,
        "events": events,
        "decision": decision,
        "complete": complete,
        "fetched_from_cache": False,
    }


def _map_worker_error(
    e: "worker_client.WorkerClientError", *, action: str
) -> HTTPException:
    """Map a rollback worker error to a coordinator-facing HTTPException.

    Phase 11.9 (Codex review of 11.7, watch item #2): the prior code
    collapsed every worker error into a 403. That over-collapses two
    operationally important signals:

    - 409 (tag preflight): operator can clear the tag and retry the
      same approval. Surfacing this as 403 would tell the operator
      "your approval is bad" and they'd re-propose unnecessarily.
    - 5xx (worker outage / transport): distinct failure mode from "your
      approval is bad". Mapping to 502 lets retries and observability
      treat it as an upstream availability problem.

    Other 4xx (403 bad token, 403 expired, 403 already used, 422 schema,
    404 missing) still collapse to 403 so the response code cannot be
    used by an unauthenticated probe to enumerate approval state.

    The HTTPException detail deliberately does NOT echo the worker's
    body for the 403 case — that's what made Codex flag the original.
    For 409 / 502 the operator NEEDS the detail to act, so we include
    a short prefix indicating the action and surface the worker's
    truncated body.
    """
    if e.status_code == 409:
        return HTTPException(
            status_code=409,
            detail=f"rollback worker conflict on {action}: {e}",
        )
    if 500 <= e.status_code < 600:
        return HTTPException(
            status_code=502,
            detail=f"rollback worker unavailable on {action}: {e}",
        )
    # All other 4xx — collapse to 403 without echoing which specific
    # worker-side check failed.
    return HTTPException(
        status_code=403,
        detail=f"rollback {action} failed",
    )


def _map_tofu_apply_error(
    e: "worker_client.WorkerClientError", *, action: str
) -> HTTPException:
    """Map a tofu-apply worker error to the surfaced coordinator HTTPException.

    Preserves the two operationally-distinct refusals (Codex C5e-3 blocker /
    carry-forward):

    - **423** (lock_refused): the OpenTofu state lock is held. Surface 423 with
      an actionable message — the operator can force-unlock then re-approve.
    - **409** (drift_refused): the saved plan no longer matches live state.
      Surface 409 — the operator must re-run C2 to regenerate a fresh plan.

    Everything else collapses so a probe cannot enumerate which worker-side
    check failed:

    - **422** (integrity/fidelity/verify) → 403 (don't leak which check).
    - **404** (approval not found) → 403.
    - **403** (bad token / operator-verify / not-pending) → 403.
    - **5xx** (incl. the synthetic 503) → 502.

    This mapper only chooses the SURFACED status. The §2 state-machine decision
    of whether to release the idempotency claim (or record a terminal decision)
    is the CALLER's — see :func:`iac_approval_post`.
    """
    if e.status_code == 423:
        return HTTPException(
            status_code=423,
            detail=(
                f"tofu-apply state lock held on {action}: force-unlock then "
                f"re-approve. {e}"
            ),
        )
    if e.status_code == 409:
        return HTTPException(
            status_code=409,
            detail=(
                f"tofu-apply plan no longer matches live state on {action}: "
                f"re-run C2. {e}"
            ),
        )
    if e.status_code == 422:
        return HTTPException(status_code=403, detail="tofu-apply rejected the plan")
    if e.status_code == 404:
        return HTTPException(status_code=403, detail="tofu-apply approval not found")
    if e.status_code == 403:
        return HTTPException(status_code=403, detail="tofu-apply rejected the request")
    # 5xx (incl. synthetic 503): availability/ambiguity — surface 502.
    return HTTPException(
        status_code=502, detail=f"tofu-apply unavailable on {action}: {e}"
    )


# --------------------------------------------------------------------------- #
# HITL approval endpoints (Phase 11.7)
# --------------------------------------------------------------------------- #
#
# Flow recap:
#
#   1. ADK calls ``propose_rollback_tool`` → coordinator hits Rollback
#      worker's ``/propose`` → worker writes a pending approval doc and
#      returns ``approval_url = f"{COORDINATOR_URL}/approvals/{id}?t=<token>"``.
#   2. Operator opens that URL → ``GET /approvals/{id}`` renders the
#      approval page with a hidden token-bearing form.
#   3. Operator clicks Approve → ``POST /approvals/{id}`` calls
#      ``worker_client.call_execute(approval_id, token)``; the Rollback
#      worker verifies the HMAC, transactionally claims the doc, and
#      shifts traffic.
#   4. Operator clicks Reject → ``POST /approvals/{id}`` transactionally
#      flips status pending→denied via ``ApprovalStore.claim_denied``.
#      A subsequent /execute attempt against the same approval ID will
#      see status="denied" and bounce out with 403 at the worker.
#
# The approval pages do NOT have the X-DriftScribe-Token guard — they're
# the operator-facing UI by design, and the approval_token (plus 15-min
# TTL plus HMAC-binds-revision plus single-use flip) IS the auth model
# for this route. Adding the token guard on top would either require
# operators to keep a separate header in their browser (operationally
# painful) or be wired in a way that defeats the no-referrer headers.


@app.get("/ui/transparency", response_class=HTMLResponse)
def transparency_ui(request: Request) -> Response:
    """Serve the transparency UI (Phase 19.B.1 scaffolding).

    No auth on the HTML itself — the markup is harmless. Every API
    call the page makes (``/chat``, ``/decisions``, ``/trace/{id}``)
    carries the ``X-DriftScribe-Token`` header, which the operator
    sets via the prompt wired up in 19.B.2. The token is held in
    ``sessionStorage`` so it does not survive a tab close.

    ``Cache-Control: no-store`` because this is an operator surface
    — a stale cached copy of the shell would confuse the
    fetch-flow tasks (19.B.3/19.B.4) and could surface yesterday's
    decisions in the rail.
    """
    resp = _TEMPLATES.TemplateResponse(request, "transparency.html", {})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/approvals/{approval_id}", response_class=HTMLResponse)
def approval_get(request: Request, approval_id: str, t: str = "") -> Response:
    """Render the HITL approval decision page.

    The ``t`` query param carries the raw approval token. The page
    embeds it in a hidden form field so the operator's Approve / Reject
    click POSTs the token back without copy-paste.

    Token-in-URL caveats — pinning the safety story so a future refactor
    doesn't lose the context:

    - Referrer-Policy: no-referrer prevents the token from leaking via
      the Referer header on any same-tab navigation.
    - Cache-Control: no-store stops shared HTTP caches from holding the
      URL.
    - The token is bound to the specific approval doc's HMAC + 15-min
      TTL + single-use transactional flip; a leaked URL outside the
      TTL is dead.
    - Cloud Run / load balancer access logs may still capture ``?t=``.
      Operationally we accept this for the hackathon — for a real
      deployment the token would move to a same-origin cookie + CSRF
      header on the POST, but that's larger surgery than 11.7 is
      scoped for.

    Status: always 200 — the page renders itself for missing /
    already-resolved / expired approvals so a probing GET cannot use
    the response code to enumerate doc presence.
    """
    store = approval_helpers.get_approval_store()
    approval = store.get(approval_id)
    expired = bool(approval) and approval_helpers.is_expired(approval)
    response = _TEMPLATES.TemplateResponse(
        request,
        "approval.html",
        {
            "approval_id": approval_id,
            "approval": approval,
            "token": t,
            "expired": expired,
        },
    )
    return _apply_approval_security_headers(response)


# --------------------------------------------------------------------------- #
# Phase C5e-2 — read-only infra-apply approval page.
#
# GET /iac-approvals/{pr_number} renders the C2 ``tofu plan`` artifact a
# plan-builder run already produced, plus a signed, artifact-bound CSRF form
# token the C5e-3 POST will verify. It is READ-ONLY: it never mints a plan
# approval, never calls the tofu-apply worker, and never reads ``plan_approvals``.
# --------------------------------------------------------------------------- #


def _resolve_iac_plan(
    s: Settings, pr_number: int
) -> tuple["iac_artifacts.C2CommentRef | None", "iac_artifacts.IacPlanView | None"]:
    """Resolve the latest C2 artifact for ``pr_number`` into ``(ref, view)``.

    Thin + monkeypatch-friendly (tests patch ``agent.main.get_repo`` and the
    ``agent.main.iac_artifacts.*`` seams). Returns:

    - ``(None, None)`` when GitHub is not configured (route renders "run C2"
      / approvals-not-configured) or no C2 marker comment exists.
    - ``(ref, None)`` when a comment was found but the artifact could not be
      fetched/verified (route renders unverifiable, Approve suppressed).
    - ``(ref, view)`` on success (``view`` carries the advisory verify result;
      the worker re-verifies authoritatively at /apply).
    """
    if not (s.github_token and s.github_repo):
        return (None, None)

    # Fail-closed at this boundary so the GET stays ALWAYS-200 (probe-safe). We
    # catch broadly on purpose: load_plan_view already converts its own
    # IacArtifactErrors into unverifiable views, and find_latest_c2_comment wraps
    # GithubException — but get_repo, a GCS permission/network error, or any
    # unexpected SDK exception could still escape and surface a 500. ``ref`` is
    # seeded to None so a comment-listing failure yields (None, None) ("run C2")
    # while a post-resolution failure yields (ref, None) (render unverifiable).
    ref: "iac_artifacts.C2CommentRef | None" = None
    try:
        repo = get_repo(s.github_token, s.github_repo)
        ref = iac_artifacts.find_latest_c2_comment(repo, pr_number)
        if ref is None:
            return (None, None)
        view = iac_artifacts.load_plan_view(ref, bucket_name=artifacts_bucket(s))
    except Exception:  # noqa: BLE001 — fail-closed: any resolver error → no/unverifiable plan
        log.warning("iac_plan_resolution_failed", extra={"pr_number": pr_number})
        return (ref, None)
    return (ref, view)


def _iac_artifact_consistent(
    ref: "iac_artifacts.C2CommentRef | None",
    view: "iac_artifacts.IacPlanView",
    pr_number: int,
) -> bool:
    """True iff the rendered/pinned artifact coherently belongs to ``pr_number``.

    Defense-in-depth (Codex C5e-2 review, BLOCKER): ``find_latest_c2_comment``
    only matches the C2 marker on the route's issue, and ``load_plan_view`` does
    not cross-check the comment ref against the fetched metadata or against the
    route PR. Without this guard a marker comment could point at a (validly
    signed) artifact for a DIFFERENT pr/head, and we would mint a form token
    binding the wrong artifact to ``/iac-approvals/{pr_number}`` — the worker's
    ``/propose`` receives only ``(artifact_uri_metadata, generation_metadata)``,
    so it does not re-establish this PR binding for us.

    We require: a parsed ref; the metadata's ``pr_number`` equals the route PR;
    and the comment ref's identity fields (head_sha, both plan hashes, the
    plan/json URIs + generations) match the fetched metadata exactly. Any
    mismatch suppresses Approve (advisory; the worker still re-verifies).
    """
    if ref is None:
        return False
    md = view.metadata
    try:
        if int(md.get("pr_number")) != pr_number:
            return False
    except (TypeError, ValueError):
        return False
    return (
        ref.head_sha == md.get("head_sha")
        and ref.plan_sha256 == md.get("plan_sha256")
        and ref.plan_json_sha256 == md.get("plan_json_sha256")
        and ref.artifact_uri_plan == md.get("artifact_uri_plan")
        and ref.artifact_uri_json == md.get("artifact_uri_json")
        and ref.generation_plan == md.get("generation_plan")
        and ref.generation_json == md.get("generation_json")
    )


@app.get("/iac-approvals/{pr_number}", response_class=HTMLResponse)
def iac_approval_get(request: Request, pr_number: int) -> Response:
    """Render the read-only infra-apply approval page for ``pr_number``.

    Auth posture: like the rollback ``approval_get``, this GET has NO app-level
    auth dependency — the whole coordinator sits behind Cloudflare Access at the
    edge, and this read-only page reveals only plan details already visible to a
    signed-in operator. The mandatory operator-identity gate
    (``require_cf_operator``) lives on the C5e-3 POST, not here.

    Always returns 200 (probe-safe): missing comment / unverifiable artifact /
    denylist violation all render an informative page with Approve suppressed
    rather than an error code that would let a probe enumerate PR state.

    Hard invariant: this handler never mints a plan approval, never calls the
    tofu-apply worker, and never reads ``plan_approvals``.
    """
    s = get_settings()
    ref, view = _resolve_iac_plan(s, pr_number)

    can_approve = False
    reason_blocked = ""
    form_token: str | None = None

    if view is None:
        reason_blocked = "No verifiable C2 plan artifact."
    elif view.unverifiable:
        reason_blocked = "artifact unverifiable"
    elif not view.integrity_ok:
        reason_blocked = "plan.json integrity mismatch"
    elif view.denylist_violations:
        reason_blocked = "denylist violations (self-protection policy)"
    elif not _iac_artifact_consistent(ref, view, pr_number):
        # The artifact does not coherently belong to this PR (metadata pr_number
        # mismatch, or comment ref ≠ fetched metadata). Fail-closed — never pin
        # an artifact for a different PR/head to this page.
        reason_blocked = "artifact does not match this PR"
    elif not s.driftscribe_token:
        reason_blocked = "approvals not configured (server token unset)"
    elif s.dry_run:
        # The POST fail-closes under dry-run (it would drive a REAL worker apply
        # while skipping the merge); suppress Approve here so the UI matches.
        reason_blocked = "infra apply disabled (coordinator in dry-run mode)"
    else:
        can_approve = True

    if can_approve:
        try:
            form_token = iac_csrf.mint_form_token(
                s,
                pr_number=pr_number,
                head_sha=view.head_sha,
                artifact_uri_metadata=view.artifact_uri_metadata,
                generation_metadata=view.generation_metadata,
                plan_sha256=view.plan_sha256,
                plan_json_sha256=view.plan_json_sha256,
                comment_id=(ref.comment_id if ref else None),
            )
        except iac_csrf.IacCsrfError:
            can_approve = False
            form_token = None
            reason_blocked = "approvals not configured (server token unset)"

    response = _TEMPLATES.TemplateResponse(
        request,
        "iac_approval.html",
        {
            "pr_number": pr_number,
            "view": view,
            "form_token": form_token,
            "can_approve": can_approve,
            "reason_blocked": reason_blocked,
        },
    )
    _apply_approval_security_headers(response)
    _apply_iac_csp(response)
    return response


# --------------------------------------------------------------------------- #
# Phase C5e-3 — propose-on-approve POST orchestration.
#
# POST /iac-approvals/{pr_number} performs the §2 orchestration state machine:
# Origin + CSRF (the signed, artifact-pinned form token) → re-resolve + pin
# assert → pre-propose readiness → idempotency claim → /propose → 5b head
# re-check → /apply (release matrix per the §2 table) → merge the exact applied
# head (reconcile on merge-fail). The tofu-apply worker remains the sole infra
# mutator and re-verifies everything authoritatively before it applies.
# --------------------------------------------------------------------------- #


def _check_iac_origin(request: Request, s: Settings) -> bool:
    """Exact same-origin check for the C5e POST (Codex important; CSRF defense).

    CF Access does NOT stop a cross-site POST, so we compare the request
    ``Origin`` to ``settings.coordinator_origin`` EXACTLY on (scheme, host,
    port). No ``Referer`` fallback. Fail-closed: a missing ``Origin`` header or
    an unconfigured ``coordinator_origin`` returns ``False``.
    """
    origin = request.headers.get("origin")
    if not origin:
        return False
    if not s.coordinator_origin:
        return False
    # Fail-closed on a malformed Origin: ``urllib.parse`` defers parsing of the
    # port until ``.port`` is read, which raises ``ValueError`` for a non-numeric
    # / out-of-range port (e.g. ``https://host:badport``). A bad Origin must be a
    # clean 403, never a 500.
    try:
        got = urllib.parse.urlsplit(origin)
        want = urllib.parse.urlsplit(s.coordinator_origin)
        # Compare scheme + host + port; use ``.port`` so default-port
        # normalization is consistent on both sides (e.g. https with/without
        # explicit :443).
        return (got.scheme, got.hostname, got.port) == (
            want.scheme,
            want.hostname,
            want.port,
        )
    except ValueError:
        return False


def _iac_event_key(
    repo: str, pr_number: int, head_sha: str, generation_metadata: str
) -> str:
    """Deterministic idempotency key for an infra-apply (Codex blocker #4).

    Keyed on ``{repo, pr_number, head_sha, generation_metadata}`` — NOT on the
    approver, so two operators acting on the same artifact cannot double-mint +
    double-apply it. The approver is recorded in the event payload + decision
    doc, never in the key.
    """
    digest = hashlib.sha256(
        json.dumps(
            {
                "repo": repo,
                "pr_number": pr_number,
                "head_sha": head_sha,
                "generation_metadata": generation_metadata,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()[:32]
    return f"iac-apply-{pr_number}-{digest}"


def _record_iac_decision(
    state: StateStore,
    event_key: str,
    *,
    apply_status: str,
    merge_state: str,
    approval_id: str | None = None,
    apply_attempt_id: str | None = None,
    head_sha: str,
    pr_number: int,
    approver: str,
) -> dict:
    """Build + persist the infra-apply decision doc (the reconcile pointer).

    Mirrors :func:`_do_rollback`'s ``record_decision`` usage. The decision doc
    is the apply-then-merge reconcile pointer: an ``apply_status=="applied"`` +
    ``merge_state=="failed"`` doc is what a re-POST reads to do a merge-only
    reconcile; an ``apply_status in {"failed","ambiguous"}`` doc is terminal.
    """
    decision_id = str(uuid.uuid4())
    decision = {
        "decision_id": decision_id,
        "event_key": event_key,
        "trace_id": current_trace_id_or_new(),
        "action": "iac_apply",
        "apply_status": apply_status,
        "merge_state": merge_state,
        "approval_id": approval_id,
        "apply_attempt_id": apply_attempt_id,
        "head_sha": head_sha,
        "pr_number": pr_number,
        "approver": approver,
    }
    if apply_status == "applied":
        decision["applied_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    state.record_decision(decision_id, event_key, decision)
    return decision


def _render_iac_outcome(
    request: Request,
    *,
    pr_number: int,
    view: "iac_artifacts.IacPlanView | None",
    decision: str,
    outcome: str,
    status_code: int = 200,
) -> Response:
    """Re-render the approval page for a terminal SUCCESS/info POST outcome.

    Suppresses the Approve form (``can_approve=False``) and shows the outcome
    banner. Applies both the approval security headers and the strict IaC CSP.
    """
    response = _TEMPLATES.TemplateResponse(
        request,
        "iac_approval.html",
        {
            "pr_number": pr_number,
            "view": view,
            "form_token": None,
            "can_approve": False,
            "reason_blocked": "",
            "decision": decision,
            "outcome": outcome,
        },
    )
    response.status_code = status_code
    _apply_approval_security_headers(response)
    _apply_iac_csp(response)
    return response


@app.post("/iac-approvals/{pr_number}", response_class=HTMLResponse)
def iac_approval_post(
    request: Request,
    pr_number: int,
    operator_email: str = Depends(require_cf_operator),
    cf_access_jwt: str | None = Header(default=None, alias="Cf-Access-Jwt-Assertion"),
    form_token: str = Form(...),
    decision: Literal["approve", "reject"] = Form(...),
) -> Response:
    """Propose-on-approve POST: run the §2 orchestration state machine.

    ``require_cf_operator`` mandates a verified Cloudflare-Access operator
    identity (401 if absent, 403 on verify-fail, 503 if CF unconfigured) and
    returns the canonical operator email — the ``approver`` bound to the plan
    approval. ``cf_access_jwt`` is the RAW header forwarded to the worker so it
    can re-verify the operator identity authoritatively at ``/apply``.

    REJECT is a coordinator-side audit no-op (no approval exists under
    propose-on-approve). APPROVE executes the ordered state machine; see the
    inline step comments and the plan's §2 table for the release matrix.
    """
    s = get_settings()

    # REJECT — no approval exists yet (propose-on-approve mints on approve), so
    # there is nothing to deny on the worker. Audit no-op + re-render, 200.
    if decision == "reject":
        _ref, view = _resolve_iac_plan(s, pr_number)
        return _render_iac_outcome(
            request,
            pr_number=pr_number,
            view=view,
            decision="reject",
            outcome="Rejected (no apply performed). No plan approval was minted.",
        )

    # --- APPROVE -------------------------------------------------------------

    # (a) Origin + CSRF (hard, raise 403/503).
    if not _check_iac_origin(request, s):
        raise HTTPException(status_code=403, detail="bad origin")
    try:
        payload = iac_csrf.verify_form_token(s, form_token, pr_number=pr_number)
    except iac_csrf.IacCsrfError as e:
        raise HTTPException(
            status_code=503, detail="approvals not configured"
        ) from e
    if payload is None:
        raise HTTPException(
            status_code=403,
            detail="stale or invalid form token; reload the approval page",
        )

    # Dry-run fail-closed (Codex C5e-3 completed-work review, BLOCKER): this POST
    # is an explicit operator apply that drives the worker's REAL /apply (propose/
    # apply are NOT dry-gated). Under coordinator dry-run we would mutate live infra
    # yet skip the merge (merge_pr_at_sha previews) and record a misleading state —
    # so refuse the whole operation BEFORE /propose rather than half-perform it.
    # Checked after Origin+CSRF so a cross-site probe still gets 403, not a mode hint.
    if s.dry_run:
        raise HTTPException(
            status_code=503,
            detail="infra apply is disabled while the coordinator runs in dry-run mode",
        )

    # (b) Re-resolve + pin: bind what-you-saw == what's-latest == what-applies.
    ref, view = _resolve_iac_plan(s, pr_number)
    if view is None or view.unverifiable:
        raise HTTPException(status_code=403, detail="artifact unverifiable")
    if not view.integrity_ok:
        raise HTTPException(status_code=403, detail="integrity mismatch")
    if view.denylist_violations:
        raise HTTPException(status_code=403, detail="denylist violations")
    if not _iac_artifact_consistent(ref, view, pr_number):
        raise HTTPException(
            status_code=403, detail="artifact does not match this PR"
        )
    if not (
        payload["head_sha"] == view.head_sha
        and payload["artifact_uri_metadata"] == view.artifact_uri_metadata
        and payload["generation_metadata"] == view.generation_metadata
        and payload["plan_sha256"] == view.plan_sha256
        and payload["plan_json_sha256"] == view.plan_json_sha256
        # Codex C5e-3 completed-work review: the token pins comment_id, so enforce
        # it too — the full exact-identity contract from the plan (ref is non-None
        # here because _iac_artifact_consistent already required it).
        and payload["comment_id"] == (ref.comment_id if ref else None)
    ):
        raise HTTPException(
            status_code=409,
            detail="the plan changed since you loaded this page; reload and re-review",
        )

    # (c) Pre-propose readiness (raise, no mint — Codex r2: readiness BEFORE claim).
    required_checks = [
        c.strip() for c in s.iac_required_checks.split(",") if c.strip()
    ]
    repo = get_repo(s.github_token, s.github_repo)
    try:
        github.assert_pr_ready_at_sha(
            repo,
            pr_number=pr_number,
            expected_head_sha=view.head_sha,
            required_checks=required_checks,
        )
    except (PrNotEligibleError, PrMergeBlockedError) as e:
        raise HTTPException(
            status_code=getattr(e, "status_code", None) or 409, detail=str(e)
        ) from e

    # (d) Idempotency claim.
    state = get_state()
    event_key = _iac_event_key(
        s.github_repo, pr_number, view.head_sha, view.generation_metadata
    )
    claimed = state.record_event(
        event_key,
        {
            "approver": operator_email,
            "pr_number": pr_number,
            "head_sha": view.head_sha,
            "trigger": "iac_apply",
        },
    )
    reconcile_existing: dict | None = None
    if not claimed:
        existing = state.find_decision_for_event(event_key)
        if existing and existing.get("merge_state") == "merged":
            return _render_iac_outcome(
                request,
                pr_number=pr_number,
                view=view,
                decision="approve",
                outcome="Already applied and merged (idempotent).",
            )
        if (
            existing
            and existing.get("apply_status") == "applied"
            and existing.get("merge_state") == "failed"
        ):
            # Merge-only reconcile: skip propose/apply; jump to the merge step.
            reconcile_existing = existing
        elif existing and existing.get("apply_status") in {"failed", "ambiguous"}:
            return _render_iac_outcome(
                request,
                pr_number=pr_number,
                view=view,
                decision="approve",
                outcome=(
                    f"Terminal state recorded: apply_status="
                    f"{existing.get('apply_status')!r}. Manual verification "
                    "required; this will NOT be retried automatically."
                ),
            )
        else:
            raise HTTPException(
                status_code=409,
                detail="an apply for this plan is already in progress",
            )

    if reconcile_existing is not None:
        # Merge-only reconcile path (re-POST after applied+merge-failed).
        approval_id = reconcile_existing.get("approval_id")
        apply_attempt_id = reconcile_existing.get("apply_attempt_id")
        return _iac_merge_step(
            request,
            s,
            state,
            repo=repo,
            event_key=event_key,
            view=view,
            required_checks=required_checks,
            approval_id=approval_id,
            apply_attempt_id=apply_attempt_id,
            operator_email=operator_email,
            pr_number=pr_number,
        )

    # (e) Propose (mints the plan approval; failure ⇒ no approval ⇒ release).
    try:
        pr_res = worker_client.call_propose(
            view.artifact_uri_metadata,
            view.generation_metadata,
            operator_email,
            cf_access_jwt,
        )
    except worker_client.WorkerClientError as e:
        state.release_event(event_key)
        raise _map_tofu_apply_error(e, action="propose") from e
    # Validate the /propose 2xx before using the ids (Codex C5e-3 review, IMPORTANT):
    # a malformed success would otherwise feed None into call_apply/call_plan_deny.
    # Check the TYPE before .get() — worker_client.call returns r.json(), which may
    # be a non-dict (array/str/null) on a 2xx; .get() on that would raise and strand
    # the claim (Codex r2). This is pre-apply (nothing mutated, nothing burned we can
    # trust), so release the claim and 502 — do NOT attempt deny with untrusted ids.
    if not isinstance(pr_res, dict):
        state.release_event(event_key)
        raise HTTPException(
            status_code=502,
            detail="tofu-apply returned a malformed propose response",
        )
    approval_id = pr_res.get("approval_id")
    approval_token = pr_res.get("approval_token")
    if not (
        isinstance(approval_id, str)
        and approval_id
        and isinstance(approval_token, str)
        and approval_token
    ):
        state.release_event(event_key)
        raise HTTPException(
            status_code=502,
            detail="tofu-apply returned a malformed propose response",
        )

    # (5b) Head re-check immediately before /apply — a push between propose and
    # apply would otherwise apply a stale saved plan then diverge from the head.
    # A read failure here must NOT strand the claim + the pending approval we
    # just minted: treat any error reading the head as "cannot prove the head is
    # safe to apply", clean up (best-effort deny + release), and fail-closed 409
    # (Codex C5e-3 completed-work review). The approval is still pending (not yet
    # applied), so deny is the correct cleanup.
    try:
        head_now = github.get_pr_head_sha(repo, pr_number)
    except Exception as e:  # noqa: BLE001 — fail-closed: cannot prove head safe
        with contextlib.suppress(Exception):
            worker_client.call_plan_deny(approval_id, approval_token)
        state.release_event(event_key)
        raise HTTPException(
            status_code=409,
            detail="could not confirm PR head before apply; re-approve",
        ) from e
    if head_now != view.head_sha:
        with contextlib.suppress(Exception):
            worker_client.call_plan_deny(approval_id, approval_token)
        state.release_event(event_key)
        raise HTTPException(
            status_code=409, detail="PR head moved after propose; re-approve"
        )

    # (f) Apply — the §2 release matrix.
    try:
        apply_res = worker_client.call_apply(
            approval_id, approval_token, cf_access_jwt
        )
    except worker_client.WorkerClientError as e:
        if e.status_code in (403, 404):
            # PRE-claim: approval NOT burned, infra NOT mutated. Clean the
            # orphaned pending we just minted, release, surface.
            with contextlib.suppress(Exception):
                worker_client.call_plan_deny(approval_id, approval_token)
            state.release_event(event_key)
            raise _map_tofu_apply_error(e, action="apply") from e
        if e.status_code in (422, 423, 409):
            # Post-claim, NON-mutating: the approval is burned but infra is
            # unchanged. Release so the operator can re-click for a fresh mint.
            state.release_event(event_key)
            raise _map_tofu_apply_error(e, action="apply") from e
        if e.status_code == 502 or e.status_code >= 500:
            # Possible partial mutation. Do NOT release. Distinguish a
            # worker-returned 502 (definite ``tofu apply`` failure) from the
            # synthetic 503 / any other 5xx (unknown whether it reached/applied).
            ambiguous = e.status_code != 502
            apply_status = "ambiguous" if ambiguous else "failed"
            _record_iac_decision(
                state,
                event_key,
                apply_status=apply_status,
                merge_state="n/a",
                approval_id=approval_id,
                head_sha=view.head_sha,
                pr_number=pr_number,
                approver=operator_email,
            )
            with contextlib.suppress(Exception):
                worker_client.call(
                    "notifier",
                    {
                        "channel": "approval",
                        "severity": "high",
                        "body": (
                            f"IaC apply {apply_status} for PR #{pr_number} "
                            f"(head {view.head_sha[:7]}, approval {approval_id}). "
                            "Manual verification required; do NOT retry blindly."
                        ),
                    },
                )
            if ambiguous:
                raise HTTPException(
                    status_code=504,
                    detail=(
                        "tofu-apply outcome uncertain (timeout/unreachable after "
                        "send); infra may have changed. Manual verification "
                        "required; do NOT retry blindly."
                    ),
                ) from e
            raise HTTPException(
                status_code=502,
                detail=(
                    "tofu-apply failed; infra may be partially mutated. Manual "
                    "verification required; do NOT retry blindly."
                ),
            ) from e
        # Defensive: any unclassified status → treat as ambiguous (no release).
        _record_iac_decision(
            state,
            event_key,
            apply_status="ambiguous",
            merge_state="n/a",
            approval_id=approval_id,
            head_sha=view.head_sha,
            pr_number=pr_number,
            approver=operator_email,
        )
        with contextlib.suppress(Exception):
            worker_client.call(
                "notifier",
                {
                    "channel": "approval",
                    "severity": "high",
                    "body": (
                        f"IaC apply ambiguous for PR #{pr_number} "
                        f"(unexpected worker status {e.status_code})."
                    ),
                },
            )
        raise HTTPException(
            status_code=504,
            detail=(
                "tofu-apply returned an unexpected status; outcome uncertain. "
                "Manual verification required; do NOT retry blindly."
            ),
        ) from e

    # Validate the /apply 2xx before merging (Codex C5e-3 review, BLOCKER): a
    # malformed 200 must NOT merge unapplied config. The worker only returns 200
    # after a real apply, so a malformed body is treated as AMBIGUOUS (may have
    # mutated) — no release, terminal decision, alert, 504, NO merge.
    apply_attempt_id = apply_res.get("apply_attempt_id") if isinstance(apply_res, dict) else None
    if not (
        isinstance(apply_res, dict)
        and apply_res.get("status") == "applied"
        and apply_res.get("approval_id") == approval_id
        and isinstance(apply_attempt_id, str)
        and apply_attempt_id
    ):
        _record_iac_decision(
            state,
            event_key,
            apply_status="ambiguous",
            merge_state="n/a",
            approval_id=approval_id,
            apply_attempt_id=apply_attempt_id if isinstance(apply_attempt_id, str) else None,
            head_sha=view.head_sha,
            pr_number=pr_number,
            approver=operator_email,
        )
        with contextlib.suppress(Exception):
            worker_client.call(
                "notifier",
                {
                    "channel": "approval",
                    "severity": "high",
                    "body": (
                        f"IaC apply returned a malformed success for PR #{pr_number} "
                        f"(head {view.head_sha[:7]}, approval {approval_id}); outcome "
                        "uncertain. Manual verification required; do NOT retry blindly."
                    ),
                },
            )
        raise HTTPException(
            status_code=504,
            detail=(
                "tofu-apply returned a malformed success response; outcome "
                "uncertain. Manual verification required; do NOT retry blindly."
            ),
        )

    # (g) Merge (apply succeeded).
    return _iac_merge_step(
        request,
        s,
        state,
        repo=repo,
        event_key=event_key,
        view=view,
        required_checks=required_checks,
        approval_id=approval_id,
        apply_attempt_id=apply_attempt_id,
        operator_email=operator_email,
        pr_number=pr_number,
    )


def _iac_merge_step(
    request: Request,
    s: Settings,
    state: StateStore,
    *,
    repo,
    event_key: str,
    view: "iac_artifacts.IacPlanView",
    required_checks: list[str],
    approval_id: str | None,
    apply_attempt_id: str | None,
    operator_email: str,
    pr_number: int,
) -> Response:
    """Step (g): merge the EXACT applied head; reconcile on merge-fail.

    Shared by the fresh apply→merge path and the merge-only reconcile re-POST.
    On merge success → record ``merged`` decision + success banner. On merge
    failure → record the ``applied``/``failed`` reconcile doc + notifier alert +
    a 200 "merge pending reconcile" banner (apply SUCCEEDED — not an operator
    error; the event is NOT released because the decision carries the reconcile
    pointer).
    """
    try:
        github.merge_pr_at_sha(
            repo,
            pr_number=pr_number,
            expected_head_sha=view.head_sha,
            required_checks=required_checks,
            merge_method=(s.iac_merge_method or "squash"),
            dry_run=s.dry_run,
        )
    except Exception as e:  # noqa: BLE001 — any merge failure → reconcile doc
        _record_iac_decision(
            state,
            event_key,
            apply_status="applied",
            merge_state="failed",
            approval_id=approval_id,
            apply_attempt_id=apply_attempt_id,
            head_sha=view.head_sha,
            pr_number=pr_number,
            approver=operator_email,
        )
        with contextlib.suppress(Exception):
            worker_client.call(
                "notifier",
                {
                    "channel": "approval",
                    "severity": "high",
                    "body": (
                        f"IaC apply SUCCEEDED but merge failed for PR #{pr_number} "
                        f"(head {view.head_sha[:7]}): {e}. Re-submit to retry the "
                        "merge (apply will NOT re-run)."
                    ),
                },
            )
        return _render_iac_outcome(
            request,
            pr_number=pr_number,
            view=view,
            decision="approve",
            outcome=(
                "Applied; merge pending reconcile — re-submit to retry the merge "
                "(the apply will NOT re-run)."
            ),
        )

    _record_iac_decision(
        state,
        event_key,
        apply_status="applied",
        merge_state="merged",
        approval_id=approval_id,
        apply_attempt_id=apply_attempt_id,
        head_sha=view.head_sha,
        pr_number=pr_number,
        approver=operator_email,
    )
    return _render_iac_outcome(
        request,
        pr_number=pr_number,
        view=view,
        decision="approve",
        outcome="Applied and merged.",
    )


@app.post("/approvals/{approval_id}", response_class=HTMLResponse)
def approval_post(
    request: Request,
    approval_id: str,
    t: str = Form(...),
    decision: Literal["approve", "reject"] = Form(...),
) -> Response:
    """Process the operator's Approve / Reject decision.

    Token validation strategy (key design choice, Phase 11.9):

    - **Approve**: the coordinator does NOT verify the HMAC itself. It
      hands ``(approval_id, t)`` to the Rollback worker's ``/execute``
      via :func:`worker_client.call_execute`, and the worker (which is
      the only service holding the HMAC key) does the verify +
      transactional pending→used flip + Cloud Run traffic update.
    - **Reject**: the coordinator likewise hands ``(approval_id, t)``
      to the Rollback worker's ``/deny`` via
      :func:`worker_client.call_deny`. The worker verifies the HMAC
      AND transactionally flips pending→denied. Same authority split as
      approve — the coordinator can only initiate either action with
      a valid operator-presented token.

    The pre-11.9 design called :func:`approval_helpers.deny` directly
    from the coordinator without token validation. Codex review of 11.7
    flagged that as a HITL availability bug (anyone with just the
    ``approval_id`` could deny a pending rollback). Both decision paths
    now go through the worker so the "compromised coordinator cannot
    mint OR silently deny executions" property holds end-to-end.

    Status code mapping for worker errors (BOTH paths):

    - **409**: passed through — tag-preflight or similar operational
      conflict that the operator can resolve. Distinct from "your
      approval is bad".
    - **5xx → 502**: worker outage. Distinct from "your approval is bad".
    - **other 4xx → 403**: collapsed. Bad token, expired, already used
      — all surface as 403 so an unauthenticated probe cannot enumerate
      approval state from the response code.

    Status codes returned by this endpoint:

    - **200**: page re-rendered showing the new state.
    - **403**: replay / already-resolved / wrong token / worker
      rejected the action with another 4xx. Generic message so probing
      cannot distinguish "wrong token" from "already used".
    - **409**: tag-preflight conflict or similar.
    - **502**: rollback worker unreachable or returned 5xx.
    """
    store = approval_helpers.get_approval_store()
    execute_result: dict | None = None

    if decision == "reject":
        try:
            execute_result = worker_client.call_deny(approval_id, t)
        except worker_client.WorkerClientError as e:
            # Worker rejected the deny: bad token, expired, missing,
            # already used/denied, etc. Pass through 409 + map 5xx to
            # 502 (see docstring); everything else collapses to 403.
            raise _map_worker_error(e, action="deny") from e
    else:  # approve
        try:
            execute_result = worker_client.call_execute(approval_id, t)
        except worker_client.WorkerClientError as e:
            # Same mapping as the reject path — see :func:`_map_worker_error`.
            raise _map_worker_error(e, action="execute") from e

    # Re-fetch the doc so the page reflects the new status.
    approval = store.get(approval_id)
    response = _TEMPLATES.TemplateResponse(
        request,
        "approval.html",
        {
            "approval_id": approval_id,
            "approval": approval,
            # Don't echo the token back into the rendered form. The
            # decision has been processed; subsequent submits should
            # come from a fresh URL with its own ``?t=``.
            "token": "",
            "expired": False,
            "decision": decision,
            "decision_result": execute_result,
        },
    )
    return _apply_approval_security_headers(response)


# --------------------------------------------------------------------------- #
# /chat — natural-language operator interface (Phase 11.7)
# --------------------------------------------------------------------------- #


class ChatRequest(BaseModel):
    """Closed schema for the /chat endpoint.

    ``extra="forbid"`` so a typo'd field surfaces as 422, not a silent
    fallback to default behavior. ``session_id`` is optional because the
    in-memory session is recreated per call anyway (cross-call agent
    memory is out of scope for 11.7 — see ``docs/architecture/multi-agent-design.md``
    §"session memory").

    Phase 17.A.3: ``workload`` selects the workload-scoped agent. The
    Literal closes the set to ``{"drift", "upgrade", "explore"}`` —
    pydantic rejects any other value with 422 before the handler body
    runs, which prevents a malformed request from reaching the workload
    loader's exception path. Defaults to ``"drift"`` so pre-17 callers
    that omit the field route as they always did.
    """

    prompt: str
    session_id: str | None = None
    workload: Literal["drift", "upgrade", "explore"] = "drift"

    model_config = ConfigDict(extra="forbid")


def _chat_error_payload(e: Exception, *, workload: str) -> tuple[int, str]:
    """Map a ``run_chat`` / ``run_chat_stream`` exception to (status, detail).

    Phase 22: shared by the JSON path (raised as :class:`HTTPException`)
    and the SSE path (surfaced as an ``event: error`` frame's
    ``status_hint``). The status/detail wording mirrors the pre-streaming
    exception ladder exactly so existing callers/tests see no change.
    """
    if isinstance(e, WorkerClientError):
        return 502, f"chat worker call failed: {e}"
    if isinstance(e, MissingDeveloperKnowledgeApiKeyError):
        return 503, (
            f"workload {workload!r} cannot reach the Developer "
            f"Knowledge MCP: {e}. See Phase 17.B.1 for the Secret "
            f"Manager binding that provisions DEVELOPER_KNOWLEDGE_API_KEY."
        )
    if isinstance(e, RuntimeError):
        return 502, f"chat agent failed: {e}"
    return 500, f"chat agent failed unexpectedly: {e}"


def _sse_frame(*, event: str | None = None, data: dict) -> str:
    """Serialize one Server-Sent Event frame."""
    head = f"event: {event}\n" if event else ""
    return f"{head}data: {json.dumps(data, default=str)}\n\n"


# Heartbeat cadence: emit an SSE comment if no event arrives within this
# window. Keeps the Cloudflare read-idle timeout (~120s) from dropping the
# connection during a long worker tool call. Does NOT extend Cloud Run's
# total request timeout (see infra/cloudbuild.yaml --timeout).
_SSE_HEARTBEAT_S = 15


async def _chat_sse(prompt: str, session_id: str | None, workload: str,
                    trace_id: str):
    """SSE generator for the /chat streaming path.

    Re-binds the trace_id + workload ContextVars INSIDE the generator
    body: by the time Starlette iterates this generator the trace-id
    middleware's ``finally`` and ``/chat``'s own workload ``finally`` have
    already reset them (``call_next`` returned as soon as the
    ``StreamingResponse`` was constructed). Without re-binding, every event
    logged/streamed during the run would carry a fresh, uncorrelated
    trace_id — corrupting both the live stream and the durable logs. The
    ``set_*`` calls happen before ``create_task`` so the producer task
    inherits the bindings (``create_task`` copies the current context).
    """
    from agent.adk_agent import run_chat_stream

    t_tok = set_trace_id(trace_id)
    w_tok = set_workload(workload)
    queue: asyncio.Queue = asyncio.Queue()

    async def _produce():
        try:
            async for item in run_chat_stream(
                prompt, session_id=session_id, workload=workload
            ):
                await queue.put(("item", item))
        except Exception as e:  # noqa: BLE001 - mapped to a status hint
            await queue.put(("error", _chat_error_payload(e, workload=workload)))
        finally:
            await queue.put(("end", None))

    producer = asyncio.create_task(_produce())
    try:
        yield _sse_frame(event="meta", data={"trace_id": trace_id})
        while True:
            try:
                kind, payload = await asyncio.wait_for(
                    queue.get(), timeout=_SSE_HEARTBEAT_S
                )
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if kind == "item":
                item = payload
                if item["type"] == "event":
                    yield _sse_frame(data=item["event"])
                else:  # "result"
                    yield _sse_frame(event="done", data={
                        "reply": item["reply"],
                        "tool_calls": item["tool_calls"],
                        "session_id": item["session_id"],
                    })
            elif kind == "error":
                status, detail = payload
                yield _sse_frame(
                    event="error",
                    data={"detail": detail, "status_hint": status},
                )
            else:  # "end"
                break
    finally:
        producer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await producer
        reset_workload(w_tok)
        reset_trace_id(t_tok)


@app.post("/chat", response_model=None)
async def chat(
    req: ChatRequest,
    request: Request,
    _: None = Depends(verify_token),
) -> dict | StreamingResponse:
    """Free-form operator interface to the coordinator.

    Routes through the SAME X-DriftScribe-Token guard as /recheck
    (Phase 11.1). Distinct from /recheck:

    - /recheck returns a structured DecisionProposal — the LLM is
      constrained to produce JSON of a fixed schema.
    - /chat returns free-form text — the LLM picks tools, may call
      multiple workers, and produces a natural-language response.

    The ADK runner picks tools from ``workload.tools`` — the
    per-workload filtered subset of ``COORDINATOR_TOOLS`` — so the LLM
    is never shown a cross-workload tool (Phase 17.A.3 capability-bound
    invariant). The full registration manifest lives in
    ``COORDINATOR_TOOLS`` in :mod:`agent.adk_agent` (pinned by the
    inventory test in ``tests/unit/test_coordinator_tool_inventory.py``);
    per-workload filtering happens at ``Agent`` construction in
    :func:`agent.adk_agent.build_agent`.
    """
    s = get_settings()
    if not s.use_adk:
        # /chat without ADK enabled has no engine to invoke. 503 (not
        # 501) because the feature exists at this revision; it's just
        # disabled. Operator flips USE_ADK=true after verifying Vertex
        # AI Gemini quota for `gemini-2.5-flash` in the deploy region
        # (asia-northeast1) — Phase 14.5 moved auth to Vertex AI ADC so
        # quota is per-project/region, not per-API-key.
        raise HTTPException(
            status_code=503,
            detail="ADK not enabled (set USE_ADK=true to enable /chat)",
        )
    # Phase 17.A.3: pre-resolve the workload so an "undeployed workload"
    # failure (e.g. upgrade before Phase 17.B/17.C/17.E land the tools +
    # worker URLs) surfaces as 503 BEFORE we boot the ADK runner. The
    # result is cached inside ``agent.workloads.registry._WORKLOAD_CACHE``,
    # so the inner ``run_chat`` re-resolution is a free dict lookup.
    #
    # Two exception classes mean "workload not deployed in this build":
    #
    # - :class:`MissingWorkerEnvError` — worker URL env var is unset. Hit
    #   by upgrade today (UPGRADE_READER_URL etc. land in 17.E).
    # - :class:`ReservedToolNotImplementedError` — symbolic tool name is
    #   reserved in the registry but the callable is None. Hit by
    #   upgrade today (``upgrade_read_dependencies`` etc. land in
    #   17.B/17.C).
    #
    # Both collapse to 503 with a clear "not deployed" message. NOT
    # caught here: bare :class:`UnknownToolError` (unknown name in the
    # registry — a YAML typo or attempted capability widening). That
    # bubbles out as a 500, which is the right operator surface: it's a
    # broken deploy / control-plane bug, not a deploy-ordering issue.
    # The 503-vs-500 split lets operators distinguish "wait for the
    # next phase" from "the current deploy is broken, file a bug".
    # Codex review of the initial 17.A.3 implementation flagged the
    # broader catch as collapsing two operationally distinct cases.
    try:
        resolution = load_workload(req.workload)
    except (
        MissingWorkerEnvError,
        ReservedToolNotImplementedError,
        MissingDeveloperKnowledgeApiKeyError,
    ) as e:
        # See the matching catch in ``/recheck`` for why
        # ``MissingDeveloperKnowledgeApiKeyError`` is in this tuple
        # (deploy-not-wired condition, same 503 surface) rather than
        # inheriting from ``MissingWorkerEnvError``.
        raise HTTPException(
            status_code=503,
            detail=(
                f"workload {req.workload!r} is not deployed: {e}. "
                f"See Phase 17.B/17.C/17.E for the wiring that lands "
                f"upgrade's tools and worker URLs."
            ),
        ) from e

    # Phase 17.C.4 (Codex 2026-05-20 follow-up): eagerly parse the
    # upgrade contract on every /chat request so a bad contract
    # surfaces as a clean 503 at request entry rather than a mid-
    # conversation runtime error inside ``run_chat``. No-op for drift;
    # see :func:`_eager_resolve_upgrade_contract`.
    _eager_resolve_upgrade_contract(resolution)

    # Phase 22: SSE streaming path. Content-negotiated on Accept — the
    # operator UI sends ``text/event-stream``; tests, /recheck, and API
    # callers that don't get the unchanged JSON dict below. Capture the
    # trace_id NOW (before returning the StreamingResponse) and re-bind it
    # inside the generator — see :func:`_chat_sse` for why. Streaming is
    # ADDITIVE: ``run_chat_stream`` still logs every event to Cloud
    # Logging exactly as the JSON path does.
    wants_sse = "text/event-stream" in request.headers.get("accept", "")
    if wants_sse:
        trace_id = current_trace_id_or_new()
        return StreamingResponse(
            _chat_sse(req.prompt, req.session_id, req.workload, trace_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Trace-Id": trace_id,
            },
        )

    from agent.adk_agent import run_chat

    # Phase 17.B.4 follow-up: bind the *caller* workload identity to the
    # ContextVar read by the Developer Knowledge MCP wrapper's structured
    # log. See the matching binding in ``_do_recheck``'s ADK path for the
    # full rationale; the short version is that ``mcp_server`` (which
    # MCP we called) is not enough — operator dashboards need to slice
    # latency/failures by caller workload too, and that comes from this
    # ContextVar. Reset in the inner ``finally`` so the outer ``try``
    # can still translate downstream errors to HTTPException without
    # leaking the binding into a sibling request.
    _workload_token = set_workload(req.workload)
    try:
        try:
            return await run_chat(
                req.prompt, session_id=req.session_id, workload=req.workload
            )
        finally:
            reset_workload(_workload_token)
    except (
        worker_client.WorkerClientError,
        MissingDeveloperKnowledgeApiKeyError,
        RuntimeError,
    ) as e:
        # Phase 22: the exception→(status, detail) mapping moved into the
        # shared :func:`_chat_error_payload` so the SSE path's ``error``
        # frame and this JSON path stay identical. The status split is the
        # same as before: WorkerClientError→502 (downstream unhealthy),
        # MissingDeveloperKnowledgeApiKeyError→503 (DK MCP not wired — a
        # deploy condition, NOT "model misbehaved"; it subclasses
        # RuntimeError so it must precede the bare RuntimeError in the
        # tuple's isinstance ladder inside the helper), bare RuntimeError→
        # 502 (ADK parse/response failure).
        status, detail = _chat_error_payload(e, workload=req.workload)
        raise HTTPException(status_code=status, detail=detail) from e
