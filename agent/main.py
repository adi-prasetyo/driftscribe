# agent/main.py
import time
from pathlib import Path
from fastapi import FastAPI, HTTPException

from agent.classifier import classify, ClassificationInput
from agent.cloud_run_client import read_live_env
from agent.config import get_settings
from agent.contract import load_contract
from agent.github_actions import (
    get_repo, open_docs_pr, open_drift_issue, open_escalation_issue,
)
from agent.models import DecisionAction, DecisionProposal
from agent.renderer import (
    render_docs_pr_body, render_drift_issue_body, render_escalation_issue_body,
)
from agent.runbook_patcher import patch_runbook
from agent.validator import validate

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


def _perform_action(s, contract, proposal, rendered: str) -> dict:
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

    # DOCS_PR: read the current runbook from the local working copy, patch it,
    # and open a PR. The local file is the dev-side source of truth before
    # deploy; once Phase 9 wires Eventarc the agent will fetch the file from
    # the base branch via the GitHub API instead.
    target = proposal.target_docs_file or "demo/docs/runbook.md"
    target_path = Path(target)
    current = (
        target_path.read_text()
        if target_path.exists()
        else f"# Runbook\n\n## {proposal.target_docs_section}\n\n"
    )
    new_content = patch_runbook(current, proposal.env_diffs, contract)

    branch = f"driftscribe/{proposal.env_diffs[0].name.lower()}-{int(time.time())}"
    return open_docs_pr(
        repo=repo,  # type: ignore[arg-type]
        branch=branch,
        base="main",
        title=f"docs(driftscribe): update {proposal.env_diffs[0].name}",
        body=rendered,
        file_path=target,
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
