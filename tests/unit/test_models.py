from agent.models import (
    DecisionProposal, DecisionAction, EnvDiff, ContractStatus,
)

def test_env_diff_holds_per_var_evidence():
    d = EnvDiff(
        name="PAYMENT_MODE",
        expected="mock",
        live="live",
        contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,
        debug_config_value="live",
        recent_pr_match=None,
    )
    assert d.name == "PAYMENT_MODE"
    assert d.contract_status == ContractStatus.PRESENT_DISALLOW_MANUAL

def test_decision_proposal_serialises():
    p = DecisionProposal(
        action=DecisionAction.DRIFT_ISSUE,
        env_diffs=[EnvDiff(
            name="PAYMENT_MODE", expected="mock", live="live",
            contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,
        )],
        target_docs_file="demo/docs/runbook.md",
        target_docs_section="Runtime Configuration",
        rationale="Contract disallows manual change.",
        confidence=0.9,
        requires_human_review=False,
    )
    d = p.model_dump()
    assert d["action"] == "drift_issue"
    assert d["env_diffs"][0]["contract_status"] == "present_disallow_manual"

def test_action_enum_values():
    assert DecisionAction.DOCS_PR.value == "docs_pr"
    assert DecisionAction.DRIFT_ISSUE.value == "drift_issue"
    assert DecisionAction.ESCALATION.value == "escalation"
    assert DecisionAction.NO_OP.value == "no_op"
