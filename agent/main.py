# agent/main.py
import hashlib
import json
import re
import secrets
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException

from agent.classifier import ClassificationInput, classify
from agent.cloud_run_client import read_live_env
from agent.config import Settings, get_settings
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
)
from agent.runbook_patcher import patch_runbook
from agent.state_store import FirestoreStateStore, InMemoryStateStore, StateStore
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


def _do_recheck(trigger: str, force: bool = False) -> dict:
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
    """
    s = get_settings()
    try:
        contract = load_contract(Path(s.contract_path))
    except Exception as e:
        # Bad contract = our deploy is broken, not GCP. 500, not 502.
        raise HTTPException(status_code=500, detail=f"contract load failed: {e}")
    try:
        live_env = read_live_env(s.target_service, s.target_region, s.gcp_project)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"cloud run read failed: {e}")

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

    proposal = classify(
        ClassificationInput(contract=contract, live_env=live_env, recent_prs=[])
    )
    validate(proposal, contract)
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
def recheck(force: bool = False):
    return _do_recheck("manual_recheck", force=force)


@app.get("/runs/{decision_id}")
def get_run(decision_id: str):
    d = get_state().get_decision(decision_id)
    if not d:
        raise HTTPException(status_code=404, detail="decision not found")
    return d
