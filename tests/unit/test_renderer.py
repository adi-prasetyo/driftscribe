from agent.renderer import (
    render_docs_pr_body, render_drift_issue_body, render_escalation_issue_body,
)
from agent.models import DecisionProposal, DecisionAction, EnvDiff, ContractStatus

def _proposal(action, diffs, **overrides):
    return DecisionProposal(
        action=action, env_diffs=diffs,
        target_docs_file="demo/docs/runbook.md",
        target_docs_section="Runtime Configuration",
        rationale="r", confidence=0.9, **overrides,
    )

def test_drift_issue_body_has_evidence_table_per_diff():
    p = _proposal(DecisionAction.DRIFT_ISSUE, [
        EnvDiff(name="PAYMENT_MODE", expected="mock", live="live",
                contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL),
    ])
    body = render_drift_issue_body(p)
    assert "PAYMENT_MODE" in body
    assert "mock" in body and "live" in body
    assert "present_disallow_manual" in body or "disallow" in body.lower()

def test_escalation_body_calls_out_missing_evidence():
    p = _proposal(DecisionAction.ESCALATION, [
        EnvDiff(name="NEW_THING", expected=None, live="x",
                contract_status=ContractStatus.ABSENT, recent_pr_match=None),
    ], requires_human_review=True)
    body = render_escalation_issue_body(p)
    assert "NEW_THING" in body
    assert "absent" in body.lower() or "no contract entry" in body.lower()
    assert "reviewer" in body.lower() or "intentional" in body.lower()

def test_docs_pr_body_describes_change():
    p = _proposal(DecisionAction.DOCS_PR, [
        EnvDiff(name="FEATURE_X", expected="false", live="true",
                contract_status=ContractStatus.PRESENT_ALLOW_MANUAL),
    ])
    body = render_docs_pr_body(p)
    assert "FEATURE_X" in body and "true" in body

# ---- Secret redaction (Codex finding) ----

def test_drift_issue_redacts_secret_value_in_evidence_table():
    p = _proposal(DecisionAction.DRIFT_ISSUE, [
        EnvDiff(name="API_TOKEN", expected="old-secret", live="new-secret",
                contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL),
    ])
    body = render_drift_issue_body(p)
    assert "API_TOKEN" in body  # var name visible — operators must know what drifted
    assert "old-secret" not in body
    assert "new-secret" not in body
    assert "redacted" in body.lower()

def test_escalation_body_redacts_unknown_secret_value():
    p = _proposal(DecisionAction.ESCALATION, [
        EnvDiff(name="DB_PASSWORD", expected=None, live="supersecret",
                contract_status=ContractStatus.ABSENT),
    ], requires_human_review=True)
    body = render_escalation_issue_body(p)
    assert "DB_PASSWORD" in body
    assert "supersecret" not in body
    assert "redacted" in body.lower()

def test_non_secret_values_not_redacted():
    p = _proposal(DecisionAction.DRIFT_ISSUE, [
        EnvDiff(name="PAYMENT_MODE", expected="mock", live="live",
                contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL),
    ])
    body = render_drift_issue_body(p)
    assert "mock" in body
    assert "live" in body
    assert "redacted" not in body.lower()
