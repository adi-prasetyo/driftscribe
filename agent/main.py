# agent/main.py
from pathlib import Path
from fastapi import FastAPI, HTTPException

from agent.classifier import classify, ClassificationInput
from agent.cloud_run_client import read_live_env
from agent.config import get_settings
from agent.contract import load_contract
from agent.models import DecisionAction, DecisionProposal
from agent.renderer import (
    render_docs_pr_body, render_drift_issue_body, render_escalation_issue_body,
)
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

    return {
        "action": proposal.action.value,
        "rendered_body": rendered,
        "rationale": proposal.rationale,
        "diffs": [d.model_dump(mode="json") for d in proposal.env_diffs],
        "target_docs_file": proposal.target_docs_file,
        "target_docs_section": proposal.target_docs_section,
        "requires_human_review": proposal.requires_human_review,
        "dry_run": s.dry_run,
    }
