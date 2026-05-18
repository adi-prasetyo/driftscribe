# agent/main.py
import re
import secrets
import time
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


@app.post("/recheck")
def recheck():
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

    proposal = classify(ClassificationInput(
        contract=contract, live_env=live_env, recent_prs=[],
    ))
    validate(proposal, contract)

    rendered = _render_for(proposal.action, proposal)
    github_result = _perform_action(s, contract, proposal, rendered)

    return {
        "action": proposal.action.value,
        "rendered_body": rendered,
        "rationale": proposal.rationale,
        "diffs": [d.model_dump(mode="json") for d in proposal.env_diffs],
        "target_docs_file": proposal.target_docs_file,
        "target_docs_section": proposal.target_docs_section,
        "requires_human_review": proposal.requires_human_review,
        "dry_run": s.dry_run,
        "github": github_result,
    }
