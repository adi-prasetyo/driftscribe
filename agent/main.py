# agent/main.py
import hashlib
import json
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict

from agent import approvals as approval_helpers
from agent import worker_client
from agent.auth import verify_token
from agent.classifier import ClassificationInput, classify
from agent.config import Settings, get_settings
from agent.worker_client import WorkerClientError
from agent.contract import OpsContract, load_contract
from agent.github_actions import (
    get_repo,
    open_docs_pr,
    open_drift_issue,
    open_escalation_issue,
)
from agent.models import DecisionAction, DecisionProposal
from agent.renderer import (
    render_docs_pr_body,
    render_drift_issue_body,
    render_escalation_issue_body,
    render_rollback_body,
)
from agent.runbook_patcher import patch_runbook
from agent.state_store import FirestoreStateStore, InMemoryStateStore, StateStore
from agent.validator import ValidationError as ProposalValidationError
from agent.validator import validate

# Match git refspec rules (https://git-scm.com/docs/git-check-ref-format):
# allow ASCII letters/digits/`_`/`-`; collapse runs of disallowed chars to `-`.
_BRANCH_SLUG = re.compile(r"[^a-z0-9_-]+")


def _branch_slug(name: str) -> str:
    """Sanitize an env-var name for use inside a git branch name."""
    slug = _BRANCH_SLUG.sub("-", name.lower()).strip("-")
    return slug or "var"


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


@app.get("/healthz")
def healthz():
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


async def _run_adk_agent(user_msg: str) -> DecisionProposal:
    """Thin wrapper so integration tests have a stable patch target.

    Lazy-imports `agent.adk_agent` so the Google ADK SDK doesn't load on the
    non-ADK code path. Patching `agent.main._run_adk_agent` (rather than
    `agent.adk_agent.run_agent`) preserves the lazy-load benefit AND keeps
    the test patch site stable across spec evolution.
    """
    from agent.adk_agent import run_agent

    return await run_agent(user_msg)


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


async def _do_recheck(trigger: str, force: bool = False) -> dict:
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
    s = get_settings()
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
        try:
            proposal = await _run_adk_agent(user_msg)
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


@app.post("/recheck")
async def recheck(force: bool = False, _: None = Depends(verify_token)):
    # ``verify_token`` runs first and raises 401/403/503 before _do_recheck.
    # The unused-parameter underscore is the standard FastAPI convention for
    # auth deps that only matter for their side effect (raising on failure).
    return await _do_recheck("manual_recheck", force=force)


@app.post("/eventarc")
async def eventarc():
    """Stub for Phase 9 (Cloud Run Audit Logs → Eventarc → /eventarc).

    Wired now so deployment manifests can target the route; the handler
    itself ships in the Eventarc phase.
    """
    raise HTTPException(status_code=501, detail="Phase 9")


@app.get("/runs/{decision_id}")
def get_run(decision_id: str):
    # Sync on purpose — this only reads from the StateStore singleton, no
    # I/O that benefits from async.
    d = get_state().get_decision(decision_id)
    if not d:
        raise HTTPException(status_code=404, detail="decision not found")
    return d


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
    """

    prompt: str
    session_id: str | None = None

    model_config = ConfigDict(extra="forbid")


@app.post("/chat")
async def chat(req: ChatRequest, _: None = Depends(verify_token)) -> dict:
    """Free-form operator interface to the coordinator.

    Routes through the SAME X-DriftScribe-Token guard as /recheck
    (Phase 11.1). Distinct from /recheck:

    - /recheck returns a structured DecisionProposal — the LLM is
      constrained to produce JSON of a fixed schema.
    - /chat returns free-form text — the LLM picks tools, may call
      multiple workers, and produces a natural-language response.

    The ADK runner picks tools from ``COORDINATOR_TOOLS`` in
    :mod:`agent.adk_agent`; the LLM CANNOT call anything outside that
    set (Layer 0 capability-bounded tool registry — enforced by the
    inventory test in Phase 11.4b).
    """
    s = get_settings()
    if not s.use_adk:
        # /chat without ADK enabled has no engine to invoke. 503 (not
        # 501) because the feature exists at this revision; it's just
        # disabled. Operator flips USE_ADK=true after verifying Gemini
        # quota.
        raise HTTPException(
            status_code=503,
            detail="ADK not enabled (set USE_ADK=true to enable /chat)",
        )
    from agent.adk_agent import run_chat

    try:
        return await run_chat(req.prompt, session_id=req.session_id)
    except worker_client.WorkerClientError as e:
        # Worker upstream failed (could be transport, schema, or worker
        # policy). 502 — the coordinator itself is healthy; the
        # downstream isn't. Status code from the worker is NOT echoed —
        # a worker's 422 (schema rejection from the LLM's tool call)
        # shouldn't surface as 422 here (that would tell the caller
        # "your /chat request was malformed" which is wrong).
        raise HTTPException(
            status_code=502,
            detail=f"chat worker call failed: {e}",
        ) from e
    except RuntimeError as e:
        # ADK parse / response failures live here. 502 (model
        # misbehaved), not 500 (coordinator deploy broken).
        raise HTTPException(
            status_code=502,
            detail=f"chat agent failed: {e}",
        ) from e
