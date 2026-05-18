import re
from typing import Any
from pydantic import BaseModel
from agent.contract import OpsContract
from agent.models import DecisionProposal, DecisionAction, EnvDiff, ContractStatus


class ClassificationInput(BaseModel):
    contract: OpsContract
    live_env: dict[str, str]
    recent_prs: list[dict[str, Any]] = []


def _strict_pr_match(prs: list[dict], var_name: str) -> str | None:
    """Find a merged PR that mentions the EXACT var name as a token (not substring)."""
    token = re.compile(rf"\b{re.escape(var_name)}\b")
    for pr in prs:
        if not pr.get("merged"):
            continue
        haystack = f"{pr.get('title','')} {pr.get('body','')}"
        if token.search(haystack):
            return pr.get("url")
    return None


_ACTION_PRIORITY = [
    DecisionAction.DRIFT_ISSUE,
    DecisionAction.ESCALATION,
    DecisionAction.DOCS_PR,
]

_RATIONALE = {
    DecisionAction.DOCS_PR: "Change is sanctioned (contract allows manual or a recent merged PR mentions the var); updating docs.",
    DecisionAction.DRIFT_ISSUE: "Change violates the contract (allow_manual_change=false). Refusing to document.",
    DecisionAction.ESCALATION: "Variable observed in production has no contract entry and no recent merged PR mentions it. Reviewer needed.",
}


def classify(inp: ClassificationInput) -> DecisionProposal:
    diffs: list[EnvDiff] = []
    actions: list[DecisionAction] = []

    contract_vars = set(inp.contract.expected_env.keys())
    live_vars = set(inp.live_env.keys())

    for name in sorted(contract_vars | live_vars):
        expected = inp.contract.expected_env.get(name)
        live_val = inp.live_env.get(name)
        expected_val = expected.value if expected else None

        if expected and live_val == expected_val:
            continue  # no drift for this var

        if expected is None:
            # Live has a var not in contract → uncertain unless strict PR match
            pr_url = _strict_pr_match(inp.recent_prs, name)
            diffs.append(EnvDiff(
                name=name,
                expected=None,
                live=live_val,
                contract_status=ContractStatus.ABSENT,
                recent_pr_match=pr_url,
            ))
            actions.append(DecisionAction.DOCS_PR if pr_url else DecisionAction.ESCALATION)
        else:
            status = (
                ContractStatus.PRESENT_ALLOW_MANUAL if expected.allow_manual_change
                else ContractStatus.PRESENT_DISALLOW_MANUAL
            )
            diffs.append(EnvDiff(
                name=name,
                expected=expected_val,
                live=live_val,
                contract_status=status,
            ))
            actions.append(
                DecisionAction.DOCS_PR if expected.allow_manual_change else DecisionAction.DRIFT_ISSUE
            )

    if not diffs:
        return DecisionProposal(
            action=DecisionAction.NO_OP,
            env_diffs=[],
            rationale="Live state matches contract.",
            confidence=1.0,
        )

    chosen = next(p for p in _ACTION_PRIORITY if p in actions)

    primary = diffs[0]
    primary_rule = inp.contract.expected_env.get(primary.name)
    if primary_rule:
        target_file = primary_rule.docs.file
        target_section = primary_rule.docs.section
    else:
        target_file = "demo/docs/runbook.md"
        target_section = "Runtime Configuration"

    return DecisionProposal(
        action=chosen,
        env_diffs=diffs,
        target_docs_file=target_file,
        target_docs_section=target_section,
        rationale=_RATIONALE[chosen],
        confidence=0.9,
        requires_human_review=(chosen == DecisionAction.ESCALATION),
    )
