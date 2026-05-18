import pytest
from agent.validator import validate, ValidationError
from agent.models import DecisionProposal, DecisionAction, EnvDiff, ContractStatus
from agent.contract import OpsContract, EnvVarRule, DocsRef

def _contract():
    return OpsContract(
        service="payment-demo",
        environment="production",
        cloud_run_service="payment-demo",
        region="asia-northeast1",
        github_repo="theghostsquad00/driftscribe",
        expected_env={
            "PAYMENT_MODE": EnvVarRule(
                value="mock",
                docs=DocsRef(file="demo/docs/runbook.md", section="Runtime Configuration"),
                allow_manual_change=False,
            ),
            "FEATURE_X": EnvVarRule(
                value="false",
                docs=DocsRef(file="demo/docs/runbook.md", section="Feature Flags"),
                allow_manual_change=True,
                operator_note="Operator-safe",
            ),
        },
    )

def _proposal(action, name, expected_val, live, status):
    return DecisionProposal(
        action=action,
        env_diffs=[EnvDiff(
            name=name, expected=expected_val, live=live, contract_status=status,
        )],
        target_docs_file="demo/docs/runbook.md",
        target_docs_section="Runtime Configuration",
        rationale="t", confidence=0.9,
    )

def test_validator_passes_correct_drift_issue():
    p = _proposal(DecisionAction.DRIFT_ISSUE, "PAYMENT_MODE", "mock", "live",
                   ContractStatus.PRESENT_DISALLOW_MANUAL)
    validate(p, _contract())

def test_validator_passes_correct_docs_pr():
    p = _proposal(DecisionAction.DOCS_PR, "FEATURE_X", "false", "true",
                   ContractStatus.PRESENT_ALLOW_MANUAL)
    p.target_docs_section = "Feature Flags"
    validate(p, _contract())

def test_validator_rejects_docs_pr_when_contract_disallows_manual():
    p = _proposal(DecisionAction.DOCS_PR, "PAYMENT_MODE", "mock", "live",
                   ContractStatus.PRESENT_DISALLOW_MANUAL)
    with pytest.raises(ValidationError, match="allow_manual_change"):
        validate(p, _contract())

def test_validator_rejects_docs_pr_for_unknown_var_without_pr_match():
    p = DecisionProposal(
        action=DecisionAction.DOCS_PR,
        env_diffs=[EnvDiff(
            name="UNKNOWN", expected=None, live="x",
            contract_status=ContractStatus.ABSENT, recent_pr_match=None,
        )],
        target_docs_file="demo/docs/runbook.md",
        target_docs_section="Runtime Configuration",
        rationale="t", confidence=0.9,
    )
    with pytest.raises(ValidationError, match="recent_pr_match"):
        validate(p, _contract())

def test_validator_rejects_secret_like_var_in_docs_pr():
    p = _proposal(DecisionAction.DOCS_PR, "API_TOKEN", "x", "y",
                   ContractStatus.PRESENT_ALLOW_MANUAL)
    with pytest.raises(ValidationError, match="secret"):
        validate(p, _contract())

def test_validator_rejects_target_docs_file_outside_repo():
    p = _proposal(DecisionAction.DOCS_PR, "FEATURE_X", "false", "true",
                   ContractStatus.PRESENT_ALLOW_MANUAL)
    p.target_docs_file = "../etc/passwd"
    with pytest.raises(ValidationError, match="path"):
        validate(p, _contract())

def test_validator_rejects_target_section_not_in_contract_for_known_var():
    p = _proposal(DecisionAction.DOCS_PR, "FEATURE_X", "false", "true",
                   ContractStatus.PRESENT_ALLOW_MANUAL)
    p.target_docs_section = "Hallucinated Section"
    with pytest.raises(ValidationError, match="section"):
        validate(p, _contract())

def test_validator_rejects_confidence_above_one():
    p = _proposal(DecisionAction.DOCS_PR, "FEATURE_X", "false", "true",
                   ContractStatus.PRESENT_ALLOW_MANUAL)
    p.target_docs_section = "Feature Flags"
    p.confidence = 1.5
    with pytest.raises(ValidationError, match="confidence"):
        validate(p, _contract())

def test_validator_rejects_confidence_below_zero():
    p = _proposal(DecisionAction.DOCS_PR, "FEATURE_X", "false", "true",
                   ContractStatus.PRESENT_ALLOW_MANUAL)
    p.target_docs_section = "Feature Flags"
    p.confidence = -0.1
    with pytest.raises(ValidationError, match="confidence"):
        validate(p, _contract())

def test_validator_rejects_drift_issue_with_empty_env_diffs():
    p = DecisionProposal(
        action=DecisionAction.DRIFT_ISSUE,
        env_diffs=[],
        rationale="bogus", confidence=0.9,
    )
    with pytest.raises(ValidationError, match="env_diff"):
        validate(p, _contract())

def test_validator_allows_no_op_with_empty_env_diffs():
    p = DecisionProposal(
        action=DecisionAction.NO_OP,
        env_diffs=[],
        rationale="all good", confidence=1.0,
    )
    validate(p, _contract())  # must not raise

def test_validator_rejects_bearer_token_var_name():
    # Defense-in-depth: BEARER is a secret-name pattern
    p = _proposal(DecisionAction.DOCS_PR, "API_BEARER", "x", "y",
                   ContractStatus.PRESENT_ALLOW_MANUAL)
    with pytest.raises(ValidationError, match="secret"):
        validate(p, _contract())
