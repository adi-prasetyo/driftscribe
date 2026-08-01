# agent/main.py
import asyncio
import contextlib
import datetime as dt
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import threading
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutureTimeout
from pathlib import Path
from typing import Any, Literal

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from google.auth import exceptions as google_auth_exceptions
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.id_token import verify_oauth2_token
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from agent import approval_i18n
from agent import approvals as approval_helpers
from agent import iac_artifacts
from agent import iac_csrf
from agent import worker_client
from agent.auth import require_cf_operator, verify_token
from agent.pause import (
    FAIL_CLOSED_REASON,
    PAUSED_DETAIL,
    PauseState,
    read_pause_state,
)
from agent.autonomy import (
    AutonomyState,
    autonomy_apply_blocked_detail,
    read_autonomy_state,
)
from agent.autonomy import FAIL_CLOSED_REASON as AUTONOMY_FAIL_CLOSED_REASON
from agent.classifier import ClassificationInput, classify
from agent.config import Settings, artifacts_bucket, get_settings
from agent.worker_client import WorkerClientError
from agent.contract import OpsContract, contract_hash, contract_preview_payload, load_contract
from agent.github_actions import (
    get_repo,
    open_docs_pr,
    open_drift_issue,
    open_escalation_issue,
)
from agent.mcp.developer_knowledge import MissingDeveloperKnowledgeApiKeyError
from agent.models import DecisionAction, DecisionProposal
from agent.renderer import (
    NOTIFIER_BODY_MAX_CHARS,
    attach_iac_pr_link,
    normalize_notifier_body,
    normalize_rollback_reason,
    render_docs_pr_body,
    render_drift_issue_body,
    render_escalation_issue_body,
    render_rollback_body,
    scrub_decision_approval,
    scrub_decision_rationale,
    scrub_pr_body,
    scrub_rationale_text,
)
from agent.runbook_patcher import patch_runbook
from agent.secret_guard import redact_event
from agent.infra_graph_cache_store import (
    FirestoreInfraGraphCacheStore,
    InfraGraphCacheStore,
    InMemoryInfraGraphCacheStore,
)
from agent.iac_pr_source_cache import (
    FirestoreIacPrSourceCacheStore,
    IacPrSourceCacheStore,
    InMemoryIacPrSourceCacheStore,
)
from agent.per_pr_cache import (
    FirestorePerPrCacheStore,
    InMemoryPerPrCacheStore,
    PerPrCacheStore,
)
from agent.state_store import FirestoreStateStore, InMemoryStateStore, StateStore
from agent.trace_fetcher import (
    CloudLoggingFetcher,
    StubTraceFetcher,
    TraceFetcher,
    _HEX32_RE,
)
from agent.validator import ValidationError as ProposalValidationError
from agent.validator import validate
from agent.capabilities import build_capabilities, WORKLOAD_NAMES
from agent.workloads import (
    MissingWorkerEnvError,
    ReservedToolNotImplementedError,
    UnknownUpgradeTargetError,
    WorkloadResolution,
    load_workload,
    reset_workload,
    set_workload,
)
from agent.workloads.registry import load_workload_spec, resolve_workload_prompts
from pydantic import ValidationError as PydanticValidationError
from driftscribe_lib import github
from driftscribe_lib.approvals import (
    PHASE_APPLYING,
    PHASE_OUTCOME_UNKNOWN,
    ROLLBACK_PHASES,
    TERMINAL_ROLLBACK_PHASES,
)
from driftscribe_lib.auth import verify_oidc_caller
from driftscribe_lib.cf_access import (
    CfAccessJwtError,
    canonical_operator_email,
    verify_cf_access_jwt,
)
from driftscribe_lib.github import PrMergeBlockedError, PrNotEligibleError
from driftscribe_lib.iac_plan_summary import BLAST_CANNOT_TOUCH_NOTE, blast_radius_phrase
from driftscribe_lib.infra_graph import (
    ADOPTABLE_ASSET_TYPES,
    build_graph,
    plan_overlay,
    plan_overlay_unavailable,
)
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

# ---------------------------------------------------------------------------
# Frontend (Svelte+Vite) static assets + Vite-manifest resolution.
#
# The operator UI (GET /) is a Svelte SPA compiled by Vite into
# ``agent/static/`` (gitignored; built in Docker/CI and locally for the smoke).
# FastAPI serves a thin shell that loads the hashed JS/CSS resolved here. The
# approval pages (GET /approvals, /iac-approvals) link the same built CSS.
#
# ``check_dir=False``: the pure-Python CI ``lint-test`` job never runs
# ``vite build``, so ``agent/static/`` is absent there — the mount must not
# raise at import. The shell route still returns 200 via the dev fallback below.
# ---------------------------------------------------------------------------
_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR), check_dir=False), name="static")

# Cached Vite manifest. Cached ONLY on a successful read (Codex review): a build
# that lands later in the same process is picked up, and tests can force a
# re-read by resetting this to None.
_VITE_MANIFEST_CACHE: dict | None = None


def _read_vite_manifest() -> dict | None:
    """Return the parsed Vite ``manifest.json`` or ``None`` if not built yet.

    Lazy + cache-on-success: a missing/malformed manifest returns ``None``
    WITHOUT caching, so a subsequent build in the same process resolves.
    """
    global _VITE_MANIFEST_CACHE
    if _VITE_MANIFEST_CACHE is not None:
        return _VITE_MANIFEST_CACHE
    try:
        data = json.loads((_STATIC_DIR / ".vite" / "manifest.json").read_text())
    except (OSError, ValueError):
        return None
    if isinstance(data, dict):
        _VITE_MANIFEST_CACHE = data
        return data
    return None


def _shell_assets() -> dict[str, str]:
    """Resolve the built JS + CSS URLs for the SPA shell and approval pages.

    The Vite entry is the single ``isEntry`` record (documented key
    ``src/main.ts``). Falls back to conventional ``/static`` names when the
    manifest is absent so the shell route still renders 200 in the pure-Python
    CI job (which never runs ``vite build``).
    """
    manifest = _read_vite_manifest()
    if manifest:
        entry = manifest.get("src/main.ts")
        if entry is None:
            for value in manifest.values():
                if isinstance(value, dict) and value.get("isEntry"):
                    entry = value
                    break
        if isinstance(entry, dict) and entry.get("file"):
            css_list = entry.get("css") or []
            return {
                "js": "/static/" + entry["file"],
                "css": ("/static/" + css_list[0]) if css_list else "/static/driftscribe.css",
            }
    return {"js": "/static/transparency.js", "css": "/static/driftscribe.css"}


# Expose the built CSS href to EVERY template render (the SPA shell passes it via
# context; the Jinja approval pages — which have many render branches: GET, POST
# success, POST blocked, 409 — read it through this global callable so we don't
# have to thread ``ds_css`` through each context dict and risk missing a branch).
# A callable (not a static value) so the lazy manifest resolution runs per render.
_TEMPLATES.env.globals["ds_css_href"] = lambda: _shell_assets()["css"]
# JA vocabulary for the approval templates' `lang == 'ja'` branches only
# (exact-match with English-identity fallback — agent/approval_i18n.py).
_TEMPLATES.env.globals["ja_type_label"] = approval_i18n.ja_type_label
_TEMPLATES.env.globals["ja_verb_label"] = approval_i18n.ja_verb_label


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
# C5e-2). The page is self-contained: ONE same-origin stylesheet (the shared
# Svelte+Vite bundle CSS — no inline ``<style>`` after the UI refresh), a
# same-origin form, no scripts, no images, no remote anything. We pin the CSP
# accordingly so a stored-XSS-style injection into the rendered plan/diff text
# cannot exfiltrate or escalate:
# - ``default-src 'none'``  — deny everything not explicitly allowed.
# - ``style-src 'self'`` — allow ONLY the same-origin built stylesheet at
#   ``/static`` (no inline styles; the built CSS contains no ``url()`` assets,
#   so no img-src/font-src relaxation is needed). Jinja autoescaping still
#   covers the dynamic plan/diff text.
# - ``form-action 'self'`` — the Approve/Reject POST may only target this origin
#   (a CSP-level companion to the POST handler's exact-Origin check in C5e-3).
# - ``base-uri 'none'`` / ``frame-ancestors 'none'`` — no ``<base>`` hijack, no
#   framing (defense-in-depth alongside ``X-Frame-Options: DENY``).
def _apply_iac_csp(response: Response) -> Response:
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'self'; form-action 'self'; "
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


# --- Multi-turn chat conversations (P1) ------------------------------------

_CONVERSATION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")

# Conversation-document fields that are server bookkeeping, never client
# contract. Both are new with the crew handoff:
#
# - ``pending_handoff`` carries ``nonce_digest`` (a hash of a live transition
#   credential) and ``brief`` (written for the joining crew, not for a reader).
#   The SPA does need to know a chip is outstanding, so this field is PROJECTED
#   rather than dropped — see :func:`_project_pending_handoff`.
# - ``chat_run_lease`` is concurrency plumbing with no reader outside this
#   module.
_CONVERSATION_INTERNAL_FIELDS = ("pending_handoff", "chat_run_lease")


def _project_pending_handoff(pending: object) -> dict | None:
    """Allowlist-project an open proposal for the client.

    Enough to re-render the confirmation chip after a reload or a
    ``?conversation=`` deep link — which is the whole reason the chip reads
    from persisted state rather than from a one-shot SSE frame — and nothing
    more. The nonce digest and the crew-facing brief stay server-side.
    """
    if not isinstance(pending, dict) or not pending.get("to"):
        return None
    expires_at = pending.get("expires_at")
    return {
        "from": pending.get("from"),
        "to": pending.get("to"),
        "reason": pending.get("reason") or "",
        "expires_at": (
            expires_at.isoformat() if hasattr(expires_at, "isoformat")
            else expires_at
        ),
    }


def _project_conversation(conv: dict) -> dict:
    """Strip server-internal fields from a conversation doc before serving it."""
    out = {k: v for k, v in conv.items() if k not in _CONVERSATION_INTERNAL_FIELDS}
    projected = _project_pending_handoff(conv.get("pending_handoff"))
    if projected is not None:
        out["pending_handoff"] = projected
    return out


def _derive_conversation_title(prompt: str) -> str:
    """First-prompt title: sanitize control/bidi + truncate. No LLM call."""
    from agent.adk_tools import _team_log_sanitize

    return _team_log_sanitize(prompt, 60) or "(untitled)"


def _resolve_chat_conversation(
    state: StateStore, conversation_id: str | None, workload: str,
    *, ephemeral: bool = False,
) -> dict:
    """Resolve the conversation for a /chat turn (crew-lock enforced).

    Absent id  -> new conversation (created lazily at persist time).
    Unknown id -> 404 (never silently fork on a typo / stale client).
    Crew-lock mismatch -> 409.
    ``ephemeral`` -> a throwaway turn that never persists (the model validator
    already forbids pairing it with a conversation_id, so this always takes the
    fresh-conversation branch). The id is minted but never written.
    Returns ``{conversation_id, workload, is_new, prior_turns, ephemeral}``.
    """
    if ephemeral or conversation_id is None:
        return {
            "conversation_id": str(uuid.uuid4()),
            "workload": workload,
            "is_new": True,
            "prior_turns": [],
            "ephemeral": ephemeral,
            # No document yet, so no proposal was open when this turn began.
            "pending_digest": None,
        }
    conv = state.get_conversation(conversation_id)
    if conv is None:
        raise HTTPException(
            status_code=404,
            detail="conversation not found",
            headers={"Cache-Control": "no-store"},
        )
    if conv.get("workload") != workload:
        raise HTTPException(
            status_code=409,
            detail=(
                f"conversation is locked to crew {conv.get('workload')!r}; "
                f"start a new chat to talk to {workload!r}"
            ),
            # The lock's own answer to "then who DOES own this?", machine-
            # readable rather than only in the prose above. Every other way a
            # client learns the current crew is a separate request that may
            # fail, which leaves the one failure this refusal should be
            # incapable of: a composer stuck on a departed crew, refused every
            # time it tries, by a message naming a crew it never adopts. This
            # header is the backstop — it rides the refusal itself, so it
            # cannot be the thing that goes missing.
            headers={
                "Cache-Control": "no-store",
                "X-Conversation-Crew": str(conv.get("workload") or ""),
            },
        )
    return {
        "conversation_id": conversation_id,
        "workload": workload,
        "is_new": False,
        "prior_turns": conv.get("turns", []),
        "ephemeral": False,
        # The proposal this turn is answering, identified rather than merely
        # counted. Compared again at persist time so a run that started before
        # a redemption cannot edit a proposal minted after it.
        "pending_digest": (conv.get("pending_handoff") or {}).get("nonce_digest"),
    }


def _mint_handoff_for_turn(conv: dict, result: dict) -> tuple[dict | None, str | None]:
    """Turn a crew's handoff proposal into a ``pending_handoff`` + its nonce.

    Returns ``(pending, nonce)``, both None when this turn proposed nothing (or
    proposed something the server refuses to record). The nonce is minted HERE,
    one step before the commit, and released to the client only after the commit
    succeeds — the model never holds the credential its own proposal will be
    confirmed with.
    """
    from agent.handoff import build_pending_handoff, mint_handoff_nonce

    proposal = result.get("handoff")
    if not isinstance(proposal, dict) or not proposal.get("to"):
        return None, None
    if proposal.get("from") != conv["workload"]:
        # Defense in depth: the tool derives ``from`` from the request's bound
        # workload, so a mismatch means something else wrote this. Drop it
        # rather than record a route whose origin we cannot vouch for.
        log.warning("handoff_proposal_origin_mismatch")
        return None, None
    nonce, digest = mint_handoff_nonce()
    return build_pending_handoff(
        proposal, digest=digest, now=dt.datetime.now(dt.timezone.utc),
    ), nonce


def _acquire_chat_run(state: StateStore, conv: dict) -> bool:
    """Claim the conversation for this turn. Fail-OPEN on a store error.

    The lease protects transcript ATTRIBUTION, not authority: the caller
    already holds the crew it is about to run, and no ordering of these
    requests grants tools it could not otherwise reach. So a store hiccup must
    not refuse an operator's turn — it degrades to today's behavior.

    Worth saying plainly rather than leaving implicit: failing open sacrifices
    the exact property the lease exists for. Without a stored lease a redemption
    can flip the crew mid-run, and ``append_turns`` revalidates neither the
    holder nor the current workload, so this turn can land in the transcript
    attributed to the crew that has since left. That is a wrong audit line, not
    a wrong permission, and it is the deliberate trade: a Firestore blip is far
    more likely than a concurrent redemption, and refusing the operator's turn
    outright is a worse answer to it than a mislabelled row.
    """
    if conv.get("ephemeral"):
        return True
    run_id = uuid.uuid4().hex
    try:
        if not state.begin_chat_run(
            conv["conversation_id"], run_id=run_id,
            now=dt.datetime.now(dt.timezone.utc),
        ):
            return False
    except Exception:  # noqa: BLE001
        log.warning("chat_run_lease_acquire_failed", exc_info=True)
        return True
    conv["run_id"] = run_id
    return True


def _release_chat_run(state: StateStore, conv: dict) -> None:
    run_id = conv.pop("run_id", None)
    if not run_id:
        return
    try:
        state.finish_chat_run(conv["conversation_id"], run_id=run_id)
    except Exception:  # noqa: BLE001 — the lease TTL is the backstop
        log.warning("chat_run_lease_release_failed", exc_info=True)


def _persist_chat_turn(
    state: StateStore, *, conv: dict, prompt: str, trace_id: str | None,
    result: dict,
) -> dict | None:
    """Atomically append the turn pair (+ any handoff proposal). Fail-soft.

    Returns None when nothing persisted, else ``{"conversation_id", "handoff"}``
    — the caller attaches those to the response ONLY on a non-None result, so we
    never hand the client an id that resolves to nothing, nor a nonce for a
    proposal that did not commit. The whole exchange (incl. lazy creation for a
    new conversation, and the proposal) is one transaction, so there are no
    half-turns and no orphaned credentials.

    An ``ephemeral`` turn writes nothing and returns None, so both transports
    (JSON + SSE) omit the conversation_id automatically — the single place that
    keeps probe traffic out of the operator's conversation history. A handoff
    proposed on an ephemeral turn is therefore dropped too, which is correct:
    there is no conversation for a transition to land on.

    ``conv["omit_user_turn"]`` suppresses the user row. Set by the handoff
    redemption path, where the operator confirmed a suggestion rather than
    typing the prompt — recording their confirmation as a typed message would
    put words in the operator's mouth in the durable transcript.
    """
    if conv.get("ephemeral"):
        return None
    try:
        turns = []
        if not conv.get("omit_user_turn"):
            turns.append(
                {"role": "user", "text": prompt, "workload": conv["workload"],
                 "trace_id": trace_id}
            )
        turns.append(
            {"role": "crew", "text": result.get("reply") or "",
             "workload": conv["workload"], "trace_id": trace_id,
             "iac_pr": result.get("iac_pr"),
             "tool_calls": result.get("tool_calls")}
        )
        create_with = (
            {"workload": conv["workload"],
             "title": _derive_conversation_title(prompt)}
            if conv.get("is_new") else None
        )
        pending, nonce = _mint_handoff_for_turn(conv, result)
        state.append_turns(
            conv["conversation_id"], turns, create_with=create_with,
            # The crew this turn actually ran as, captured at request entry. If
            # a redemption moved the conversation while this run was in flight,
            # the store keeps the turn rows but refuses this run's edits to the
            # open proposal — a stale writer must not delete or overwrite the
            # joining crew's live suggestion. See _may_touch_pending_handoff.
            expect_workload=conv["workload"],
            expect_pending_digest=conv.get("pending_digest"),
            pending_handoff=pending,
            # An operator turn ANSWERS any outstanding suggestion: being asked
            # "shall I bring in Provision?" and replying with something else is
            # a reply, so the proposal is spent and retires with this write.
            # Gated on a typed row, not merely on "no new proposal": the
            # redemption path omits the user turn precisely because the
            # operator did not type it, and a crew's own follow-up must not
            # answer a question that was put to the operator.
            clear_pending_handoff=any(t["role"] == "user" for t in turns),
        )
        conv["is_new"] = False
        out: dict = {"conversation_id": conv["conversation_id"]}
        # Present only on the turn a confirmed handoff runs: which crew just
        # took over. Carried on ``conv`` by the redemption endpoint so BOTH
        # transports report it — the JSON caller named no crew in its request,
        # so this is the only place it learns which one answered.
        if conv.get("crew_change"):
            out["crew_change"] = conv["crew_change"]
        if pending is not None and nonce is not None:
            out["handoff"] = {
                "from": pending["from"],
                "to": pending["to"],
                "reason": pending["reason"],
                # The one and only time this value is ever transmitted.
                "nonce": nonce,
                "expires_at": pending["expires_at"].isoformat(),
            }
        return out
    except Exception:  # noqa: BLE001 — reply already produced; never break it
        log.warning("chat_turn_persist_failed", exc_info=True)
        return None


async def _persisting_chat_stream(
    workload: str, prompt: str, conv: dict, trace_id: str | None,
    session_id: str | None, *, autonomy_mode: str, demo_anon: bool = False,
    denied_tools: frozenset[str] | None = None,
):
    """Wrap _chat_stream: seed prior turns in, persist the new turn out.

    Single persist site for the SSE path (all crews) and the JSON provision path
    (both already route through _chat_stream). The fan-out's internal delegation
    to run_chat_stream is invisible here, so we persist exactly once. The
    caller-supplied ADK session_id is forwarded unchanged (separate concept from
    conversation_id).

    Also the single release site for the conversation's run lease: the lease is
    acquired at request entry (so a busy thread gets a clean 409 before any
    streaming starts) and released in the ``finally`` here, AFTER persistence.
    Releasing earlier would let a redemption land between the run and its own
    commit, which is exactly the interleave the lease exists to prevent.
    """
    state = get_state()
    try:
        async for item in _chat_stream(
            workload, prompt, session_id, autonomy_mode=autonomy_mode,
            prior_turns=conv["prior_turns"], demo_anon=demo_anon,
            denied_tools=denied_tools,
        ):
            if item.get("type") == "result":
                # Off-load the (possibly Firestore-transactional) write to a
                # thread so a slow commit can't stall the SSE done/heartbeat
                # frames or other async requests on the event loop.
                persisted = await asyncio.to_thread(
                    _persist_chat_turn, state, conv=conv, prompt=prompt,
                    trace_id=trace_id, result=item,
                )
                if persisted:
                    item = {**item, **persisted}
            yield item
    finally:
        _release_chat_run(state, conv)


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
            # TRACE_LOG_LOOKBACK_DAYS lets a deployment with longer log
            # retention widen the /trace search window; unset/blank/malformed
            # falls back to the fetcher's own default (≥ the 365d bucket).
            _trace_fetcher_singleton = CloudLoggingFetcher(
                project=s.gcp_project,
                **_trace_lookback_kwarg(),
            )
    return _trace_fetcher_singleton


def _trace_lookback_kwarg() -> dict[str, int]:
    """Read TRACE_LOG_LOOKBACK_DAYS into a kwarg dict (empty on unset/bad).

    Returning a dict rather than a value lets the caller fall through to
    ``CloudLoggingFetcher``'s own default when the env var is absent or
    non-numeric, so the default lives in exactly one place.
    """
    raw = os.environ.get("TRACE_LOG_LOOKBACK_DAYS")
    if not raw:
        return {}
    try:
        days = int(raw)
    except ValueError:
        return {}
    return {"lookback_days": days} if days > 0 else {}


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
# 25s accommodates the fetcher's two-phase query: the fast narrow phase
# (~1.5s, serves live polls / the post-turn backfill / recent opens) plus a
# worst-case retention-deep wide phase (~17s measured at a 400-day floor) for
# old traces. The prior 5.0s budget was tuned for the narrow-only world and
# turned EVERY wide query into a 503 (2026-07-06 /trace outage) — and since
# ``fut.cancel()`` can't kill a running thread, timing out early never even
# saved the executor slot.
_TRACE_FETCH_TIMEOUT_S = 25.0
# Max entries pulled per /trace fetch. A result at this cap is treated as
# possibly-truncated (see the guard in ``get_trace``), so it's never cached as
# a complete timeline.
_TRACE_FETCH_LIMIT = 500

# In-process completion cache. Keyed by trace_id; value is (written_at,
# payload). Only completed-AND-stable timelines are cached (see
# ``_observe_and_check_stability`` below) — in-flight traces refetch
# every poll so the UI sees fresh events.
_TRACE_CACHE: dict[str, tuple[float, dict]] = {}
_TRACE_CACHE_TTL_S = 300.0

# Soft cap mirroring ``_OBSERVATIONS_SOFT_CAP`` below — same leak class:
# expiry is lazy and per-key on read, so a completed trace cached once and
# never re-opened (the common case for one-shot recheck/chat runs) has no
# other eviction path. FIFO eviction by insertion order. Sizing: entries
# here are full /trace payloads (tens of KiB, much bigger than observation
# entries), so the cap is smaller: 256 × ~50 KiB ≈ 12 MiB ballpark — the
# cap bounds entry COUNT, not bytes; payload size itself is unbounded.
_TRACE_CACHE_SOFT_CAP = 256

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


# apply_status values recorded when an apply REQUEST has already ended (the
# _record_iac_decision docstring: ``applied`` is the apply-succeeded pointer —
# the apply request is done even though the merge may still reconcile later;
# ``failed`` / ``failed_state_suspect`` / ``ambiguous`` are failure
# terminals). ``waiting_for_rebake`` is deliberately EXCLUDED: it is recorded
# BEFORE the merge / re-bake work as the crash-recovery pointer
# (the create-class path), so its trace may still be streaming events —
# those timelines keep the never-cached behavior.
_IAC_RUN_ENDED_STATUSES = frozenset(
    {"applied", "failed", "failed_state_suspect", "ambiguous"}
)


def _iac_run_ended(decision: object) -> bool:
    """True when the decision doc proves the trace's request already finished.

    Feeds the ``require_final_response`` relaxation in
    :func:`_observe_and_check_stability`: iac_apply traces never emit
    ``final_response``, so THEIR completion marker is a run-ended decision
    doc instead.

    Keys on ``apply_status`` ALONE — deliberately. Serve-time
    ``reconcile_merge_state()`` can promote ``merge_state`` failed→merged on
    the fly but never touches ``apply_status``, so this predicate cannot be
    flipped by a serve-time transform. Do NOT extend it to read
    ``merge_state`` without revisiting that.
    """
    return (
        isinstance(decision, dict)
        and decision.get("action") == "iac_apply"
        and decision.get("apply_status") in _IAC_RUN_ENDED_STATUSES
    )


def _observe_and_check_stability(
    trace_id: str, events: list[dict], *, require_final_response: bool = True
) -> bool:
    """Decide whether the timeline is complete via OBSERVED stability.

    Two conditions both required for ``complete=True``:

    1. A completion marker is present. For chat/recheck traces this is a
       ``final_response`` event — the agent emits this near the end of
       every run (``_emit_llm_usage`` follows it for token accounting, so
       ``final_response`` is not strictly the very last entry — the
       30-second grace window catches the usage emit and any other tail
       events). ``iac_apply`` traces emit NONE of the six timeline event
       kinds (no LLM loop; their timelines are structurally empty), so they
       can never satisfy this condition — the caller passes
       ``require_final_response=False`` for them, but ONLY when the
       decision doc proves the request already ended (see
       :func:`_iac_run_ended`); the run-ended decision doc IS their
       completion marker. With the flag off, stability alone decides,
       including for an empty event list.
    2. The signature (over every event) has been the SAME for at
       least :data:`_STABILITY_GRACE_S` of WALL-CLOCK time in OUR
       observations. NOT the log entry timestamps — those can arrive
       out of order from Cloud Logging. A later straggler event changes
       the signature, resets stability, and the caller's ≤300s cache TTL
       self-heals once this function is next consulted.

    On a signature change, the observation resets — the new timeline
    has to hold steady for another full grace window before we'd cache
    it. On a "no completion marker" poll (``require_final_response=True``
    and no ``final_response`` present), the observation is dropped
    entirely so a transient empty fetch doesn't pollute the next
    poll's stability check.
    """
    if require_final_response and not any(
        e.get("event") == "final_response" for e in events
    ):
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

    FIFO soft-cap (same pattern and caveats as the
    ``_TRACE_OBSERVATIONS`` eviction in
    :func:`_observe_and_check_stability`): only NEW keys can grow the
    dict, so an overwrite of an existing trace_id never evicts a
    sibling.
    """
    if trace_id not in _TRACE_CACHE and len(_TRACE_CACHE) >= _TRACE_CACHE_SOFT_CAP:
        oldest_key = next(iter(_TRACE_CACHE))
        _TRACE_CACHE.pop(oldest_key, None)
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
    # Delegates so the rollback preview's contract_hash and this key can never
    # be "the same algorithm written twice" (ds-uwc).
    return contract_hash(contract)


def _observed_env_or_none(reader_payload: object) -> dict[str, str] | None:
    """Return the Reader Worker's ``env`` block, or ``None`` if it is not the
    shape we are willing to call an observation (ds-b3m).

    ``None`` is the honest answer for a malformed payload, and it matters which
    way this degrades. The rollback gate treats ``None`` as "no ground truth,
    refuse"; coercing a bad payload to ``{}`` instead would tell every consumer
    that the service genuinely has no env vars, which is a confident wrong
    answer rather than an absent one — and on the gate specifically it would
    turn every contract var into "unreadable" in one step.

    An EMPTY dict from a well-formed payload is left alone: a service really can
    have no env block, and that is an observation, not a failure. Only the
    wrapper shape is checked here — a non-dict payload, a missing/non-dict
    ``env``, or any non-string key or value in it.
    """
    if not isinstance(reader_payload, dict):
        return None
    env = reader_payload.get("env")
    if not isinstance(env, dict):
        return None
    for k, v in env.items():
        if not isinstance(k, str) or not isinstance(v, str):
            return None
    return env


def _cached_rollback_needs_ground_truth(
    cached: dict | None, observed_env: dict[str, str] | None
) -> bool:
    """True when a cached decision must NOT be served to this request (ds-b3m).

    The idempotency cache is consulted BEFORE ``validate()``, so a cache hit is
    a second way to obtain a rollback approval — one that skips the gate
    entirely. The event key is derived from ``live_env`` CONTENT with no
    provenance, and on the ADK path a failed reader read leaves ``live_env``
    reconstructed from ``proposal.env_diffs``. The model has already seen the
    reader's result, so an accurate report reconstructs the exact dict the
    coordinator would have read and hashes identically: an ungrounded request
    lands on a grounded run's key and is handed its rollback approval.

    So the rule is about the CACHED DECISION's action, not the incoming
    proposal's. A first attempt at this scoped an event-key namespace by the
    NEW proposal's action, which still let a grounded ROLLBACK be served to an
    ungrounded request whose model happened to propose ``drift_issue`` — the
    key matched, the guard did not fire, and the response carried the approval
    anyway. What is being handed back is what has to be judged.

    A cached NON-rollback is still served normally: it is what stops a retry
    opening a second PR or issue, and no non-rollback action has a gate that
    reads observed env, so provenance tells us nothing about it.
    """
    if observed_env is not None:
        return False
    return bool(cached) and cached.get("action") == DecisionAction.ROLLBACK.value


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


_READER_RETRY_DELAYS = (0.5, 1.0)

# Per-PHASE read budget, not an end-to-end one. MUST be set explicitly:
# ``worker_client.call`` has no entry for "reader" in _WORKER_DEFAULT_TIMEOUTS,
# so it would otherwise inherit the 30s _HTTPX_TIMEOUT and three attempts could
# burn ~92s of connect+read alone. See the sizing argument in _read_with_retry.
_READER_ATTEMPT_TIMEOUT_S = 9.0

# Retry ADMISSION budget — deliberately not called a deadline, because it is
# not one. It gates whether another attempt may START; it cannot interrupt an
# attempt already running, and a first call that hangs for a minute returns
# without this ever being consulted. What it buys is that a SLOW reader stops
# accumulating attempts, which is the failure mode retries would otherwise make
# worse. See _read_with_retry for the full statement of what is and is not
# bounded. Enforcing a real caller deadline needs an interruptible/off-loop call
# (ds-99u), which is out of scope here.
_READER_RETRY_ADMISSION_S = 25.0


async def _read_with_retry() -> object:
    """Call the Reader Worker, retrying a transient failure a bounded number of
    times before giving up (ds-q38).

    Exists for liveness, not correctness. The coherence gate refuses to record
    anything when this read fails — which is right, because nothing would
    corroborate what the agent saw — but on the autonomous lane that refusal has
    NO retry driver behind it: a failed read is not a Cloud Run mutation, so it
    emits no audit log and no new Eventarc delivery, the original event is
    already fast-acked, and ``_EventarcCoalescer`` only reruns when a separate
    delivery set ``dirty``. So a single transient blip would drop the only audit
    for a real drift, silently and indefinitely — the same class of outcome as
    the bug being fixed.

    **Sized to be cheaper than the single call it replaced — with the limits of
    that claim stated, because the obvious version of it is false.** The read
    this wraps used to be one ``worker_client.call("reader", {})`` inheriting
    the default 30s timeout, and it runs inside a request that already spent
    20-120s on the agent turn and may still owe the Rollback Worker's /propose,
    against Cloud Run's 300s request timeout. So each attempt is given 9s.

    What that 9s does NOT buy is an end-to-end bound on one attempt. A scalar
    httpx timeout applies **per phase** (connect, read, write, pool), not to the
    call as a whole, and ``worker_client.call`` mints an ID token before the
    HTTPX client is even reached — metadata-server work with its own retries and
    timeouts, repeated on every attempt. So "9 x 3 + sleeps = 28.5s" is a
    configured-budget comparison, not a wall-clock guarantee, and it is labelled
    that way in the test that pins it. Codex round 6 caught the stronger claim.

    **Nothing here is a deadline, and the earlier drafts of this docstring said
    otherwise twice.** ``_READER_RETRY_ADMISSION_S`` gates whether another
    attempt may START — it cannot interrupt one already running, so a single
    call that hangs returns whenever it returns and never consults it. It is
    also not a cap on the total: three ordinary 9s failures cost 28.5s and are
    all admitted, because each admission check passes at the moment it runs.
    What it does buy is that a SLOW reader stops accumulating attempts, which is
    precisely the case where retrying makes things worse rather than better.

    A genuine caller deadline would need the blocking call to be interruptible
    or off-loop (ds-99u). Until then the honest summary is: attempts are bounded
    per phase by httpx, their COUNT is bounded by admission, and total wall
    clock is bounded by neither.

    9s per attempt is also enough to survive a Reader COLD START, and a cold
    start is precisely the transient this is for: attempt 1 may time out while
    the container boots, and attempt 2 then finds it warm.

    One honest limitation: ``worker_client.call`` is synchronous, so each
    attempt blocks the event loop for its duration. That is pre-existing — the
    single call it replaced blocked the same loop the same way — and the budget
    above reduces the configured worst case rather than growing it. Moving the
    call off-loop is a real improvement but a separate one (ds-99u), filed
    rather than smuggled in here.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    last: Exception | None = None
    for delay in (*_READER_RETRY_DELAYS, None):
        try:
            return worker_client.call(
                "reader", {}, timeout=_READER_ATTEMPT_TIMEOUT_S
            )
        except Exception as e:  # noqa: BLE001 — re-raised below if all fail
            last = e
            if delay is None:
                break
            # Admission, not interruption: stop stacking attempts on top of a
            # read that has already eaten the budget. Checked BEFORE sleeping so
            # the sleep is counted rather than being what overshoots.
            if loop.time() - started + delay >= _READER_RETRY_ADMISSION_S:
                break
            await asyncio.sleep(delay)
    assert last is not None
    raise last


def _revision_or_none(reader_payload: object) -> str | None:
    """The Reader Worker's ``revision``, or ``None`` if it is not a usable one.

    Separate from :func:`_observed_env_or_none` because it answers a different
    question and must not weaken that one. ``read_live_state`` documents an
    empty ``revision`` as a real state (a service whose deploys have all
    failed), so an empty string is NOT a revision we can compare and is folded
    into ``None`` here — the caller treats unknown as unverifiable, not as
    equal.
    """
    if not isinstance(reader_payload, dict):
        return None
    revision = reader_payload.get("revision")
    if not isinstance(revision, str) or not revision:
        return None
    return revision


def _observation_skew(
    observed_env: dict[str, str],
    analyzed_env: dict[str, str],
) -> list[str]:
    """Names on which what the agent OBSERVED disagrees with what the event key
    is HASHED FROM (ds-q38). Empty list == the decision and its key describe the
    same world.

    ``analyzed_env`` is the Reader Worker's own response to the agent's
    ``read_live_env_tool`` call, reported back through ``run_agent``'s
    ``reader_sink``; ``observed_env`` is this request's own post-turn read. Two
    observations, one comparison — which is what makes this a coherence check
    rather than a re-reading of the model's report.

    The key is hashed from the post-turn read, not from the read the agent
    reasoned over, and the two can straddle a deploy. On 2026-07-29 one did: the
    agent read ``PAYMENT_MODE=mock`` at 05:28:28, a revision carrying ``live``
    was created at 05:28:30, and the post-turn read at 05:28:40 saw ``live``.
    The resulting ``no_op`` ("no configuration drift is present") was persisted
    under the DRIFTED env's key, where it then permanently outranked every
    correct rollback proposal for that env — a ``no_op`` row has no TTL, only
    rollbacks expire.

    Deliberately NOT driven by ``proposal.env_diffs``. That collection is
    incomplete by contract — the drift prompt asks for variables that DIFFER,
    the deterministic classifier emits ``no_op`` with ``env_diffs=[]``, and the
    validator accepts it — so a diff-driven comparison iterates zero entries and
    pronounces the world coherent no matter how far it has moved. That was the
    first attempted fix, and it would have left the bug intact.

    Not a validator and not a second copy of the ds-b3m gate: it asks only
    whether the row we are about to persist is ABOUT the world its key names.
    Callers supply two real observations or refuse before reaching here.
    """
    # Whole-snapshot comparison: an ADDED or REMOVED variable is skew just as
    # much as a changed one.
    return sorted(
        name
        for name in set(analyzed_env) | set(observed_env)
        if analyzed_env.get(name) != observed_env.get(name)
    )


def _cached_decision_is_contradicted(
    cached: dict | None, proposal: DecisionProposal
) -> bool:
    """True when a cached ``no_op`` must not be served to a request whose own
    fresh proposal is an ACTION (ds-q38).

    Scoped to a cached ``no_op`` on purpose, and the asymmetry is the point:
    ``no_op`` is the only action that persists a claim about the world
    ("nothing is wrong here") while creating no artifact an operator can see.
    Every other cached action already leaves a PR, an issue, or an approval —
    serving one again is the idempotency this cache exists to provide, and
    re-proposing over it would duplicate that artifact.

    Failing toward silence is what makes the ``no_op`` direction the dangerous
    one: the operator sees nothing at all, so there is no dead link or stale
    PR to notice. A fresh non-``no_op`` proposal is grounded in a read taken
    now, so when the two disagree the cached row is the one describing a world
    that has moved on.

    Deliberately NOT keyed off the model-reported env (ds-b3m): the cache is a
    second route to a rollback approval, so what is compared here is the
    ACTION the pipeline arrived at, never values the model handed us.
    """
    if not cached:
        return False
    if cached.get("action") != DecisionAction.NO_OP.value:
        return False
    return proposal.action != DecisionAction.NO_OP


def _cached_decision_is_stale(
    cached: dict | None, proposal: DecisionProposal
) -> bool:
    """Single "may this cached decision still be served" question, so the two
    reasons a cache entry can be dead cannot be checked in one place and
    forgotten in the other.

    Both call sites below must ask the SAME question — the CAS-loser re-read
    especially, or it hands back the very row it just declined to serve. That
    is the ds-uwc/desk lesson again: a predicate consulted in three places and
    guarded in two is a bug with a delay on it.
    """
    if not cached:
        return False
    return _cached_rollback_is_expired(cached) or _cached_decision_is_contradicted(
        cached, proposal
    )


@app.get("/healthz")
@app.get("/health")
def healthz():
    # `/health` is the externally reachable alias. Cloud Run's GFE reserves
    # paths ending in `z` (Cloud Run "Known issues") and intercepts `/healthz`
    # with its own 404 before the request reaches FastAPI — so any external
    # uptime check or runbook smoke must hit `/health` instead. Keep
    # `/healthz` for in-cluster / unit-test callers that already wired to it.
    return {"ok": True}


@app.get("/iac-apply/reachability")
def iac_reachability(
    _: None = Depends(verify_token),
) -> Response:
    """Read-only diagnostic: can the coordinator reach its downstream workers?

    Phase C5c GO/NO-GO gate. After the coordinator is moved onto Direct VPC
    egress (so internal-ingress workers become reachable) the live question is
    binary: does the coordinator's outbound path to its ``*.run.app`` workers
    actually work, or did the ``run.app`` private DNS zone rewrite blackhole it?
    A broken network gate returns a 403/404 *pre-app*, which the C5c plan warns
    is "trivially mistaken for auth failure" — so this endpoint fans
    :func:`worker_client.probe_worker_health` out across EVERY configured worker
    and reports per-worker reachability plus a single ``go`` verdict.

    Token-guarded via :func:`verify_token` exactly like ``/recheck`` /
    ``/decisions`` — so it is curl-able on the tagged no-traffic revision URL
    (``X-DriftScribe-Token`` header) during the staged smoke, and behind
    Cloudflare Access later. The token is accepted via header ONLY (verify_token
    reads the header / CF JWT header — there is no query-param token path).

    Pure read-only fan-out of GETs to each worker's canonical (POST) path: no
    GitHub, no GCS, no approval, no mutation (GET on a POST route is inert).
    ``Cache-Control: no-store`` because a stale cached verdict during a cutover
    would be actively misleading.

    The signal is ``app_reached`` (status NOT in {401, 403, 404}), not bare
    ``reachable``. ``/healthz`` is GFE-reserved (404 pre-app), and for the
    internal-ingress ``tofu_apply`` a 404 is indistinguishable from an ingress
    rejection — so the probe GETs the canonical POST path and takes the app's
    **405** as proof the request traversed network → ingress → IAM → app router.
    A ``401/403`` is an auth/IAM reject a real ``/apply`` would also hit, so it
    is NOT green. See :func:`worker_client.probe_worker_health`.

    Gates (the source of truth for the worker set is
    :data:`worker_client._WORKER_URL_ENV`, iterated here so a new worker can
    never be silently omitted):

    * ``worker_healthy`` — the ``tofu_apply`` worker (the sole infra mutator,
      the NEW path C5c enables) is ``app_reached``: its app router answered
      ``405`` (not a pre-app 404, not an auth-reject 401/403). For an
      internal-ingress service this unambiguously proves the VPC routing delivers
      the call AS INTERNAL.
    * ``all_siblings_reachable`` — every NON-``tofu_apply`` worker is
      ``app_reached`` (the rewritten DNS zone didn't regress its route to a
      pre-app 404 / auth reject). A worker whose URL is unset counts as NOT
      reached: this is
      fail-closed — a sibling URL silently dropped from the deploy must block the
      cutover rather than let it through (in prod all siblings have URLs set).
    * ``go = worker_healthy AND all_siblings_reachable``.

    Status codes: ``503`` when ``TOFU_APPLY_URL`` is unset (the new path cannot
    exist yet — body still carries ``results`` for diagnosis); ``200`` when
    ``go``; ``502`` otherwise.
    """
    results = [
        worker_client.probe_worker_health(worker)
        for worker in worker_client._WORKER_URL_ENV
    ]
    by_worker = {r["worker"]: r for r in results}

    tofu_apply_result = by_worker.get("tofu_apply")
    # tofu_apply has no configured URL → the new path can't exist yet. Fail
    # closed at 503, but still hand back results so the operator can diagnose
    # the rest of the fan-out in the same call.
    if tofu_apply_result is None or tofu_apply_result["error"] == "url_unset":
        return JSONResponse(
            status_code=503,
            content={
                "go": False,
                "detail": "TOFU_APPLY_URL not configured",
                "results": results,
            },
            headers={"Cache-Control": "no-store"},
        )

    worker_healthy = tofu_apply_result["app_reached"]
    all_siblings_reachable = all(
        r["app_reached"] for r in results if r["worker"] != "tofu_apply"
    )
    go = worker_healthy and all_siblings_reachable

    return JSONResponse(
        status_code=200 if go else 502,
        content={
            "go": go,
            "worker_healthy": worker_healthy,
            "all_siblings_reachable": all_siblings_reachable,
            "results": results,
        },
        headers={"Cache-Control": "no-store"},
    )


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
    user_msg: str, *, workload: str = "drift", autonomy_mode: str,
    reader_sink: list | None = None,
) -> DecisionProposal:
    """Thin wrapper so integration tests have a stable patch target.

    Lazy-imports `agent.adk_agent` so the Google ADK SDK doesn't load on the
    non-ADK code path. Patching `agent.main._run_adk_agent` (rather than
    `agent.adk_agent.run_agent`) preserves the lazy-load benefit AND keeps
    the test patch site stable across spec evolution.

    ``workload`` selects the workload-scoped agent. Defaults to ``"drift"``
    so any pre-17.A.3 patch site that calls this with a positional
    ``user_msg`` only still works. ``autonomy_mode`` is a REQUIRED keyword
    arg — forwarded to :func:`agent.adk_agent.run_agent` so the dial filters
    the tool set at Layer 0.

    ``reader_sink`` (ds-q38) collects what the Reader Worker actually told the
    agent, so the caller can verify the decision describes the world its
    idempotency key names. Optional and keyword-only so every existing patch
    site that mocks this function keeps working — a mock that ignores it
    simply reports no observation, which the caller treats as "the agent never
    looked" rather than as agreement.
    """
    from agent.adk_agent import run_agent

    return await run_agent(
        user_msg,
        workload=workload,
        autonomy_mode=autonomy_mode,
        reader_sink=reader_sink,
    )


def _notify_rollback_approval(rendered: str) -> dict[str, Any]:
    """Deliver a rendered approval body to the operator channel; return a SAFE
    record of what happened.

    **Advisory by contract.** Every failure is captured and classified — no
    delivery outcome may strand a minted approval, because the decision row
    (already durable by the time this runs) is what makes the approval
    reachable, not the webhook. This is the same stance
    ``propose_rollback_tool`` has always taken on the chat lane.

    **NEVER persists exception text**, and that is a security property, not
    tidiness. ``WorkerClientError.__str__`` embeds the worker's response body
    (``agent/worker_client.py``); the Notifier puts the DOWNSTREAM webhook's
    body snippet into its own 502 detail (``workers/notifier/main.py``); and a
    webhook that echoes the request — ``httpbin.org/post`` does exactly that —
    round-trips the notification body, which carries the tokened
    ``approval_url``. ``GET /decisions`` is served anonymously during a public
    demo window (``infra/cloudflare/worker/src/proxy.js``), so persisting
    ``str(e)`` here would publish a live single-use rollback credential to any
    visitor. Classification only: never ``str(e)``, ``e.body``, or a downstream
    snippet.
    """
    try:
        worker_client.call(
            "notifier",
            {
                "channel": "approval",
                "severity": "high",
                # ds-thm: ``rendered`` is already bounded at render time (see
                # _do_rollback), which is the guarantee that matters. This is a
                # second, independent bound for the OTHER callers of this
                # helper and for any future one that forgets — it is a no-op on
                # an already-conforming body.
                "body": normalize_notifier_body(rendered),
            },
        )
    except WorkerClientError as e:
        # Expected class: the notifier or its downstream refused. Only the
        # numeric status is safe to keep — see the docstring. The log call is
        # itself suppressed: this function's contract is that NOTHING escapes
        # it, and a pathological handler would otherwise be the one path that
        # still turned a delivery failure into a request failure (same posture
        # as adk_tools._notify_approval_pending).
        with contextlib.suppress(Exception):
            log.warning(
                "rollback_notify_failed",
                extra={"error_code": "worker_error", "notify_status": e.status_code},
            )
        return {
            "state": "failed",
            "error_code": "worker_error",
            "status_code": e.status_code,
        }
    except Exception as e:  # noqa: BLE001 — advisory: must never strand approval
        # NOT a WorkerClientError: a bug in the client, a transport surprise, a
        # serialization error. This class is OURS, so we want to know — but only
        # the exception TYPE is logged, never the message or a traceback. An
        # earlier draft used log.exception and justified it as "Cloud Logging
        # only, never the body"; that is not something the raising code
        # guarantees. A serialization error is precisely the class that quotes
        # the payload it choked on, and the payload here is the rendered body
        # carrying the tokened approval url. Cloud Logging is access-controlled,
        # not a safe place for a live credential (Codex review). Same posture as
        # adk_tools' store-failure logging, which records type(e).__name__ for
        # this reason.
        with contextlib.suppress(Exception):
            log.warning(
                "rollback_notify_failed_unexpected",
                extra={"error_code": "internal_error", "exc_type": type(e).__name__},
            )
        return {"state": "failed", "error_code": "internal_error"}
    return {"state": "delivered"}


def _do_rollback(
    s: Settings,
    proposal: DecisionProposal,
    event_key: str,
    trigger: str,
    *,
    autonomy_mode: str,
) -> dict:
    """ROLLBACK control flow: propose-via-worker → render → record → notify.

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
    - ROLLBACK: ``claim_event → propose → render → RECORD → notify``. Render
      REQUIRES the approval URL from the worker's response, so it cannot run
      until the propose call has succeeded. Claiming the event BEFORE propose
      means a concurrent retry can't double-mint approval docs. On any failure
      up to and including the decision write, the claim is released so retries
      can proceed.

    ds-hdt — why the decision row is written BEFORE the notification, and why
    a failed notification no longer fails the request:

    * The row IS the operator's surface. The desk renders a pending rollback
      straight off ``approval.approval_url``
      (``frontend/src/lib/approval.ts:isRollbackAwaitingOperator``), so an
      approval with a row is reachable and an approval without one is not:
      ``ApprovalStore`` is primary-key-only (no list), and
      ``/infra/pending-approvals`` covers IaC PRs alone.
    * Nothing retries us. Since the Eventarc fast-ack (#268) ``/eventarc``
      returns 200 and runs the audit in a background task, so a coordinator
      that dies between notify and record is not re-delivered by Pub/Sub.
      Recording second would strand the approval with no surface AND no
      retry — the exact ds-hdt outage, where a 503 webhook meant the
      autonomous self-heal could never complete.
    * Releasing the claim on a notify failure would now be actively wrong.
      That release existed because the approval was unreachable, so a retry
      minting a fresh one was the only recovery; with the row durable, a
      retry would mint a SECOND live approval for one drift.

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
        # ds-q38: reached only after the caller's cache check already declined
        # (and CAS-evicted) whatever was here, so a contradicted ``no_op``
        # surfacing NOW means the Firestore store's event_key recovery query
        # resurrected it between the evict and a competitor's re-claim
        # (ds-bej). Handing it back would answer a rollback proposal with
        # "no configuration drift is present" — the exact ds-q38 failure, one
        # layer down. 409 instead: the caller retries and the winner's real
        # decision is there by then.
        if _cached_decision_is_stale(existing, proposal):
            raise HTTPException(status_code=409, detail="event in-progress, retry")
        if existing:
            return existing
        raise HTTPException(status_code=409, detail="event in-progress, retry")

    if autonomy_mode == "observe":
        # Observe suppression (ClickOps item 11) — a deliberate DIVERGENCE
        # from the dry_run behavior documented above. dry_run still calls
        # /propose so demos get an approval URL; the dial is an operator
        # trust boundary, not a demo convenience. In Observe NOTHING leaves
        # the coordinator: no approval doc is minted, no notification is
        # sent. The decision row below is the only artifact. The event was
        # claimed above so Eventarc retries don't re-run the LLM.
        rendered = (
            f"DriftScribe proposed a rollback to revision "
            f"{proposal.target_revision} but did not create the approval — "
            f"the autonomy dial is set to Observe. Raise the dial to Propose "
            f"to let rollback proposals mint operator approvals.\n\n"
            f"Rationale: {scrub_rationale_text(proposal.rationale, proposal.env_diffs)}"
        )
        decision_id = str(uuid.uuid4())
        response = {
            "decision_id": decision_id,
            "event_key": event_key,
            "trace_id": current_trace_id_or_new(),
            "action": "rollback",
            "decision_path": "adk",
            "rendered_body": rendered,
            "rationale": proposal.rationale,
            "diffs": [d.model_dump(mode="json") for d in proposal.env_diffs],
            "target_revision": proposal.target_revision,
            "requires_human_review": True,
            "dry_run": s.dry_run,
            # NO "dry_run_effective": that field disambiguates the
            # propose-despite-dry-run behavior, which did not happen here.
            # NO "approval": nothing was minted — readers must not see a
            # null-shaped approval and branch on it.
            "autonomy_mode": "observe",
            "suppressed_by_autonomy": True,
            "trigger": trigger,
        }
        state.record_decision(decision_id, event_key, response)
        return response

    # Side effect #1: mint the approval via the Rollback Worker. The worker
    # owns the HMAC key, the Firestore approvals collection write, and the
    # TTL; the coordinator only receives the resulting URL.
    try:
        propose_result = worker_client.call(
            "rollback",
            {
                "target_revision": proposal.target_revision,
                # Scrub before the worker stores it: the rollback worker renders
                # `reason` on the operator approval page (workers/rollback), so a
                # secret quoted in the rationale would leak there. The notification
                # body (render_rollback_body below) is already scrubbed; this
                # closes the `reason` boundary too. (PR 2)
                #
                # ds-j0i: then CLAMP to the worker's own bound. Sending this
                # unbounded is what took autonomous self-heal down on
                # 2026-07-31 — a 581-char rationale against the worker's
                # then-500-char cap returned 422, so no approval was minted.
                # normalize_rollback_reason also supplies a deterministic
                # fallback for an EMPTY rationale, which the worker's
                # min_length=1 would reject the same way. Scrub first, clamp
                # second — redaction changes length.
                "reason": normalize_rollback_reason(
                    scrub_rationale_text(proposal.rationale, proposal.env_diffs)
                ),
                # ds-uwc: lets the worker record what this rollback would
                # change. Additive and optional on the worker; see
                # contract_preview_payload.
                **contract_preview_payload(s.contract_path),
            },
            # The rollback worker is --concurrency=1, so this can queue behind a
            # blocking /execute (up to its 60s LRO cap). The default 30s budget
            # would time out while the request is still queued — and a client
            # timeout does not cancel server work, so the worker could go on to
            # create the approval whose raw token is returned exactly once,
            # orphaning it while the retry below mints a second. See the
            # constant's own comment.
            timeout=worker_client.QUEUED_BEHIND_EXECUTE_TIMEOUT,
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

    # Validate the approval tuple with the SAME correlation the chat lane uses
    # (ds-y5i). This replaced a bare truthiness check on url/id, which was
    # tolerable only while the webhook was the operator's surface: a malformed
    # pair simply produced a bad notification. Now the decision row is the
    # surface, and an unvalidated pair persists a desk CTA that 404s, never
    # retires (an unparseable ``expires_at`` fail-safes to "not expired" in
    # both the SPA and ``_cached_rollback_is_expired``), or — with an id/url
    # disagreement — joins its status from one approval while the operator's
    # click executes another. A row is only worth writing if the approval it
    # names is REACHABLE.
    #
    # Local import mirrors the existing ``_team_log_sanitize`` call below:
    # agent.adk_tools imports agent.main lazily in the other direction.
    from agent.adk_tools import _validated_approval

    validated = _validated_approval(propose_result)
    if validated is None:
        # Malformed worker response — bail rather than render a broken body.
        # Release the claim so the operator can retry once the worker is fixed.
        state.release_event(event_key)
        raise HTTPException(
            status_code=502,
            detail=(
                "rollback worker response did not carry a usable approval "
                "(id / url / expiry correlation failed); refusing to record a "
                "decision whose approval the operator could not act on"
            ),
        )
    # ``approval_token`` is deliberately dropped here: _validated_approval
    # returns it (the chat lane must hand it to the caller), but the autonomous
    # lane persists this dict, and the token already rides inside approval_url.
    approval_url = validated["approval_url"]
    approval_id = validated["approval_id"]
    expires_at = validated["expires_at"]
    # ds-hdt: the row gets a RELATIVE url; the notification keeps the absolute
    # one. _approval_url_matches deliberately does NOT require the origin to be
    # ours (see its docstring + ds-x5l): a worker whose COORDINATOR_URL has
    # drifted would otherwise lose rollbacks entirely, and the residual was
    # accepted because "the SPA declines the CTA, but the chat reply and the
    # webhook still carry the raw string, so the operator can click it there".
    #
    # That accepted residual does not survive THIS change. The autonomous lane
    # has no chat reply, and the webhook is the thing that is broken — so the
    # desk is the only surface, and the desk is exactly what drops an
    # off-origin url (safeApprovalHref → selectPendingRollback). A drifted
    # COORDINATOR_URL would therefore reproduce the ds-hdt symptom this change
    # exists to remove: a durable decision the operator cannot act on.
    #
    # Rejecting off-origin here would trade that for the outage the docstring
    # warns about. Canonicalizing costs nothing instead: every frontend
    # consumer already funnels this through safeApprovalHref, which keeps only
    # ``pathname + search`` and throws the origin away. A host-less url is
    # inherently same-origin — the same property iacApprovalHref is built on —
    # so it survives config drift AND cannot name a foreign host. The path and
    # the single ``t`` pair were both exact-validated above, so this is a
    # reconstruction of verified parts, not a rewrite of an untrusted string
    # (and the result still satisfies _approval_url_matches, which accepts a
    # schemeless path-relative url).
    approval_url_for_row = (
        f"/approvals/{approval_id}?t={validated['approval_token']}"
    )

    # render_rollback_body is a pure function over the proposal + URL, so it
    # *shouldn't* raise — but if a future renderer change introduces a code
    # path that does, we must release the claim. Without this, a renderer
    # exception would leave the event claimed and perma-409 subsequent retries.
    try:
        # ds-thm: bounded to the Notifier's declared ``body`` cap AT RENDER, so
        # the fixed template — the approval URL and the traffic warning — is
        # never part of any text that gets cut. Clamping the assembled body
        # instead meant repairing whatever Markdown the cut had broken, which
        # took six wrong attempts and still could not be made sound; see
        # render_rollback_body's docstring.
        # ds-thm: the notification renders this as a Markdown autolink
        # (``<url>``), and CommonMark autolinks require an ABSOLUTE URI — a
        # relative ``/approvals/…`` renders as literal text with no link at
        # all. ``_approval_url_matches`` deliberately accepts the relative form
        # (see above), so the shapes the validator admits are wider than the
        # shapes the renderer can make clickable: the same producer/consumer
        # contract mismatch this bead is about, in URL shape rather than length.
        #
        # Canonicalized against OUR configured origin rather than rejected,
        # because rejecting here would strand an already-minted approval
        # (ds-hdt). If neither origin is configured the link cannot be made
        # absolute by anyone, and the body still carries the path as text.
        # "Is it absolute?" is decided with the SAME semantics the validator
        # used to accept it — ``urlsplit``'s scheme, which is case-insensitive.
        # A ``startswith(("http://", "https://"))`` check disagrees on
        # ``HTTPS://…``: the validator admits it, the prefix check calls it
        # relative, and the result is
        # ``https://coordinator/HTTPS://worker/approvals/…`` — a link that
        # renders perfectly and points at a coordinator path that does not
        # exist. Two checks for the same question must not use two definitions
        # (Codex); that disagreement is this bead's whole subject.
        notify_url = approval_url
        try:
            absolute = bool(urllib.parse.urlsplit(notify_url).scheme)
        except ValueError:  # pragma: no cover — validated upstream
            absolute = False
        if not absolute:
            origin = (s.coordinator_origin or "").rstrip("/")
            if origin:
                notify_url = f"{origin}/{notify_url.lstrip('/')}"
        rendered = render_rollback_body(
            proposal, notify_url, max_chars=NOTIFIER_BODY_MAX_CHARS
        )
    except Exception as e:
        state.release_event(event_key)
        raise HTTPException(
            status_code=500, detail=f"rollback render failed: {e}"
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
            # Relative on purpose — see the canonicalization note above.
            "approval_url": approval_url_for_row,
            "expires_at": expires_at,
        },
        # ds-hdt: delivery is settled AFTER this row is durable, so it starts
        # as ``pending`` and is patched once. A reader that sees ``pending``
        # must read it as "not yet known", and an ABSENT ``notify`` key (every
        # row written before ds-hdt) as "never recorded" — neither is
        # "delivered". Same unknown-≠-empty rule the desk already follows.
        "notify": {"state": "pending"},
        # Every new rollback decision records the dial mode it was made under
        # (propose / propose_apply here — observe short-circuits above).
        "autonomy_mode": autonomy_mode,
        "trigger": trigger,
    }
    # Persist BEFORE notifying — see the ds-hdt block in this function's
    # docstring. The row is the operator's surface; the notification is a
    # courtesy on top of it.
    try:
        state.record_decision(decision_id, event_key, response)
    except Exception as e:
        # No durable surface, so do NOT notify: a delivered link to an approval
        # that appears nowhere is the ds-hdt failure wearing a different hat.
        # Release the claim so a retry can mint a fresh approval; the orphan is
        # bounded by the worker's 15-min execute-time expiry check.
        state.release_event(event_key)
        # Type only, no message and no traceback: the value that failed to
        # write is this decision document, which carries the tokened approval
        # url, and a store exception may echo the document it rejected.
        log.error(
            "rollback_decision_record_failed",
            extra={"exc_type": type(e).__name__},
        )
        raise HTTPException(
            status_code=502,
            detail="rollback decision could not be recorded; no notification sent",
        ) from e

    # Side effect #2: ask the Notifier worker to deliver the rendered body to
    # the operator-facing channel. severity="high" tracks the approval-required
    # nature; channel="approval" routes to the operator inbox. Advisory — see
    # _notify_rollback_approval.
    notify_outcome = _notify_rollback_approval(rendered)
    response["notify"] = notify_outcome
    try:
        state.set_decision_notify_outcome(decision_id, notify_outcome)
    except Exception:  # noqa: BLE001 — the row is already durable + actionable
        # Only the delivery annotation is lost; the approval remains reachable,
        # so this must not fail the request. The row keeps ``pending``, which
        # is honest: we genuinely no longer know what happened to the delivery.
        #
        # Full traceback is safe HERE, unlike the two sites above: the value
        # handed to the store is ``{"notify": <classification>}`` — three scalar
        # keys, no credential — so an exception that echoes its payload has
        # nothing sensitive to echo. The distinction is the payload, not the
        # log level; don't collapse these three sites to one style.
        log.exception(
            "rollback_notify_outcome_patch_failed",
            extra={"decision_id": decision_id},
        )
    return response


# Workloads with no autonomous /recheck pipeline — chat-only by design.
# ``explore`` is strictly read-only and exists only as a free-form /chat
# surface; ``provision`` (Phase D) authors IaC edits and opens ONE PR from
# /chat — neither has a DecisionProposal renderer / observation pass, so
# /recheck refuses them early (see the guard at the top of _do_recheck).
# Kept as an explicit set (not a schema flag) to mirror the inline
# upgrade /recheck refusal below — both are routing facts owned here.
CHAT_ONLY_WORKLOAD_NAMES: frozenset[str] = frozenset({"explore", "provision"})


# Workloads that have a LIVE autonomous trigger — they run without anyone
# asking. This is the source of truth for the operator-facing "Autonomous"
# vs "On-demand" signal (GET /capabilities ``autonomous`` field; the crew
# picker's group + badge). It is deliberately NOT derived from a workload's
# ``observation_kind`` (which encodes *intent*, not a wired trigger): only
# ``drift`` actually fires on its own in this build —
#   * Eventarc audit-log events hit ``/eventarc`` with workload HARDCODED to
#     "drift" (see the eventarc handler below); no other workload has a
#     trigger bound to it.
#   * ``/recheck workload="upgrade"`` returns 503 (the upgrade autonomous
#     pipeline is unimplemented, post-Phase-17), and explore/provision are
#     chat-only (CHAT_ONLY_WORKLOAD_NAMES above) — so none of the other
#     three can run unprompted.
# Honest labelling beats aspirational labelling: if an upgrade/Patch trigger
# is ever wired (a real ``/eventarc-upgrade`` or scheduler), add it here and
# the UI follows automatically. Both this set and CHAT_ONLY_WORKLOAD_NAMES
# are routing/trigger facts owned in this module; ``agent.capabilities``
# imports this lazily (mirroring ``test_chat_only_coherence_with_main``).
AUTONOMOUS_TRIGGER_WORKLOADS: frozenset[str] = frozenset({"drift"})


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

    # Autonomy dial (ClickOps item 11): read ONCE here — fail-closed to
    # observe — and thread the mode through the rest of the pipeline. In
    # Observe the agent runs (tool-filtered) and the decision is RECORDED,
    # but the GitHub action / rollback worker calls are SUPPRESSED below.
    # The /recheck pipeline is NEVER refused by the dial (contrast pause's
    # full stop); observing is the point of Observe.
    autonomy = _autonomy_state_fail_closed()

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
        # ds-q38: read BEFORE the turn as well as after it. The key is hashed
        # from the post-turn read, but the decision was reasoned out over
        # whatever the agent saw somewhere in between — and on 2026-07-29 a
        # deploy landed in that gap, so a "no drift" verdict was filed under the
        # DRIFTED env's key and outranked every correct rollback for it
        # afterwards.
        #
        # Bracketing the turn is what makes the two comparable WITHOUT having to
        # observe the agent's own tool calls. If the before and after reads
        # agree, the world was stable across the whole turn, so whatever the
        # agent read in between — once, five times, or never — necessarily saw
        # the same env. That proof holds no matter how the agent behaves, which
        # a check built on the agent's reported diffs or on capturing its tool
        # results cannot claim: diffs are incomplete by contract (``no_op`` with
        # ``env_diffs=[]`` is legal), and a captured tool result never reaches
        # this task anyway — ADK runs each function call in its own
        # ``asyncio.Task``, and a Task starts from a COPY of the context, so a
        # ContextVar written inside a tool is invisible here. Measured:
        # child sees the snapshot, parent sees None.
        #
        # ds-q38: collect what the Reader Worker actually told the agent, so
        # the decision can be checked against the world its key will name.
        # Bracketing the turn with the coordinator's own reads was tried first
        # and is strictly weaker: it proves the WORLD held still, never that
        # the agent looked at it, so an agent that skipped the read entirely
        # (``no_op`` with ``env_diffs=[]`` is legal and the validator accepts
        # it) sailed through. Reading the agent's own tool results answers both
        # questions with one mechanism — and costs no extra Reader call.
        reader_observations: list[dict] = []
        _workload_token = set_workload(workload)
        try:
            try:
                proposal = await _run_adk_agent(
                    user_msg,
                    workload=workload,
                    autonomy_mode=autonomy.mode,
                    reader_sink=reader_observations,
                )
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
            #
            # ds-q38: retried, because refusing on this read has no retry driver
            # behind it. A failed read is not a Cloud Run mutation, so it emits
            # no audit log and no new Eventarc delivery; the original event was
            # already fast-acked, and the coalescer only reruns when a SEPARATE
            # delivery marked it dirty. Without this, one transient Reader blip
            # loses the only audit for a real drift, indefinitely. Bounded (not
            # a loop) so a Reader that is genuinely down still fails fast, and
            # cheap next to the model turn that already ran — this retries the
            # read alone, never the turn.
            _post_payload = await _read_with_retry()
            live_env = _observed_env_or_none(_post_payload)
            post_turn_revision = _revision_or_none(_post_payload)
        except Exception:
            live_env = None
            post_turn_revision = None
        # ds-q38: the agent's OWN observations decide what may be persisted.
        # One invariant, no degraded branches: a decision is recorded only when
        # the agent looked exactly once and what it saw is what this request
        # sees now. Every other shape refuses, because every other shape means
        # the row would describe a world nobody confirmed.
        #
        # The two previous drafts both leaked here. Hashing the key from
        # ``proposal.env_diffs`` when a read fails uses a SUBSET that can
        # legally be empty, so a populated service's decision lands under the
        # `{}`-env key. And hashing it from a coordinator read the agent never
        # saw proves nothing about the analysis. Both were "tolerances" that
        # amounted to keeping the poisoning.
        distinct_observations = []
        for obs in reader_observations:
            if obs not in distinct_observations:
                distinct_observations.append(obs)
        if live_env is None:
            # Nothing this request read. Keep the historical shape exactly:
            # hash from the model's reported diffs so the pipeline still
            # reaches the ds-b3m gate, which refuses any rollback because
            # ``observed_env`` is None. The coherence gate REFUSES this case
            # too, but does so after ``validate`` so ds-b3m keeps its own
            # 502 — the same ordering lesson as the skew check below.
            # Sentinel `<ABSENT>` keeps live=None distinct from live="".
            observed_env = None
            live_env = {
                d.name: "<ABSENT>" if d.live is None else d.live
                for d in proposal.env_diffs
            }
        else:
            observed_env = live_env
    else:
        try:
            # Reader Worker enforces TARGET_SERVICE/region/project via its own
            # boot config (Layer 2); the coordinator no longer passes them and
            # no longer holds project-wide roles/run.viewer (Phase 13 trim).
            reader_payload = worker_client.call("reader", {})
        except WorkerClientError as e:
            # Same 502 semantics as before — a Reader Worker failure is still
            # an upstream-dep failure from the operator's POV. The classifier
            # path has no fallback; without live_env we cannot classify.
            raise HTTPException(status_code=502, detail=f"reader worker failed: {e}")
        observed_env = _observed_env_or_none(reader_payload)
        if observed_env is None:
            # The call succeeded but the payload is not the shape we require.
            # The classifier cannot work from it either, so this is the same
            # class of failure as the exception above and gets the same 502 —
            # rather than being coerced into an empty dict, which would read to
            # every downstream consumer as "the service has no env vars".
            raise HTTPException(
                status_code=502,
                detail="reader worker returned a malformed env payload",
            )
        live_env = observed_env
        # Coherent by construction on this path: the classifier is a pure
        # function of the very read the key is hashed from, so the analyzed and
        # observed snapshots are the same object. Set explicitly rather than
        # left None so the skew check compares snapshots here too, instead of
        # silently dropping to the diff-list fallback.
        # No agent turn on this path — the classifier is a pure function of
        # this single read, so the decision and its key are the same
        # observation by construction and there is nothing to reconcile. The
        # gate below is scoped to ``s.use_adk`` for exactly that reason.
        distinct_observations: list[dict] = []
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
    # ds-q38: a cached decision that may no longer be served — expired rollback
    # or a ``no_op`` this run's proposal contradicts — held back for eviction
    # until AFTER validate() + the coherence gate. See the branch that sets it
    # for why the CAS cannot happen at lookup time.
    stale_cached: dict | None = None
    if not force:
        existing = state.find_decision_for_event(event_key)
        if existing:
            if _cached_rollback_needs_ground_truth(existing, observed_env):
                # Refuse rather than fall through and re-propose: falling
                # through would mint a SECOND approval for the same event.
                raise HTTPException(
                    status_code=502,
                    detail=(
                        "adk proposal rejected by safety gate: cached rollback "
                        "not served — no observed live env is available to "
                        "corroborate it, and the idempotency cache is read "
                        "before the gate. Retry once the reader is reachable."
                    ),
                )
            if _cached_decision_is_stale(existing, proposal):
                # NOT evicted here — remembered, and the CAS happens after
                # validate() + the coherence gate. The ordering is the whole
                # point, and it now covers BOTH staleness reasons.
                #
                # Evicting at lookup time lets a request that is about to be
                # REFUSED destroy good cached state on its way out: a
                # policy-invalid proposal fails validate(), or an incoherent one
                # fails the gate, but the pointer is already gone. The decision
                # document survives, the Firestore store's event_key recovery
                # query keeps resurfacing it (ds-bej), and because
                # ``evict_cached_decision`` compare-and-deletes a pointer that
                # no longer exists, nothing can ever evict it again — every
                # later request takes the CAS-loser branch and 409s forever.
                #
                # The expired-rollback branch used to evict here safely, because
                # nothing between the CAS and the re-propose could refuse. Since
                # ds-q38 added refusals after this point that is no longer true,
                # so it moves too rather than being left as the one path that
                # still mutates before the request has earned it.
                stale_cached = existing
            else:
                return existing

    try:
        # ds-b3m: ``observed_env`` — NOT ``live_env``. On the ADK path those two
        # differ exactly when the Reader Worker read failed: ``live_env`` is
        # then a reconstruction from ``proposal.env_diffs``, kept only so the
        # idempotency key stays stable, while ``observed_env`` is None. Passing
        # ``live_env`` here would hand the model's own diff array back to the
        # gate as "observed state" — a strict check whose subject was laundered
        # by its caller, which is the defect class this repo keeps re-shipping.
        validate(proposal, contract, live_env=observed_env)
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

    # ds-q38: refuse to PERSIST a decision that is not about the world its key
    # names. ``event_key`` above is hashed from the POST-turn read, while
    # ``proposal`` was reasoned out over whatever the agent saw during the
    # turn. On 2026-07-29 a deploy landed in that gap: the agent read
    # ``PAYMENT_MODE=mock``, a revision carrying ``live`` was created 1.5s
    # later, and the post-turn read saw ``live``. The resulting ``no_op`` was
    # filed under the DRIFTED key, where it outranked every correct rollback
    # proposal for that env from then on — a ``no_op`` row has no TTL.
    #
    # The gate compares what the agent ACTUALLY observed (reported back through
    # ``reader_sink``) with what the key is hashed from. An earlier draft
    # bracketed the turn with the coordinator's own reads instead; that proved
    # the world held still but never that the agent looked at it.
    #
    # AFTER ``validate``, deliberately. ds-b3m's validator already refuses a
    # skewed ROLLBACK, and refuses it with a 502 whose shape says "the model
    # responded and the safety gate refused" — non-retryable on purpose.
    # Running this gate first would convert that into a retry-shaped 409 and
    # silently retire a distinction ds-b3m's tests exist to protect. What was
    # missing was never the rollback case: it is that NOTHING refused a skewed
    # ``no_op``, the one action that persists "nothing is wrong here" while
    # creating no artifact an operator could notice.
    #
    # Dropping the event is safe and self-healing because the change that
    # caused the skew announces itself: a service mutation emits an audit log
    # (v1 ``ReplaceService`` or v2 ``UpdateService`` depending on the client —
    # both wired as triggers, see infra/scripts/setup_secrets.sh) and therefore
    # its own delivery. One arriving mid-audit sets ``_EventarcCoalescer.dirty``
    # and the trailing rerun re-audits a consistent world; one arriving later
    # starts a fresh audit. ``_run_eventarc_audit_once`` catches HTTPException
    # and logs ``_rejected``, and the coalescer loop still honours ``dirty``
    # after a refusal — pinned by
    # ``test_a_refused_audit_still_gets_its_trailing_rerun``.
    #
    # The recovery is therefore only as good as trigger coverage: a mutation
    # path that emits neither methodName would skew an audit with nothing
    # scheduled to look again. That is the same coverage assumption the
    # autonomous lane already rests on, not a new one introduced here.
    #
    # ⚠️ Scope, stated precisely: this refuses to PERSIST. A cache HIT above
    # returns before any of it, so "everything else is a 409" is true of new
    # rows only — an already-cached decision can still be served for a key this
    # request never corroborated (notably when the post-turn read failed and the
    # key came from ``proposal.env_diffs``). That serves a stale answer; it does
    # not create a poisoned row, and closing it means moving the cache lookup
    # after the gate, which would change ds-b3m's ordering. Left explicit rather
    # than implied.
    if s.use_adk:
        refusal = None
        if not distinct_observations:
            # The agent never read live env, so nothing ties its verdict to any
            # world. The workload prompt requires the call, but a prompt is not
            # an enforcement boundary.
            refusal = "the agent did not read live state"
        elif len(distinct_observations) > 1:
            # It watched the world move and there is no honest answer to "which
            # reading is the conclusion about" — ADK may run function calls in
            # parallel, so arrival order is not authority either.
            refusal = "the agent saw live state change while it was reasoning"
        elif observed_env is None:
            # This request could not read, so nothing corroborates that what
            # the agent saw still holds. The historical behaviour hashed the
            # key from ``proposal.env_diffs`` here — a SUBSET that can legally
            # be empty, so a populated service's decision could land under the
            # `{}`-env key and collide with a genuinely empty one later.
            refusal = "live state could not be read after the agent ran"
        else:
            analyzed = distinct_observations[0]
            # Revision as well as env. Env equality alone is weaker than it
            # looks: revisions A(mock) -> B(live) -> C(mock) present identical
            # env at both ends while the world moved twice, and
            # ``read_live_state`` pairs env with the revision it came from
            # precisely so the two are not reasoned about separately. Unknown
            # counts as a mismatch — a check that cannot see its subject must
            # fail, not abstain.
            if (
                analyzed["revision"] is None
                or post_turn_revision is None
                or analyzed["revision"] != post_turn_revision
            ):
                refusal = "the serving revision changed while the agent was reasoning"
            elif _observation_skew(observed_env, analyzed["env"]):
                refusal = "live state changed while the agent was reasoning"
        if refusal is not None:
            # Count-only logging — never env names or values. Both this detail
            # and log fields are read by operators, and this is exactly the
            # text class that has carried live env values before, which is why
            # the notify path persists a classification rather than a message.
            log.warning(
                "recheck_unverifiable_observation",
                extra={
                    "trigger": trigger,
                    "workload": workload,
                    "observation_count": len(distinct_observations),
                    "post_read_ok": observed_env is not None,
                    "action": proposal.action.value,
                },
            )
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{refusal}, so this decision could not be confirmed to "
                    f"describe the state it would be keyed on. Not recorded; "
                    f"retry."
                ),
            )

    if stale_cached is not None:
        # NOW the fresh proposal has earned the right to replace what was
        # cached: it passed the deterministic validator and it is about the same
        # world its key names. Compare-and-delete, never an unconditional
        # release — two concurrent Eventarc deliveries seeing the same stale row
        # must not both re-propose and double-mint approvals for one drift.
        stale_id = stale_cached.get("decision_id")
        if not (stale_id and state.evict_cached_decision(event_key, stale_id)):
            # CAS lost: the pointer now names a DIFFERENT decision, so another
            # request is already repairing this key. Re-read and hand back its
            # result if it has landed and is itself servable; otherwise 409 so
            # the caller retries cleanly rather than falling through to claim an
            # event slot the winner is mid-flight on.
            #
            # Note this branch is NOT taken when the pointer is simply absent —
            # that returns True, because a repair that died after evicting must
            # stay retryable rather than wedging the key at 409 forever. See
            # evict_cached_decision's docstring; the property is pinned by
            # test_a_failed_repair_leaves_the_key_retryable.
            existing = state.find_decision_for_event(event_key)
            if _cached_rollback_needs_ground_truth(existing, observed_env):
                raise HTTPException(
                    status_code=502,
                    detail=(
                        "adk proposal rejected by safety gate: cached rollback "
                        "not served — no observed live env is available to "
                        "corroborate it."
                    ),
                )
            if existing and not _cached_decision_is_stale(existing, proposal):
                return existing
            raise HTTPException(status_code=409, detail="event in-progress, retry")
        log.info(
            "recheck_evicted_stale_decision",
            extra={
                "trigger": trigger,
                "workload": workload,
                "cached_action": stale_cached.get("action"),
                "fresh_action": proposal.action.value,
            },
        )

    # ROLLBACK branches out before render because the render needs the
    # approval URL minted by the Rollback Worker's /propose. The Phase 11.9
    # carry-over #3 safety property — no rollback executes without operator
    # approval — lives in _do_rollback: it only proposes + notifies, never
    # mutates Cloud Run.
    if proposal.action == DecisionAction.ROLLBACK:
        return _do_rollback(
            s, proposal, event_key, trigger, autonomy_mode=autonomy.mode
        )

    rendered = _render_for(proposal.action, proposal)

    # Claim the event BEFORE any side effects so retries don't spawn duplicate
    # PRs/issues. If the claim is refused (race), look up the recorded
    # decision; if no decision yet, surface 409 so the caller can retry.
    claimed = state.record_event(event_key, {"trigger": trigger})
    if not claimed:
        existing = state.find_decision_for_event(event_key)
        # THIRD cached-rollback return site, and the least obvious one. Only a
        # NON-rollback proposal reaches here (a rollback branched out at
        # _do_rollback well above), so it looks unrelated — but the request can
        # miss the cache on its first lookup, lose the claim to a CONCURRENT
        # grounded run that recorded a ROLLBACK under the same key, and have
        # this re-read hand that rollback's approval back.
        #
        # The first two sites were guarded and this one was not, which is the
        # argument for a named predicate over an inline condition: "may this
        # request be served this decision" has to be asked everywhere a
        # decision is served, and it is easy to find two of three.
        if _cached_rollback_needs_ground_truth(existing, observed_env):
            raise HTTPException(
                status_code=502,
                detail=(
                    "adk proposal rejected by safety gate: cached rollback not "
                    "served — no observed live env is available to corroborate "
                    "it."
                ),
            )
        # ds-q38 completes the set the comment above argues for: this site now
        # asks the SAME "may this request be served this decision" question as
        # the other three. A contradicted ``no_op`` can reach here the same way
        # a rollback can — the first lookup missed, the claim was lost, and the
        # re-read resurfaced a row (ds-bej) that says nothing is wrong while
        # this run concluded something is.
        if _cached_decision_is_stale(existing, proposal):
            raise HTTPException(status_code=409, detail="event in-progress, retry")
        if existing:
            return existing
        raise HTTPException(status_code=409, detail="event in-progress, retry")

    # Autonomy dial (ClickOps item 11): in Observe the pipeline observes,
    # decides, and RECORDS — but does not touch GitHub. The decision row is
    # the operator-visible "would have" artifact; the rail renders it
    # distinctly (suppressed_by_autonomy). no_op is never a side effect, so
    # it is never suppressed (the dry-run preview shape is preserved). In
    # Propose / Propose+Apply the action executes exactly as before.
    suppressed = autonomy.mode == "observe" and proposal.action != DecisionAction.NO_OP
    if suppressed:
        github_result = {
            "suppressed_by_autonomy": "observe",
            "url": None,
            "action": proposal.action.value,
        }
    else:
        try:
            github_result = _perform_action(s, contract, proposal, rendered)
        except HTTPException:
            # Side effect failed — release the claim so retries can proceed.
            # The patcher's atomic pre-check + branch random suffix mean a
            # retry won't create duplicate partial state.
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
        # Every new drift decision records the dial mode it was made under.
        "autonomy_mode": autonomy.mode,
        "trigger": trigger,
    }
    if suppressed:
        # Only suppressed rows carry this marker — the rail keys its
        # "recorded, not executed" treatment off it.
        response["suppressed_by_autonomy"] = True
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

    workload: Literal["drift", "upgrade", "explore", "provision"] = "drift"

    model_config = ConfigDict(extra="forbid")


class PauseToggleRequest(BaseModel):
    """Request body for POST /pause.

    ``extra="forbid"`` surfaces typo'd fields as 422 rather than silently
    dropping them — critical for an operator-facing toggle where a mistaken
    field name would otherwise be a silent no-op.

    ``reason`` is capped at 500 chars (arbitrary but generous). Empty or
    whitespace-only strings are stripped to ``None`` by the route handler so
    the stored doc is clean (empty reason = no reason provided, not an empty
    string that clutters the audit log).
    """

    paused: StrictBool
    reason: str | None = Field(default=None, max_length=500)

    model_config = ConfigDict(extra="forbid")


class AutonomyToggleRequest(BaseModel):
    """Request body for POST /autonomy.

    ``mode`` is Literal-constrained so an unknown mode is a 422 at the edge,
    never an ambiguous write. ``extra="forbid"`` surfaces typo'd fields as 422
    rather than silently dropping them. ``reason`` is capped at 500 chars;
    empty / whitespace-only strings are stripped to ``None`` by the route
    handler so the stored audit doc stays clean.
    """

    mode: Literal["observe", "propose", "propose_apply"]
    reason: str | None = Field(default=None, max_length=500)

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
    # Pause gate (kill switch): refuse 423 before any recheck work. ``force``
    # does NOT bypass — pause outranks force (a kill switch the operator can
    # accidentally override is not a kill switch). Read fail-closed per request.
    if _pause_state_fail_closed().paused:
        raise HTTPException(status_code=423, detail=PAUSED_DETAIL)
    workload = (req or RecheckRequest()).workload
    # Serve-time rationale scrub (PR 2): wrapping the handler return covers
    # _do_recheck's fresh response, its cached-existing return, AND the rollback
    # response it routes through _do_rollback — one site, all paths.
    return scrub_decision_rationale(
        await _do_recheck("manual_recheck", force=force, workload=workload)
    )


# Module-level Google auth transport: verify_oauth2_token needs a transport
# instance to fetch Google's signing-key JWKS. Constructing it once at import
# time avoids allocating a new ``requests.Session`` per /eventarc call.
_GOOGLE_AUTH_TRANSPORT = GoogleAuthRequest()

# Serializes EVERY use of the shared transport above: it wraps a single
# ``requests.Session``, which is not documented thread-safe, and its two
# users run on different threads — /eventarc verifies on
# ``asyncio.to_thread`` worker threads, the (sync-route) infra-graph
# prewarm verify on FastAPI's threadpool. Pre-offload, /eventarc calls
# were implicitly serialized by blocking the event loop; the lock
# preserves that property without the stall. Both endpoints are
# low-frequency, so contention is negligible.
_GOOGLE_AUTH_TRANSPORT_LOCK = threading.Lock()


@app.post("/eventarc")
async def eventarc(
    request: Request,
    background_tasks: BackgroundTasks,
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
    - **200 dispatched** — in-scope event acknowledged; the recheck runs
      as a background task AFTER this response. Body is
      ``{"dispatched": "background", "trigger": "eventarc", service,
      region}`` — it never carries the decision (rationale can quote live
      secret values; /runs/{id} and /decisions are the scrubbed read
      surfaces). 2026-07-29 incident: the handler used to await the full
      Anchor turn (20–120s) here, but Pub/Sub push only waits
      ``ackDeadlineSeconds`` for the response, so every delivery was
      treated as failed and redelivered for hours — saturating the
      maxScale=1/concurrency=2 service ("Rate exceeded." for operators).
      Ack-fast is the Pub/Sub-documented contract for slow handlers.
      NOTE: background work needs CPU after the response — the service
      must run with CPU always allocated (``--no-cpu-throttling``), or
      the recheck stalls once the request closes.
    - **_do_recheck failures** — logged, never propagated. A non-2xx here
      would make Eventarc redeliver into the same storm the fast-ack
      exists to prevent. Recovery story mirrors the pause gate: the drift
      is re-discovered by the next audit event or a manual /recheck. See
      ``_eventarc_background_recheck``.

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
    # ``asyncio.to_thread``: the verify does a blocking JWKS HTTPS fetch on
    # every call (no caching — see above) and this is an async route, so
    # calling it bare would stall the shared event loop — including every
    # in-flight /chat SSE stream — for the duration of the fetch. The lock
    # serializes access to the shared requests.Session transport (see
    # ``_GOOGLE_AUTH_TRANSPORT_LOCK``).
    def _verify_serialized() -> dict:
        with _GOOGLE_AUTH_TRANSPORT_LOCK:
            return verify_oauth2_token(
                token, _GOOGLE_AUTH_TRANSPORT, audience=s.eventarc_audience
            )

    try:
        claims = await asyncio.to_thread(_verify_serialized)
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

    # Pause gate (kill switch): checked HERE — AFTER the service/region whitelist
    # so an off-target event NEVER reads the flag (that ordering is load-bearing:
    # a Firestore outage must not turn every drive-by audit event into a
    # fail-closed read), and BEFORE _do_recheck so no recheck runs while paused.
    # 200-ignored (NOT 423): Eventarc retries on non-2xx, so a 423 here would
    # storm the trigger for the whole pause window. The same retry-storm-safe
    # shape as non-target-service — the event is acknowledged and DROPPED, not
    # queued for replay. A drift event that the agent declines to act on while
    # paused is re-discovered by the next manual or scheduled recheck on resume.
    if _pause_state_fail_closed().paused:
        # Structured log so operators can query Cloud Logging for events dropped
        # by the kill switch — the access-log 200 alone is not queryable by cause.
        log.info(
            "eventarc_event_dropped_paused",
            extra={"service": service, "region": region},
        )
        return {"ignored": "paused", "service": service, "region": region}

    # In-scope event: ack now, recheck in the background (2026-07-29
    # incident — see the docstring's 200-dispatched bullet). Starlette
    # background tasks run after the response body is sent, so the request
    # slot is freed and Pub/Sub sees its 200 within the ack deadline
    # regardless of how long the Anchor turn takes.
    #
    # Phase 17.A.3 (Codex blocker): the workload is HARDCODED to "drift"
    # server-side. Cloud Run audit-log events are drift's input source by
    # definition. The caller-presented payload does NOT extend authority
    # to workload selection — any ``workload`` field in the body is
    # ignored. An event-triggered upgrade workload, if ever added, will
    # get its own endpoint with its own server-side binding (e.g.
    # ``/eventarc-upgrade`` against a dependabot-style trigger).
    background_tasks.add_task(_eventarc_background_recheck, service, region)
    return {
        "dispatched": "background",
        "trigger": "eventarc",
        "service": service,
        "region": region,
    }


class _EventarcCoalescer:
    """Single-flight state for the Eventarc background audits.

    Codex review of the fast-ack: ``_do_recheck`` runs the Gemini turn
    BEFORE deriving its idempotency event_key (the key hashes the
    proposal's env_diffs), so N concurrent background audits are N full
    LLM turns — the request slots used to bound that at concurrency=2,
    the background path would not. The audit is payload-blind (it reads
    CURRENT live state, never the triggering event), so coalescing loses
    nothing: one in-flight audit plus at most one trailing rerun covers
    any burst, and the rerun observes everything the coalesced events
    changed.

    Plain bools, no lock: both check-then-set sites run on the single
    event loop with no await between check and set.
    """

    def __init__(self) -> None:
        self.active = False
        self.dirty = False


_EVENTARC_COALESCER = _EventarcCoalescer()


async def _eventarc_background_recheck(service: str, region: str) -> None:
    """Run the Eventarc-triggered recheck after the push delivery was acked.

    Failure containment is the whole point: whatever ``_do_recheck`` raises
    or returns, NOTHING may escape — the 200 is already on the wire, and an
    exception here would surface as an ASGI-cycle error with no one to
    receive it. Outcomes are logged structured, bounded fields ONLY:
    decision content (``rationale``) can quote live secret values, and
    ``HTTPException.detail`` is just as unsafe (validator messages
    interpolate live env values, ADK parse errors embed raw model output,
    worker failures carry response bodies) — so neither ever reaches a log
    field. ``status_code`` alone classifies the rejection.

    - ``eventarc_background_recheck_done`` — decision recorded/reused.
    - ``eventarc_background_recheck_rejected`` — _do_recheck refused with
      an HTTPException (worker 502, claim-race 409, contract 500). The
      event is dropped, not retried; the next audit event, scheduled
      recheck, or manual /recheck re-discovers the drift (same recovery
      story as the pause gate above).
    - ``eventarc_background_recheck_failed`` — unexpected crash (full
      traceback via ``log.exception``) or a malformed non-dict return
      (``reason="non_dict_result"``, no traceback). Never ``_done`` — the
      e2e smoke reads ``_done`` as audit-completed.
    - ``eventarc_background_recheck_coalesced`` — an audit was already in
      flight; this event marked it dirty and the trailing rerun (logged
      as ``_rerun``) covers it. See :class:`_EventarcCoalescer`.
    """
    co = _EVENTARC_COALESCER
    if co.active:
        co.dirty = True
        log.info(
            "eventarc_background_recheck_coalesced",
            extra={"service": service, "region": region},
        )
        return
    co.active = True
    try:
        while True:
            # Cleared BEFORE the audit: an event landing DURING it may
            # postdate the state the audit read, so it must trigger the
            # rerun below.
            co.dirty = False
            await _run_eventarc_audit_once(service, region)
            if not co.dirty:
                break
            log.info(
                "eventarc_background_recheck_rerun",
                extra={"service": service, "region": region},
            )
    finally:
        co.active = False
        co.dirty = False


async def _run_eventarc_audit_once(service: str, region: str) -> None:
    """One contained audit pass — see ``_eventarc_background_recheck``."""
    try:
        decision = await _do_recheck("eventarc", workload="drift")
        # Result handling stays INSIDE the containment: a malformed
        # (non-dict) return must not raise into the ASGI cycle — and it
        # must log ``_failed``, not ``_done``: the e2e smoke's log probe
        # reads ``_done`` as proof the audit completed, so reporting a
        # malformed result as done would greenlight the smoke during
        # contract skew.
        if not isinstance(decision, dict):
            log.error(
                "eventarc_background_recheck_failed",
                extra={
                    "service": service,
                    "region": region,
                    "reason": "non_dict_result",
                },
            )
            return
        log.info(
            "eventarc_background_recheck_done",
            extra={
                "service": service,
                "region": region,
                "decision_id": decision.get("decision_id"),
                "action": decision.get("action"),
            },
        )
    except HTTPException as e:
        log.warning(
            "eventarc_background_recheck_rejected",
            extra={
                "service": service,
                "region": region,
                "status_code": e.status_code,
            },
        )
    except Exception:
        log.exception(
            "eventarc_background_recheck_failed",
            extra={"service": service, "region": region},
        )


# Worker-injected demo marker (see infra/cloudflare/worker/src/proxy.js):
# present EXACTLY on anonymous, token-injected requests during the hackathon
# demo window — the Worker strips any inbound copy before injecting its own,
# and never adds it to CF-JWT-carrying (operator) requests. Spoofing it on a
# direct run.app call only redacts the spoofer's own view (fail-safe), so a
# presence check is sufficient.
_DEMO_ANON_MARKER = "X-DriftScribe-Demo-Anonymous"


def _is_demo_anonymous(request: Request) -> bool:
    return request.headers.get(_DEMO_ANON_MARKER) is not None


@app.get("/runs/{decision_id}")
def get_run(decision_id: str):
    # Sync on purpose — this only reads from the StateStore singleton, no
    # I/O that benefits from async.
    d = get_state().get_decision(decision_id)
    if not d:
        raise HTTPException(status_code=404, detail="decision not found")
    # Serve-time rationale scrub (PR 2) — this read is UNAUTHENTICATED, so a
    # secret quoted in the LLM rationale must not leak by decision_id.
    #
    # Approval-link scrub (hackathon A.2): ALWAYS here, not demo-marker-gated
    # like /decisions — this read is unauthenticated, decision_ids become
    # enumerable through the demo-window /decisions, nothing in the UI
    # consumes /runs, and the operator's approval click-through paths (rail,
    # chat timeline, notifier webhook) don't go through it.
    return scrub_decision_approval(scrub_decision_rationale(d))


@app.get("/decisions")
def list_decisions_endpoint(
    request: Request,
    response: Response,
    limit: int = 50,
    _: None = Depends(verify_token),
    state: StateStore = Depends(get_state),
) -> dict:
    """List past decisions, newest first, for the operator transparency UI.

    Phase 19.A.7 — backs the ``/`` (operator SPA) decision history
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
    # Per-row serve-time transforms (all copy-on-change, never-mutate):
    #   1. scrub_decision_rationale — strip secret-like values from the rationale.
    #   2. attach_iac_pr_link — derive github.url -> the PR for iac_apply rows, from
    #      the trusted config repo, so the rail can link a row to its GitHub PR.
    #   3. reconcile_merge_state — promote a stale applied+merge=failed row to
    #      merged when GitHub confirms the PR merged out-of-band (compute-only, no
    #      persist). The provider memoizes the github client so a window with N
    #      stale rows builds it once, and a warm merge-cache makes zero GitHub calls.
    #   4. attach_approval_status (Task 3.0b) — join the approval doc's status +
    #      resolution timestamp onto rollback rows. The reader memoizes Firestore
    #      reads per approval_id for this request and is fail-soft (never 500s
    #      the rail on a Firestore hiccup).
    settings = get_settings()
    repo = settings.github_repo
    repo_provider = _memoized_repo_provider(settings)
    approval_reader = _memoized_approval_reader()
    rows = [
        attach_approval_status(
            reconcile_merge_state(
                attach_iac_pr_link(scrub_decision_rationale(d), repo),
                repo_provider=repo_provider,
                settings=settings,
            ),
            approval_reader=approval_reader,
        )
        for d in state.list_decisions(limit=limit)
    ]
    # Operator decision 2026-07-09 (docs/plans/2026-07-09-operator-seat-demo-
    # window.md): the anonymous demo-window scrub of approval.approval_url is
    # removed here — a visitor holds the operator seat, so the rail's Approve CTA
    # must work for them. Rollback rows carry the live single-use ?t= token, same
    # as the operator sees; bounds (single-use, 15-min TTL, worker refuses no-op
    # targets, and payment-demo being a fixture rather than a real workload) make
    # handing it out acceptable. 2026-08-01: "self-healing baseline" USED to be a
    # fourth bound here; demo-reset.yml is gone, so restoring the baseline is now
    # a manual operator step (demo-window.sh open checklist, step 0). The RESOURCE
    # scope is unchanged (always fixture-only); the TIME bound is not — recovery
    # went from ~2h to "until an operator notices". Do not re-cite a schedule as
    # part of this acceptance, and do not treat it as grandfathered when the
    # window reopens.
    # Reverses
    # audit A.2's serve-time scrub for /decisions. (/runs stays always-scrubbed —
    # separate justification there.)
    return {"decisions": rows}


# In-process inventory cache for GET /infra/graph (perf). One live Cloud Asset
# Inventory enumeration takes ~25-35s for a real estate, and the Infrastructure
# panel re-fetches on every page load, so without a cache every load spins for
# ~half a minute (measured live). Caching the SUCCESSFUL inventory for a short
# TTL makes reloads instant. Mirrors the ``_TRACE_CACHE`` pattern and is
# deliberately LOCK-FREE: the GIL makes the single tuple read/assign atomic, and
# at the demo's single-operator scale a couple of concurrent misses issuing
# parallel fetches is harmless — herd-coalescing is NOT part of the contract.
# Only successes are cached (see the assignment guard below): a degraded/error
# inventory is never stored, so a transient CAI/worker outage retries on the
# next request instead of being pinned stale for the whole TTL.
# ---- L2 (Firestore) cache store: survives scale-to-zero cold starts -------- #
# Backend selection is gated on gcp_project ALONE — deliberately NOT on dry_run
# like get_state(). The live demo runs DRY_RUN=true (the "Option C" stance),
# under which get_state() picks the in-process InMemoryStateStore; mirroring that
# here would make the Firestore cache in-memory in prod and defeat the whole
# cold-start-survival goal. A read-only resource-map cache has no side effects,
# so DRY_RUN's no-mutations stance is irrelevant to it.
_infra_graph_cache_store_singleton: "InfraGraphCacheStore | None" = None
# Test injection seam (see _set_infra_graph_cache_store_for_tests): plugs an
# in-process / fake store so tests never construct a real Firestore client.
_infra_graph_cache_store_override: "InfraGraphCacheStore | None" = None

# Bump when the persisted payload contract changes so a deploy ignores
# stale-shaped docs written by an older revision. L1 is naturally cleared by an
# instance recycle; L2 survives deploys, so it needs an explicit version gate.
_INFRA_GRAPH_L2_FORMAT_VERSION = 4  # v4: invalidate v3 docs cached before the
# unmatched_iac projection existed — a v3 doc lacks it, so the unmatched-
# declarations band would appear or vanish depending on whether an instance
# served a pre-deploy L2 record. v3: invalidate v2 docs cached from a pre-#193
# worker that never actually populated not_in_iac_control_plane (v2 was bumped
# with the reader code but the worker lagged, so v2 docs hold field-less
# inventories that force build_graph's raw-drift fallback). v2: field added.
# A written_at more than this far in the FUTURE is distrusted (clock skew /
# hand-edited doc) and treated as a miss rather than served stale forever.
_INFRA_GRAPH_L2_CLOCK_SKEW_TOLERANCE_S = 60.0


def get_infra_graph_cache_store() -> InfraGraphCacheStore:
    """Return the process-wide L2 cache store singleton — Firestore when a
    project is configured, else an in-process double."""
    global _infra_graph_cache_store_singleton
    if _infra_graph_cache_store_override is not None:
        return _infra_graph_cache_store_override
    if _infra_graph_cache_store_singleton is None:
        s = get_settings()
        if s.gcp_project:
            _infra_graph_cache_store_singleton = FirestoreInfraGraphCacheStore(
                project=s.gcp_project
            )
        else:
            _infra_graph_cache_store_singleton = InMemoryInfraGraphCacheStore()
    return _infra_graph_cache_store_singleton


def _set_infra_graph_cache_store_for_tests(store: InfraGraphCacheStore) -> None:
    """Test-only: inject an L2 store (in-process / fake) via the override seam."""
    global _infra_graph_cache_store_override
    _infra_graph_cache_store_override = store


# IaC PR-source cache (the approval page "view source" affordance). Same
# backend-selection posture as the infra-graph cache: Firestore when a project is
# configured, else an in-process double. Gating on gcp_project ALONE (not dry_run)
# is deliberate — prod runs DRY_RUN=true, and a read-only cache has no side
# effects, so it must persist regardless (the lesson from the infra-graph L2 cache).
_iac_pr_source_cache_store_singleton: "IacPrSourceCacheStore | None" = None
_iac_pr_source_cache_store_override: "IacPrSourceCacheStore | None" = None
_IAC_PR_SOURCE_FORMAT_VERSION = 1
_IAC_PR_SOURCE_CLOCK_SKEW_TOLERANCE_S = 60.0


def get_iac_pr_source_cache_store() -> IacPrSourceCacheStore:
    """Return the process-wide IaC PR-source cache store singleton."""
    global _iac_pr_source_cache_store_singleton
    if _iac_pr_source_cache_store_override is not None:
        return _iac_pr_source_cache_store_override
    if _iac_pr_source_cache_store_singleton is None:
        s = get_settings()
        if s.gcp_project:
            _iac_pr_source_cache_store_singleton = FirestoreIacPrSourceCacheStore(
                project=s.gcp_project
            )
        else:
            _iac_pr_source_cache_store_singleton = InMemoryIacPrSourceCacheStore()
    return _iac_pr_source_cache_store_singleton


def _set_iac_pr_source_cache_store_for_tests(store: IacPrSourceCacheStore) -> None:
    """Test-only: inject an IaC PR-source store via the override seam."""
    global _iac_pr_source_cache_store_override
    _iac_pr_source_cache_store_override = store


def _reset_iac_pr_source_cache_for_tests() -> None:
    """Test-only: clear the IaC PR-source store singleton + injection override."""
    global _iac_pr_source_cache_store_singleton, _iac_pr_source_cache_store_override
    _iac_pr_source_cache_store_singleton = None
    _iac_pr_source_cache_store_override = None


# --- Open-trace follow-up (2026-06-27): two more per-PR read-through caches. -- #
# Same gcp_project-only gating posture as the source cache (read-only, must
# persist through DRY_RUN=true + scale-to-zero). Both store ONE doc per PR.

# (a) Merge-status reconcile cache: "is PR #n merged at the as-applied head?".
_iac_pr_merge_cache_store_singleton: "PerPrCacheStore | None" = None
_iac_pr_merge_cache_store_override: "PerPrCacheStore | None" = None
_IAC_PR_MERGE_FORMAT_VERSION = 1
_IAC_PR_MERGE_CLOCK_SKEW_TOLERANCE_S = 60.0
# merged=True is TERMINAL (a PR never un-merges) → no TTL expiry while the
# head_sha still matches. merged=False is transient → re-probe after this.
_IAC_PR_MERGE_UNMERGED_TTL_S = 120.0


def get_iac_pr_merge_cache_store() -> PerPrCacheStore:
    """Return the process-wide merge-status cache store singleton."""
    global _iac_pr_merge_cache_store_singleton
    if _iac_pr_merge_cache_store_override is not None:
        return _iac_pr_merge_cache_store_override
    if _iac_pr_merge_cache_store_singleton is None:
        s = get_settings()
        if s.gcp_project:
            _iac_pr_merge_cache_store_singleton = FirestorePerPrCacheStore(
                collection="iac_pr_merge_status", project=s.gcp_project
            )
        else:
            _iac_pr_merge_cache_store_singleton = InMemoryPerPrCacheStore()
    return _iac_pr_merge_cache_store_singleton


def _set_iac_pr_merge_cache_store_for_tests(store: "PerPrCacheStore | None") -> None:
    """Test-only: inject (or, with None, clear) the merge-status store override."""
    global _iac_pr_merge_cache_store_override
    _iac_pr_merge_cache_store_override = store


def _reset_iac_pr_merge_cache_for_tests() -> None:
    """Test-only: clear the merge-status store singleton + injection override."""
    global _iac_pr_merge_cache_store_singleton, _iac_pr_merge_cache_store_override
    _iac_pr_merge_cache_store_singleton = None
    _iac_pr_merge_cache_store_override = None


# (b) PR-body cache: the agent-authored description shown in the open-trace card.
_iac_pr_body_cache_store_singleton: "PerPrCacheStore | None" = None
_iac_pr_body_cache_store_override: "PerPrCacheStore | None" = None
_IAC_PR_BODY_FORMAT_VERSION = 1
_IAC_PR_BODY_CLOCK_SKEW_TOLERANCE_S = 60.0
# A PR body is mutable but stable for merged historical PRs; head_sha is the
# real freshness key, this TTL is only a long backstop.
_IAC_PR_BODY_TTL_S = 86400.0


def get_iac_pr_body_cache_store() -> PerPrCacheStore:
    """Return the process-wide PR-body cache store singleton."""
    global _iac_pr_body_cache_store_singleton
    if _iac_pr_body_cache_store_override is not None:
        return _iac_pr_body_cache_store_override
    if _iac_pr_body_cache_store_singleton is None:
        s = get_settings()
        if s.gcp_project:
            _iac_pr_body_cache_store_singleton = FirestorePerPrCacheStore(
                collection="iac_pr_body", project=s.gcp_project
            )
        else:
            _iac_pr_body_cache_store_singleton = InMemoryPerPrCacheStore()
    return _iac_pr_body_cache_store_singleton


def _set_iac_pr_body_cache_store_for_tests(store: "PerPrCacheStore | None") -> None:
    """Test-only: inject (or, with None, clear) the PR-body store override."""
    global _iac_pr_body_cache_store_override
    _iac_pr_body_cache_store_override = store


def _reset_iac_pr_body_cache_for_tests() -> None:
    """Test-only: clear the PR-body store singleton + injection override."""
    global _iac_pr_body_cache_store_singleton, _iac_pr_body_cache_store_override
    _iac_pr_body_cache_store_singleton = None
    _iac_pr_body_cache_store_override = None


def _memoized_repo_provider(settings: Settings):
    """Return a 0-arg callable that builds the github ``Repo`` AT MOST ONCE per
    request and memoizes it (so GET /decisions with N stale rows constructs the
    client once, not per row — ``get_repo`` is uncached and costs a REST call).
    Fail-soft: a construction error memoizes ``None`` so callers degrade to a
    miss rather than 5xx."""
    box: dict[str, object] = {}

    def provider():
        if "repo" not in box:
            try:
                box["repo"] = get_repo(settings.github_token, settings.github_repo)
            except Exception as e:  # noqa: BLE001 — fail-soft; reconcile must never break a serve path
                log.warning("reconcile_get_repo_failed", extra={"error": type(e).__name__})
                box["repo"] = None
        return box["repo"]

    return provider


def _read_merge_status_cache(pr_number: int, head_sha: str) -> "bool | None":
    """Return the cached merged verdict for ``(pr_number, head_sha)`` or None on a
    miss. Validates defensively (mirrors ``_read_iac_source_cache``): exact
    ``format_version``, head_sha match (a moved/force-pushed head is a miss), a
    bool ``merged``, a finite ``written_at`` not far in the future, and — for a
    transient ``merged=False`` — within the short TTL. ``merged=True`` never
    expires (terminal + head-pinned)."""
    try:
        record = get_iac_pr_merge_cache_store().get(pr_number)
    except Exception as e:  # noqa: BLE001 — a misbehaving store is a miss, never a 5xx
        log.warning("iac_pr_merge_read_error", extra={"error": type(e).__name__})
        return None
    if not isinstance(record, dict):
        return None
    if record.get("format_version") != _IAC_PR_MERGE_FORMAT_VERSION:
        return None
    if record.get("head_sha") != head_sha:
        return None
    merged = record.get("merged")
    if not isinstance(merged, bool):
        return None
    written_at = record.get("written_at")
    if not isinstance(written_at, (int, float)) or not math.isfinite(written_at):
        return None
    age = time.time() - written_at
    if age < -_IAC_PR_MERGE_CLOCK_SKEW_TOLERANCE_S:
        return None  # stamped in the future → distrust
    if merged is False and age > _IAC_PR_MERGE_UNMERGED_TTL_S:
        return None  # a not-yet-merged result is stale → re-probe
    return merged


def _resolve_pr_merged(
    pr_number: int, head_sha: str, *, repo_provider, settings: Settings
) -> "bool | None":
    """Whether PR #``pr_number`` is merged at ``head_sha`` — from cache, else one
    GitHub probe. ``None`` when indeterminate (no token/repo, or a fail-soft
    error). Caches ``True`` ~permanently (terminal) and ``False`` briefly. The
    ``repo_provider`` is only called on a cache miss (so a warm cache makes zero
    GitHub calls)."""
    if not (settings.github_token and settings.github_repo):
        return None
    cached = _read_merge_status_cache(pr_number, head_sha)
    if cached is not None:
        return cached
    repo = repo_provider()
    if repo is None:
        return None
    try:
        merged = bool(github.is_pr_merged_at_head(repo, pr_number, head_sha))
    except Exception as e:  # noqa: BLE001 — GitHub hiccup must not break the serve path
        log.warning(
            "pr_merge_status_probe_failed",
            extra={"error": type(e).__name__, "pr_number": pr_number},
        )
        return None
    # Best-effort persist: the store already swallows write errors, but wrap the
    # call too so even a misbehaving store object can't break the serve path
    # (Codex review — mirrors the _resolve_iac_source caller-side guard).
    try:
        get_iac_pr_merge_cache_store().set(
            pr_number,
            {
                "format_version": _IAC_PR_MERGE_FORMAT_VERSION,
                "head_sha": head_sha,
                "merged": merged,
                "written_at": time.time(),
            },
        )
    except Exception as e:  # noqa: BLE001 — a cache-write failure must not fail the serve
        log.warning("iac_pr_merge_write_error", extra={"error": type(e).__name__})
    return merged


def reconcile_merge_state(decision: object, *, repo_provider, settings: Settings) -> object:
    """Serve-time, COMPUTE-ONLY merge_state reconcile (2026-06-27 follow-up).

    Promote a stale ``apply_status="applied"`` + ``merge_state="failed"`` decision
    to ``merged`` when GitHub confirms the PR is merged AT THE AS-APPLIED
    ``head_sha`` (the out-of-band manual-merge case). NEVER persists: the
    StateStore is append-only and a write from a GET would (a) be a side-effecting
    read and (b) look like a fresh apply to the SPA watermark. The stored doc
    stays faithful to what happened at the time; the UI shows current truth.

    Conventions mirror :func:`scrub_decision_rationale`: returns the input
    unchanged BY IDENTITY for any ineligible decision (non-dict, non-iac_apply,
    not applied, not merge=failed, invalid pr_number/head_sha) and when GitHub
    does not confirm a head-matching merge; copy-on-change otherwise; never
    mutates the input; never raises. ``merge_reconciled: True`` is added as a
    cosmetic marker (the SPA can note "confirmed on GitHub")."""
    if not isinstance(decision, dict):
        return decision
    if decision.get("action") != "iac_apply":
        return decision
    if decision.get("apply_status") != "applied":
        return decision
    if decision.get("merge_state") != "failed":
        return decision
    pr_number = decision.get("pr_number")
    head_sha = decision.get("head_sha")
    # ``type(...) is int`` excludes bool (True/False) from passing as a PR number.
    if type(pr_number) is not int or pr_number <= 0:
        return decision
    if not isinstance(head_sha, str) or not head_sha:
        return decision
    merged = _resolve_pr_merged(
        pr_number, head_sha, repo_provider=repo_provider, settings=settings
    )
    if merged is True:
        return {**decision, "merge_state": "merged", "merge_reconciled": True}
    return decision


# Non-terminal phases a /reconcile round-trip can actually move. `claimed` is
# excluded on purpose: nothing was started, so there is no operation to ask
# about — the worker would just tell us so.
_RECONCILABLE_PHASES = frozenset({PHASE_APPLYING, PHASE_OUTCOME_UNKNOWN})

# Max reconcile round-trips per GET /decisions. See _maybe_reconcile.
_DECISIONS_RECONCILE_BUDGET = 3

# Don't reconcile a doc whose phase was recorded less than this ago — /execute
# is still expected to own it, and the worker's single concurrency slot means we
# would queue behind that very call. Comfortably past its 60s LRO cap.
_RECONCILE_MIN_AGE_S = 90.0

# ds-7j0 — the two brakes on an UNRESOLVABLE row.
#
# /reconcile writes nothing on its three non-settling exits (read failed / not
# done / done-with-no-response). That is correct — re-stamping `phase_at` there
# would slide the staleness clock forward and hide a genuinely stuck rollback,
# which is the ds-2mc defect inverted — but it also means nothing about the row
# changes, so it stayed eligible forever with no attempt counter and no ceiling.
# Two ways that becomes permanent load on a worker pinned to --concurrency=1
# --max-instances=1 (iac/cloudrun.tf) that ALSO serves Approve: the out-of-band
# `driftscribeRunOperationsReader` binding gets dropped (now declared in
# iac-operator/, but that root is operator-applied and nothing plans it on a
# schedule — see workers/rollback/main.py::_get_operations_client), or
# Cloud Run garbage-collects an old LRO and every read is NOT_FOUND. Either way
# every GET /decisions burned up to _DECISIONS_RECONCILE_BUDGET round-trips, on
# every 45s poll, per open tab, indefinitely.
#
# Neither brake touches the doc: the coordinator's projection is compute-only and
# the worker owns those writes.
#
#   1. A per-approval cooldown, so N open tabs polling at 45s cost at most one
#      attempt per cooldown per coordinator instance instead of N per poll.
#   2. A hard ceiling on `phase_at` age. Past it the answer will not change; a
#      rollback that has been unsettled for a day needs an operator, not another
#      round-trip.
_RECONCILE_RETRY_AFTER_S = 300.0
_RECONCILE_GIVE_UP_AFTER_S = 24 * 60 * 60.0

# approval_id -> monotonic timestamp of the last reconcile ATTEMPT (not of its
# outcome — a failed attempt is exactly the one worth backing off from). Bounded
# like _TRACE_CACHE: this keys on approval ids, which are unique per proposal, so
# an unbounded dict is a slow leak in a long-lived instance.
_RECONCILE_ATTEMPTED: dict[str, float] = {}
_RECONCILE_ATTEMPTED_MAX = 512


def _reconcile_cooldown_active(approval_id: str, *, now: float) -> bool:
    """True while ``approval_id`` is inside its post-attempt cooldown."""
    last = _RECONCILE_ATTEMPTED.get(approval_id)
    return last is not None and (now - last) < _RECONCILE_RETRY_AFTER_S


def _note_reconcile_attempt(approval_id: str, *, now: float) -> None:
    if len(_RECONCILE_ATTEMPTED) >= _RECONCILE_ATTEMPTED_MAX:
        # Drop the oldest half rather than clearing outright, so a burst of new
        # approvals can't wipe every live cooldown and re-open the stampede.
        for stale, _ in sorted(_RECONCILE_ATTEMPTED.items(), key=lambda kv: kv[1])[
            : _RECONCILE_ATTEMPTED_MAX // 2
        ]:
            _RECONCILE_ATTEMPTED.pop(stale, None)
    _RECONCILE_ATTEMPTED[approval_id] = now


def _reset_reconcile_state_for_tests() -> None:
    """Test helper — drop the reconcile cooldown table. Mirrors
    ``_reset_state_for_tests``; the cooldown is process-global, so without this
    one test's attempt silently suppresses the next test's."""
    _RECONCILE_ATTEMPTED.clear()


def _phase_at_age_s(audit: dict) -> float | None:
    """Seconds since ``apply_audit.phase_at``, or ``None`` if it cannot be read.

    ``None`` means the caller must NOT reconcile (fail closed). The previous
    inline version failed OPEN on a non-datetime ``phase_at`` — it skipped the
    check and reconciled anyway — which drops the latency guard precisely when
    the doc is malformed. And a NAIVE datetime raised straight out of
    ``_maybe_reconcile``, through the reader's blanket except, memoizing
    ``None`` for the row: the desk then lost status AND phase for it and went
    silent, rather than showing the unresolved card. Both are handled here.
    """
    phase_at = audit.get("phase_at")
    if not isinstance(phase_at, dt.datetime):
        return None
    if phase_at.tzinfo is None:
        # Firestore hands back tz-aware values; a naive one came from somewhere
        # else. UTC is this codebase's convention everywhere (see _utcnow).
        phase_at = phase_at.replace(tzinfo=dt.timezone.utc)
    try:
        return (dt.datetime.now(dt.timezone.utc) - phase_at).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return None


class _ApprovalReadFailed:
    """Sentinel: the approval doc could not be READ (as opposed to not existing).

    Distinct from ``None`` on purpose — see the ds-mml branch in
    ``attach_approval_status``. A dedicated object rather than a string or a
    bool so it can never collide with a real value or be truthiness-confused.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover — debugging aid only
        return "<approval read failed>"


_APPROVAL_READ_FAILED = _ApprovalReadFailed()


# ds-ihi — cross-request cache for approvals that can never change again.
#
# The per-request memo below keys on approval_id, and every proposal mints a
# fresh one, so it collapses nothing in practice: GET /decisions?limit=50 made
# one SEQUENTIAL Firestore read per rollback row, on the route overviewStore
# polls every 45s and on every focus. Twenty rollback rows meant twenty
# serialized round-trips inside a fetch the desk awaits alongside the graph and
# pending-list.
#
# Terminality is an invariant here, not a guess, which is what makes caching
# across requests safe: record_phase REFUSES to overwrite a terminal phase
# (TERMINAL_ROLLBACK_PHASES), and a denial is terminal at flip time inside
# _claim. So a doc in one of those states is immutable and re-reading it can
# only ever return the same bytes. Everything else — pending, claimed, applying,
# outcome_unknown — is still read every time, because /reconcile exists
# precisely to change it.
_TERMINAL_APPROVALS: dict[str, object] = {}
_TERMINAL_APPROVALS_MAX = 512


def _is_terminal_approval(record) -> bool:  # noqa: ANN001
    """True when nothing can move this approval again. See _TERMINAL_APPROVALS."""
    if record is None:
        return False
    if getattr(record, "status", None) == "denied":
        return True
    phase = (getattr(record, "apply_audit", None) or {}).get("phase")
    return phase in TERMINAL_ROLLBACK_PHASES


def _remember_if_terminal(approval_id: str, record) -> None:  # noqa: ANN001
    if not _is_terminal_approval(record):
        return
    if len(_TERMINAL_APPROVALS) >= _TERMINAL_APPROVALS_MAX:
        # Plain FIFO: insertion order is arrival order, and every entry is
        # equally immutable, so there is no cleverer eviction to be had.
        for stale in list(_TERMINAL_APPROVALS)[: _TERMINAL_APPROVALS_MAX // 2]:
            _TERMINAL_APPROVALS.pop(stale, None)
    _TERMINAL_APPROVALS[approval_id] = record


def _reset_terminal_approval_cache_for_tests() -> None:
    """Test helper — see ``_reset_reconcile_state_for_tests``."""
    _TERMINAL_APPROVALS.clear()


def _memoized_approval_reader():
    """Return a 0-arg-per-``approval_id`` callable that reads the approval
    doc's ``(status, resolved_at)`` ONCE per approval_id per request — twice for
    the rare row that gets reconciled, which re-reads to serve the settled value
    (Task 3.0b, 2026-07-28 — mirrors ``_memoized_repo_provider``'s "build the
    client once per request" shape, applied here to per-row *reads* instead
    of a single client construction).

    Only rows with an ``approval_id`` ever call this (see
    ``attach_approval_status``'s entry gate), so a window with N rollback
    rows referencing the SAME approval (unusual but not impossible — repeated
    /recheck calls after a stale claim) makes exactly one Firestore read, and
    a window with none makes zero.

    Fail-soft: a store construction error OR a read error memoizes ``None``
    so the caller degrades to an un-enriched row — the read must never break
    GET /decisions, the operator's main surface."""
    cache: dict[str, object] = {}
    # Per-request reconcile budget. Deliberately tiny: this is the operator's
    # main serve path, and each reconcile is a worker round-trip. /execute
    # terminalizes the doc itself on every path it survives, so an ELIGIBLE row
    # (non-terminal, has a handle, and older than _RECONCILE_MIN_AGE_S) is the
    # exception rather than the rule. A rollback in flight right now is
    # deliberately NOT eligible — see the age gate in _maybe_reconcile — so the
    # normal case, including a refresh during a live rollback, spends none of
    # this. The count bound plus _RECONCILE_HTTPX_TIMEOUT's 15s read caps the
    # worst case at ~45s, under the ~100s edge budget.
    budget = [_DECISIONS_RECONCILE_BUDGET]

    def read(approval_id: str):
        if approval_id not in cache:
            settled = _TERMINAL_APPROVALS.get(approval_id)
            if settled is not None:
                cache[approval_id] = settled
                return settled
            try:
                store = approval_helpers.get_approval_store()
                record = store.get(approval_id)
                served = _maybe_reconcile(store, approval_id, record, budget)
                _remember_if_terminal(approval_id, served)
                cache[approval_id] = served
            except Exception as e:  # noqa: BLE001 — fail-soft; must never break the serve path
                log.warning(
                    "decision_approval_status_read_failed",
                    extra={"error": type(e).__name__},
                )
                # NOT None — see _APPROVAL_READ_FAILED. Memoized for the request
                # (one blip shouldn't cost 50 retries) but never beyond it.
                cache[approval_id] = _APPROVAL_READ_FAILED
        return cache[approval_id]

    return read


def _maybe_reconcile(store, approval_id: str, record, budget: list[int]):
    """Ask the rollback worker to finalize an approval still recorded as
    in-flight, then re-read it. Returns the record to serve.

    This is what makes ``applying``/``outcome_unknown`` a temporary state rather
    than a permanent one. Without a caller, the worker's ``/reconcile`` endpoint
    and the operation handle it consumes would be exactly the dead machinery
    ds-2mc was filed about: ``operation_name`` was written for a poller that did
    not exist, which is how a failed rollback stayed indistinguishable from a
    successful one.

    Entirely best-effort. Any failure — no budget, worker down, malformed
    response — degrades to serving the record we already have, which is honest
    on its own terms (``outcome_unknown`` renders as unconfirmed, not as
    success). It must never break GET /decisions.
    """
    audit = getattr(record, "apply_audit", None)
    if not isinstance(audit, dict):
        return record
    phase = audit.get("phase")
    if phase not in _RECONCILABLE_PHASES:
        return record
    # No operation handle means there is nothing for the worker to look up —
    # e.g. a transport error around update_service that never got one back.
    # Spending a round-trip to be told that helps nobody.
    if not (audit.get("detail") or {}).get("operation_name"):
        return record
    # Don't chase a rollback /execute is still actively working on.
    #
    # This is the load-bearing latency guard, not a nicety. The rollback worker
    # runs --concurrency=1, so a reconcile for a FRESH `applying` queues behind
    # the very /execute that owns it and blocks for that call's remaining LRO
    # budget. The operator's focus-return refresh lands in exactly that window,
    # which is the worst possible moment to stall GET /decisions — and
    # overviewStore awaits it alongside the graph and pending-list fetches, so
    # all three would wait. /execute will terminalize the doc itself; reconcile
    # exists for what it leaves behind, not to race it.
    age = _phase_at_age_s(audit)
    if age is None or age < _RECONCILE_MIN_AGE_S:
        return record
    # ds-7j0's ceiling: past this the answer will not change, and the round-trip
    # is pure load on the worker that also serves Approve.
    if age > _RECONCILE_GIVE_UP_AFTER_S:
        log.info(
            "decisions_reconcile_gave_up",
            extra={"approval_id": approval_id, "age_s": int(age)},
        )
        return record
    # ds-7j0's cooldown. Checked BEFORE the budget so a row still cooling down
    # doesn't consume a slot another row could use — the budget is per-request,
    # the cooldown is per-approval, and spending the former on a row we have
    # already decided not to call for would starve the rows we would.
    now = time.monotonic()
    if _reconcile_cooldown_active(approval_id, now=now):
        return record
    if budget[0] <= 0:
        log.info("decisions_reconcile_budget_exhausted", extra={"approval_id": approval_id})
        return record

    budget[0] -= 1
    # Marked before the call, not after: an attempt that throws is exactly the
    # one worth backing off from, and a worker that is timing out must not be
    # retried on the very next poll by every open tab.
    _note_reconcile_attempt(approval_id, now=now)
    try:
        worker_client.call_reconcile(approval_id)
        return store.get(approval_id)
    except Exception as e:  # noqa: BLE001 — fail-soft by design
        log.warning(
            "decisions_reconcile_failed",
            extra={"approval_id": approval_id, "error": type(e).__name__},
        )
        return record


def attach_approval_status(decision: object, *, approval_reader) -> object:
    """Serve-time: for a rollback row (an ``approval`` sub-object carrying a
    non-empty ``approval_id``), join the approval doc's ``status`` and its
    resolution timestamp (``resolved_at``, added by Task 3.0b Part A) into a
    served copy. COMPUTE-ONLY — the approval doc is the source of truth for
    status; this never persists anything back onto the decision.

    Part C (honest degradation) — the entire point of this transform: a
    missing approval doc, an unknown/non-string status, or a doc that
    predates the ``resolved_at`` field (``record.resolved_at is None``) NEVER
    causes a timestamp to be synthesized. ``resolved_at`` is either the real
    value from the approval doc or explicit ``None`` — NEVER the decision's
    own ``created_at`` (that's when the proposal was made, not when a human
    resolved it). A consumer must be able to tell "resolved, time unknown"
    apart from "resolved at 14:05".

    ``approval_reader`` is a 0-arg-per-id callable (``str -> Approval |
    None``); production callers pass :func:`_memoized_approval_reader`,
    which is ALSO responsible for catching store errors (mirrors the
    ``reconcile_merge_state`` / ``_resolve_pr_merged`` split — the transform
    trusts its injected reader and does not itself swallow exceptions).

    Conventions mirror ``reconcile_merge_state`` / ``scrub_decision_rationale``:
    identity on no-change (non-dict, no ``approval`` sub-object, no/blank
    ``approval_id``, doc not found, status unknown), copy-on-change
    otherwise, never mutates the input, and — for rows with NO ``approval``
    sub-object at all — a byte-identical passthrough."""
    if not isinstance(decision, dict):
        return decision
    approval = decision.get("approval")
    if not isinstance(approval, dict):
        return decision
    approval_id = approval.get("approval_id")
    if not isinstance(approval_id, str) or not approval_id:
        return decision
    record = approval_reader(approval_id)
    if record is _APPROVAL_READ_FAILED:
        # ds-mml: "we could not read this" is NOT the same as "there is no such
        # doc", and collapsing them costs a real click. The frontend treats an
        # absent status as still-pending (a compat rule this backend cannot
        # change — pre-status decisions must keep rendering a CTA), so a
        # transient Firestore blip on a BURNED approval briefly re-offered a
        # live Approve button that the worker then refuses: a dead end, and one
        # that reads as the product being broken. One extra field lets the
        # frontend tell the two apart and decline to guess.
        return {
            **decision,
            "approval": {**approval, "status_unavailable": True},
        }
    if record is None:
        return decision
    status = getattr(record, "status", None)
    if not isinstance(status, str) or not status:
        return decision
    resolved_at = getattr(record, "resolved_at", None)
    if not isinstance(resolved_at, dt.datetime):
        resolved_at = None  # never synthesize — see docstring

    # Outcome phase (ds-2mc). `status == "used"` only means the single-use
    # credential was spent — the flip precedes the traffic shift by
    # construction — so the phase is what tells a consumer whether the rollback
    # actually happened. Without it the desk seals "applied" on a rollback that
    # may have failed.
    #
    # The phase STRING only, allowlisted against the known vocabulary. The
    # sibling `detail` map is deliberately never projected: it carries API error
    # type names and operation paths, which are operator-debugging material, not
    # something to ship to every browser polling /decisions. An unrecognized or
    # absent phase degrades to None — "outcome unknown" — never to success.
    # `phase_at` rides along because `resolved_at` cannot order these rows: it
    # exists only on a confirmed success, so every failed/unconfirmed row would
    # tie at null. It is the observation time of the CURRENT phase, and is the
    # only signal that distinguishes a rollback applying right now from one
    # whose worker died mid-wait an hour ago.
    phase = None
    phase_at = None
    audit = getattr(record, "apply_audit", None)
    if isinstance(audit, dict):
        raw_phase = audit.get("phase")
        if isinstance(raw_phase, str) and raw_phase in ROLLBACK_PHASES:
            phase = raw_phase
        raw_phase_at = audit.get("phase_at")
        if isinstance(raw_phase_at, dt.datetime):
            phase_at = raw_phase_at
    return {
        **decision,
        "approval": {
            **approval,
            "status": status,
            "resolved_at": resolved_at,
            "phase": phase,
            "phase_at": phase_at,
        },
    }


_INFRA_INVENTORY_CACHE: "tuple[float, dict] | None" = None


def _reset_infra_graph_cache_for_tests() -> None:
    """Clear BOTH cache layers (the in-process L1 tuple and the L2 store
    singleton + injection override). Test-only seam — an autouse fixture calls
    this between cases so a cached success (or an injected store) can't leak
    across tests."""
    global _INFRA_INVENTORY_CACHE, _infra_graph_cache_store_singleton
    global _infra_graph_cache_store_override
    _INFRA_INVENTORY_CACHE = None
    _infra_graph_cache_store_singleton = None
    _infra_graph_cache_store_override = None


def _read_l2_cache(l2_ttl: float) -> "tuple[dict, float] | None":
    """Return ``(inventory, age_s)`` from the L2 store if a fresh, valid record
    exists, else None.

    Validates defensively (Codex review): a record is honored only if it's a
    dict with the exact ``format_version``, a finite numeric ``written_at`` that
    isn't far in the future, and a non-``error`` dict payload. Any other shape —
    including a Firestore read error, which the store swallows to ``None`` —
    is a miss, so the caller falls through to a live fetch."""
    try:
        record = get_infra_graph_cache_store().get()
    except Exception as e:  # noqa: BLE001 — a misbehaving store must never break the request
        # The Firestore store already swallows its own errors to None; this is
        # belt-and-suspenders so the request handler's fail-soft holds for ANY
        # store impl (the cache must never turn /infra/graph into a 5xx).
        log.warning("infra_graph_l2_read_error", extra={"error": type(e).__name__})
        return None
    if not isinstance(record, dict):
        return None
    if record.get("format_version") != _INFRA_GRAPH_L2_FORMAT_VERSION:
        return None
    written_at = record.get("written_at")
    payload = record.get("payload")
    if not isinstance(written_at, (int, float)) or not math.isfinite(written_at):
        return None
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    age = time.time() - written_at
    if age < -_INFRA_GRAPH_L2_CLOCK_SKEW_TOLERANCE_S:
        return None  # stamped in the future → distrust rather than serve stale
    if age > l2_ttl:
        return None
    return payload, max(0.0, age)


def _warn_on_control_plane_skew(inventory: dict) -> None:
    """Skew canary (#193 half-deploy guard): the actionable-drift badge needs the
    infra_reader worker to emit ``not_in_iac_control_plane`` per adoptable type.

    A worker deployed BEFORE #193 omits the key entirely, so :func:`build_graph`
    silently falls back to raw drift (``drift_adoptable == drift``) and every
    adoptable badge over-reports. The tell is a MISSING key (not a zero, which is
    a legitimate "no control-plane drift") on an adoptable type that has drift —
    i.e. a coordinator/worker version skew. Log it at WARNING so a half-deploy is
    visible server-side instead of only surfacing as wrong counts in the UI.

    Called on the FRESH-fetch success path from BOTH ``GET /infra/graph`` and the
    pre-warm hook (so a scheduled pre-warm can't cache a stale-worker inventory
    unobserved). Never raises — the callers must not turn a read into a 5xx.
    """
    by_type = inventory.get("by_type")
    if not isinstance(by_type, dict):
        return
    stale_types = sorted(
        atype
        for atype, entry in by_type.items()
        if isinstance(entry, dict)
        and atype in ADOPTABLE_ASSET_TYPES
        and isinstance(entry.get("not_in_iac"), int)
        and entry.get("not_in_iac", 0) > 0
        and "not_in_iac_control_plane" not in entry
    )
    if stale_types:
        log.warning(
            "infra_graph_inventory_missing_control_plane_count",
            extra={
                "stale_asset_types": stale_types,
                "hint": (
                    "infra_reader worker predates #193; redeploy it so "
                    "actionable-drift badges stop over-reporting raw drift"
                ),
            },
        )


def _persist_infra_inventory(inventory: dict, *, l1_ttl: float, l2_ttl: float) -> bool:
    """Cache a SUCCESSFUL inventory in both layers; return whether the PERSISTENT
    L2 layer was durably written.

    The return value is the meaningful signal for the pre-warm endpoint: ``cached``
    must mean "the cold-start cache was actually warmed", so it tracks L2 only
    (Codex review). L1 is per-instance / best-effort and never makes ``cached``
    true on its own; an L2 disabled (``l2_ttl <= 0``) or a swallowed Firestore
    write failure both return False.

    Strips ``declared_not_found`` (the only field carrying full declared-vs-live
    canonical resource paths; ``build_graph`` never reads it) before persisting,
    so the at-rest L2 doc — and the L1 copy, via this single write path — omits
    it. The bounded, redaction-safe ``unmatched_iac`` projection is DELIBERATELY
    retained: it carries only asset_type + short name + HCL address (no canonical
    path, confidence, or possible_causes), and the operator UI's unmatched-
    declarations band is built from it, so it must survive L1/L2. L1 stamps a
    monotonic clock; L2 stamps wall-clock + the format version."""
    global _INFRA_INVENTORY_CACHE
    stripped = {k: v for k, v in inventory.items() if k != "declared_not_found"}
    if l1_ttl > 0:
        _INFRA_INVENTORY_CACHE = (time.monotonic(), stripped)
    l2_written = False
    if l2_ttl > 0:
        l2_written = get_infra_graph_cache_store().set(
            {
                "format_version": _INFRA_GRAPH_L2_FORMAT_VERSION,
                "written_at": time.time(),
                "payload": stripped,
            }
        )
    return l2_written


@app.get("/infra/graph")
def get_infra_graph(
    response: Response,
    _: None = Depends(verify_token),
) -> dict:
    """Resource-map graph for the operator UI's Infrastructure panel (Phase 1).

    Proxies the read-only ``infra_reader`` worker (the SPA can't reach the
    internal-ingress worker directly) and reshapes its whole-project CAI
    inventory into a redaction-safe, NODE-ONLY graph DTO via
    :func:`driftscribe_lib.infra_graph.build_graph`. Nodes are grouped by
    asset_type and flagged managed-in-IaC vs drift; secret/sensitive types are
    counts-only (never a name). ``edges`` is always ``[]`` — the partial
    topology is a Phase-4 follow-up.

    Token-guarded via :func:`verify_token` exactly like ``/decisions`` /
    ``/trace`` (header only). ``Cache-Control: no-store`` — the inventory
    reflects mutable live state, so no proxy/browser cache should hold a stale
    resource map. Freshness is instead managed by a short SERVER-side TTL cache
    (``INFRA_GRAPH_CACHE_TTL_S``, default 60s; ``<= 0`` disables): a single CAI
    enumeration takes ~25-35s, so caching the inventory makes the panel's
    every-page-load re-fetch instant within the window. The
    ``X-Infra-Graph-Cache: hit|miss|disabled`` response header (with
    ``X-Infra-Graph-Cache-Age-S`` on a hit) makes the cache observable to an
    operator inspecting headers, even though ``Cache-Control`` stays no-store.

    Degradation (soft-fail to 200, never 5xx): the panel is best-effort, so a
    failure becomes a ``degraded`` DTO the UI renders as an "unavailable" note
    rather than a hard error:

    * the worker's own CAI soft-fail (``{"error": "cloud_asset_unavailable"}``
      at 200) flows through :func:`build_graph` → ``degraded=True``; and
    * a real transport/config failure reaching the worker
      (:class:`WorkerClientError` — e.g. ``INFRA_READER_URL`` unset, or the
      worker down) is caught here and mapped to a synthetic
      ``infra_reader_unavailable`` degraded DTO (the status code is preserved
      in the ``detail`` for diagnosis).

    Neither degraded outcome is cached, so an outage clears as soon as the
    underlying problem does.
    """
    global _INFRA_INVENTORY_CACHE
    response.headers["Cache-Control"] = "no-store"
    s = get_settings()
    l1_ttl = s.infra_graph_cache_ttl_s
    l2_ttl = s.infra_graph_l2_cache_ttl_s

    # L1 — in-process, per-instance, monotonic clock. Fastest path; dies on
    # scale-to-zero recycle.
    if l1_ttl > 0:
        cached = _INFRA_INVENTORY_CACHE
        if cached is not None:
            written_at, cached_inventory = cached
            age = time.monotonic() - written_at
            if age <= l1_ttl:
                response.headers["X-Infra-Graph-Cache"] = "hit"
                response.headers["X-Infra-Graph-Cache-Age-S"] = f"{age:.1f}"
                # build_graph is a pure read over the inventory, so re-running it
                # on the cached dict each request keeps the DTO shaped by current
                # code without mutating what we cached.
                return build_graph(cached_inventory)

    # L2 — Firestore, shared/persistent, wall clock. Survives the cold start that
    # L1 can't: a freshly-recycled instance reads the doc and serves a warm map.
    if l2_ttl > 0:
        l2 = _read_l2_cache(l2_ttl)
        if l2 is not None:
            l2_inventory, l2_age = l2
            # Read-through: promote into L1 so subsequent requests on this
            # instance serve from memory instead of re-reading Firestore on every
            # poll. Worst-case staleness rises from L2_TTL to L2_TTL + L1_TTL —
            # bounded, and well within the panel's tolerance (CAI is eventually
            # consistent and the SPA shows a freshness caveat).
            if l1_ttl > 0:
                _INFRA_INVENTORY_CACHE = (time.monotonic(), l2_inventory)
            response.headers["X-Infra-Graph-Cache"] = "hit-l2"
            response.headers["X-Infra-Graph-Cache-Age-S"] = f"{l2_age:.1f}"
            return build_graph(l2_inventory)

    # Neither layer served. Distinguish a genuine miss (some caching is enabled)
    # from caching being fully disabled, for an operator inspecting headers.
    response.headers["X-Infra-Graph-Cache"] = (
        "miss" if (l1_ttl > 0 or l2_ttl > 0) else "disabled"
    )

    try:
        inventory = worker_client.call("infra_reader", {})
    except WorkerClientError as e:
        # Soft-fail to a degraded 200 so the panel degrades instead of erroring,
        # but log at WARNING so a real worker outage (e.g. INFRA_READER_URL unset)
        # is visible server-side rather than hidden behind the friendly UI note.
        # NOT cached — a transport failure must retry on the next request.
        log.warning(
            "infra_graph_worker_unavailable",
            extra={"status_code": e.status_code, "error": str(e)},
        )
        return build_graph(
            {
                "error": "infra_reader_unavailable",
                "detail": f"{e.status_code}: {e.body}",
            }
        )
    # The worker soft-fails a CAI permission/availability failure to a 200 with
    # an ``error`` key (not a non-2xx, so it doesn't raise above). Log it at
    # WARNING too — symmetric with the transport-failure branch — so a genuine
    # CAI outage is visible coordinator-side, not only as the friendly UI note.
    if isinstance(inventory, dict) and inventory.get("error"):
        log.warning(
            "infra_graph_inventory_error",
            extra={"error": inventory.get("error"), "detail": inventory.get("detail")},
        )
    elif isinstance(inventory, dict):
        _warn_on_control_plane_skew(inventory)
        # Success only: cache in both layers (never an error/degraded payload,
        # never a non-dict) so a healthy map is reused but an outage isn't pinned.
        _persist_infra_inventory(inventory, l1_ttl=l1_ttl, l2_ttl=l2_ttl)
    return build_graph(inventory)


@app.post("/internal/infra-graph/refresh")
def refresh_infra_graph(request: Request) -> dict:
    """Pre-warm hook: force a live ``infra_reader`` fetch and repopulate the cache.

    The activation point for the OPTIONAL Cloud Scheduler pre-warm (see
    docs/plans/2026-06-18-infra-graph-l2-firestore-cache.md and the
    ``SETUP_INFRA_PREWARM`` block in infra/scripts/setup_secrets.sh). Keeping the
    L2 Firestore doc fresh means even a true cold open (fresh instance, empty L1,
    no previously-rendered graph) is instant instead of paying the ~30s CAI fetch.

    Auth mirrors ``/eventarc``: OIDC-verified against ``infra_prewarm_audience``
    with a dedicated ``infra-prewarm-sa`` allowlist. 503 if unconfigured (dormant
    until provisioned), 401 on token failure, 403 on a non-allowlisted caller.

    On success: 200 ``{"cached": true, "resource_count": N}``. On a worker/CAI
    error: fail-SOFT 200 ``{"cached": false, "reason": ...}`` (logged) — a non-2xx
    would make Cloud Scheduler retry and risk storming the slow CAI worker; the
    next scheduled tick warms it instead.
    """
    s = get_settings()
    # 503 canaries — fail-closed if pre-warm wasn't provisioned (same shape as
    # /eventarc). An unset audience leaves the endpoint dormant.
    if not s.infra_prewarm_audience:
        raise HTTPException(
            status_code=503,
            detail="auth not configured: INFRA_PREWARM_AUDIENCE unset",
        )
    if not s.gcp_project:
        raise HTTPException(
            status_code=503,
            detail="auth not configured: GCP_PROJECT unset (cannot build expected SA email)",
        )
    # 401 on token failure / 403 on a caller that isn't the dedicated prewarm SA.
    # Lock: shared-Session transport, see ``_GOOGLE_AUTH_TRANSPORT_LOCK``.
    with _GOOGLE_AUTH_TRANSPORT_LOCK:
        verify_oidc_caller(
            request,
            audience=s.infra_prewarm_audience,
            allowed_emails={f"infra-prewarm-sa@{s.gcp_project}.iam.gserviceaccount.com"},
            transport=_GOOGLE_AUTH_TRANSPORT,
        )

    try:
        inventory = worker_client.call("infra_reader", {})
    except WorkerClientError as e:
        log.warning(
            "infra_graph_prewarm_worker_unavailable",
            extra={"status_code": e.status_code, "error": str(e)},
        )
        return {"cached": False, "reason": "infra_reader_unavailable"}
    if not isinstance(inventory, dict) or inventory.get("error"):
        reason = (
            inventory.get("error") if isinstance(inventory, dict) else "non_dict_inventory"
        )
        log.warning("infra_graph_prewarm_inventory_error", extra={"error": reason})
        return {"cached": False, "reason": "inventory_error"}

    _warn_on_control_plane_skew(inventory)
    l2_written = _persist_infra_inventory(
        inventory,
        l1_ttl=s.infra_graph_cache_ttl_s,
        l2_ttl=s.infra_graph_l2_cache_ttl_s,
    )
    resource_count = inventory.get("total_resources")
    if not l2_written:
        # cached==true must mean the PERSISTENT cold-start cache was warmed. If L2
        # is disabled or the Firestore write was swallowed, say so plainly rather
        # than report a healthy pre-warm that didn't actually persist anything.
        reason = (
            "l2_disabled"
            if s.infra_graph_l2_cache_ttl_s <= 0
            else "l2_write_failed"
        )
        log.warning("infra_graph_prewarm_not_persisted", extra={"reason": reason})
        return {"cached": False, "reason": reason, "resource_count": resource_count}
    return {"cached": True, "resource_count": resource_count}


# --------------------------------------------------------------------------- #
# Open infra changes (pending approvals) — Infra-panel surface
# --------------------------------------------------------------------------- #
_PENDING_APPROVALS_CACHE: "tuple[float, list[dict]] | None" = None
_PENDING_APPROVALS_TTL_S = 60.0
_INFRA_PR_LABEL = "driftscribe-infra"


def _list_pending_approvals() -> list[dict]:
    """Open infra PRs awaiting approval, newest first. Raises on GitHub error
    (the endpoint maps that to a degraded 200).

    Uses the issues API with a SERVER-SIDE label filter:
    ``get_issues(state="open", labels=[driftscribe-infra])`` returns only the
    labeled items, and a PR is an issue whose ``.pull_request`` is set. The issue
    object already carries ``number/title/body/html_url`` (a PR's body IS its
    issue body), so NO per-PR ``get_pull`` round-trip is needed.

    Trust model (adversarial review): the ``driftscribe-infra`` label is applied
    only by the tofu-editor worker, but that is a deployment GitHub-permissions
    assumption, NOT an API-enforced constraint (a collaborator with Triage+ could
    label an arbitrary PR). This surface is read-only and the approve link is built
    solely from the numeric PR number, so the worst case of a mislabeled PR is a
    spurious row, never a bad action.
    """
    from agent.iac_pr_trace_store import get_iac_pr_trace_store
    from driftscribe_lib.pending_approvals import build_pending_approval

    s = get_settings()
    repo = get_repo(s.github_token, s.github_repo)
    # ds-qua: the authoring reasoning for each listed PR, keyed by the SAME
    # ``s.github_repo`` these rows are built from — PR numbers are repository-local,
    # and the authoring side can target a different repo via
    # IAC_EDITOR_TARGET_REPO_OVERRIDE. One point lookup per open infra PR (a handful),
    # per 60s cache miss.
    #
    # Every touch is individually guarded, including acquiring the store. The trace is
    # OPTIONAL evidence; losing it must cost the link, never the row. Unguarded, one
    # unexpected store exception would escape into this function's caller, which
    # answers with an empty ``degraded`` list — blanking the whole panel and hiding
    # approvals the operator can actually act on. The store already fail-softs
    # internally; this is the belt to that suspenders, because the failure mode it
    # guards is so much worse than what it costs.
    def _authoring_trace(pr_number: int) -> str | None:
        try:
            return get_iac_pr_trace_store().get(s.github_repo, pr_number)
        except Exception as e:  # noqa: BLE001 — optional evidence must never blank the panel
            # Type only, no exc_info: this guard exists to absorb an ARBITRARY broken
            # store, whose exception text is not ours to trust — and a Firestore
            # PermissionDenied embeds the full document resource path. Same discipline
            # the store itself applies.
            log.warning(
                "pending_approval_trace_lookup_failed", extra={"error": type(e).__name__}
            )
            return None

    out: list[dict] = []
    # PyGithub accepts label NAMES (strings) here; it resolves them to the GitHub
    # label query param. sort/direction are passed EXPLICITLY (not left to the API
    # default) so the "newest first" promise is enforced, not assumed.
    for issue in repo.get_issues(
        state="open", labels=[_INFRA_PR_LABEL], sort="created", direction="desc"
    ):
        if getattr(issue, "pull_request", None) is None:
            continue  # a real issue, not a PR
        out.append(
            build_pending_approval(
                issue.number,
                issue.title or "",
                issue.html_url or "",
                issue.body or "",
                _authoring_trace(issue.number),
            )
        )
    return out


@app.get("/infra/pending-approvals")
def get_pending_approvals(
    response: Response,
    _: None = Depends(verify_token),
) -> dict:
    """Open infra PRs awaiting operator approval, for the Infra panel.

    Additive + fail-soft: a GitHub error returns ``{"approvals": [], "degraded":
    True}`` (never a 5xx) so the panel degrades gracefully. Short in-process TTL
    cache (the list changes slowly and the panel polls). ``Cache-Control:
    no-store``. Token-guarded exactly like ``/infra/graph`` and ``/decisions``.
    """
    global _PENDING_APPROVALS_CACHE
    response.headers["Cache-Control"] = "no-store"
    cached = _PENDING_APPROVALS_CACHE
    if cached is not None and (time.monotonic() - cached[0]) <= _PENDING_APPROVALS_TTL_S:
        return {"approvals": cached[1]}
    try:
        approvals = _list_pending_approvals()
    except Exception:  # noqa: BLE001 — fail-soft, never 5xx the panel
        log.warning("pending_approvals_listing_failed", exc_info=True)
        return {"approvals": [], "degraded": True}
    _PENDING_APPROVALS_CACHE = (time.monotonic(), approvals)
    return {"approvals": approvals}


@app.get("/infra/graph/preview")
def get_infra_graph_preview(
    response: Response,
    pr: int = Query(ge=1),
    _: None = Depends(verify_token),
) -> dict:
    """Advisory map overlay for a pending IaC PR (ClickOps Wave 2 item 6).

    Resolves the PR's C2 plan artifact through the SAME ladder as the
    /iac-approvals GET and reshapes its integrity-checked plan summary into
    the redaction-safe ghost-node overlay DTO
    (driftscribe_lib.infra_graph.plan_overlay). Read-only and advisory:
    always 200 with {available: false, reason} for every not-available
    outcome (probe-safe parity with the approval page); no pause rung
    (pause gates mutations; this mirrors show_summary, which renders the
    summary card even while approve is suppressed by pause/dry-run/token).

    NOT wired into any polling path: each call costs a GitHub comment list
    + two GCS fetches, so the SPA fetches it only on explicit operator
    intent (preview activation / Refresh / Retry).
    """
    response.headers["Cache-Control"] = "no-store"
    s = get_settings()
    ref, view = _resolve_iac_plan(s, pr)
    if view is None:
        return plan_overlay_unavailable(pr, "no_plan")
    if (
        view.unverifiable
        or not view.integrity_ok
        or view.denylist_violations
        or not _iac_artifact_consistent(ref, view, pr)
    ):
        return plan_overlay_unavailable(pr, "artifact_error")
    # Terminal-decision suppression — best-effort, same identity + terminal
    # set as the approval GET; a lookup failure must not take the preview
    # down (advisory display). Runs UNCONDITIONALLY (unlike the approval GET,
    # which only runs this when can_approve — see Decision 2 divergence block).
    if s.github_repo:
        existing = None
        try:
            _event_key = _iac_event_key(
                s.github_repo, pr, view.head_sha, view.generation_metadata
            )
            existing = get_state().find_decision_for_event(_event_key)
        except Exception:  # noqa: BLE001 — best-effort, advisory route
            log.warning(
                "iac_preview_decision_lookup_failed", extra={"pr_number": pr}
            )
        if existing is not None:
            _st = existing.get("apply_status")
            _ms = existing.get("merge_state")
            if (_st == "applied" and _ms == "merged") or _st in {
                "failed", "failed_state_suspect", "ambiguous",
            }:
                return plan_overlay_unavailable(pr, "resolved")
    summary = view.change_summary
    if summary is None:
        return plan_overlay_unavailable(pr, "summary_unavailable")
    return plan_overlay(pr, summary)


@app.get("/capabilities")
def get_capabilities_route(
    response: Response,
    _: None = Depends(verify_token),
) -> dict:
    """The agent's safety cage, serialized from the same constants the
    enforcement code imports (agent/capabilities.py — see its module
    docstring for the drift-pin test inventory). Token-guarded like
    /decisions and /infra/graph. Static per deploy; no-store keeps the
    header story consistent with its sibling read routes."""
    response.headers["Cache-Control"] = "no-store"
    return build_capabilities()


# --------------------------------------------------------------------------- #
# Open crew-prompt viewer — no auth; prompts are baked from the public repo
# --------------------------------------------------------------------------- #

_PROMPTS_DEMO_NOTE = (
    "Demo: each crew's system prompt is shown to everyone here so judges can read "
    "exactly what instructions the agent runs under. The prompts are baked into the "
    "running image from the public repo — and they are soft guidance: the "
    "deterministic post-LLM validators, the fail-closed denylist, and the human "
    "approval gates (not the prompt) are the real safety boundary."
)


@app.get("/workloads/{name}/prompts")
def get_workload_prompts(name: str, response: Response) -> dict:
    """Open, read-only view of a crew's system prompt(s).

    No auth — mirrors the /iac-approvals GET and /runs: the prompts are baked
    from the public repo, so there is nothing to hide and showing them is the
    feature. Served from local disk (no GitHub fetch, no cache); the prompt is
    NOT the enforcement boundary (see the demo note).
    """
    if name not in WORKLOAD_NAMES:
        raise HTTPException(status_code=404, detail=f"unknown workload {name!r}")
    spec = load_workload_spec(name)
    prompts = resolve_workload_prompts(name)
    response.headers["Cache-Control"] = "no-store"
    return {
        "workload": spec.name,
        "display_name": spec.display_name,
        "descriptor": spec.descriptor,
        "recheck_prompt": prompts.recheck_prompt,
        "chat_prompt": prompts.chat_prompt,
        "chat_prompt_distinct": prompts.chat_prompt_distinct,
        "source_dir": f"workloads/{spec.name}",
        "revision": os.environ.get("K_REVISION", "local"),
        "demo_note": _PROMPTS_DEMO_NOTE,
    }


# --------------------------------------------------------------------------- #
# Operator pause / kill switch — Wave 2 item 5
# --------------------------------------------------------------------------- #


def _operator_actor_from_jwt(cf_access_jwt: str | None) -> str:
    """Best-effort operator attribution for config toggles (pause, autonomy).

    Extracted verbatim from post_pause_route — behavior unchanged: default
    ``"operator-token"``; CF-Access JWT upgrade to the canonical operator
    email when the team domain + aud tag are configured AND the assertion
    verifies; silent fallback on any ``CfAccessJwtError`` so a stale CF
    cookie or rotated key cannot break a legitimate token-authenticated
    toggle.
    """
    settings = get_settings()
    actor = "operator-token"
    if (
        cf_access_jwt
        and settings.cf_access_team_domain
        and settings.cf_access_aud_tag
    ):
        try:
            claims = verify_cf_access_jwt(
                cf_access_jwt,
                settings.cf_access_team_domain,
                settings.cf_access_aud_tag,
            )
            actor = canonical_operator_email(claims)
        except CfAccessJwtError:
            # Silent fallback — a stale cookie or rotated key shouldn't block
            # a toggle that is authenticated by the operator token.
            pass
    return actor


def _pause_state_fail_closed() -> PauseState:
    """Resolve the StateStore AND read the pause flag, fail-closed end-to-end.

    ``read_pause_state`` never raises on ``get_pause()`` errors, but
    ``get_state()`` ITSELF can raise (first-call Firestore client construction).
    Without this guard a store-init failure would 500 the mutation gates —
    worst case an in-scope /eventarc event 500s and Eventarc RETRIES (storm)
    instead of getting the 200-ignored contract. ONE mechanism for every
    pause read (the five gates, the two approval GET displays, GET /pause).

    Lives here, not in :mod:`agent.pause`, because it needs ``get_state`` and
    pause.py must stay import-free of main (circular import).
    """
    try:
        state = get_state()
    except Exception:  # noqa: BLE001 — fail-closed by contract, never raise
        log.warning("pause_state_store_unavailable", exc_info=True)
        return PauseState(paused=True, reason=FAIL_CLOSED_REASON, read_error=True)
    return read_pause_state(state)


def _serialize_pause_state(ps: PauseState) -> dict[str, Any]:
    """Serialize a PauseState to the wire shape shared by GET and POST /pause.

    ``updated_at`` is a ``datetime`` (InMemory) or a Firestore
    ``DatetimeWithNanoseconds`` — both have ``.isoformat()``, so we try that
    first and fall back to ``str()`` for any other datetime-like type. ``None``
    stays ``None`` (flag never written).
    """
    if ps.updated_at is None:
        updated_at_str = None
    elif hasattr(ps.updated_at, "isoformat"):
        updated_at_str = ps.updated_at.isoformat()
    else:
        updated_at_str = str(ps.updated_at)
    return {
        "paused": ps.paused,
        "reason": ps.reason,
        "actor": ps.actor,
        "updated_at": updated_at_str,
        "read_error": ps.read_error,
    }


@app.get("/pause")
def get_pause_route(
    response: Response,
    _: None = Depends(verify_token),
) -> dict:
    """Return the current pause flag state.

    A read failure is NOT an error response — it returns the fail-closed view
    (paused=True, read_error=True) with 200, because that IS the system's
    effective state. Callers that distinguish error from intentional-pause
    must check ``read_error``. The state store is resolved INSIDE the body
    (via :func:`_pause_state_fail_closed`, not ``Depends(get_state)``) so a
    store-init failure ALSO yields the fail-closed view rather than a 500.

    ``Cache-Control: no-store`` mirrors /capabilities — this is operator
    safety status that must never be served from a proxy or browser cache.
    """
    response.headers["Cache-Control"] = "no-store"
    return _serialize_pause_state(_pause_state_fail_closed())


@app.post("/pause")
def post_pause_route(
    req: PauseToggleRequest,
    response: Response,
    _: None = Depends(verify_token),
    cf_access_jwt: str | None = Header(default=None, alias="Cf-Access-Jwt-Assertion"),
) -> dict:
    """Toggle the operator pause flag.

    Actor attribution is best-effort: if CF Access is configured AND a
    ``Cf-Access-Jwt-Assertion`` header verifies, the canonical operator email
    is used; otherwise falls back to ``"operator-token"``. Silent fallback on
    any ``CfAccessJwtError`` so a stale CF cookie cannot break a legitimate
    token-authenticated toggle. This mirrors the verify_token dual-credential
    pattern while naming the human when possible.

    A WRITE failure raises 502 — the operator must KNOW the toggle did NOT
    take effect. This is always safe (fail: a failed-pause write leaves the
    system running, which the operator sees; a failed-resume write leaves it
    paused, which is also visible) but failing silently would be dangerous
    for a kill switch. The state store is resolved INSIDE the try below (not
    ``Depends(get_state)``) so a store-init failure gets the SAME contractual
    502 instead of a generic 500 before the body ever runs.

    ``Cache-Control: no-store`` matches the GET — the response body IS pause
    status, and a cached copy could mislead the operator about safety state.
    """
    response.headers["Cache-Control"] = "no-store"

    # --- Actor attribution (best-effort; any failure → fallback) ---
    actor = _operator_actor_from_jwt(cf_access_jwt)

    # Strip whitespace-only reason to None so the stored doc is clean.
    reason = req.reason.strip() if req.reason else None
    reason = reason or None  # empty string after strip → None

    try:
        doc = get_state().set_pause(paused=req.paused, reason=reason, actor=actor)
    except Exception as exc:  # noqa: BLE001
        # Surface BOTH store-resolution and write failures as 502 — the operator
        # must see that the toggle didn't take effect. Unlike read failures
        # (which fail closed silently), a silent write failure could leave the
        # operator believing the system is paused when it is still running (or
        # vice versa).
        raise HTTPException(
            status_code=502,
            detail=(
                f"pause toggle did NOT take effect — storage write failed: {exc}"
            ),
        ) from exc

    log.info("pause_toggled", extra={"paused": req.paused, "actor": actor, "reason": reason})

    # Build the response from the as-written doc so the caller sees what was
    # actually persisted (including the server-authoritative updated_at from
    # Firestore's read-after-write in FirestoreStateStore.set_pause).
    ps = PauseState(
        paused=bool(doc.get("paused")),
        reason=doc.get("reason"),
        actor=doc.get("actor"),
        updated_at=doc.get("updated_at"),
        read_error=False,
    )
    return _serialize_pause_state(ps)


# --------------------------------------------------------------------------- #
# Operator autonomy dial — ClickOps item 11
# --------------------------------------------------------------------------- #


def _autonomy_state_fail_closed() -> AutonomyState:
    """Resolve the StateStore AND read the dial, fail-closed end-to-end.

    Mirrors _pause_state_fail_closed: read_autonomy_state never raises on
    get_autonomy() errors, but get_state() ITSELF can raise (first-call
    Firestore client construction). The fail-closed direction here is
    mode="observe" — the MOST restrictive — NOT the absent-doc default
    "propose_apply": an unreadable dial means we cannot KNOW what the
    operator chose, so the only honest stance is report-only.

    ONE mechanism for every dial read (the Layer-0 reads, the pipeline
    suppression site, the apply gates, the approval displays, GET /autonomy).
    """
    try:
        state = get_state()
    except Exception:  # noqa: BLE001 — fail-closed by contract, never raise
        log.warning("autonomy_state_store_unavailable", exc_info=True)
        return AutonomyState(
            mode="observe", reason=AUTONOMY_FAIL_CLOSED_REASON, read_error=True
        )
    return read_autonomy_state(state)


def _autonomy_note_for_display(a: AutonomyState) -> str:
    """Calm operator-facing dial note for the approval display pages.

    Distinguishes the two Observe causes (Codex should-consider 3): a
    fail-closed read must NEVER be presented as the operator's choice. A
    configured restriction names the mode the operator picked; a read failure
    says the effective mode is Observe and that it is failing closed.
    """
    if a.read_error:
        return approval_i18n.REASON_EN["autonomy_unreadable"]
    return autonomy_apply_blocked_detail(a.mode)


def _serialize_autonomy_state(a: AutonomyState) -> dict[str, Any]:
    """Serialize an AutonomyState to the wire shape shared by GET and POST.

    ``updated_at`` shaping mirrors _serialize_pause_state exactly: a
    ``datetime`` (InMemory) or Firestore ``DatetimeWithNanoseconds`` both have
    ``.isoformat()``; fall back to ``str()`` for any other datetime-like type;
    ``None`` stays ``None`` (dial never written).
    """
    if a.updated_at is None:
        updated_at_str = None
    elif hasattr(a.updated_at, "isoformat"):
        updated_at_str = a.updated_at.isoformat()
    else:
        updated_at_str = str(a.updated_at)
    return {
        "mode": a.mode,
        "reason": a.reason,
        "actor": a.actor,
        "updated_at": updated_at_str,
        "read_error": a.read_error,
    }


@app.get("/autonomy")
def get_autonomy_route(
    response: Response,
    _: None = Depends(verify_token),
) -> dict:
    """Return the current autonomy dial state.

    Fail-closed read serialized at 200 — observe/read_error=True IS the
    system's effective state, so a read failure is not an error response.
    The store is resolved INSIDE the body (via _autonomy_state_fail_closed,
    not Depends(get_state)) so a store-init failure ALSO yields the
    fail-closed view rather than a 500. ``Cache-Control: no-store`` mirrors
    GET /pause — operator safety status must never be served from cache.
    """
    response.headers["Cache-Control"] = "no-store"
    return _serialize_autonomy_state(_autonomy_state_fail_closed())


@app.post("/autonomy")
def post_autonomy_route(
    req: AutonomyToggleRequest,
    response: Response,
    _: None = Depends(verify_token),
    cf_access_jwt: str | None = Header(default=None, alias="Cf-Access-Jwt-Assertion"),
) -> dict:
    """Set the autonomy dial. Mirrors POST /pause exactly.

    Token-gated; audited (reason / actor / updated_at). Actor attribution is
    best-effort via _operator_actor_from_jwt (CF-Access email when configured,
    else "operator-token"). A WRITE failure raises 502 — the operator must
    KNOW the toggle did NOT take effect; the store is resolved INSIDE the try
    so a store-init failure gets the SAME contractual 502 instead of a generic
    500. The response is built from the as-written document so the caller sees
    the real server-authoritative updated_at. ``Cache-Control: no-store``
    matches the GET.
    """
    response.headers["Cache-Control"] = "no-store"

    # --- Actor attribution (best-effort; any failure → fallback) ---
    actor = _operator_actor_from_jwt(cf_access_jwt)

    # Strip whitespace-only reason to None so the stored doc is clean.
    reason = req.reason.strip() if req.reason else None
    reason = reason or None  # empty string after strip → None

    try:
        doc = get_state().set_autonomy(mode=req.mode, reason=reason, actor=actor)
    except Exception as exc:  # noqa: BLE001
        # Surface BOTH store-resolution and write failures as 502 — the
        # operator must see that the toggle didn't take effect. A silent write
        # failure could leave the operator believing the dial moved when it
        # did not.
        raise HTTPException(
            status_code=502,
            detail=(
                f"autonomy toggle did NOT take effect — storage write failed: {exc}"
            ),
        ) from exc

    log.info("autonomy_toggled", extra={"mode": req.mode, "actor": actor, "reason": reason})

    # Build the response from the as-written doc so the caller sees what was
    # actually persisted (including the server-authoritative updated_at from
    # Firestore's read-after-write in FirestoreStateStore.set_autonomy).
    a = AutonomyState(
        mode=str(doc.get("mode")),
        reason=doc.get("reason"),
        actor=doc.get("actor"),
        updated_at=doc.get("updated_at"),
        read_error=False,
    )
    return _serialize_autonomy_state(a)


def _decision_created_at_hint(decision: object) -> dt.datetime | None:
    """The decision's ``created_at`` as a tz-aware datetime, or None.

    Feeds ``CloudLoggingFetcher``'s bounded hint window: ``get_trace`` reads
    the decision doc BEFORE the log fetch, and its ``created_at`` (persisted
    on every decision since 19.A.7 and backfilled from the Firestore
    server timestamp by find_decision_by_trace_id for older docs) pins WHEN
    the trace happened, so the fetcher can search a narrow window instead of
    the retention-deep floor. Defensive on shape — missing/str/garbage
    degrades to None (fetcher falls back to its two-phase query); never
    raises.
    """
    if not isinstance(decision, dict):
        return None
    raw = decision.get("created_at")
    if isinstance(raw, dt.datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=dt.timezone.utc)
    if isinstance(raw, str):
        try:
            parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.timezone.utc)
    return None


@app.get("/trace/{trace_id}")
def get_trace(
    trace_id: str,
    request: Request,
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
    # Serve-time rationale scrub (PR 2): the persisted decision stores the LLM
    # rationale RAW; scrub it here (the same boundary where events are
    # redacted below) so a secret quoted in prose never reaches the SPA, the
    # legacy template, or a raw API caller. This single var feeds BOTH the
    # cache-hit return and the fresh return below.
    decision = scrub_decision_rationale(state.find_decision_by_trace_id(trace_id))
    # Serve-time merge_state reconcile (2026-06-27): apply the SAME compute-only
    # transform the rail (/decisions) uses, so the open-trace card and the rail
    # never disagree on a stale applied+merge=failed row. Compute-only — no
    # persist; warm merge-cache → no GitHub call.
    _trace_settings = get_settings()
    decision = reconcile_merge_state(
        decision,
        repo_provider=_memoized_repo_provider(_trace_settings),
        settings=_trace_settings,
    )
    # Operator decision 2026-07-09 (docs/plans/2026-07-09-operator-seat-demo-
    # window.md): the anonymous demo-window scrub of the rollback approval link
    # (from the decision AND every event string) is removed — a visitor holds the
    # operator seat, so the timeline's approval links must be live for them, same
    # as the operator sees. Reverses audit A.2's serve-time scrub for /trace.
    cached = _cache_get(trace_id)
    if cached is not None:
        return {**cached, "decision": decision, "fetched_from_cache": True}

    # Real timeout via a Future boundary. The google-cloud-logging
    # client's ``list_entries`` has no timeout parameter in 3.15.x —
    # without this wrapper, a hung fetch would tie up the request
    # threadpool slot indefinitely. ``fut.cancel()`` on timeout is
    # best-effort (Python can't kill a thread mid-call) but it at
    # least prevents the Future from being awaited again.
    fut = _TRACE_FETCH_EXECUTOR.submit(
        fetcher.fetch,
        trace_id,
        limit=_TRACE_FETCH_LIMIT,
        # Bounded hint window: the decision was read above, BEFORE this fetch,
        # precisely so old traces don't pay the retention-deep scan.
        around=_decision_created_at_hint(decision),
    )
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

    # Canonical render order: ascending by (timestamp, insert_id). We sort
    # here rather than trusting the fetcher's order — CloudLoggingFetcher pulls
    # ``timestamp desc`` (fast for recently-ingested logs) and doesn't break
    # same-millisecond ties, so this is the single source of display order.
    events.sort(key=lambda e: (e.get("timestamp", ""), e.get("insert_id", "")))

    # Defense-in-depth: redact again at render. Phase 19.A.3 already
    # redacts at emit, but historical entries (pre-Phase-19) and any
    # future emit site that forgets ``redact_event`` are caught here.
    # ``redact_event`` returns ``object`` per signature but yields a
    # dict for dict inputs — every entry is a dict from
    # ``_entry_to_dict``, so the cast is sound.
    events = [redact_event(e) for e in events]  # type: ignore[misc]

    complete = _observe_and_check_stability(
        trace_id,
        events,
        # iac_apply traces never emit final_response; a RUN-ENDED decision
        # doc + observed stability is their completion signal instead.
        require_final_response=not _iac_run_ended(decision),
    )
    # Truncation guard: the fetch is capped at _TRACE_FETCH_LIMIT. A full page
    # means the fetch likely dropped entries — and because the fetcher pulls
    # ``timestamp desc``, it drops the OLDEST, keeping the newest final_response
    # that _observe_and_check_stability treats as "done". Left unchecked, a
    # truncated timeline would be blessed complete and cached with its head
    # missing. Never happens at today's ~10-20 events/trace, but guard + warn so
    # a future chatty trace surfaces loudly instead of silently losing history.
    if len(events) >= _TRACE_FETCH_LIMIT:
        log.warning(
            "trace_timeline_truncated",
            extra={"trace_id": trace_id, "count": len(events),
                   "limit": _TRACE_FETCH_LIMIT},
        )
        complete = False
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


def _read_pr_body_cache(pr_number: int, head_sha: str) -> "dict | None":
    """Return ``{"body": str|None, "truncated": bool}`` from the PR-body cache for
    THIS head_sha, else None (a miss → caller refetches). Validates defensively
    (mirrors ``_read_iac_source_cache``): exact ``format_version``, head_sha
    match, a str-or-None ``body`` (a tampered non-str is a miss), a bool
    ``truncated``, and a finite ``written_at`` within TTL + clock-skew."""
    try:
        record = get_iac_pr_body_cache_store().get(pr_number)
    except Exception as e:  # noqa: BLE001 — a misbehaving store is a miss, never a 5xx
        log.warning("iac_pr_body_read_error", extra={"error": type(e).__name__})
        return None
    if not isinstance(record, dict):
        return None
    if record.get("format_version") != _IAC_PR_BODY_FORMAT_VERSION:
        return None
    if record.get("head_sha") != head_sha:
        return None
    body = record.get("body")
    if body is not None and not isinstance(body, str):
        return None  # tampered doc
    truncated = record.get("truncated")
    if not isinstance(truncated, bool):
        return None
    written_at = record.get("written_at")
    if not isinstance(written_at, (int, float)) or not math.isfinite(written_at):
        return None
    age = time.time() - written_at
    if age < -_IAC_PR_BODY_CLOCK_SKEW_TOLERANCE_S:
        return None
    if age > _IAC_PR_BODY_TTL_S:
        return None
    return {"body": body, "truncated": truncated}


def _resolve_pr_body(
    pr_number: int, head_sha: str, *, repo_provider, settings: Settings
) -> "tuple[str | None, bool, bool]":
    """Resolve the SCRUBBED PR body for the open-trace card, read-through cache.

    Returns ``(body, truncated, cached)``. Fail-soft: an indeterminate result
    (no token/repo, fetch error) is ``(None, False, False)`` — the endpoint stays
    200 and the UI omits the section. Scrub happens BEFORE the cache write so the
    stored doc never holds an un-scrubbed body."""
    # Gate on github config BEFORE the cache read (Codex completed-work review):
    # mirrors _resolve_pr_merged and honours the documented "no token -> body:null"
    # contract — a warm cache must not leak a body when GitHub is unconfigured.
    if not (settings.github_token and settings.github_repo):
        return None, False, False
    cached = _read_pr_body_cache(pr_number, head_sha)
    if cached is not None:
        return cached["body"], cached["truncated"], True
    repo = repo_provider()
    if repo is None:
        return None, False, False
    try:
        fetched = github.get_pr_body(repo, pr_number)
    except Exception as e:  # noqa: BLE001 — GitHub hiccup must not break the always-200 serve
        log.warning(
            "pr_body_fetch_failed",
            extra={"error": type(e).__name__, "pr_number": pr_number},
        )
        return None, False, False
    scrubbed = scrub_pr_body(fetched.get("body"))
    body = scrubbed if (scrubbed is None or isinstance(scrubbed, str)) else None
    truncated = bool(fetched.get("truncated"))
    # Best-effort persist: the store swallows write errors, but wrap the call too
    # so even a misbehaving store object can't break the always-200 serve (Codex
    # review — mirrors the _resolve_iac_source caller-side guard).
    try:
        get_iac_pr_body_cache_store().set(
            pr_number,
            {
                "format_version": _IAC_PR_BODY_FORMAT_VERSION,
                "head_sha": head_sha,
                "body": body,
                "truncated": truncated,
                "written_at": time.time(),
            },
        )
    except Exception as e:  # noqa: BLE001 — a cache-write failure must not fail the GET
        log.warning("iac_pr_body_write_error", extra={"error": type(e).__name__})
    return body, truncated, False


@app.get("/trace/{trace_id}/pr-body")
def get_trace_pr_body(
    trace_id: str,
    response: Response,
    _: None = Depends(verify_token),
    state: StateStore = Depends(get_state),
) -> dict:
    """The agent-authored PR body for the iac_apply decision behind ``trace_id``,
    for the open-trace "what this change did" disclosure.

    Token-gated like ``/trace``. Binds to the PERSISTED decision (Codex MF4 — a
    bare ``pr_number`` couldn't safely pick among a PR's multiple lifecycle docs)
    and derives ``head_sha`` server-side from it (never trusts a client SHA).

    * 400 — ``trace_id`` is not 32-char lowercase hex.
    * 404 — no decision for the trace, or it isn't an ``iac_apply`` with a
      resolvable PR.
    * 200 ``{pr_number, head_sha, body: str|null, body_truncated, cached}`` — for
      a valid iac_apply decision; ``body`` is null on a fail-soft GitHub/cache
      miss (the UI just omits the section)."""
    if not _HEX32_RE.fullmatch(trace_id):
        raise HTTPException(
            status_code=400,
            detail="trace_id must be 32-char lowercase hex",
            headers={"Cache-Control": "no-store"},
        )
    response.headers["Cache-Control"] = "no-store"
    decision = state.find_decision_by_trace_id(trace_id)
    if not isinstance(decision, dict) or decision.get("action") != "iac_apply":
        raise HTTPException(
            status_code=404,
            detail="no iac_apply decision for this trace",
            headers={"Cache-Control": "no-store"},
        )
    pr_number = decision.get("pr_number")
    head_sha = decision.get("head_sha")
    if (
        type(pr_number) is not int
        or pr_number <= 0
        or not isinstance(head_sha, str)
        or not head_sha
    ):
        raise HTTPException(
            status_code=404,
            detail="decision has no resolvable PR",
            headers={"Cache-Control": "no-store"},
        )
    s = get_settings()
    body, truncated, cached = _resolve_pr_body(
        pr_number, head_sha, repo_provider=_memoized_repo_provider(s), settings=s
    )
    return {
        "pr_number": pr_number,
        "head_sha": head_sha,
        "body": body,
        "body_truncated": truncated,
        "cached": cached,
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


def _recorded_phase(store, approval_id: str) -> str | None:  # noqa: ANN001
    """The rollback phase currently recorded on an approval, or ``None``.

    Fail-soft on every failure mode (no doc, read error) because both callers
    treat ``None`` as "we know nothing", which only ever makes the ds-z4z
    fall-through stricter.
    """
    try:
        approval = store.get(approval_id)
    except Exception:  # noqa: BLE001 — a failed probe must not mask the worker error
        log.exception("approve: could not read approval phase")
        return None
    if approval is None:
        return None
    phase = (getattr(approval, "apply_audit", None) or {}).get("phase")
    return phase if isinstance(phase, str) and phase else None


def _approval_if_phase_advanced(store, approval_id: str, *, before: str | None):  # noqa: ANN001, ANN201
    """Return the approval doc iff its phase CHANGED since ``before``.

    The narrow question after a worker 5xx: did *this* request get far enough to
    write an outcome? ``apply_audit.phase`` is written atomically with the
    ``pending → used`` claim, so a transition across this call means the claim
    landed here and the doc's account beats the transport error.

    A phase that was ALREADY there proves only that some earlier request claimed
    this approval. Accepting that would let a spent-token replay during a real
    outage render the original outcome at HTTP 200 and hide the outage — so an
    unchanged phase returns ``None`` and the caller raises as before.
    """
    if (after := _recorded_phase(store, approval_id)) is None or after == before:
        return None
    try:
        return store.get(approval_id)
    except Exception:  # noqa: BLE001 — same fail-soft rule as above
        log.exception("approve: could not re-read approval after worker error")
        return None


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


@app.get("/", response_class=HTMLResponse)
def transparency_ui(request: Request) -> Response:
    """Serve the operator UI shell (Svelte+Vite SPA) at the site root ``/``.

    No auth on the HTML itself — the shell is harmless. Every API call the
    Svelte app makes (``/chat``, ``/decisions``, ``/trace/{id}``) carries the
    ``X-DriftScribe-Token`` header (or relies on Cloudflare Access). The token
    is held in ``sessionStorage['driftscribe_token']`` so it does not survive a
    tab close.

    The shell loads the hashed JS/CSS resolved from the Vite manifest
    (:func:`_shell_assets`); when the bundle is not built (pure-Python CI /
    dev), the dev fallback still returns a 200 shell with ``id="app"``.

    ``Cache-Control: no-store`` because this is an operator surface — a stale
    cached shell could surface yesterday's decisions in the rail.
    """
    assets = _shell_assets()
    resp = _TEMPLATES.TemplateResponse(
        request,
        "transparency.html",
        {"ds_js": assets["js"], "ds_css": assets["css"]},
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/ui/transparency-legacy", response_class=HTMLResponse)
def transparency_ui_legacy(request: Request) -> Response:
    """Serve the pre-refresh single-file UI (one-release safety net).

    Kept reachable during the demo window in case the Svelte SPA needs a
    fallback. Same unauthenticated, ``no-store`` contract as the new shell.
    """
    resp = _TEMPLATES.TemplateResponse(request, "transparency_legacy.html", {})
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _rollback_change_view(approval: object) -> dict[str, object]:
    """Turn an approval's ``env_snapshot`` into what the approval page renders
    for "what this rollback will change" (ds-uwc).

    Always returns a dict with a ``state`` of ``"ok"`` or ``"unknown"``, and
    NEVER raises — the approval GET is contractually always-200 (a probing GET
    must not be able to enumerate doc presence by status code).

    ``"unknown"`` is the important one, and it is deliberately not the same as
    "no changes". A missing snapshot means the approval predates ds-uwc, or the
    worker could not read one of the two revisions, or the coordinator's
    contract has moved since — in every case the honest render is *we could not
    work out what this would change*, because an empty table reads as a promise
    that nothing will change and that is the one wrong answer here.

    Coverage is checked, not assumed. If the contract now declares a var the
    snapshot never inspected — a contract edited inside the approval's 15-minute
    TTL, or a coordinator/worker version skew — the snapshot cannot answer "does
    the target satisfy the contract" for that var, so the whole view degrades to
    unknown rather than reporting a clean bill from a partial scan.

    No env VALUE reaches this function. The worker stores names, booleans and
    the two revision names; the values shown on the page are the CONTRACT's
    literals, which are public. That is a property of the snapshot's shape, not
    a redaction step that could be forgotten here.
    """
    snapshot = getattr(approval, "env_snapshot", None)
    if not isinstance(snapshot, dict):
        return {"state": "unknown", "reason": "absent"}
    try:
        contract = load_contract(Path(get_settings().contract_path))
    except Exception as e:  # noqa: BLE001
        log.warning("approval_view_contract_unavailable", extra={"error": type(e).__name__})
        # Distinct reason: "we could not load the contract" is not "we could not
        # read the target revision", and telling the operator the wrong one
        # sends them to the wrong place.
        return {"state": "unknown", "reason": "contract_unavailable"}

    snapshot_vars = snapshot.get("contract_vars")
    if not isinstance(snapshot_vars, dict):
        return {"state": "unknown", "reason": "absent"}
    # "Recorded no contract information" is NOT "recorded against a contract
    # that has since moved", and the difference is a live mid-rollout state
    # rather than a hypothetical: the worker must deploy first (ProposeRequest
    # is extra="forbid"), so every approval minted in the window before the
    # coordinator ships carries contract_vars={} and an empty hash. Calling
    # those "the contract changed" sends the operator hunting for an edit that
    # never happened. Checked BEFORE the key-set comparison, which would
    # otherwise claim the same wrong reason first.
    if not snapshot.get("contract_hash"):
        return {"state": "unknown", "reason": "absent"}
    if set(snapshot_vars) != set(contract.expected_env):
        return {"state": "unknown", "reason": "contract_changed"}
    if snapshot.get("contract_hash") != contract_hash(contract):
        return {"state": "unknown", "reason": "contract_changed"}

    # The snapshot must describe the revision THIS approval will roll onto.
    # Matching key sets prove the snapshot answered the right QUESTIONS; they
    # say nothing about whether it answered them about the right SUBJECT, and
    # the acknowledgment below is derived entirely from these booleans. A
    # snapshot recorded against some other revision would have the gate judging
    # a target nobody is about to deploy. (Today the worker writes both fields
    # in one call so they always agree — which is exactly why this is cheap,
    # and exactly the kind of assumption that stops holding quietly.)
    if snapshot.get("target_revision") != getattr(approval, "target_revision", None):
        log.warning(
            "approval_view_target_mismatch",
            extra={"snapshot_target": str(snapshot.get("target_revision"))[:64]},
        )
        return {"state": "unknown", "reason": "absent"}

    # Matching NAMES is not a complete answer per var. An entry missing one of
    # the two booleans would silently read as False below — "unchanged" and, for
    # an allow_manual var, "fine" — so a partial scan would report a clean bill
    # from evidence it never had. Demand both results, as real booleans.
    for entry in snapshot_vars.values():
        if not isinstance(entry, dict):
            # Also the totality guard: a truthy non-mapping (``["junk"]``)
            # would raise on ``.get`` below, and this handler is contractually
            # always-200.
            return {"state": "unknown", "reason": "absent"}
        if not all(
            isinstance(entry.get(k), bool)
            for k in ("changed", "target_matches_contract")
        ):
            return {"state": "unknown", "reason": "absent"}

    # ``changed_names`` is the whole-env half of the same observation, and the
    # two halves have to agree. The worker derives both from one pair of
    # source/target maps so they always do — but this function exists to defend
    # against a malformed or skewed doc, and "the writer is careful" is not a
    # check. Silently tolerating a contradiction is the worst outcome available:
    # a snapshot claiming PAYMENT_MODE changed while its own per-var result says
    # it did not renders as a clean bill with the variable listed nowhere.
    changed_names = snapshot.get("changed_names")
    if not isinstance(changed_names, list) or not all(
        isinstance(n, str) for n in changed_names
    ):
        return {"state": "unknown", "reason": "absent"}
    changed_set = set(changed_names)
    for name, entry in snapshot_vars.items():
        if entry["changed"] != (name in changed_set):
            log.warning("approval_view_snapshot_inconsistent", extra={"var": name[:64]})
            return {"state": "unknown", "reason": "absent"}

    rows = []
    violates = False
    for name, rule in sorted(contract.expected_env.items()):
        entry = snapshot_vars[name]
        # ``is True`` — kept even though the loop above has established both are
        # real booleans, so neither guard silently depends on the other.
        changed = entry.get("changed") is True
        matches = entry.get("target_matches_contract") is True
        if not matches and not rule.allow_manual_change:
            violates = True
        rows.append(
            {
                "name": name,
                "changed": changed,
                "target_matches_contract": matches,
                "contract_value": rule.value,
                "allow_manual_change": rule.allow_manual_change,
                # The blast-radius answer ds-b3m could not give: an
                # operator-toggleable var this rollback would move.
                "reverts_operator_change": changed and rule.allow_manual_change,
            }
        )

    other_changed = sorted(n for n in changed_set if n not in contract.expected_env)
    return {
        "state": "ok",
        "rows": rows,
        "other_changed": other_changed,
        "violates": violates,
        "source_revision": snapshot.get("source_revision") or "",
        "observed_at": snapshot.get("observed_at") or "",
    }


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
    try:
        approval = store.get(approval_id)
    except Exception as e:  # noqa: BLE001
        # The docstring above promises this handler is always-200 and
        # probe-safe, but this read was unguarded until ds-uwc: a Firestore
        # blip produced a 500 and, with it, exactly the presence oracle the
        # always-200 rule exists to deny. Degrade to the not-found render,
        # which is what a probe already sees for an id that does not exist.
        log.warning("approval_page_read_failed", extra={"error": type(e).__name__})
        approval = None
    expired = bool(approval) and approval_helpers.is_expired(approval)
    change_view = _rollback_change_view(approval) if approval else None
    # No usable token → the page cannot act: BOTH Approve and Reject hand
    # (approval_id, t) to the rollback worker for HMAC verification, so
    # rendering the form would only manufacture a doomed POST (observed live
    # 2026-07-08: a tokenless Approve died as raw 422 JSON). The literal
    # ``<redacted>`` guard stays as a harmless defense: after the 2026-07-09
    # operator-seat decision the demo-anonymous /decisions, /trace, /chat and
    # /conversations serve scrubs are gone (visitors get the live link), but the
    # surviving scrubs (unauthenticated ``/runs``, the model-facing
    # decisions-history and cross-crew read_conversations reads) still emit
    # ``<redacted>``, so a visitor pasting such a link still lands here; it can
    # never be a real token (the redactor's value class excludes ``<``). The
    # template renders an explanatory note instead.
    token_missing = not t.strip() or t.strip() == "<redacted>"
    # Pause gate (display): the page shows what its POST would do — Approve
    # disabled + a calm note while paused; Reject stays active (the POST allows
    # reject while paused). _pause_state_fail_closed keeps the GET ALWAYS-200
    # (probe-safe): a store-resolution failure fails closed to a paused display,
    # never a 500.
    paused = _pause_state_fail_closed().paused
    # Autonomy dial (display): mirror the paused treatment — when the dial is
    # below Propose+Apply, Approve is disabled and a calm note explains the
    # dial (Reject stays active). Read fail-closed so a store failure shows the
    # restrictive display, never a 500.
    autonomy = _autonomy_state_fail_closed()
    autonomy_blocked = autonomy.mode != "propose_apply"
    lang = approval_i18n.resolve_lang(request)
    response = _TEMPLATES.TemplateResponse(
        request,
        "approval.html",
        {
            "lang": lang,
            "approval_id": approval_id,
            "approval": approval,
            "token": t,
            "token_missing": token_missing,
            "expired": expired,
            "paused": paused,
            "autonomy_blocked": autonomy_blocked,
            "change_view": change_view,
            "autonomy_detail": approval_i18n.localize_reason(
                _autonomy_note_for_display(autonomy), lang
            ),
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


# "View source" affordance: the .tf source a PR adds/changes, shown on the
# approval page. During the hackathon demo it is visible to EVERYONE (so judges
# can inspect what the agent authored); the page labels this as a demo posture.
_IAC_SOURCE_DEMO_NOTE = (
    "Demo: the generated OpenTofu source is shown to everyone here so judges can "
    "inspect exactly what the agent authored. Outside the demo this view would be "
    "operator-only."
)


def _valid_iac_source_files(files: object) -> bool:
    """True iff ``files`` is a well-formed source payload — a list of dicts each
    with a ``path`` that is an ``iac/**.tf`` (no ``..`` segment) string and a
    ``content`` that is ``None`` or a str. Guards the cache read against a
    tampered / old-shaped Firestore doc whose ``format_version`` happens to match
    (a malformed entry → miss → refetch + overwrite the bad doc)."""
    if not isinstance(files, list):
        return False
    for f in files:
        if not isinstance(f, dict):
            return False
        p = f.get("path")
        if (
            not isinstance(p, str)
            or not p.startswith("iac/")
            or not p.endswith(".tf")
            or ".." in p.split("/")
        ):
            return False
        c = f.get("content")
        if c is not None and not isinstance(c, str):
            return False
    return True


def _read_iac_source_cache(
    pr_number: int, head_sha: str, ttl: float
) -> "tuple[list, bool] | None":
    """Return ``(files, truncated)`` from the IaC PR-source cache if a fresh,
    valid record for THIS head_sha exists, else None (a miss → caller refetches).

    Validates defensively (mirrors ``_read_l2_cache``): honoured only if it's a
    dict with the exact ``format_version``, a ``head_sha`` matching the PR's
    current head (a moved PR is a miss), a finite ``written_at`` that isn't far in
    the future, within ``ttl``, and a list ``files`` payload. Any other shape — or
    a store read error the store already swallowed to None — is a miss."""
    try:
        record = get_iac_pr_source_cache_store().get(pr_number)
    except Exception as e:  # noqa: BLE001 — a misbehaving store must never break the GET
        log.warning("iac_pr_source_read_error", extra={"error": type(e).__name__})
        return None
    if not isinstance(record, dict):
        return None
    if record.get("format_version") != _IAC_PR_SOURCE_FORMAT_VERSION:
        return None
    if record.get("head_sha") != head_sha:
        return None  # PR head moved (new commit / force-push) → refetch + overwrite
    written_at = record.get("written_at")
    files = record.get("files")
    if not isinstance(written_at, (int, float)) or not math.isfinite(written_at):
        return None
    if not _valid_iac_source_files(files):
        return None
    age = time.time() - written_at
    if age < -_IAC_PR_SOURCE_CLOCK_SKEW_TOLERANCE_S:
        return None  # stamped in the future → distrust
    if age > ttl:
        return None
    return files, bool(record.get("truncated", False))


def _resolve_iac_source(
    s: Settings, pr_number: int, head_sha: str, *, force: bool = False
) -> "tuple[list, bool]":
    """Resolve the PR's changed ``.tf`` source for the approval page, read-through
    cached on the verified ``head_sha``.

    Returns ``(files, truncated)`` — always fail-soft: GitHub unconfigured, a fetch
    error, or a misbehaving cache all degrade to ``([], False)`` so the always-200
    GET simply omits the source block. ``force=True`` (the refresh endpoint) skips
    the cache read and always refetches + resaves. The fetched content is pinned to
    ``head_sha`` (the exact commit being approved)."""
    if not (s.github_token and s.github_repo):
        return [], False
    ttl = s.iac_pr_source_cache_ttl_s
    if not force and ttl > 0:
        cached = _read_iac_source_cache(pr_number, head_sha, ttl)
        if cached is not None:
            return cached
    try:
        repo = get_repo(s.github_token, s.github_repo)
        result = github.list_pr_iac_tf_files(repo, pr_number, head_sha)
    except Exception as e:  # noqa: BLE001 — never turn the read-only GET into a 5xx
        log.warning(
            "iac_pr_source_fetch_failed",
            extra={"pr_number": pr_number, "error": type(e).__name__},
        )
        return [], False
    files = result.get("files", [])
    truncated = bool(result.get("truncated", False))
    if ttl > 0:
        try:
            get_iac_pr_source_cache_store().set(
                pr_number,
                {
                    "format_version": _IAC_PR_SOURCE_FORMAT_VERSION,
                    "written_at": time.time(),
                    "head_sha": head_sha,
                    "files": files,
                    "truncated": truncated,
                },
            )
        except Exception as e:  # noqa: BLE001 — a cache-write failure must not fail the GET
            log.warning(
                "iac_pr_source_persist_failed",
                extra={"pr_number": pr_number, "error": type(e).__name__},
            )
    return files, truncated


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
        view = iac_artifacts.load_plan_view(
            ref, bucket_name=artifacts_bucket(s), expected_repo=s.github_repo or None
        )
    except Exception:  # noqa: BLE001 — fail-closed: any resolver error → no/unverifiable plan
        log.warning("iac_plan_resolution_failed", extra={"pr_number": pr_number})
        return (ref, None)
    return (ref, view)


def _iac_pr_existence(s: Settings, pr_number: int) -> tuple[bool | None, int | None]:
    """Best-effort: does ``pr_number`` exist, and what is the repo's newest PR?

    Called ONLY on the no-plan render path (``view is None``) so the approval
    page can tell a genuinely nonexistent PR number apart from a real PR whose
    C2 plan simply has not been built yet — otherwise the two are
    indistinguishable (a real dogfooding papercut: ``/iac-approvals/200``
    rendered "no plan built yet" for a PR that never existed). Returns
    ``(pr_exists, highest_pr)``:

    - ``pr_exists``: ``True`` if the PR resolves, ``False`` on a confirmed 404,
      ``None`` when undeterminable (GitHub unconfigured, auth/network/SDK
      error) — the caller falls back to the existing plan-pending copy on ``None``.
    - ``highest_pr``: the repo's most-recently-created PR number (a friendly
      "the newest PR is #M" hint), populated ONLY on a confirmed 404, or
      ``None`` if it can't be read.

    Fail-soft on every path (the GET must stay always-200 / probe-safe): any
    error degrades to ``(None, None)`` and today's generic message. The newest-PR
    lookup runs ONLY for the confirmed-404 case, so the common plan-pending
    render adds just one GitHub round-trip and the approvable path adds none.
    """
    if not (s.github_token and s.github_repo):
        return (None, None)
    try:
        repo = get_repo(s.github_token, s.github_repo)
    except Exception:  # noqa: BLE001 — undeterminable existence → generic copy
        log.warning("iac_pr_existence_repo_failed", extra={"pr_number": pr_number})
        return (None, None)
    try:
        repo.get_pull(pr_number)
        return (True, None)  # exists; the newest-PR hint is only for the 404 case
    except Exception as e:  # noqa: BLE001 — 404 = confirmed-missing; else undeterminable
        if getattr(e, "status", None) != 404:
            log.warning(
                "iac_pr_existence_lookup_failed", extra={"pr_number": pr_number}
            )
            return (None, None)
    # Confirmed 404 → nonexistent PR number. Best-effort newest-PR hint so the
    # page can point the operator at a real number instead of a dead guess.
    highest: int | None = None
    try:
        newest = repo.get_pulls(state="all", sort="created", direction="desc")
        highest = int(newest[0].number)
    except Exception:  # noqa: BLE001 — the hint is optional; never break the page
        highest = None
    return (False, highest)


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
    (``require_cf_operator``) lives on the C5e-3 POST, not here. One UX
    consequence of that split (hackathon A.2, demo window): when CF Access IS
    configured, a request WITHOUT a ``Cf-Access-Jwt-Assertion`` header can never
    POST successfully, so the page suppresses the Approve form and renders an
    operator-only note instead of a button that would 401 (anonymous judges
    during the open-access window; direct run.app probes any time).

    Always returns 200 (probe-safe): missing comment / unverifiable artifact /
    denylist violation all render an informative page with Approve suppressed
    rather than an error code that would let a probe enumerate PR state.

    Hard invariant: this handler never mints a plan approval, never calls the
    tofu-apply worker, never reads ``plan_approvals``, and mints NO CSRF form
    token for a plan whose apply is already terminal. It is read-only but NOT
    fully stateless: it does ONE best-effort read of the IaC decision/reconcile
    pointer (``StateStore.find_decision_for_event`` — the same read the POST does
    before readiness, NOT ``plan_approvals``) so it can suppress the Approve form
    for an already applied+merged or terminally-failed plan instead of showing a
    misleading, idempotently-guarded button. Any failure of that read falls back
    to the artifact-only view, keeping the GET always-200.
    """
    s = get_settings()
    # Explicit ?lang= only (JA appended by the SPA's approval-link builders);
    # no param — every pre-existing link/test/probe — stays English.
    lang = approval_i18n.resolve_lang(request)
    ref, view = _resolve_iac_plan(s, pr_number)

    # No-plan render path: tell a nonexistent PR number apart from a real PR
    # whose plan has not been built yet (dogfooding papercut — /iac-approvals/200
    # showed "no plan built yet" for a PR that never existed). Best-effort, only
    # on this rare path so the normal approve path adds no GitHub round-trips;
    # fail-soft → (None, None) keeps the GET always-200 and the generic copy.
    # `ref is None` narrows to the genuine no-marker cases (no comment found, or a
    # nonexistent PR — both yield ref=None); the rare (ref, None) artifact-load
    # failure already has a marker comment for this number, so a probe would be
    # wasted (it would just render the same generic copy on this path).
    pr_exists: bool | None = None
    highest_pr: int | None = None
    if view is None and ref is None:
        pr_exists, highest_pr = _iac_pr_existence(s, pr_number)

    can_approve = False
    reason_blocked = ""
    # Severity classifies WHY approve is suppressed so the page can render it
    # appropriately (see iac_approval.html): "error" = a genuine hard-stop the
    # operator SHOULD be alarmed by (bad/unsafe artifact); "pending" = the gate
    # simply isn't ready yet (no plan, not configured, dry-run) — calm, not red.
    reason_severity = ""  # "" (approvable) | "error" | "pending" | "operator_only"
    form_token: str | None = None

    # Operator-identity presence for the anonymous-viewer rung below (hackathon
    # A.2). The POST hard-requires a VALID Cf-Access-Jwt-Assertion via
    # require_cf_operator whenever CF Access is configured, so a GET arriving
    # WITHOUT that header can never lead to a successful approve. Presence-only
    # on purpose: cryptographic verification stays on the POST — a forged
    # header here only buys the same form + CSRF token every request got
    # before this rung existed, and the POST still rejects it. When CF Access
    # is NOT configured (local dev / tests), the rung is inert.
    _cf_configured = bool(s.cf_access_team_domain and s.cf_access_aud_tag)
    _cf_anonymous = _cf_configured and not request.headers.get(
        "Cf-Access-Jwt-Assertion"
    )

    # Pause state for the gate ladder rung below. _pause_state_fail_closed keeps
    # the GET ALWAYS-200 (probe-safe): a failure resolving the StateStore itself
    # fails closed to a paused DISPLAY (same fail-closed direction as a get_pause
    # read error), never a 500.
    _pause = _pause_state_fail_closed()
    # Autonomy dial state for the gate ladder rung below (after the pause rung).
    # Read fail-closed so the DISPLAY matches the fail-closed POST gate.
    _autonomy = _autonomy_state_fail_closed()

    # Gate reasons come from approval_i18n.REASON_EN (single source, so the
    # JA render map can never go stale against a reworded literal here).
    if view is None:
        reason_blocked = approval_i18n.REASON_EN["no_artifact"]
        reason_severity = "pending"
    elif view.unverifiable:
        reason_blocked = approval_i18n.REASON_EN["unverifiable"]
        reason_severity = "error"
    elif not view.integrity_ok:
        reason_blocked = approval_i18n.REASON_EN["integrity_mismatch"]
        reason_severity = "error"
    elif view.denylist_violations:
        reason_blocked = approval_i18n.REASON_EN["denylist"]
        reason_severity = "error"
    elif not _iac_artifact_consistent(ref, view, pr_number):
        # The artifact does not coherently belong to this PR (metadata pr_number
        # mismatch, or comment ref ≠ fetched metadata). Fail-closed — never pin
        # an artifact for a different PR/head to this page.
        reason_blocked = approval_i18n.REASON_EN["pr_mismatch"]
        reason_severity = "error"
    elif _cf_anonymous:
        # Anonymous viewer (no CF Access JWT). Placed BEFORE the operator-state
        # rungs (token / dry-run / pause / dial) on purpose: an anonymous
        # viewer should read "operator-only", not dial-speak that invites them
        # to change settings they cannot reach — during the demo window the
        # dial is pinned below Propose+Apply, and without this ordering every
        # judge would land on the autonomy note. The artifact hard-stops above
        # still render for everyone (a bad artifact is everyone's alarm).
        reason_blocked = approval_i18n.REASON_EN["operator_only"]
        reason_severity = "operator_only"
    elif not s.driftscribe_token:
        reason_blocked = approval_i18n.REASON_EN["not_configured"]
        reason_severity = "pending"
    elif s.dry_run:
        # The POST fail-closes under dry-run (it would drive a REAL worker apply
        # while skipping the merge); suppress Approve here so the UI matches.
        reason_blocked = approval_i18n.REASON_EN["dry_run"]
        reason_severity = "pending"
    elif _pause.paused:
        # Pause gate (kill switch): one more rung — the POST refuses 423 while
        # paused, so suppress Approve here and mint NO CSRF form token. "pending"
        # severity rides the existing calm approve-pending note (not red — pause
        # is operator intent, not a broken artifact). Read errors take the same
        # rung so the fail-closed DISPLAY matches the fail-closed POST.
        reason_blocked = (
            approval_i18n.REASON_EN["paused"]
            if not _pause.read_error
            else approval_i18n.REASON_EN["paused_unreadable"]
        )
        reason_severity = "pending"
    elif _autonomy.mode != "propose_apply":
        # Autonomy dial gate (ClickOps item 11): one more rung after pause —
        # the POST refuses 409 below Propose+Apply, so suppress Approve here and
        # mint NO CSRF token. "pending" severity (calm — the artifact is fine;
        # the dial is the operator's choice, NOT a broken plan). Distinguish the
        # two Observe causes: a fail-closed read must never be presented as the
        # operator's choice (Codex should-consider 3).
        reason_blocked = _autonomy_note_for_display(_autonomy)
        reason_severity = "pending"
    else:
        can_approve = True

    # Best-effort decision-state awareness (runs BEFORE the CSRF token mint so a
    # resolved plan never even mints a token): an artifact that is otherwise
    # approvable but whose apply is already TERMINAL must not present an
    # actionable Approve form. We read ONLY the decision/reconcile pointer (NOT
    # plan_approvals) with the SAME event-key identity the POST uses, and fall
    # back to the artifact-only view on ANY error so the GET stays always-200.
    # `decision`/`outcome` reuse the outcome-banner template path (the bottom
    # form/callout is suppressed when `decision` is set). The form is KEPT for
    # still-actionable states (waiting_for_rebake = the post-rebake apply;
    # applied+failed = the merge-only reconcile) — mirroring the POST's
    # _handle_existing_iac_decision routing.
    resolved_decision = ""
    resolved_outcome = ""
    resolved_outcome_severity = ""
    # The lookup also runs when the ONLY blocker is the anonymous-viewer rung
    # (reason_severity == "operator_only" is set exactly there): a judge
    # clicking a historical rail row should see the honest "already applied"
    # / terminal-failure banner, not a generic operator-only note. can_approve
    # stays False on that path, so the still-actionable states (rebake /
    # merge-reconcile) keep the form ONLY for identified operators.
    _anonymous_only = not can_approve and reason_severity == "operator_only"
    if (can_approve or _anonymous_only) and view is not None and s.github_repo:
        existing = None
        try:
            _event_key = _iac_event_key(
                s.github_repo, pr_number, view.head_sha, view.generation_metadata
            )
            existing = get_state().find_decision_for_event(_event_key)
        except Exception:  # noqa: BLE001 — best-effort; never break the always-200 GET
            log.warning(
                "iac_decision_state_lookup_failed", extra={"pr_number": pr_number}
            )
            existing = None
        if existing is not None:
            # Serve-time merge_state reconcile (2026-06-27, Codex MF3): if the
            # PR was merged out-of-band, promote the stale applied+merge=failed
            # decision to merged so this page suppresses the Approve form. Without
            # this, the page would keep inviting a click whose POST writes a NEW
            # applied+merged doc — exactly the mutation the compute-only reconcile
            # exists to avoid. Identity for any other state; fail-soft.
            existing = reconcile_merge_state(
                existing, repo_provider=_memoized_repo_provider(s), settings=s
            )
            _st = existing.get("apply_status")
            _ms = existing.get("merge_state")
            _superseded_by = existing.get("superseded_by_pr")
            if (
                _st == "waiting_for_rebake"
                and type(_superseded_by) is int
                and _superseded_by > 0
            ):
                can_approve = False
                resolved_decision = "approve"
                # EN output is byte-identical to the previous inline strings
                # (pinned by tests/unit/test_approval_i18n.py); JA under ?lang=ja.
                resolved_outcome = approval_i18n.outcome_superseded(
                    _superseded_by, lang
                )
                # calm severity (leave resolved_outcome_severity == "") — this is
                # an expected recovery outcome, not a broken artifact.
            elif _st == "applied" and _ms == "merged":
                can_approve = False
                resolved_decision = "approve"
                resolved_outcome = approval_i18n.outcome_already_applied(lang)
            elif _st in {"failed", "failed_state_suspect", "ambiguous"}:
                can_approve = False
                resolved_decision = "approve"
                resolved_outcome_severity = "error"
                resolved_outcome = approval_i18n.outcome_terminal(_st, lang)
            # Any other recorded status (waiting_for_rebake, applied+failed, …)
            # is still actionable / idempotently guarded by the POST → KEEP form.

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
                generation_iac_tree=view.generation_iac_tree,
                iac_tree_hash=view.iac_tree_hash,
            )
        except iac_csrf.IacCsrfError:
            can_approve = False
            form_token = None
            reason_blocked = approval_i18n.REASON_EN["not_configured"]
            reason_severity = "pending"

    # Blast-radius phrase: computed pre-template so the template gate is a simple
    # `{% if blast_phrase %}` truthy test. The phrase is non-empty iff the view
    # has a parsed, non-empty change_summary (i.e. entries exist). On all other
    # paths — view=None, unverifiable, empty plan, error, resolved — either
    # change_summary is None/empty and blast_radius_phrase returns "" directly,
    # or the view is absent; the template {% if blast_phrase %} gate then
    # suppresses the line entirely on those paths.
    _summary = view.change_summary if view is not None else None
    _blast_phrase = (
        blast_radius_phrase(_summary)
        if (_summary is not None and _summary.entries)
        else ""
    )

    # "View source" affordance: the .tf the PR adds/changes, shown in-app (read-
    # through cached on the verified head_sha). Resolved ONLY for a trustworthy,
    # PR-consistent artifact — so the head_sha we key/fetch on provably belongs to
    # THIS PR (no cross-PR fetch) and we never render source for a tampered or
    # mismatched artifact. Independent of can_approve: source is visible even when
    # Approve is suppressed for a non-artifact reason (anonymous / dial / pause /
    # dry-run). Fail-soft inside _resolve_iac_source → ([], False) keeps the GET 200.
    iac_source_files: list = []
    iac_source_truncated = False
    _artifact_trustworthy = (
        view is not None
        and not view.unverifiable
        and view.integrity_ok
        and _iac_artifact_consistent(ref, view, pr_number)
    )
    if _artifact_trustworthy:
        iac_source_files, iac_source_truncated = _resolve_iac_source(
            s, pr_number, view.head_sha
        )

    ctx = {
        "lang": lang,
        "pr_number": pr_number,
        "view": view,
        # No-plan branch only (view is None): pr_exists=False → "PR doesn't exist"
        # copy + the newest-PR hint; True/None → the existing plan-pending copy.
        "pr_exists": pr_exists,
        "highest_pr": highest_pr,
        "form_token": form_token,
        "can_approve": can_approve,
        # Gate LOGIC ran on the English constants above; only the display
        # string localizes (exact-match map, identity fallback).
        "reason_blocked": approval_i18n.localize_reason(reason_blocked, lang),
        "reason_severity": reason_severity,
        # Gate 1 for the plain-language "What this change does" card: render it
        # only on non-error pages (reason_severity covers unverifiable, integrity
        # mismatch, denylist, AND the route-only artifact-vs-PR consistency check)
        # that are NOT a terminal outcome page (resolved_decision is set exactly
        # on the applied+merged / terminally-failed renders). The template adds a
        # belt-and-braces re-check of the view's own verdict (Gate 2).
        "show_summary": reason_severity != "error" and not resolved_decision,
        # Blast-radius line (Wave 2 item 8): can-affect phrase + cannot-touch note.
        # blast_phrase="" when the summary is absent/empty (see computation above)
        # → the {% if blast_phrase %} gate in the template suppresses the line.
        # cannot_touch_note is always the lib constant (POST re-renders that omit
        # these keys are protected by | default("") in the template).
        "blast_phrase": (
            approval_i18n.blast_phrase_ja(_summary) if lang == "ja" else _blast_phrase
        ),
        "cannot_touch_note": approval_i18n.localize_const(
            BLAST_CANNOT_TOUCH_NOTE, lang
        ),
        # "View source" block (PR1). Files is a list of {path, content|None, bytes};
        # the demo note is always shown with the block. (The manual "refresh source"
        # control was removed from the UI as confusing — source is pinned to
        # head_sha, so it can't change without a new commit / new page. The backing
        # POST .../refresh-source endpoint is retained for reintroduction if needed.)
        "iac_source_files": iac_source_files,
        "iac_source_truncated": iac_source_truncated,
        "iac_source_demo_note": approval_i18n.localize_const(
            _IAC_SOURCE_DEMO_NOTE, lang
        ),
    }
    if resolved_decision:
        # Render the terminal-state outcome banner + suppress the bottom form.
        ctx["decision"] = resolved_decision
        ctx["outcome"] = resolved_outcome
        ctx["outcome_severity"] = resolved_outcome_severity
    response = _TEMPLATES.TemplateResponse(request, "iac_approval.html", ctx)
    _apply_approval_security_headers(response)
    _apply_iac_csp(response)
    return response


@app.post("/iac-approvals/{pr_number}/refresh-source")
def iac_refresh_source(
    request: Request,
    pr_number: int,
    operator_email: str = Depends(require_cf_operator),
) -> Response:
    """Force a re-fetch + re-save of the PR's ``.tf`` source cache, then redirect
    back to the approval GET.

    NOTE: the approval page no longer renders a "refresh source" button (removed as
    confusing — source is pinned to head_sha and cannot change without a new
    commit). This endpoint is intentionally retained so the control can be
    reintroduced without a backend change; it is otherwise unlinked.

    Operator-gated on purpose: VIEWING source is open to everyone (incl. the demo's
    anonymous viewers, served from cache / a fetch-on-miss), but the manual refresh
    drives a GitHub fetch + a Firestore write, so it requires a verified Cloudflare
    Access operator (``require_cf_operator``: 503 if CF Access unconfigured, 401/403
    on a bad/missing JWT) plus the same-origin CSRF check the approval POST uses
    (``_check_iac_origin``). It cannot approve/apply/merge anything — it only
    re-reads public PR content the operator can already see — so no plan-bound CSRF
    token is needed. Best-effort: a fetch/cache error is swallowed by
    ``_resolve_iac_source`` and the redirect still happens (the page just shows
    whatever it can)."""
    s = get_settings()
    if not _check_iac_origin(request, s):
        raise HTTPException(status_code=403, detail="cross-site POST refused")
    ref, view = _resolve_iac_plan(s, pr_number)
    if (
        view is not None
        and not view.unverifiable
        and view.integrity_ok
        and _iac_artifact_consistent(ref, view, pr_number)
    ):
        _resolve_iac_source(s, pr_number, view.head_sha, force=True)
    return RedirectResponse(url=f"/iac-approvals/{pr_number}", status_code=303)


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
    """Same-origin check for the C5e approval POST (CSRF defense; CF Access does
    NOT stop a cross-site POST).

    Two accepted signals, in order:

    1. **Exact ``Origin`` match** — compared to ``settings.coordinator_origin``
       on (scheme, host, port). No ``Referer`` fallback.
    2. **``Sec-Fetch-Site: same-origin`` fallback** when ``Origin`` is absent or
       the opaque string ``"null"``. The C5e page ships ``Referrer-Policy:
       no-referrer``, which makes Chromium serialize the Origin of a *navigation*
       (form) POST as ``"null"`` even for a genuine same-origin submit — so an
       Origin-only check rejects every real approval. ``Sec-Fetch-Site`` is a
       Forbidden header name: the browser sets it and page JavaScript cannot, so
       a cross-site attacker — even one that suppresses its own Origin to
       ``"null"`` via ``no-referrer`` — gets ``cross-site`` here and is rejected.

    Fail-closed: an unconfigured ``coordinator_origin`` refuses ALL POSTs;
    otherwise a missing/``"null"`` Origin without ``Sec-Fetch-Site: same-origin``
    (older engines), or a real Origin that doesn't exactly match, returns
    ``False``.
    """
    if not s.coordinator_origin:
        # Unconfigured ⇒ the approval POST is disabled (fail-closed), independent
        # of any request header — preserves the "empty coordinator_origin refuses
        # POSTs" invariant (agent/config.py) even with the Sec-Fetch-Site fallback.
        return False
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        # Opaque/absent Origin (e.g. a no-referrer navigation POST): trust only
        # the browser-asserted, unspoofable Fetch-Metadata same-origin signal.
        return request.headers.get("sec-fetch-site") == "same-origin"
    # Fail-closed on a malformed Origin: ``urllib.parse`` defers parsing of the
    # port until ``.port`` is read, which raises ``ValueError`` for a non-numeric
    # / out-of-range port (e.g. ``https://host:badport``). A bad Origin must be a
    # clean 403, never a 500.
    try:
        got = urllib.parse.urlsplit(origin)
        want = urllib.parse.urlsplit(s.coordinator_origin)
        # Compare (scheme, host, port). ``.port`` is ``None`` for an implicit port
        # and an int for an explicit one, and they are NOT cross-normalized — so
        # configure ``coordinator_origin`` WITHOUT an explicit ``:443`` to match a
        # browser ``Origin`` (which omits the default port).
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


def _fetch_pr_title(repo, pr_number: int) -> str | None:
    """Best-effort PR title for the decision-rail subtitle. Fail-soft: a cosmetic
    field must NEVER break or back out an apply, so any GitHub error degrades to
    ``None`` (logged). Collapses newlines/runs of whitespace to single spaces
    (the title renders on one ellipsised line — anti-spoof), strips, caps at 200.
    Returns ``None`` for an empty/whitespace-only title."""
    try:
        raw = (repo.get_pull(pr_number).title or "")
        return " ".join(raw.split())[:200] or None
    except Exception as e:  # noqa: BLE001 — cosmetic; degrade, never propagate
        log.warning(
            "iac_pr_title_fetch_failed",
            extra={"pr_number": pr_number, "error": str(e)},
        )
        return None


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
    pr_title: str | None = None,
    applied_at: str | None = None,
) -> dict:
    """Build + persist the infra-apply decision doc (the reconcile pointer).

    Mirrors :func:`_do_rollback`'s ``record_decision`` usage. The decision doc
    is the apply-then-merge reconcile pointer: an ``apply_status=="applied"`` +
    ``merge_state=="failed"`` doc is what a re-POST reads to do a merge-only
    reconcile; an ``apply_status in {"failed","failed_state_suspect","ambiguous"}``
    doc is terminal.

    ``pr_title`` (optional) is the as-applied GitHub PR title, captured once per
    request via :func:`_fetch_pr_title` and rendered as the rail row's subtitle.
    Persisted only when a non-empty string is supplied (the PR URL, by contrast,
    is derived at serve time — see :func:`attach_iac_pr_link`).

    ``applied_at`` (optional) overrides the recorded apply moment for an
    ``applied`` row. A FRESH apply leaves it ``None`` → stamped to now (the apply
    just happened). The merge-only reconcile re-POST passes the ORIGINAL apply
    moment from the prior decision so re-recording the merged outcome doesn't
    restamp the apply day — which otherwise floats a month-old apply to the top of
    the rail and mislabels its "When" (the rail/trace surface this field). Ignored
    unless ``apply_status == "applied"``.
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
    if pr_title:
        decision["pr_title"] = pr_title
    if apply_status == "applied":
        decision["applied_at"] = applied_at or dt.datetime.now(
            dt.timezone.utc
        ).isoformat()
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
    outcome_severity: str = "",
) -> Response:
    """Re-render the approval page for a terminal SUCCESS/info POST outcome.

    Suppresses the Approve form (``can_approve=False``) and shows the outcome
    banner. ``outcome_severity="error"`` styles the (``decision="approve"``)
    banner as a red hard-stop instead of the default green note — used for a
    TERMINAL apply FAILURE so it does not read as success. Applies both the
    approval security headers and the strict IaC CSP.
    """
    response = _TEMPLATES.TemplateResponse(
        request,
        "iac_approval.html",
        {
            # Keep the page chrome in the operator's language on the POST
            # re-render (the form carries ?lang= through); the outcome string
            # itself is minted by the POST handlers and stays English.
            "lang": approval_i18n.resolve_lang(request),
            "pr_number": pr_number,
            "view": view,
            "form_token": None,
            "can_approve": False,
            "reason_blocked": "",
            # An outcome banner (decision) is the single source of truth on this
            # render; the template suppresses the bottom callout when `decision`
            # is set, so severity is irrelevant here.
            "reason_severity": "",
            "decision": decision,
            "outcome": outcome,
            "outcome_severity": outcome_severity,
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

    REJECT is a coordinator-side **non-binding** no-op (no approval exists under
    propose-on-approve, so there is nothing to deny). It persists nothing — no
    decision row, no audit entry — calls no worker, and never touches GitHub, so
    the infra PR stays open and a later GET re-shows the Approve/Reject form. It
    is "not now", not a recorded rejection; the outcome banner says so. APPROVE
    executes the ordered state machine; see the inline step comments and the
    plan's §2 table for the release matrix.
    """
    s = get_settings()

    # REJECT — no approval exists yet (propose-on-approve mints on approve), so
    # there is nothing to deny on the worker. Audit no-op + re-render, 200.
    #
    # Three consequences the banner copy spells out for the operator, because a
    # bare "Rejected" reads like a recorded, binding decision and it is none of
    # those things:
    #   1. Nothing is persisted — no decision row, no audit entry. A reject is
    #      pure local feedback on this one render.
    #   2. The GitHub PR is untouched — reject never calls GitHub, so the infra
    #      PR stays open exactly as it was (only APPROVE merges it).
    #   3. Because (1), a fresh GET re-resolves the plan and shows the Approve /
    #      Reject form again — there is no stored "rejected" flag to suppress it.
    if decision == "reject":
        _ref, view = _resolve_iac_plan(s, pr_number)
        return _render_iac_outcome(
            request,
            pr_number=pr_number,
            view=view,
            decision="reject",
            outcome=(
                "Rejected (no apply performed). No plan approval was minted. "
                "This is a local decline only: it is not recorded, and it does "
                "not change or close the GitHub PR, so reopening this page will "
                "show the Approve and Reject options again."
            ),
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

    # Pause gate (kill switch): refuse 423 in the same dry-run-precedent slot —
    # AFTER Origin+CSRF (so a cross-site probe still gets 403, never a pause
    # hint) and BEFORE _resolve_iac_plan / /propose. The REJECT path above is
    # already a coordinator-side audit no-op and stays UNGATED. Read fail-closed.
    if _pause_state_fail_closed().paused:
        raise HTTPException(status_code=423, detail=PAUSED_DETAIL)

    # Autonomy dial gate (ClickOps item 11): the apply pipeline is
    # Propose+Apply territory. AFTER the pause gate (pause outranks the dial;
    # both fail closed) and BEFORE plan re-resolution / _handle_existing_iac_decision
    # (Codex must-fix 4: the gate must fire before a waiting_for_rebake /
    # resume-apply re-POST can route into a merge or apply). 409, NOT 423 —
    # this is the operator's own configured mode, not the kill switch; clients
    # must not render it as "paused". The REJECT path above stays ungated.
    _autonomy = _autonomy_state_fail_closed()
    if _autonomy.mode != "propose_apply":
        raise HTTPException(
            status_code=409, detail=autonomy_apply_blocked_detail(_autonomy.mode)
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
        # C6: the token also pins the iac-tree sidecar identity the operator saw, so a
        # sidecar swap between GET and POST is caught (the worker re-derives + verifies
        # the real sidecar regardless — this is operator-review integrity).
        and payload["generation_iac_tree"] == view.generation_iac_tree
        and payload["iac_tree_hash"] == view.iac_tree_hash
    ):
        raise HTTPException(
            status_code=409,
            detail="the plan changed since you loaded this page; reload and re-review",
        )

    # repo + required_checks + the idempotency key are needed by BOTH the
    # existing-decision routing (c0) and the fresh path. They are computed BEFORE
    # readiness because a C6 resume re-POST hits a MERGED/closed PR — running
    # assert_pr_ready_at_sha first would fail before the ``waiting_for_rebake``
    # decision is ever consulted (Codex C6 blocker 2).
    required_checks = [
        c.strip() for c in s.iac_required_checks.split(",") if c.strip()
    ]
    repo = get_repo(s.github_token, s.github_repo)
    state = get_state()
    event_key = _iac_event_key(
        s.github_repo, pr_number, view.head_sha, view.generation_metadata
    )
    # Capture the as-applied PR title ONCE per request (fail-soft, cosmetic) for the
    # decision-rail subtitle, and thread it into every _record_iac_decision below.
    # The existing-decision paths prefer the title already on the prior decision
    # (first-approved wins) over this fresh read — see _handle_existing_iac_decision.
    pr_title = _fetch_pr_title(repo, pr_number)

    # (c0) Existing-decision routing (READ-ONLY) — runs FIRST so a resume / merge-only
    # reconcile / terminal / already-done re-POST is handled without (and before) PR
    # readiness. A fresh plan has no decision yet → fall through to readiness + claim.
    existing = state.find_decision_for_event(event_key)
    if existing is not None:
        return _handle_existing_iac_decision(
            request, s, state, existing, repo=repo, event_key=event_key, view=view,
            required_checks=required_checks, operator_email=operator_email,
            pr_number=pr_number, cf_access_jwt=cf_access_jwt, pr_title=pr_title,
        )

    # (c) Pre-propose readiness (raise, no mint — Codex r2: readiness BEFORE claim).
    # No decision yet ⇒ this flow has not merged the PR, so it should still be open.
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
    claimed = state.record_event(
        event_key,
        {
            "approver": operator_email,
            "pr_number": pr_number,
            "head_sha": view.head_sha,
            "trigger": "iac_apply",
        },
    )
    if not claimed:
        # Raced: a decision appeared between (c0) and the claim. Re-route on it.
        existing = state.find_decision_for_event(event_key)
        if existing is not None:
            return _handle_existing_iac_decision(
                request, s, state, existing, repo=repo, event_key=event_key, view=view,
                required_checks=required_checks, operator_email=operator_email,
                pr_number=pr_number, cf_access_jwt=cf_access_jwt, pr_title=pr_title,
            )
        raise HTTPException(
            status_code=409,
            detail="an apply for this plan is already in progress",
        )

    # (e) Route. A CREATE-class plan takes the C6 two-step merge-FIRST path (merge →
    # operator re-bake → resume apply); every other plan (no-op / in-place update of a
    # main-declared resource) takes the C5 apply-first path below, unchanged.
    if view.has_create:
        return _iac_create_merge_first(
            request, s, state, repo=repo, event_key=event_key, view=view,
            required_checks=required_checks, operator_email=operator_email,
            pr_number=pr_number, pr_title=pr_title,
        )

    # ---- C5 apply-first path (non-create): propose → head re-check → apply → merge ----
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
            # worker-returned 502 (a definite worker-side tofu failure — the apply
            # itself, or the pre-apply probe; either way no successful mutation)
            # from the synthetic 503 / any other 5xx (unknown whether it
            # reached/applied), and — within the 502 case — the worker's
            # ``failed_state_suspect`` phase (the failed apply could not be PROVEN
            # to have left state clean). That last case needs a state RECONCILE
            # before any retry, not just "verify" — so it gets its own
            # apply_status + a sharper message pointing at the recovery runbook.
            #
            # The suspect signal is the literal ``failed_state_suspect`` token in
            # the worker's response body — a cross-service contract: the worker
            # puts the token early in its 502 ``detail`` (well within
            # worker_client's 500-char body truncation) and it is pinned by tests
            # on BOTH boundaries. Follow-up (tracked): promote this to a structured
            # ``phase`` field in the worker JSON body rather than substring-sniffing
            # the human-readable detail.
            ambiguous = e.status_code != 502
            state_suspect = (not ambiguous) and ("failed_state_suspect" in (e.body or ""))
            apply_status = (
                "ambiguous" if ambiguous
                else "failed_state_suspect" if state_suspect
                else "failed"
            )
            _record_iac_decision(
                state,
                event_key,
                apply_status=apply_status,
                merge_state="n/a",
                approval_id=approval_id,
                head_sha=view.head_sha,
                pr_number=pr_number,
                approver=operator_email,
                pr_title=pr_title,
            )
            next_action = (
                "The failed apply could not be proven to have left state clean — "
                "run the apply-failure recovery runbook (state reconcile) before "
                "any retry."
                if state_suspect
                else "Manual verification required; do NOT retry blindly."
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
                            f"{next_action}"
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
                    "tofu-apply failed and state may be partially mutated "
                    "(failed_state_suspect); run the apply-failure recovery "
                    "runbook (state reconcile) before any retry."
                    if state_suspect
                    else "tofu-apply failed; infra may be partially mutated. "
                    "Manual verification required; do NOT retry blindly."
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
            pr_title=pr_title,
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
            pr_title=pr_title,
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
        pr_title=pr_title,
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
    pr_title: str | None = None,
    applied_at: str | None = None,
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
        # A PERMANENT block (branch protection: a required review OR status not
        # yet satisfied) is NOT cleared by a plain re-submit — the apply succeeded
        # and the merge needs out-of-band resolution (approve the review / satisfy
        # the check / admin-merge). Word that distinctly from a transient failure
        # where re-submit (the merge-only reconcile) genuinely retries. (C5g
        # carry-forward 4.)
        permanent = isinstance(e, github.PrMergeBlockedError) and e.permanent
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
            pr_title=pr_title,
            applied_at=applied_at,
        )
        if permanent:
            alert = (
                f"IaC apply SUCCEEDED but the merge for PR #{pr_number} "
                f"(head {view.head_sha[:7]}) is BLOCKED BY BRANCH PROTECTION: {e}. "
                "Resolve out-of-band (approve the required review, satisfy the "
                "required check, or admin-merge) — re-submitting alone will NOT "
                "merge it."
            )
            outcome = (
                "Applied; the merge is blocked by branch protection (a required "
                "review or status is not yet satisfied). Resolve it out-of-band "
                "(approve the review, satisfy the required check, or admin-merge), "
                "then re-submit — re-submitting alone will NOT merge it. The apply "
                "will NOT re-run."
            )
        else:
            alert = (
                f"IaC apply SUCCEEDED but merge failed for PR #{pr_number} "
                f"(head {view.head_sha[:7]}): {e}. Re-submit to retry the merge "
                "(apply will NOT re-run)."
            )
            outcome = (
                "Applied; merge pending reconcile — re-submit to retry the merge "
                "(the apply will NOT re-run)."
            )
        with contextlib.suppress(Exception):
            worker_client.call(
                "notifier",
                # ds-thm: ``alert`` interpolates ``{e}``, whose ``str()`` embeds
                # a GitHub/PyGithub response body of no fixed size. This is the
                # "apply SUCCEEDED but merge failed" page — the one an operator
                # most needs delivered — and the suppress above means a 422
                # would lose it silently.
                {"channel": "approval", "severity": "high",
                 "body": normalize_notifier_body(alert)},
            )
        return _render_iac_outcome(
            request,
            pr_number=pr_number,
            view=view,
            decision="approve",
            outcome=outcome,
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
        pr_title=pr_title,
        applied_at=applied_at,
    )
    return _render_iac_outcome(
        request,
        pr_number=pr_number,
        view=view,
        decision="approve",
        outcome="Applied and merged.",
    )


# --------------------------------------------------------------------------- #
# Phase C6b — create-class merge-FIRST routing + resume (head-config delivery).
# --------------------------------------------------------------------------- #


def _handle_existing_iac_decision(
    request: Request,
    s: Settings,
    state: StateStore,
    existing: dict,
    *,
    repo,
    event_key: str,
    view: "iac_artifacts.IacPlanView",
    required_checks: list[str],
    operator_email: str,
    pr_number: int,
    cf_access_jwt: str | None,
    pr_title: str | None = None,
) -> Response:
    """Route a re-POST that already has a recorded decision (runs BEFORE readiness).

    - ``waiting_for_rebake`` → the C6 RESUME (propose→apply against the re-baked
      worker; no merge, no readiness — the PR is merged/closed).
    - ``applied`` + ``merged`` → idempotent "already applied and merged".
    - ``applied`` + ``failed`` → merge-only reconcile (existing C5 path).
    - ``failed``/``failed_state_suspect``/``ambiguous`` → terminal render.
    - anything else → an apply is in progress (409).
    """
    status = existing.get("apply_status")
    merge_state = existing.get("merge_state")

    # First-approved title wins: prefer the title captured on the PRIOR decision over
    # a fresh read, so a PR title edited after the first approval can't overwrite the
    # as-approved snapshot on later lifecycle rows (Codex review).
    pr_title = existing.get("pr_title") or pr_title

    _superseded_by = existing.get("superseded_by_pr")
    if (
        status == "waiting_for_rebake"
        and type(_superseded_by) is int
        and _superseded_by > 0
    ):
        # Superseded by a later PR that already applied+merged — the saved plan is
        # permanently stale; never resume it. (Mirror the GET suppression.)
        return _render_iac_outcome(
            request, pr_number=pr_number, view=view, decision="approve",
            outcome=(
                f"Superseded by PR #{_superseded_by}, which is applied and merged. "
                "This plan is stale (its resource already exists) — nothing to "
                "approve here."
            ),
        )

    if status == "waiting_for_rebake":
        if merge_state != "merged":
            # Crash/failure AFTER recording the intent but BEFORE the merge completed
            # (or the merge itself failed) — re-drive the IDEMPOTENT merge (a PR merged
            # at the expected head returns already_merged; an unmerged one merges now).
            # This is the recovery for the merge-first crash window (Codex C6b-1 blocker).
            return _iac_merge_then_wait(
                request, s, state, repo=repo, event_key=event_key, view=view,
                required_checks=required_checks, operator_email=operator_email,
                pr_number=pr_number, pr_title=pr_title,
            )
        return _iac_resume_apply(
            request, s, state, repo=repo, event_key=event_key, view=view,
            operator_email=operator_email, pr_number=pr_number, cf_access_jwt=cf_access_jwt,
            pr_title=pr_title,
        )
    if status == "applied" and merge_state == "merged":
        return _render_iac_outcome(
            request, pr_number=pr_number, view=view, decision="approve",
            outcome="Already applied and merged (idempotent).",
        )
    if status == "applied" and merge_state == "failed":
        # Merge-only reconcile: the apply already happened — only the merge is being
        # (re)driven — so carry the ORIGINAL apply moment forward rather than letting
        # the re-record restamp it to now. Guard the untyped persisted dict: only a
        # non-empty str is usable; an old row without it falls back to now (best
        # effort, not a correctness guarantee).
        prior_applied_at = existing.get("applied_at")
        return _iac_merge_step(
            request, s, state, repo=repo, event_key=event_key, view=view,
            required_checks=required_checks,
            approval_id=existing.get("approval_id"),
            apply_attempt_id=existing.get("apply_attempt_id"),
            operator_email=operator_email, pr_number=pr_number, pr_title=pr_title,
            applied_at=prior_applied_at
            if isinstance(prior_applied_at, str) and prior_applied_at
            else None,
        )
    if status in {"failed", "failed_state_suspect", "ambiguous"}:
        note = (
            "The failed apply could not be proven to have left state clean — run the "
            "apply-failure recovery runbook (state reconcile) before any retry; this "
            "will NOT be retried automatically."
            if status == "failed_state_suspect"
            else "Manual verification required; this will NOT be retried automatically."
        )
        return _render_iac_outcome(
            request, pr_number=pr_number, view=view, decision="approve",
            outcome=f"Terminal state recorded: apply_status={status!r}. {note}",
            outcome_severity="error",  # a terminal failure must not read as green/success
        )
    raise HTTPException(
        status_code=409, detail="an apply for this plan is already in progress"
    )


def _iac_create_merge_first(
    request: Request,
    s: Settings,
    state: StateStore,
    *,
    repo,
    event_key: str,
    view: "iac_artifacts.IacPlanView",
    required_checks: list[str],
    operator_email: str,
    pr_number: int,
    pr_title: str | None = None,
) -> Response:
    """C6 step 1: a CREATE-class plan is merged to ``main`` FIRST, then the operator
    re-bakes the worker, then re-opens this page to Apply (the resume). The worker
    cannot admit the create until it is re-baked from the merged main + the iac/-tree
    hash matches — so we merge here and hand off, recording ``waiting_for_rebake``.

    The fail-closed sidecar check runs FIRST and releases the event (nothing recorded
    yet). After it passes, the ``waiting_for_rebake``+``pending`` pointer is recorded
    BEFORE the merge and ``_iac_merge_then_wait`` does the merge: on merge failure that
    pointer is KEPT (the recovery handle), not released, so a re-submit re-tries the
    idempotent merge.
    """
    # A create needs the C2 sidecar (the worker's hash gate is mandatory for creates).
    if not view.generation_iac_tree or not view.iac_tree_hash:
        state.release_event(event_key)
        raise HTTPException(
            status_code=409,
            detail="create-class plan has no iac-tree sidecar in the C2 comment; "
            "re-run the plan-builder (C2) so the sidecar is produced",
        )
    # Record the resume pointer (merge_state="pending") BEFORE the irreversible,
    # no-auto-revert merge. If the coordinator crashes between the merge and the
    # post-merge record, a re-POST finds waiting_for_rebake+pending and re-drives the
    # idempotent merge — closing the merge-first crash window (Codex C6b-1 blocker).
    _record_iac_decision(
        state, event_key, apply_status="waiting_for_rebake", merge_state="pending",
        head_sha=view.head_sha, pr_number=pr_number, approver=operator_email,
        pr_title=pr_title,
    )
    return _iac_merge_then_wait(
        request, s, state, repo=repo, event_key=event_key, view=view,
        required_checks=required_checks, operator_email=operator_email, pr_number=pr_number,
        pr_title=pr_title,
    )


def _iac_merge_then_wait(
    request: Request,
    s: Settings,
    state: StateStore,
    *,
    repo,
    event_key: str,
    view: "iac_artifacts.IacPlanView",
    required_checks: list[str],
    operator_email: str,
    pr_number: int,
    pr_title: str | None = None,
) -> Response:
    """Idempotent merge → record ``waiting_for_rebake``+``merged`` → instruct re-bake.

    Shared by the fresh create-class path and the pending-recovery path. The decision
    pointer is ALREADY ``waiting_for_rebake`` (``pending`` here, becoming ``merged``),
    so on merge failure it is LEFT IN PLACE (the recovery pointer) — never released —
    and a re-submit re-tries the (idempotent) merge once any branch-protection block is
    resolved. ``merge_pr_at_sha`` returns ``already_merged`` for a PR merged at the
    expected head, so re-driving after a crash is a safe no-op."""
    try:
        github.merge_pr_at_sha(
            repo, pr_number=pr_number, expected_head_sha=view.head_sha,
            required_checks=required_checks,
            merge_method=(s.iac_merge_method or "squash"), dry_run=s.dry_run,
        )
    except Exception as e:  # noqa: BLE001 — merge failed ⇒ no mutation; keep the pointer
        permanent = isinstance(e, github.PrMergeBlockedError) and e.permanent
        detail = (
            f"merge for PR #{pr_number} is blocked by branch protection ({e}); "
            "resolve out-of-band (approve the required review / satisfy the required "
            "check / admin-merge), then re-submit — nothing was applied"
            if permanent
            else f"merge for PR #{pr_number} failed ({e}); nothing was applied — re-submit to retry"
        )
        raise HTTPException(status_code=409, detail=detail) from e

    # Merged. Promote the pointer to merged + instruct the operator to re-bake.
    _record_iac_decision(
        state, event_key, apply_status="waiting_for_rebake", merge_state="merged",
        head_sha=view.head_sha, pr_number=pr_number, approver=operator_email,
        pr_title=pr_title,
    )
    if view.has_import:
        merge_outcome = (
            f"Merged to main (PR #{pr_number}, head {view.head_sha[:7]}). This plan "
            "ADOPTS (imports) an existing resource into IaC management — nothing in "
            "your infrastructure will be created or modified — but the worker must "
            "still be RE-BAKED from the new main before it can apply. Operator: run "
            "`gcloud builds submit --config=infra/cloudbuild.tofu-apply.yaml "
            "--substitutions=_TAG=$(git rev-parse --short HEAD) "
            "--project=driftscribe-hack-2026`, then RELOAD this page and click Apply "
            f"to complete. Expected iac_tree_hash: {view.iac_tree_hash}."
        )
    else:
        merge_outcome = (
            f"Merged to main (PR #{pr_number}, head {view.head_sha[:7]}). This plan "
            "CREATES a resource, so the worker must be RE-BAKED from the new main "
            "before it can apply. Operator: run `gcloud builds submit "
            "--config=infra/cloudbuild.tofu-apply.yaml "
            "--substitutions=_TAG=$(git rev-parse --short HEAD) "
            "--project=driftscribe-hack-2026`, then RELOAD this page and click Apply "
            f"to complete. Expected iac_tree_hash: {view.iac_tree_hash}."
        )
    return _render_iac_outcome(
        request, pr_number=pr_number, view=view, decision="approve",
        outcome=merge_outcome,
    )


def _iac_resume_apply(
    request: Request,
    s: Settings,
    state: StateStore,
    *,
    repo,
    event_key: str,
    view: "iac_artifacts.IacPlanView",
    operator_email: str,
    pr_number: int,
    cf_access_jwt: str | None,
    pr_title: str | None = None,
) -> Response:
    """C6 step 2 (resume): the create-class PR is already merged; the operator has
    (hopefully) re-baked the worker. Drive propose→apply against it, forwarding the
    sidecar generation. NO merge (done) and NO readiness (PR closed). The worker's
    iac/-tree hash gate is the real guard: if the re-bake hasn't happened (or main
    advanced), propose/apply fail-closed and the ``waiting_for_rebake`` decision is
    LEFT IN PLACE so the operator can re-bake and retry.

    Post-merge failure handling (§3.6): no-mutation refusals (4xx/423/409, incl.
    tree_mismatch when not re-baked) keep ``waiting_for_rebake`` for retry; a 502 apply
    failure is ALWAYS terminal ``failed_state_suspect`` — FREEZE + orphan reconcile,
    because a failed CREATE can leave a live resource absent from state (Codex C6
    blocker 4); a non-502 5xx is terminal ``ambiguous``. The merge is never auto-
    reverted (merged main is the desired state). See
    docs/runbooks/iac-apply-failure-recovery.md.
    """
    gen_iac_tree = view.generation_iac_tree
    if not gen_iac_tree:
        raise HTTPException(
            status_code=409,
            detail="create-class resume has no iac-tree sidecar; re-run the plan-builder",
        )

    # C6c re-bake readiness pre-check: confirm the worker is baked from the approved
    # head's config BEFORE burning a propose. Best-effort — a GET failure (worker
    # unreachable, or an older revision without the endpoint) falls through to
    # propose→apply, where the worker's apply-time hash gate is the authoritative
    # guard. A DEFINITE mismatch short-circuits with a precise "not re-baked" message.
    if view.iac_tree_hash:
        baked_hash: str | None = None
        try:
            baked_hash = worker_client.get_baked_iac_hash().get("iac_tree_hash")
        except Exception as e:  # noqa: BLE001 — best-effort: log + fall through to the gate
            log.info("iac_rebake_precheck_unavailable", extra={"pr_number": pr_number, "error": str(e)})
        if isinstance(baked_hash, str) and baked_hash and baked_hash != view.iac_tree_hash:
            return _render_iac_outcome(
                request, pr_number=pr_number, view=view, decision="approve",
                outcome=(
                    "Merged, but the worker is NOT re-baked from the merged main yet: "
                    f"its baked iac_tree_hash ({baked_hash[:12]}…) != the approved "
                    f"({view.iac_tree_hash[:12]}…). Re-bake (`gcloud builds submit "
                    "--config=infra/cloudbuild.tofu-apply.yaml "
                    "--substitutions=_TAG=$(git rev-parse --short HEAD)`), then RELOAD "
                    "and click Apply. If main advanced with another iac/ change, "
                    "re-run the plan-builder (re-plan)."
                ),
            )

    # Propose (mints a fresh approval). A refusal here (e.g. the worker's 422 tree
    # gate when not yet re-baked) is NO-mutation → keep waiting_for_rebake, render a
    # retry-after-rebake page rather than a hard error.
    try:
        pr_res = worker_client.call_propose(
            view.artifact_uri_metadata, view.generation_metadata, operator_email,
            cf_access_jwt, generation_iac_tree=gen_iac_tree,
        )
    except worker_client.WorkerClientError as e:
        return _iac_resume_not_ready(request, view, pr_number, action="propose", err=e)
    if not isinstance(pr_res, dict):
        raise HTTPException(status_code=502, detail="tofu-apply returned a malformed propose response")
    approval_id = pr_res.get("approval_id")
    approval_token = pr_res.get("approval_token")
    if not (isinstance(approval_id, str) and approval_id and isinstance(approval_token, str) and approval_token):
        raise HTTPException(status_code=502, detail="tofu-apply returned a malformed propose response")

    # Apply (the merge already happened, so no head re-check; the worker hash gate guards it).
    try:
        apply_res = worker_client.call_apply(
            approval_id, approval_token, cf_access_jwt, generation_iac_tree=gen_iac_tree,
        )
    except worker_client.WorkerClientError as e:
        if e.status_code in (403, 404, 422, 423, 409):
            # PRE-claim (403/404) or post-claim NON-mutating (422/423/409, incl.
            # tree_mismatch_refused when the worker isn't re-baked): no infra change.
            # Best-effort clean the orphaned pending; keep waiting_for_rebake for retry.
            with contextlib.suppress(Exception):
                worker_client.call_plan_deny(approval_id, approval_token)
            return _iac_resume_not_ready(request, view, pr_number, action="apply", err=e)
        # 5xx / unknown on a CREATE resume: a failed `tofu apply` that CREATES can
        # leave a live ORPHAN resource that was never written to state — which the
        # worker's post-failure "clean" diagnosis CANNOT disprove (a resource absent
        # from state is absent from the refresh). So a 502 here is ALWAYS
        # failed_state_suspect (FREEZE + orphan reconcile), never a retryable plain
        # "failed" (Codex C6 blocker 4); a non-502 5xx (timeout/unreachable) is
        # ambiguous. Both are terminal — the operator runs the recovery runbook.
        apply_status = "failed_state_suspect" if e.status_code == 502 else "ambiguous"
        _record_iac_decision(
            state, event_key, apply_status=apply_status, merge_state="merged",
            approval_id=approval_id, head_sha=view.head_sha, pr_number=pr_number,
            approver=operator_email, pr_title=pr_title,
        )
        if view.has_import:
            _notifier_body = (
                f"C6 adopt-class apply {apply_status} for PR #{pr_number} (already MERGED to "
                f"main, head {view.head_sha[:7]}). An import that fails normally writes no state "
                "and creates nothing, but that is verified, never assumed — run the apply-failure "
                "recovery runbook before any retry."
            )
            _detail_body = (
                f"tofu-apply {apply_status} on the adopt-class resume; the PR is already merged. "
                "An import that fails normally writes no state and creates nothing, but that is "
                "verified, never assumed — run the apply-failure recovery runbook before any retry."
            )
        else:
            _notifier_body = (
                f"C6 create-class apply {apply_status} for PR #{pr_number} (already MERGED to "
                f"main, head {view.head_sha[:7]}). A created resource may exist out of state — "
                "run the apply-failure recovery runbook (orphan check) before any retry."
            )
            _detail_body = (
                f"tofu-apply {apply_status} on the create-class resume; the PR is already merged and a "
                "created resource may exist out of state — run the apply-failure recovery runbook "
                "(orphan check) before any retry."
            )
        with contextlib.suppress(Exception):
            worker_client.call("notifier", {"channel": "approval", "severity": "high", "body": _notifier_body})
        raise HTTPException(status_code=(502 if apply_status != "ambiguous" else 504), detail=_detail_body) from e

    if not (isinstance(apply_res, dict) and apply_res.get("status") == "applied"
            and apply_res.get("approval_id") == approval_id
            and isinstance(apply_res.get("apply_attempt_id"), str) and apply_res.get("apply_attempt_id")):
        _record_iac_decision(
            state, event_key, apply_status="ambiguous", merge_state="merged",
            approval_id=approval_id, head_sha=view.head_sha, pr_number=pr_number, approver=operator_email,
            pr_title=pr_title,
        )
        raise HTTPException(status_code=504, detail="tofu-apply returned a malformed success on the create-class resume; verify manually")

    _record_iac_decision(
        state, event_key, apply_status="applied", merge_state="merged",
        approval_id=approval_id, apply_attempt_id=apply_res.get("apply_attempt_id"),
        head_sha=view.head_sha, pr_number=pr_number, approver=operator_email,
        pr_title=pr_title,
    )
    success_outcome = (
        "Applied (adopt) — the existing resource is now under IaC management; "
        "nothing was modified. The PR was already merged to main. Done."
        if view.has_import else
        "Applied (create) — the PR was already merged to main. Done."
    )
    return _render_iac_outcome(
        request, pr_number=pr_number, view=view, decision="approve",
        outcome=success_outcome,
    )


def _iac_resume_not_ready(
    request: Request, view: "iac_artifacts.IacPlanView", pr_number: int, *, action: str, err
) -> Response:
    """Render the 'merged but the worker is not re-baked yet (or main advanced)' page.
    The ``waiting_for_rebake`` decision is unchanged, so the operator re-bakes and
    re-submits. 200 (the operator's action is legitimate, just premature) — NOT an
    error code."""
    return _render_iac_outcome(
        request, pr_number=pr_number, view=view, decision="approve",
        outcome=(
            f"Merged, but the worker could not apply yet (tofu-apply {action} refused: "
            f"status {getattr(err, 'status_code', '?')}). The worker is likely not re-baked "
            "from the merged main, or main advanced after the merge. Re-bake "
            "(`gcloud builds submit --config=infra/cloudbuild.tofu-apply.yaml "
            "--substitutions=_TAG=$(git rev-parse --short HEAD)`), confirm the baked "
            f"iac_tree_hash is {view.iac_tree_hash}, then RELOAD and click Apply. If main "
            "advanced with another iac/ change, re-run the plan-builder (re-plan)."
        ),
    )


@app.post("/approvals/{approval_id}", response_class=HTMLResponse)
def approval_post(
    request: Request,
    approval_id: str,
    t: str = Form(...),
    decision: Literal["approve", "reject"] = Form(...),
    ack_target_violates_contract: str = Form(""),
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
    # Set only on the ds-z4z fall-through below (worker 5xx, doc records a
    # phase) — see the comment there.
    approval_after_error = None

    # Pause gate (kill switch): read ONCE here — the gate below uses it for the
    # approve refusal AND the re-render context carries it to disable Approve in
    # the page. APPROVE is gated 423 (it drives a real Cloud Run traffic shift);
    # REJECT is ALLOWED while paused — denying a pending rollback is the
    # safety-direction (it prevents action). Blocking reject would keep a live
    # approval pending, the opposite of what a kill switch is for. The helper
    # also covers a get_state() failure (fail-closed paused → approve 423,
    # reject still goes through to the worker — the safety direction holds).
    pause = _pause_state_fail_closed()
    # Autonomy dial: read once here for the approve gate AND the re-render
    # context (so the page disables Approve + explains the dial). Reject stays
    # ungated, same safety direction as pause.
    autonomy = _autonomy_state_fail_closed()
    autonomy_blocked = autonomy.mode != "propose_apply"

    if decision == "reject":
        try:
            execute_result = worker_client.call_deny(approval_id, t)
        except worker_client.WorkerClientError as e:
            # Worker rejected the deny: bad token, expired, missing,
            # already used/denied, etc. Pass through 409 + map 5xx to
            # 502 (see docstring); everything else collapses to 403.
            raise _map_worker_error(e, action="deny") from e
    else:  # approve
        if pause.paused:
            raise HTTPException(status_code=423, detail=PAUSED_DETAIL)
        # Autonomy dial gate: AFTER the pause gate (pause outranks the dial),
        # 409 not 423 — the operator's own configured mode, not the kill
        # switch. Reject above stays ungated.
        if autonomy_blocked:
            raise HTTPException(
                status_code=409,
                detail=autonomy_apply_blocked_detail(autonomy.mode),
            )
        # ds-uwc: a target revision that PROVABLY violates the contract needs a
        # deliberate second click, not just a badge next to an unchanged button.
        # Rolling onto it removes one drift and introduces another.
        #
        # What this is, precisely, so nobody mistakes it for more: a SPEED BUMP,
        # not an authorization control. Authorization is still — only — the
        # single-use HMAC token the worker verifies. This page authenticates
        # possession of a link, not an operator's identity, and it is
        # anonymously reachable during a demo window. An older coordinator
        # during a rollout renders no checkbox and executes as it always did;
        # that is why the snapshot stays a display field under the additive-only
        # rule in driftscribe_lib/approvals.py.
        #
        # Derived from the approval DOC — immutable, worker-written — and never
        # from a hidden form field, which the same click could have set.
        #
        # An UNKNOWN snapshot does not demand the acknowledgment. Every approval
        # minted before ds-uwc is unknown, as is every one from a worker that
        # has not deployed yet; requiring it there would put a new obstacle in
        # front of the operator without a single new fact to justify it. Only a
        # snapshot that positively PROVES a violation asks for the tick.
        try:
            _approval_for_ack = store.get(approval_id)
        except Exception as e:  # noqa: BLE001 — a read blip must not block a rollback
            log.warning("approval_ack_read_failed", extra={"error": type(e).__name__})
            _approval_for_ack = None
        if _approval_for_ack is not None:
            _view = _rollback_change_view(_approval_for_ack)
            if _view.get("state") == "ok" and _view.get("violates"):
                if ack_target_violates_contract.strip() != "1":
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "the target revision does not satisfy the ops "
                            "contract for a var marked allow_manual_change="
                            "false; approving it needs the acknowledgment on "
                            "the approval page"
                        ),
                    )

        # The phase BEFORE we hand the token to the worker, so the error handler
        # below can tell "this request claimed and then failed" from "an old
        # approval plus a current outage". Read fail-soft: None here only ever
        # makes the fall-through stricter.
        phase_before = _recorded_phase(store, approval_id)
        try:
            execute_result = worker_client.call_execute(approval_id, t)
        except worker_client.WorkerClientError as e:
            # ds-z4z: a 5xx out of /execute is frequently NOT an outage. The
            # worker answers 504 with "rollback still in progress; outcome
            # unconfirmed" for any LRO that outlives its poll budget, and 502
            # when the poll itself broke — both are DESIGNED outcomes of the
            # ds-2mc work, and in both the traffic shift is usually landing
            # fine. Raising here produced a FastAPI JSON error body reading
            # "rollback worker unavailable", so the four phase branches added
            # to approval.html were unreachable on the POST path (an operator
            # only ever saw them by hand-revisiting the URL), and the demo's
            # headline flow narrated a working rollback as a broken system.
            #
            # So: ask the doc before calling it an outage. A recorded phase
            # means the worker got at least as far as claiming the approval,
            # which makes the doc — not the transport error — the truth worth
            # rendering. Fall through to the normal re-render with
            # execute_result still None, so the page shows the phase copy and
            # NOT the "Rollback dispatched" note.
            #
            # TWO gates, and both are load-bearing.
            #
            # 5xx ONLY. Without this the fall-through fires on a 403 too, so
            # replaying a spent token against a phased doc answers 200 while the
            # same replay against a pending one answers 403 — a status-code
            # oracle for approval state, which is the exact property
            # _map_worker_error's 4xx collapse exists to deny. (Codex review of
            # this commit caught it: the comment above described the gate, the
            # code did not have it.)
            #
            # And the phase must have CHANGED across this call. A phase alone
            # proves only that SOME request once claimed this approval, not that
            # this one did — so a spent-token replay arriving while the worker is
            # genuinely unreachable would otherwise render the original outcome
            # and hide a live outage behind an HTTP 200. Requiring a transition
            # ties the page we render to the request that caused it.
            #
            # Given both, no new disclosure: the doc moved because THIS token
            # claimed it, and GET /approvals already renders this same phase copy
            # to anyone holding the URL (it is always-200 by design).
            if 500 <= e.status_code < 600:
                approval_after_error = _approval_if_phase_advanced(
                    store, approval_id, before=phase_before
                )
            if approval_after_error is None:
                raise _map_worker_error(e, action="execute") from e
            log.warning(
                "approve: worker %s on execute, but the approval records phase=%s "
                "— rendering the recorded outcome instead of an outage",
                e.status_code,
                (approval_after_error.apply_audit or {}).get("phase"),
                extra={"approval_id": approval_id},
            )

    # Re-fetch the doc so the page reflects the new status. On the ds-z4z path
    # above we already hold it — don't pay a second Firestore read for it.
    approval = approval_after_error if approval_after_error is not None else store.get(approval_id)
    response = _TEMPLATES.TemplateResponse(
        request,
        "approval.html",
        {
            # Chrome-only localization on the POST re-render (?lang= carried
            # by the form action); Python-minted outcome details stay English.
            "lang": approval_i18n.resolve_lang(request),
            "approval_id": approval_id,
            "approval": approval,
            # Don't echo the token back into the rendered form. The
            # decision has been processed; subsequent submits should
            # come from a fresh URL with its own ``?t=``.
            "token": "",
            "expired": False,
            "decision": decision,
            "decision_result": execute_result,
            "paused": pause.paused,
            "autonomy_blocked": autonomy_blocked,
            # ds-uwc: the post-decision re-render keeps the "what this changed"
            # card, so the record of what was approved stays on the page the
            # operator is looking at. Computed from the same immutable snapshot,
            # so it does not re-observe the service and cannot rewrite history.
            "change_view": _rollback_change_view(approval) if approval else None,
            "autonomy_detail": approval_i18n.localize_reason(
                _autonomy_note_for_display(autonomy),
                approval_i18n.resolve_lang(request),
            ),
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

    Phase 17.A.3 introduced ``workload``; the PR #109 follow-up made it
    REQUIRED. ``workload`` selects which agent — and therefore which
    tool set, including mutation tools — answers the request, so it
    must be explicit: a workload-less POST once defaulted to the
    mutation-capable drift workload and an out-of-domain probe prompt
    became fabricated docs PR #109. The Literal closes the set; pydantic
    422s both a missing field and an unknown value before the handler
    body runs. The SPA always sends workload (App.svelte); /recheck and
    /eventarc keep their own documented drift defaults — those are
    autonomous surfaces, not this one.

    Hackathon A.4 added the length caps — cost rails for the anonymous
    judging window. ``prompt`` is bounded far above honest chat use (a
    pasted log or diff fits; ~2k tokens of model input) so the only thing
    the cap ever rejects is a deliberately huge body, before it reaches
    Gemini. ``session_id`` is an opaque id (UUID-sized in practice), so
    its cap is tighter. Validation runs before the handler body, so an
    over-cap request never starts a run.
    """

    prompt: str = Field(max_length=8000)
    session_id: str | None = Field(default=None, max_length=128)
    # Durable multi-turn thread id (P1). Distinct from the inert ADK
    # ``session_id``: this is the conversation the turn belongs to, crew-locked
    # to ``workload``. Server-generated UUIDs; the pattern rejects path escapes
    # so a client echo can't smuggle a ``/`` into a Firestore doc id.
    conversation_id: str | None = Field(
        default=None, max_length=128, pattern=r"^[A-Za-z0-9_-]{1,128}$"
    )
    workload: Literal["drift", "upgrade", "explore", "provision"]
    # Opt-in throwaway turn: run the chat and return the reply, but persist NO
    # conversation doc and echo NO conversation_id. For health/verification
    # probes so repeated identical checks don't pile up in the operator's
    # conversation history. It suppresses ONLY conversation persistence — traces,
    # tool calls, decisions, and PRs still happen, so use it for read-only probes
    # (a fresh-conversation flood from authenticated verification traffic via the
    # run.app URL is the gap the CF demo rate-limiter doesn't cover).
    ephemeral: StrictBool = Field(default=False)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _ephemeral_excludes_conversation(self) -> "ChatRequest":
        # An ephemeral one-shot persists nothing, so continuing a durable thread
        # is contradictory — surface it as 422 rather than silently dropping one.
        if self.ephemeral and self.conversation_id is not None:
            raise ValueError(
                "ephemeral chats cannot continue a conversation_id"
            )
        return self


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


def _paused_chat_reply(pause: PauseState) -> str:
    """Build the calm operator-facing reply for a /chat turn refused under pause.

    Honest by construction: name the actor / time / reason only when the pause
    doc actually carries them, and say so plainly when the flag itself could not
    be read (read_error) — in that case the system is fail-closed paused even
    though no operator chose it, and the operator deserves to know which.
    """
    if pause.read_error:
        return (
            "DriftScribe is paused — the pause state could not be read, so the "
            "system fails closed. No tools were run and no changes were made. "
            "Resume from the pause control in the operator UI once the pause "
            "state is readable again."
        )
    by = f" by {pause.actor}" if pause.actor else ""
    # Same timestamp shape as _serialize_pause_state: datetime (InMemory) and
    # Firestore DatetimeWithNanoseconds both have .isoformat(); str() covers any
    # other datetime-like — one timestamp format codebase-wide, even in prose.
    if pause.updated_at is None:
        at = ""
    elif hasattr(pause.updated_at, "isoformat"):
        at = f" at {pause.updated_at.isoformat()}"
    else:
        at = f" at {pause.updated_at}"
    reason = f" — reason: {pause.reason}" if pause.reason else ""
    return (
        f"DriftScribe is paused — an operator suspended all agent activity{by}"
        f"{at}{reason}. No tools were run and no changes were made. Resume from "
        "the pause control in the operator UI."
    )


def _paused_chat_response(
    pause: PauseState, *, wants_sse: bool, session_id: str | None,
    conversation_id: str | None = None,
) -> "dict | StreamingResponse":
    """Return the calm /chat refusal — 200 on BOTH JSON and SSE paths.

    The deliberate exception to the 423 pause refusal (see the pause plan §3/§4):
    the operator-facing chat surface gets a readable answer, not an error toast;
    machine callers detect ``paused: true`` in the body. No LLM call is made.

    SSE: a one-frame stream — a single ``done`` frame carrying the SAME dict and
    NO ``meta`` frame. There is no trace for a refused turn, so the SPA's
    traceId stays null and its trace backfill no-ops; the headers below mirror
    the normal SSE branch minus ``X-Trace-Id``.
    """
    payload = {
        "reply": _paused_chat_reply(pause),
        "tool_calls": [],
        "session_id": session_id or "",
        "paused": True,
    }
    if conversation_id:
        payload["conversation_id"] = conversation_id
    if not wants_sse:
        return payload

    async def _one_done_frame():
        yield _sse_frame(event="done", data=payload)

    return StreamingResponse(
        _one_done_frame(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# Heartbeat cadence: emit an SSE comment if no event arrives within this
# window. Keeps the Cloudflare read-idle timeout (~120s) from dropping the
# connection during a long worker tool call. Does NOT extend Cloud Run's
# total request timeout (see infra/cloudbuild.yaml --timeout).
_SSE_HEARTBEAT_S = 15


def _chat_stream(
    workload: str, prompt: str, session_id: str | None, *, autonomy_mode: str,
    prior_turns: list[dict] | None = None, demo_anon: bool = False,
    denied_tools: frozenset[str] | None = None,
):
    """Select the chat-stream async generator for a workload.

    The ``provision`` workload (Phase D5) routes through the parallel
    fan-out orchestrator ``agent.fanout.run_provision_fanout_stream`` — which
    internally falls back to the single-agent ``run_chat_stream`` for a
    1-slice/coupled change — while every other workload uses
    ``run_chat_stream`` directly. BOTH yield the SAME
    ``{"type":"event"|"result"}`` item shapes, so all downstream framing
    (``_chat_sse`` SSE frames, the JSON drain) is workload-agnostic. Imports
    are lazy to avoid pulling ADK/fanout at module import and to dodge an
    import cycle (``agent.fanout`` imports ``agent.adk_agent``).

    ``autonomy_mode`` is a REQUIRED keyword arg, forwarded to BOTH the fan-out
    orchestrator (which gates its coordinator-direct editor call) and the
    single-agent path (Layer-0 tool filtering)."""
    if workload == "provision":
        from agent.fanout import run_provision_fanout_stream
        return run_provision_fanout_stream(
            prompt, session_id, autonomy_mode=autonomy_mode,
            prior_turns=prior_turns, demo_anon=demo_anon,
            denied_tools=denied_tools,
        )
    from agent.adk_agent import run_chat_stream
    return run_chat_stream(
        prompt, session_id=session_id, workload=workload,
        autonomy_mode=autonomy_mode, prior_turns=prior_turns,
        demo_anon=demo_anon, denied_tools=denied_tools,
    )


async def _drain_chat_stream_result(agen) -> dict:
    """Drain a chat-stream async generator to the JSON ``/chat`` result dict.

    Mirrors ``run_chat``'s drain (Phase 22) but works on any selected stream
    so the fan-out orchestrator's JSON output stays identical to the
    single-agent path's. The orchestrator and ``run_chat_stream`` both yield
    zero-or-more ``{"type":"event"}`` items followed by exactly one
    ``{"type":"result", ...}``; we ignore the events (the JSON path has no
    timeline) and project the single result into the same
    ``{reply, tool_calls, session_id}`` shape ``run_chat`` returns (plus an
    optional ``iac_pr`` pointer when a first-authoring infra run produced one).
    Raising on an exhausted stream with no result keeps the "no final response"
    RuntimeError identical to ``run_chat``'s, so the ``/chat`` ``except``
    tuple maps it the same way.

    The generator is closed in a ``finally``. Returning on the terminal item
    leaves it SUSPENDED at its last ``yield``, so any cleanup it owns — for
    ``_persisting_chat_stream``, releasing the conversation's run lease — runs
    only at async-generator finalization, which the event loop schedules
    whenever the last reference drops. CPython usually gets there quickly; this
    makes it deterministic rather than usually."""
    try:
        return await _drain(agen)
    finally:
        await agen.aclose()


async def _drain(agen) -> dict:
    async for item in agen:
        if item["type"] == "result":
            out = {
                "reply": item["reply"],
                "tool_calls": item["tool_calls"],
                "session_id": item["session_id"],
            }
            # Contract parity with the SSE done frame: pass the approval pointer
            # through when a first-authoring infra run produced one.
            if item.get("iac_pr"):
                out["iac_pr"] = item["iac_pr"]
            # Multi-turn (P1): echo the durable thread id when the turn persisted.
            if item.get("conversation_id"):
                out["conversation_id"] = item["conversation_id"]
            # Crew handoff: the proposal + its single-use nonce, present only
            # when this turn proposed one AND the proposal committed.
            if item.get("handoff"):
                out["handoff"] = item["handoff"]
            if item.get("crew_change"):
                out["crew_change"] = item["crew_change"]
            return out
    raise RuntimeError("ADK chat agent produced no final response")


async def _chat_sse(prompt: str, session_id: str | None, conv: dict,
                    workload: str, trace_id: str, *, autonomy_mode: str,
                    demo_anon: bool = False,
                    denied_tools: frozenset[str] | None = None):
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

    The stream generator is selected by :func:`_chat_stream` (Phase D5-7):
    ``provision`` fans out via ``run_provision_fanout_stream``, every other
    workload uses ``run_chat_stream`` — both yield identical item shapes, so
    the queue/heartbeat/frame-shaping below stays workload-agnostic.
    """
    t_tok = set_trace_id(trace_id)
    w_tok = set_workload(workload)
    queue: asyncio.Queue = asyncio.Queue()

    async def _produce():
        try:
            async for item in _persisting_chat_stream(
                workload, prompt, conv, trace_id, session_id,
                autonomy_mode=autonomy_mode, demo_anon=demo_anon,
                denied_tools=denied_tools,
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
                # Operator decision 2026-07-09: the anonymous SSE scrub of the
                # rollback approval ?t= token is removed — the visitor holds the
                # operator seat and must receive the live approval link in the
                # stream, same as the operator. Reverses audit C1's SSE scrub.
                if item["type"] == "event":
                    yield _sse_frame(data=item["event"])
                else:  # "result"
                    done_data = {
                        "reply": item["reply"],
                        "tool_calls": item["tool_calls"],
                        "session_id": item["session_id"],
                    }
                    # Only a first-authoring infra run carries this — the SPA
                    # reads it to render a clickable "Review & approve" CTA.
                    if item.get("iac_pr"):
                        done_data["iac_pr"] = item["iac_pr"]
                    # Multi-turn (P1): the durable thread id the client stores +
                    # replays on the next turn. Present only when the turn
                    # persisted.
                    if item.get("conversation_id"):
                        done_data["conversation_id"] = item["conversation_id"]
                    # Crew handoff: the proposal rides the TERMINAL frame, not a
                    # dedicated mid-stream one. The nonce does not exist until
                    # the proposal persists, and persistence happens at the end
                    # of the stream — a mid-stream frame could only advertise a
                    # credential that is not yet real.
                    if item.get("handoff"):
                        done_data["handoff"] = item["handoff"]
                    if item.get("crew_change"):
                        done_data["crew_change"] = item["crew_change"]
                    yield _sse_frame(event="done", data=done_data)
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
        # AI Gemini quota for the coordinator model (COORDINATOR_MODEL in
        # agent.adk_agent, currently gemini-3.5-flash on the global endpoint)
        # — Phase 14.5 moved auth to Vertex AI ADC so quota is
        # per-project/region, not per-API-key.
        raise HTTPException(
            status_code=503,
            detail="ADK not enabled (set USE_ADK=true to enable /chat)",
        )
    # Pause gate (kill switch): checked right after the use_adk 503 (a
    # misconfigured deploy keeps its existing error) and BEFORE workload
    # resolution / any ADK boot — no LLM call may happen while paused, because
    # an LLM turn IS agent activity. Deliberate exception to the 423 refusal:
    # /chat returns 200 with a calm reply (+ paused=true) on BOTH the JSON and
    # SSE paths so the operator gets a readable answer, not an error toast.
    pause = _pause_state_fail_closed()
    if pause.paused:
        wants_sse = "text/event-stream" in request.headers.get("accept", "")
        # Crew-lock invariant still holds while paused: a supplied conversation
        # id must exist and match the workload (404/409). No conversation is
        # created and no turn persists on the paused path.
        if req.conversation_id is not None:
            _resolve_chat_conversation(
                get_state(), req.conversation_id, req.workload
            )
        return _paused_chat_response(
            pause, wants_sse=wants_sse, session_id=req.session_id,
            conversation_id=req.conversation_id,
        )
    # Autonomy dial (ClickOps item 11): read AFTER the pause gate (pause
    # outranks the dial). Chat is NEVER refused by the dial — tools are
    # filtered at Layer 0 instead — so we read the mode once here and thread
    # it through every streaming/JSON path below.
    autonomy = _autonomy_state_fail_closed()
    # Mark anonymous public-demo callers (the CF Worker injects the operator
    # token but tags the request X-DriftScribe-Demo-Anonymous). Threaded into
    # every streaming/JSON path below. After the 2026-07-09 operator-seat
    # reversal this no longer withholds the approval link (a visitor holds the
    # operator seat) — it selects the per-crew demo-environment note in
    # build_chat_agent and still drops the free-form provision authoring tool
    # (apply-tier stays denied EXCEPT the upgrade_merge_pr carve-out). Operators
    # (no marker) are unaffected.
    demo_anon = _is_demo_anonymous(request)
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

    # Multi-turn (P1): resolve the conversation ONCE here — before the SSE/JSON
    # branch — so the crew-lock 404/409 fires uniformly for both transports
    # before any streaming starts. Absent id → a new conversation (created
    # lazily at persist time). Capture the trace_id here too so both transports
    # link the persisted crew turn to the same /trace/{id}.
    state = get_state()
    conv = _resolve_chat_conversation(
        state, req.conversation_id, req.workload, ephemeral=req.ephemeral
    )
    # Claim the conversation before any LLM work starts. The crew-lock check
    # above is point-in-time — the agent does not run until the streaming
    # generator is iterated, and persistence then uses the CAPTURED workload —
    # so without this a redemption could flip the crew mid-run and the durable
    # transcript would attribute the turn to a crew that was not driving.
    if not _acquire_chat_run(state, conv):
        raise HTTPException(
            status_code=409,
            detail="this conversation already has a turn in flight",
            headers={"Cache-Control": "no-store"},
        )
    trace_id = current_trace_id_or_new()

    # Phase 22: SSE streaming path. Content-negotiated on Accept — the
    # operator UI sends ``text/event-stream``; tests, /recheck, and API
    # callers that don't get the unchanged JSON dict below. The trace_id is
    # captured above and re-bound inside the generator — see :func:`_chat_sse`
    # for why. Streaming is ADDITIVE: ``run_chat_stream`` still logs every
    # event to Cloud Logging exactly as the JSON path does.
    wants_sse = "text/event-stream" in request.headers.get("accept", "")
    if wants_sse:
        return StreamingResponse(
            _chat_sse(
                req.prompt, req.session_id, conv, req.workload, trace_id,
                autonomy_mode=autonomy.mode, demo_anon=demo_anon,
            ),
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
            if req.workload == "provision":
                # Phase D5-7: the JSON provision path drains the SAME fan-out
                # orchestrator the SSE path streams (via :func:`_chat_stream`),
                # so a 1-slice change (internal fallback to ``run_chat_stream``)
                # and an N-slice fan-out both project into the identical
                # ``{reply, tool_calls, session_id}`` dict ``run_chat`` returns.
                # ``run_chat`` is kept for every other workload — an existing
                # test patches ``agent.adk_agent.run_chat`` and would break if
                # drift were routed away from it. The orchestrator raises the
                # same ``WorkerClientError``/``RuntimeError`` types as the chat
                # path, so the outer ``except`` + ``_chat_error_payload``
                # mapping below covers it unchanged.
                out = await _drain_chat_stream_result(
                    _persisting_chat_stream(
                        "provision", req.prompt, conv, trace_id, req.session_id,
                        autonomy_mode=autonomy.mode, demo_anon=demo_anon,
                    )
                )
                # Operator decision 2026-07-09: anon receives the live approval
                # link in the /chat JSON reply, same as the operator (audit C1
                # reversed).
                return out
            # JSON-others STILL goes through run_chat (pinned by
            # test_provision_fanout_route / test_chat_endpoint) with the
            # caller's session_id; multi-turn seeding rides the prior_turns
            # kwarg and persistence wraps around it, attaching conversation_id
            # only when the write succeeded.
            result = await run_chat(
                req.prompt, session_id=req.session_id, workload=req.workload,
                autonomy_mode=autonomy.mode, prior_turns=conv["prior_turns"],
                demo_anon=demo_anon,
            )
            persisted = await asyncio.to_thread(
                _persist_chat_turn, state, conv=conv, prompt=req.prompt,
                trace_id=trace_id, result=result,
            )
            # Drop the RAW proposal unconditionally: it carries the brief (for
            # the joining crew, not the client) and no nonce. Only the
            # persisted projection is a usable contract, and only a committed
            # proposal has one — an ephemeral turn or a failed write must
            # advertise no handoff at all.
            result.pop("handoff", None)
            if persisted:
                result.update(persisted)
            # Operator decision 2026-07-09: anon receives the live approval link
            # in the /chat JSON reply, same as the operator (audit C1 reversed).
            return result
        finally:
            # The SSE + provision paths release inside
            # ``_persisting_chat_stream``; this branch owns its own release.
            if req.workload != "provision":
                _release_chat_run(state, conv)
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


class HandoffRedeemRequest(BaseModel):
    """Confirm (or decline) a crew handoff a crew proposed on a previous turn.

    Note what is NOT here: the target crew. ``from`` and ``to`` are read from
    the persisted proposal, so the caller confirms a specific route the server
    already recorded rather than naming one. Combined with the nonce, that is
    what makes this an intent-BOUND confirmation instead of a second way to ask
    for an arbitrary workload.
    """

    conversation_id: str
    nonce: str
    accept: bool


# Refusal token -> HTTP status. Every one of these leaves the conversation
# exactly as it was; none of them burn the proposal (except the ones where it
# is already gone), so a refused confirmation costs the operator one click.
_HANDOFF_REFUSAL_STATUS: dict[str, int] = {
    "not_found": 404,
    "no_pending": 409,   # nothing to confirm — already used, declined, or never made
    # Deliberately NOT 403. The nonce is a single-use resource credential, not
    # an authentication factor: the caller is already authenticated (or is an
    # allowlisted anonymous visitor during the demo window) and is simply
    # presenting a credential that does not match this conversation's proposal.
    # 403 said "your operator token was rejected", which the SPA's ``apiFetch``
    # believes literally — it clears the stored token and raises the token
    # modal (see the demo-allowlist gap, PR #208, for the same failure shape).
    # An anonymous judge clicking a superseded chip would be thrown out of the
    # demo. 409 puts it with its siblings, and X-Handoff-Refusal keeps the
    # distinction the status no longer carries.
    "invalid_nonce": 409,
    "expired": 410,      # it existed and the window closed
    "stale": 409,        # the thread moved on; this route no longer starts here
    "busy": 409,         # a turn is in flight
}

_HANDOFF_REFUSAL_DETAIL: dict[str, str] = {
    "not_found": "conversation not found",
    "no_pending": "no crew handoff is awaiting confirmation on this conversation",
    "invalid_nonce": "this confirmation is not valid for this conversation",
    "expired": "this handoff has expired; ask the crew to suggest it again",
    "stale": "this conversation has already moved to another crew",
    "busy": "this conversation already has a turn in flight",
}


@app.post("/chat/handoff", response_model=None)
async def chat_handoff(
    req: HandoffRedeemRequest,
    request: Request,
    _: None = Depends(verify_token),
) -> dict | StreamingResponse:
    """Redeem a single-use crew-handoff proposal.

    This endpoint exists so ``POST /chat`` never has to become a way to change
    a conversation's crew. ``ChatRequest.workload`` stays a closed ``Literal``
    and ``_resolve_chat_conversation``'s 409 stays exactly as written — that
    lock is scar tissue (a workload-less POST once defaulted to the
    mutation-capable drift workload and an out-of-domain probe prompt became
    fabricated docs PR #109), so the design works around it rather than
    loosening it.

    On ``accept`` the confirmation IS the turn: the joining crew runs
    immediately against the handing crew's brief, because a join that then
    waits for the operator to retype what they already said is precisely the
    friction this replaces. That turn runs with
    ``HANDOFF_FIRST_TURN_DENIED_TOOLS`` subtracted in code.

    What the nonce buys, stated honestly: intent binding, expiry, and replay
    prevention. NOT "a human clicked" — during the public demo window anonymous
    visitors deliberately hold the operator seat, so the real guarantee is a
    separate, authenticated confirmation request bound to a specific proposal.
    """
    from agent.handoff import (
        HANDOFF_FIRST_TURN_DENIED_TOOLS,
        crew_display_name,
        handoff_prompt,
    )

    s = get_settings()
    if not s.use_adk:
        raise HTTPException(
            status_code=503,
            detail="ADK not enabled (set USE_ADK=true to enable /chat)",
        )
    wants_sse = "text/event-stream" in request.headers.get("accept", "")
    # Path-safe id guard, same as GET /conversations/{id}: a malformed id is
    # not-found, never a value handed to ``.document()``.
    if not _CONVERSATION_ID_RE.fullmatch(req.conversation_id):
        raise HTTPException(
            status_code=404,
            detail="conversation not found",
            headers={"Cache-Control": "no-store"},
        )
    # Pause outranks everything, and is checked BEFORE redemption on purpose:
    # confirming would start an LLM turn, which IS agent activity. Refusing
    # without burning the proposal means the operator can confirm the same chip
    # once they resume, instead of having to coax the crew into suggesting it
    # again.
    pause = _pause_state_fail_closed()
    if pause.paused:
        return _paused_chat_response(
            pause, wants_sse=wants_sse, session_id=None,
            conversation_id=req.conversation_id,
        )

    state = get_state()
    # The joining run's lease is claimed in the SAME transaction that burns the
    # nonce. Claiming it afterwards left a window in which an ordinary turn
    # could take the conversation first — and the confirmation would then have
    # to refuse having ALREADY spent its credential and moved the crew.
    joining_run_id = uuid.uuid4().hex
    outcome = await asyncio.to_thread(
        state.redeem_handoff,
        req.conversation_id, nonce=req.nonce, accept=req.accept,
        now=dt.datetime.now(dt.timezone.utc),
        trace_id=current_trace_id_or_new(),
        run_id=joining_run_id,
    )
    if not outcome.get("ok"):
        reason = outcome.get("error", "no_pending")
        raise HTTPException(
            status_code=_HANDOFF_REFUSAL_STATUS.get(reason, 409),
            detail=_HANDOFF_REFUSAL_DETAIL.get(reason, "handoff refused"),
            # The reason as a machine-readable token, because the status alone
            # is ambiguous where it matters: 409 covers both "already used /
            # superseded" (the proposal is dead, ask again) and "a turn is
            # already running" (retry in a moment). Those need different copy,
            # and the SPA must not have to pattern-match an English sentence to
            # tell them apart. A header rather than a body field so the error
            # shape stays exactly what every other endpoint returns.
            headers={"Cache-Control": "no-store", "X-Handoff-Refusal": reason},
        )
    pending = outcome["pending"]

    if not req.accept:
        # Declining is a real POST, not a client-side dismiss: it burns the
        # nonce and records a row the crew reads next turn. Without it the crew
        # re-proposes every turn and no prompt-level restraint can see the
        # refusal to respect it. No LLM runs.
        payload = {
            "reply": (
                f"Staying with {crew_display_name(pending['from'])}. "
                f"{crew_display_name(pending['to'])} was not brought in."
            ),
            "tool_calls": [],
            "session_id": "",
            "conversation_id": req.conversation_id,
            "handoff_declined": {"from": pending["from"], "to": pending["to"]},
        }
        if not wants_sse:
            return payload

        async def _one_done_frame():
            yield _sse_frame(event="done", data=payload)

        return StreamingResponse(
            _one_done_frame(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # --- accepted: the joining crew runs now --------------------------------
    #
    # From here on the redemption HAS COMMITTED: the nonce is spent and the
    # conversation's crew is already rewritten. Every error raised below is
    # therefore a *post-commit* error, which is a different thing from the
    # refusals above even when it shares their status class. The client cannot
    # tell them apart from the response alone, and getting it wrong is not
    # cosmetic — it would keep showing a chip for a spent proposal and keep the
    # composer bound to the crew that already left, so the operator's next
    # typed turn is refused by the crew lock. ``X-Handoff-Redeemed`` says which
    # side of the commit the failure fell on.
    workload = pending["to"]
    # Reserving the joining run inside the burn transaction closed the race
    # where an ordinary turn could take the lease first — but it hands this
    # function a lease it now has to not drop. Nothing below owns the release
    # until a runner does, and everything between here and there can raise:
    # a crew whose worker env is missing, a contract fetch, the autonomy read,
    # a transient store failure. Leak one and the conversation is wedged for
    # the full lease TTL, which the operator cannot type their way out of,
    # because a typed turn takes the same lease. That would make the 503
    # branch's promise below false.
    _joining_lease_owned = True
    try:
        try:
            resolution = load_workload(workload)
        except (
            MissingWorkerEnvError,
            ReservedToolNotImplementedError,
            MissingDeveloperKnowledgeApiKeyError,
        ) as e:
            # The crew flip has already committed. That is deliberate: the
            # transition is what the operator confirmed, and a failed first run
            # is an ordinary error they can retry by typing. Undoing the flip
            # here would silently discard a confirmed decision.
            #
            # "Retry by typing" only works if the client MOVED WITH the flip.
            # The marker added below is what lets it: without it the SPA keeps
            # the departed crew selected and its retry is refused by the very
            # lock this transition just moved.
            raise HTTPException(
                status_code=503,
                detail=f"workload {workload!r} is not deployed: {e}.",
            ) from e
        _eager_resolve_upgrade_contract(resolution)

        autonomy = _autonomy_state_fail_closed()
        # Re-evaluated at redemption, NOT inherited from the proposing turn: the
        # dial may have moved, and an anonymous visitor's restrictions must apply
        # to the crew that is actually about to run.
        demo_anon = _is_demo_anonymous(request)

        stored = state.get_conversation(req.conversation_id) or {}
        conv = {
            "conversation_id": req.conversation_id,
            "workload": workload,
            "is_new": False,
            "prior_turns": stored.get("turns", []),
            "ephemeral": False,
            # The operator confirmed a suggestion; they did not type this
            # prompt. Recording a user turn here would put words in their mouth
            # in a transcript whose whole value is being trustworthy.
            "omit_user_turn": True,
            "crew_change": {"from": pending["from"], "to": pending["to"]},
            # Redemption deleted the proposal it burned, so nothing is open for
            # this run to answer. If the joining crew proposes something of its
            # own, that write compares against this same "none was open".
            "pending_digest": None,
            # Already held: ``redeem_handoff`` reserved it transactionally.
            # Carried here so the normal release path in
            # ``_persisting_chat_stream`` (and the JSON branch's ``finally``)
            # frees it exactly as for a typed turn.
            "run_id": outcome.get("run_id") or joining_run_id,
        }
        prompt = handoff_prompt(pending)
        trace_id = current_trace_id_or_new()

        if wants_sse:
            response = StreamingResponse(
                _chat_sse(
                    prompt, None, conv, workload, trace_id,
                    autonomy_mode=autonomy.mode, demo_anon=demo_anon,
                    denied_tools=HANDOFF_FIRST_TURN_DENIED_TOOLS,
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "X-Trace-Id": trace_id,
                },
            )
            # Handover: the stream's own ``finally`` owns the release from here,
            # and ``_drain_chat_stream_result``'s ``aclose`` guarantees it runs
            # even if the generator is left suspended. Releasing here as well
            # would free a lease the run still holds.
            _joining_lease_owned = False
            return response

        from agent.adk_agent import run_chat

        _workload_token = set_workload(workload)
        try:
            try:
                # Same handover, one frame earlier: both branches below release
                # in the ``finally`` directly beneath them.
                _joining_lease_owned = False
                if workload == "provision":
                    return await _drain_chat_stream_result(
                        _persisting_chat_stream(
                            "provision", prompt, conv, trace_id, None,
                            autonomy_mode=autonomy.mode, demo_anon=demo_anon,
                            denied_tools=HANDOFF_FIRST_TURN_DENIED_TOOLS,
                        )
                    )
                result = await run_chat(
                    prompt, session_id=None, workload=workload,
                    autonomy_mode=autonomy.mode, prior_turns=conv["prior_turns"],
                    demo_anon=demo_anon,
                    denied_tools=HANDOFF_FIRST_TURN_DENIED_TOOLS,
                )
                persisted = await asyncio.to_thread(
                    _persist_chat_turn, state, conv=conv, prompt=prompt,
                    trace_id=trace_id, result=result,
                )
                result.pop("handoff", None)
                if persisted:
                    result.update(persisted)
                return result
            finally:
                if workload != "provision":
                    _release_chat_run(state, conv)
                reset_workload(_workload_token)
        except (
            worker_client.WorkerClientError,
            MissingDeveloperKnowledgeApiKeyError,
            RuntimeError,
        ) as e:
            status, detail = _chat_error_payload(e, workload=workload)
            raise HTTPException(status_code=status, detail=detail) from e
    except HTTPException as e:
        # Stamp every post-commit failure, whatever raised it. Done here rather
        # than at each raise site so an error path added later cannot forget
        # it — past the burn, "the transition happened" is the default and has
        # to be opt-out, not opt-in.
        e.headers = {**(e.headers or {}), "X-Handoff-Redeemed": "1"}
        raise
    except Exception as e:  # noqa: BLE001
        # "Whatever raised it" has to mean that literally. An exception that is
        # not already an HTTPException — a store read that fails after the
        # burn, a contract fetch, anything added below later — would otherwise
        # reach the client as a bare 500 carrying no marker, and an unmarked
        # response is read as a PRE-commit refusal. That is the precise state
        # this header exists to prevent: chip still live, composer still bound
        # to the crew that already left, and the operator's next typed turn
        # refused by the crew lock they cannot see.
        #
        # Converting here rather than letting it propagate is the whole point:
        # the marker cannot ride an exception FastAPI turns into a 500 for us.
        raise HTTPException(
            status_code=500,
            detail="the crew changed, but its first reply could not be started",
            headers={"X-Handoff-Redeemed": "1"},
        ) from e
    finally:
        if _joining_lease_owned:
            _release_chat_run(
                state,
                {"conversation_id": req.conversation_id,
                 "run_id": outcome.get("run_id") or joining_run_id},
            )


@app.get("/conversations")
def list_conversations_endpoint(
    request: Request,
    response: Response,
    limit: int = 50,
    workload: str | None = None,
    _: None = Depends(verify_token),
    state: StateStore = Depends(get_state),
) -> dict:
    """List recent conversations (metadata only), newest-updated first.

    Backs the operator's conversation history rail (P2). Token-guarded like
    /decisions; bounded by ``limit`` (1..200) so a misconfigured caller can't
    pull the whole collection. Turns are NOT embedded — the rail only needs
    title/crew/timestamps; fetch a single conversation's turns via
    ``GET /conversations/{id}``.
    """
    if limit < 1 or limit > 200:
        raise HTTPException(
            status_code=400,
            detail="limit must be 1..200",
            headers={"Cache-Control": "no-store"},
        )
    response.headers["Cache-Control"] = "no-store"
    rows = [
        _project_conversation(r) for r in
        state.list_conversations(limit=limit, workload=workload)
    ]
    # Operator decision 2026-07-09 (audit M1 reversed): conversations are shared
    # team memory by design in the public window. The visitor holds the operator
    # seat, so a persisted turn's live ?t= approval link is served to anonymous
    # readers too, same as the operator sees.
    return {"conversations": rows}


@app.get("/conversations/{conversation_id}")
def get_conversation_endpoint(
    conversation_id: str,
    request: Request,
    response: Response,
    _: None = Depends(verify_token),
    state: StateStore = Depends(get_state),
) -> dict:
    """Full ordered turns for rehydrating a conversation on reload (P2)."""
    response.headers["Cache-Control"] = "no-store"
    # Path-safe id guard (Firestore doc id; reject path escapes). A malformed
    # id is treated as not-found rather than reaching ``.document()``.
    if not _CONVERSATION_ID_RE.fullmatch(conversation_id):
        raise HTTPException(
            status_code=404,
            detail="conversation not found",
            headers={"Cache-Control": "no-store"},
        )
    conv = state.get_conversation(conversation_id)
    if conv is None:
        raise HTTPException(
            status_code=404,
            detail="conversation not found",
            headers={"Cache-Control": "no-store"},
        )
    # Operator decision 2026-07-09 (audit M1 reversed): shared team memory is
    # shared-seat by design in the public window. Anonymous readers get the full
    # turns with any live ?t= approval link intact, same as the operator.
    return _project_conversation(conv)
