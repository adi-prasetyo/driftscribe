"""Renderer tests for ROLLBACK action (Phase 13.2).

The rollback body is the operator-facing markdown surface delivered by the
Notifier worker (severity="approval"). It must:

- Surface the approval URL prominently as a clickable autolink.
- Show the target revision so the operator can verify the rollback target.
- State the 15-minute approval-link expiry explicitly.
- Run the rationale through the secret-scrubber (defense-in-depth against
  an LLM quoting a credential value).
- Not call out the approval token as a separate field — the full URL is the
  only token-carrier (the body is delivered through whatever notifier surface
  the operator subscribes to, and printing the token twice doubles the leak
  surface for no benefit).

These tests are TDD-style and intentionally mirror the structure of
``test_renderer.py`` for the other actions.
"""

from agent.models import (
    ContractStatus,
    DecisionAction,
    DecisionProposal,
    EnvDiff,
)
from agent.renderer import render_rollback_body


_APPROVAL_URL = "https://example.run.app/approvals/abc-123?t=xyz"


def _proposal(diffs=None, **overrides) -> DecisionProposal:
    if diffs is None:
        diffs = [
            EnvDiff(
                name="PAYMENT_MODE",
                expected="mock",
                live="live",
                contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,
            )
        ]
    defaults = {
        "action": DecisionAction.ROLLBACK,
        "env_diffs": diffs,
        "target_revision": "payment-demo-00042-abc",
        "rationale": "Hard contract violation; revert to last-known-good revision.",
        "confidence": 0.95,
        "requires_human_review": True,
    }
    defaults.update(overrides)
    return DecisionProposal(**defaults)


def test_rollback_body_happy_path():
    """Smoke test — body contains the URL, target revision, expiry note,
    an approval CTA, and the evidence-table headers."""
    body = render_rollback_body(_proposal(), _APPROVAL_URL)
    assert _APPROVAL_URL in body
    assert "payment-demo-00042-abc" in body
    # Match flexibly — phrasing-tolerant check on the 15-minute expiry note.
    assert "15 minutes" in body or "15-min" in body or "15 minute" in body
    # CTA phrasing — be tolerant of "approve" / "review" / "approval".
    lowered = body.lower()
    assert "approve" in lowered or "review" in lowered
    # Evidence table headers come from the shared _evidence_table helper.
    assert "Var" in body
    assert "Expected" in body
    assert "Live" in body


def test_rollback_body_renders_approval_url_verbatim():
    """The token-carrying URL must appear in the body exactly as passed in.

    The operator needs the full URL — including the ``?t=<token>`` query
    param — to make the approval request. Any truncation, URL-encoding, or
    escaping here would break the HITL flow.
    """
    body = render_rollback_body(_proposal(), _APPROVAL_URL)
    assert _APPROVAL_URL in body


def test_rollback_body_does_not_surface_token_separately():
    """The body must NOT call out the approval token as a separate field.

    The full URL is the only token-carrier. Printing a ``Token: xyz`` line
    in addition would double the leak surface (if the body is logged or
    forwarded somewhere the URL gets sanitized but the bare token doesn't).
    """
    body = render_rollback_body(_proposal(), _APPROVAL_URL)
    # No labeled "token" field anywhere outside the URL itself.
    # We can't just assert "xyz" only appears once (the URL contains it)
    # but we can assert the body never prints the token without the
    # surrounding URL context.
    lowered = body.lower()
    # No "token:" / "approval token:" / "Token = " style key-value rows.
    assert "token:" not in lowered
    assert "approval token" not in lowered
    # The raw token value (`xyz`) appears only inside the URL.
    assert body.count("xyz") == 1


def test_rollback_body_scrubs_secret_in_rationale():
    """Defense-in-depth — if the LLM rationale quotes a credential-shaped
    value, the renderer scrubs it. Mirror of the existing per-action tests
    in ``test_renderer.py``.
    """
    p = DecisionProposal(
        action=DecisionAction.ROLLBACK,
        env_diffs=[
            EnvDiff(
                name="API_TOKEN",
                expected="abcdef1234",
                live="newsecret5678",
                contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,
            )
        ],
        target_revision="payment-demo-00042-abc",
        rationale="API_TOKEN changed from abcdef1234 to newsecret5678.",
        confidence=0.95,
        requires_human_review=True,
    )
    body = render_rollback_body(p, _APPROVAL_URL)
    assert "abcdef1234" not in body
    assert "newsecret5678" not in body
    assert "API_TOKEN" in body  # var name still visible


def test_rollback_body_url_wrapped_in_autolink_form():
    """The URL is wrapped in ``<...>`` markdown autolink form so long URLs
    don't line-break in renderers that aren't aggressive about URL
    detection."""
    body = render_rollback_body(_proposal(), _APPROVAL_URL)
    assert f"<{_APPROVAL_URL}>" in body


def test_rollback_body_warns_traffic_shift():
    """The footer must make clear that approval shifts 100% of traffic to
    the target revision. This is the operator's last chance to back out
    before clicking through."""
    body = render_rollback_body(_proposal(), _APPROVAL_URL)
    lowered = body.lower()
    assert "100%" in body or "100 %" in body
    assert "traffic" in lowered


def test_rollback_body_is_plain_markdown():
    """No Jinja2 syntax leak, no unfilled ``{ }`` placeholders, no Python
    repr leak in the rendered output."""
    body = render_rollback_body(_proposal(), _APPROVAL_URL)
    # No unfilled placeholders (the f-string would have rendered everything;
    # any remaining `{` / `}` would indicate a missed substitution or a
    # Jinja2 stray).
    assert "{{" not in body
    assert "}}" not in body
    # No Python repr leak — DecisionAction.ROLLBACK should never appear as
    # the enum repr ("<DecisionAction.ROLLBACK: 'rollback'>") in the body.
    assert "DecisionAction." not in body
    assert "<EnvDiff" not in body
