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


def test_rationale_scrubs_secret_value_substrings():
    # LLM rationale might quote the value verbatim — must not leak to GitHub
    p = DecisionProposal(
        action=DecisionAction.DRIFT_ISSUE,
        env_diffs=[EnvDiff(
            name="API_TOKEN", expected="abcdef1234", live="newsecret5678",
            contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,
        )],
        target_docs_file="demo/docs/runbook.md",
        target_docs_section="Runtime Configuration",
        rationale="API_TOKEN changed from abcdef1234 to newsecret5678.",
        confidence=0.9,
    )
    body = render_drift_issue_body(p)
    assert "abcdef1234" not in body
    assert "newsecret5678" not in body
    assert "API_TOKEN" in body  # var name still visible


def test_recent_pr_match_redacted_for_secret_var():
    p = _proposal(DecisionAction.ESCALATION, [
        EnvDiff(name="OAUTH_KEY", expected=None, live="zzzz9999",
                contract_status=ContractStatus.ABSENT,
                recent_pr_match="https://github.com/x/x/pull/42?leaks=zzzz9999"),
    ], requires_human_review=True)
    body = render_escalation_issue_body(p)
    assert "zzzz9999" not in body
    assert "OAUTH_KEY" in body


def test_evidence_table_escapes_pipe_in_var_name():
    # Defensive: an env var name with `|` would break the table column count
    p = _proposal(DecisionAction.DRIFT_ISSUE, [
        EnvDiff(name="WEIRD|NAME", expected="a", live="b",
                contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL),
    ])
    body = render_drift_issue_body(p)
    assert "WEIRD\\|NAME" in body
    # Confirm row still has 6 columns by checking line that includes the name
    row = [ln for ln in body.splitlines() if "WEIRD" in ln][0]
    # 6 cells -> 7 pipes in a markdown row (leading + trailing + 5 separators)
    # We escaped the | inside the name, so it shouldn't count
    cells = row.replace("\\|", "X").split("|")
    assert len(cells) == 8  # leading empty + 6 cells + trailing empty


def test_credentialed_url_value_redacted_even_when_name_is_innocuous():
    # DATABASE_URL doesn't match the name pattern in the old version, but
    # the value embeds credentials. Must redact.
    p = _proposal(DecisionAction.DRIFT_ISSUE, [
        EnvDiff(name="MY_CONNECTION", expected=None,
                live="postgres://admin:s3cr3t@db.example.com:5432/prod",
                contract_status=ContractStatus.ABSENT),
    ])
    body = render_drift_issue_body(p)
    assert "s3cr3t" not in body
    assert "admin" not in body or "admin:s3cr3t" not in body
    assert "redacted" in body.lower()


def test_database_url_var_name_redacted_by_expanded_pattern():
    # DATABASE_URL contains "URL" which is now in SECRET_NAME_PATTERN
    p = _proposal(DecisionAction.DRIFT_ISSUE, [
        EnvDiff(name="DATABASE_URL", expected="old", live="postgres://u:p@h/d",
                contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL),
    ])
    body = render_drift_issue_body(p)
    assert "old" not in body  # short value but still redacted by name
    assert "postgres" not in body
    assert "DATABASE_URL" in body


def test_newlines_in_value_normalized_in_table():
    # A value containing newlines must not break the markdown row layout
    p = _proposal(DecisionAction.DRIFT_ISSUE, [
        EnvDiff(name="PAYMENT_MODE", expected="mock", live="line1\nline2\r\nline3",
                contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL),
    ])
    body = render_drift_issue_body(p)
    # Find the row containing PAYMENT_MODE — must be a single line (no embedded newlines)
    payment_lines = [ln for ln in body.splitlines() if "PAYMENT_MODE" in ln]
    assert len(payment_lines) == 1
    assert "line1" in payment_lines[0] and "line2" in payment_lines[0]


def test_empty_string_live_value_not_collapsed_to_dash():
    # Empty live value is a real drift signal (var was unset)
    p = _proposal(DecisionAction.DRIFT_ISSUE, [
        EnvDiff(name="PAYMENT_MODE", expected="mock", live="",
                contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL),
    ])
    body = render_drift_issue_body(p)
    # The live cell should render as empty backticks ` ` (or similar), not as `—`
    # We check that the "expected=mock" row doesn't conflate live="" with live=None
    # by ensuring the literal "`—`" appears at most once (in debug_config_value cell)
    assert body.count("`—`") <= 1
