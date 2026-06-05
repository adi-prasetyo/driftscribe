"""Unit tests for the iac_approval.html bottom-callout severity logic.

The GET handler classifies WHY Approve is suppressed (``reason_severity``) and
the POST outcome path renders a ``decision`` banner. The template must:

- show the Approve form when ``can_approve``;
- render NOTHING at the bottom when an outcome ``decision`` is shown (so a
  successful approve does not also show a red "Approve disabled" error box under
  the green "Approved" banner — the bug this change fixes);
- render the red ``ds-blocked`` callout ONLY for a genuine hard-stop
  (``reason_severity == "error"``: unverifiable / integrity mismatch / denylist
  / artifact-mismatch);
- render a calm grey ``ds-note`` ("Approval not available yet …") for a
  not-ready reason (``reason_severity == "pending"``: not configured / dry-run).

Rendered directly through the app's Jinja env (``ds_css_href`` is an env global)
with a minimal view stub — no FastAPI request / GitHub / GCS needed.
"""
from __future__ import annotations

from types import SimpleNamespace

from agent.main import _TEMPLATES


def _view() -> SimpleNamespace:
    """Minimal stand-in carrying only the attrs the template reads."""
    return SimpleNamespace(
        head_sha="a" * 40,
        unverifiable=False,
        integrity_ok=True,
        denylist_violations=[],
        metadata={},
        artifact_uri_metadata="gs://b/pr-42/metadata.json",
        generation_metadata="1700000000000003",
        tofu_show_text="# google_storage_bucket.x will be created",
    )


def _render(**ctx) -> str:
    base = {
        "pr_number": 42,
        "view": _view(),
        "form_token": None,
        "can_approve": False,
        "reason_blocked": "",
        "reason_severity": "",
    }
    base.update(ctx)
    return _TEMPLATES.env.get_template("iac_approval.html").render(base)


def test_outcome_decision_suppresses_bottom_callout():
    html = _render(decision="approve", outcome="Approved — infra apply dispatched.")
    assert "Approved — infra apply dispatched." in html
    # The success banner is authoritative; no contradictory red error box below.
    assert "Approve disabled" not in html
    assert 'data-testid="approve-blocked"' not in html
    assert 'data-testid="approve-pending"' not in html


def test_error_severity_renders_red_blocked_box():
    html = _render(reason_blocked="plan.json integrity mismatch", reason_severity="error")
    assert 'data-testid="approve-blocked"' in html
    assert 'class="ds-blocked"' in html
    assert "Approve disabled: plan.json integrity mismatch" in html
    assert 'data-testid="approve-pending"' not in html


def test_pending_severity_renders_grey_note_not_red():
    html = _render(
        reason_blocked="infra apply disabled (coordinator in dry-run mode)",
        reason_severity="pending",
    )
    assert 'data-testid="approve-pending"' in html
    assert 'class="ds-note"' in html
    assert "Approval not available yet" in html
    assert "infra apply disabled (coordinator in dry-run mode)" in html
    # NOT the red error treatment.
    assert "Approve disabled" not in html
    assert 'data-testid="approve-blocked"' not in html


def test_can_approve_renders_form_and_no_callout():
    html = _render(can_approve=True, form_token="signed-token")
    assert 'data-testid="approve-button"' in html
    assert 'data-testid="token-field"' in html
    assert 'data-testid="approve-blocked"' not in html
    assert 'data-testid="approve-pending"' not in html
