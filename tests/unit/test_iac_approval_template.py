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
from driftscribe_lib.iac_plan_summary import AttrChange, ChangeEntry, PlanSummary


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
        change_summary=None,
    )


def _render(**ctx) -> str:
    base = {
        "pr_number": 42,
        "view": _view(),
        "form_token": None,
        "can_approve": False,
        "reason_blocked": "",
        "reason_severity": "",
        "show_summary": True,
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


def test_outcome_severity_error_styles_banner_red():
    # A TERMINAL apply failure is rendered via decision="approve" + a red banner
    # (outcome_severity="error") so it never reads as green success.
    html = _render(
        decision="approve",
        outcome="Terminal state recorded: apply_status='failed_state_suspect'.",
        outcome_severity="error",
    )
    assert "Terminal state recorded" in html
    assert 'class="ds-blocked"' in html  # banner is the red hard-stop variant
    # bottom form/callout still suppressed (decision is set)
    assert 'data-testid="approve-button"' not in html
    assert 'data-testid="approve-blocked"' not in html
    assert 'data-testid="approve-pending"' not in html


def test_outcome_decision_default_severity_is_green_note():
    # Without outcome_severity the success banner stays the green note (the
    # `outcome_severity is defined` guard must not break the default path).
    html = _render(decision="approve", outcome="Already applied and merged.")
    assert "Already applied and merged." in html
    # The banner is NOT the red hard-stop; the only ds-blocked in the page would
    # be a banner, and there is none here.
    assert 'class="ds-blocked"' not in html


# --------------------------------------------------------------------------- #
# Task 6 — "What this change does" plain-language summary card.
# --------------------------------------------------------------------------- #


def _summary(**kw):
    base = dict(
        entries=(
            ChangeEntry(
                verb="create", rtype="google_storage_bucket",
                type_label="Cloud Storage bucket", name="assets",
                address="google_storage_bucket.assets", location="asia-northeast1",
            ),
        ),
        n_create=1,
    )
    base.update(kw)
    return PlanSummary(**base)


def test_change_summary_card_renders_entry_and_green_note():
    view = _view()
    view.change_summary = _summary()
    html = _render(view=view)
    assert 'data-testid="change-summary"' in html
    assert "Cloud Storage bucket" in html and "assets" in html
    assert "asia-northeast1" in html
    assert 'data-testid="no-destroy-note"' in html
    assert "ds-verb--create" in html


def test_destroy_warning_replaces_green_note():
    view = _view()
    view.change_summary = _summary(
        entries=(ChangeEntry(
            verb="destroy", rtype="google_pubsub_topic", type_label="Pub/Sub topic",
            name="orders", address="google_pubsub_topic.orders",
        ),),
        n_create=0, n_destroy=1,
    )
    html = _render(view=view)
    assert 'data-testid="destroy-warning"' in html
    assert 'data-testid="no-destroy-note"' not in html
    assert "ds-verb--destroy" in html


def test_unclassified_change_blocks_green_note():
    view = _view()
    view.change_summary = _summary(
        entries=(ChangeEntry(
            verb="change", rtype="google_x", type_label="x",
            name="n", address="google_x.n",
        ),),
        n_create=0, n_change=1,
    )
    html = _render(view=view)
    assert 'data-testid="no-destroy-note"' not in html
    assert 'data-testid="destroy-warning"' not in html
    assert "cannot fully classify" in html


def test_attr_diff_rows_and_sensitive_marker():
    view = _view()
    view.change_summary = _summary(
        entries=(ChangeEntry(
            verb="update", rtype="google_cloud_run_v2_service",
            type_label="Cloud Run service", name="svc",
            address="google_cloud_run_v2_service.svc",
            attr_changes=(
                AttrChange("template.env[0].value", '"1"', '"2"'),
                AttrChange("password", "(sensitive)", "(sensitive)", sensitive=True),
            ),
        ),),
        n_create=0, n_update=1,
    )
    html = _render(view=view)
    assert "template.env[0].value" in html
    assert "sensitive value changed" in html


def test_summary_values_are_html_escaped():
    view = _view()
    view.change_summary = _summary(
        entries=(ChangeEntry(
            verb="update", rtype="google_storage_bucket",
            type_label="Cloud Storage bucket", name="b",
            address="google_storage_bucket.b",
            attr_changes=(AttrChange("label", '"<script>alert(1)</script>"', '"x"'),),
        ),),
        n_create=0, n_update=1,
    )
    html = _render(view=view)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_no_summary_renders_fallback_note():
    html = _render()  # stub has change_summary=None
    assert 'data-testid="summary-unavailable"' in html
    assert 'data-testid="change-summary"' not in html


def test_empty_summary_renders_no_changes_card():
    view = _view()
    view.change_summary = PlanSummary(entries=())
    html = _render(view=view)
    assert 'data-testid="change-summary-empty"' in html
    assert "does not modify any infrastructure" in html


def test_unverifiable_view_suppresses_summary_section():
    view = _view()
    view.unverifiable = True
    view.change_summary = _summary()
    html = _render(view=view, show_summary=False,
                   reason_blocked="artifact unverifiable", reason_severity="error")
    assert 'data-testid="change-summary"' not in html
    assert 'data-testid="summary-unavailable"' not in html


def test_integrity_mismatch_suppresses_summary_even_if_route_flag_leaks():
    # Belt-and-braces: _plan_json is populated even on a digest MISMATCH, so
    # the template independently requires integrity_ok — a card claiming
    # "integrity-checked" must never render for a tampered artifact.
    view = _view()
    view.integrity_ok = False
    view.change_summary = _summary()
    html = _render(view=view, show_summary=True)
    assert 'data-testid="change-summary"' not in html


def test_denylist_violation_suppresses_summary():
    # No green "nothing destroyed" reassurance under a red denylist hard-stop.
    view = _view()
    view.denylist_violations = [("delete-action-forbidden-v1", "x")]
    view.change_summary = _summary()
    html = _render(view=view, show_summary=True)
    assert 'data-testid="change-summary"' not in html
    assert 'data-testid="no-destroy-note"' not in html


def test_missing_show_summary_defaults_to_no_card():
    # The POST outcome renders never pass show_summary => no card there.
    view = _view()
    view.change_summary = _summary()
    base = {"pr_number": 42, "view": view, "form_token": None,
            "can_approve": False, "reason_blocked": "", "reason_severity": ""}
    from agent.main import _TEMPLATES
    html = _TEMPLATES.env.get_template("iac_approval.html").render(base)
    assert 'data-testid="change-summary"' not in html


def test_forget_entry_has_explainer_and_no_green_line():
    view = _view()
    view.change_summary = _summary(
        entries=(ChangeEntry(
            verb="forget", rtype="google_storage_bucket",
            type_label="Cloud Storage bucket", name="b",
            address="google_storage_bucket.b",
        ),),
        n_create=0, n_forget=1,
    )
    html = _render(view=view)
    assert "ds-verb--forget" in html
    assert "stops being managed" in html  # explainer: live resource NOT deleted
    assert 'data-testid="no-destroy-note"' not in html


def test_deposed_marker_rendered():
    view = _view()
    view.change_summary = _summary(
        entries=(ChangeEntry(
            verb="destroy", rtype="google_storage_bucket",
            type_label="Cloud Storage bucket", name="b",
            address="google_storage_bucket.b", deposed="byebye01",
        ),),
        n_create=0, n_destroy=1,
    )
    html = _render(view=view)
    assert "leftover copy" in html  # deposed ≠ the current object


def test_hidden_entries_note_and_truncated_attrs_note():
    view = _view()
    view.change_summary = _summary(n_hidden=3)
    html = _render(view=view)
    assert "3 more resource change(s)" in html
