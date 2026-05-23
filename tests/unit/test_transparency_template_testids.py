"""Phase 20.6.0: stable data-testid selectors required for Playwright UI E2E.

These meta-tests pin the contract between the transparency / approval
templates and the Playwright spec landing in Task 20.6. The spec keys off
``data-testid=...`` attributes (rather than CSS classes or DOM IDs) so the
operator-facing visual styling can evolve without breaking the E2E suite.

Adding or removing a testid here must be done in lockstep with the
Playwright selector list — otherwise a UI rename quietly orphans the test.
"""
from pathlib import Path

REQUIRED_TESTIDS_TRANSPARENCY = {
    "chat-prompt",
    "chat-submit",
    "final-response",
    "past-decisions-pane",
    "past-decision-item",
    "open-trace-button",
    "historical-banner",
}


def test_transparency_template_has_required_testids():
    body = Path("agent/templates/transparency.html").read_text()
    missing = [
        tid
        for tid in REQUIRED_TESTIDS_TRANSPARENCY
        if f'data-testid="{tid}"' not in body
    ]
    assert not missing, f"missing data-testids: {missing}"


def test_approval_template_has_testids():
    body = Path("agent/templates/approval.html").read_text()
    for tid in ("approve-button", "reject-button", "token-field"):
        assert (
            f'data-testid="{tid}"' in body
        ), f"approval.html missing data-testid={tid!r}"


def test_data_group_unchanged():
    body = Path("agent/templates/transparency.html").read_text()
    for group in ("coordinator", "tools", "mcp"):
        assert f'data-group="{group}"' in body


def test_sessionstorage_key_documented():
    """Phase 20 reminder: Playwright will set sessionStorage['driftscribe_token']."""
    body = Path("agent/templates/transparency.html").read_text()
    assert "driftscribe_token" in body  # underscore, NOT dot
