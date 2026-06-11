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

Also covers the "What this change does" change-summary card: per-verb rendering,
sensitive masking, HTML escaping, and the double trust gate (route
``show_summary`` flag + the template's own unverifiable / integrity / denylist
re-checks) that keeps the card off untrustworthy artifacts.

Rendered directly through the app's Jinja env (``ds_css_href`` is an env global)
with a minimal view stub — no FastAPI request / GitHub / GCS needed.
"""
from __future__ import annotations

from types import SimpleNamespace

from agent.main import _TEMPLATES
from driftscribe_lib.iac_plan_summary import (
    BLAST_CANNOT_TOUCH_NOTE,
    AttrChange,
    ChangeEntry,
    PlanSummary,
)


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
        cost_summary=None,
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


def test_unverifiable_suppresses_summary_even_if_route_flag_leaks():
    # Belt-and-braces: the template's own gate must hold without Gate 1.
    view = _view()
    view.unverifiable = True
    view.change_summary = _summary()
    html = _render(view=view, show_summary=True)
    assert 'data-testid="change-summary"' not in html


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
    view.change_summary = _summary(
        entries=(ChangeEntry(
            verb="update", rtype="google_storage_bucket",
            type_label="Cloud Storage bucket", name="b",
            address="google_storage_bucket.b",
            attr_changes=(AttrChange("x", "1", "2"),),
            attrs_truncated=True,
        ),),
        n_create=0, n_update=1, n_hidden=3,
    )
    html = _render(view=view)
    assert "3 more resource change(s)" in html
    assert "more attribute changes" in html


# --------------------------------------------------------------------------- #
# Task (adopt-button-ui Phase 4) — calm adoption framing for adopt-only plans:
# a dedicated `adopt-note` banner (replaces the generic green no-destroy note)
# and a reframed blast line ("puts under management at most"). Both are guarded
# by s.adopt_only; the destructive branch stays FIRST and unchanged.
# --------------------------------------------------------------------------- #


def _norm(html: str) -> str:
    """Collapse all whitespace runs to single spaces — the template wraps copy
    across indented lines; a browser renders it as one space, so assertions on
    multi-word phrases must compare the same way."""
    import re
    return re.sub(r"\s+", " ", html)


def _import_summary(n_import=1, **kw):
    """An adopt-only summary: one import entry, n_import counts only."""
    base = dict(
        entries=(
            ChangeEntry(
                verb="import", rtype="google_storage_bucket",
                type_label="Cloud Storage bucket", name="my-old-uploads",
                address="google_storage_bucket.my_old_uploads", imported=True,
            ),
        ),
        n_create=0,
        n_import=n_import,
    )
    base.update(kw)
    return PlanSummary(**base)


def test_adopt_only_plan_renders_adopt_note_not_generic_green_note():
    view = _view()
    view.change_summary = _import_summary()
    html = _render(view=view)
    text = _norm(html)
    assert 'data-testid="adopt-note"' in html
    assert "Nothing in your infrastructure will be modified" in text
    assert "this only puts 1 resource under management" in text
    assert "count it as managed once the change merges" in text
    # The generic green no-destroy note must NOT also render (the banner is the
    # adopt-only branch, not the else branch).
    assert 'data-testid="no-destroy-note"' not in html
    # The adopt-note uses the calm OK styling, not the red hard-stop.
    assert 'data-testid="destroy-warning"' not in html


def test_adopt_note_pluralizes_resource_count():
    view = _view()
    view.change_summary = _import_summary(
        n_import=2,
        entries=(
            ChangeEntry(verb="import", rtype="google_storage_bucket",
                        type_label="Cloud Storage bucket", name="a",
                        address="google_storage_bucket.a", imported=True),
            ChangeEntry(verb="import", rtype="google_pubsub_topic",
                        type_label="Pub/Sub topic", name="b",
                        address="google_pubsub_topic.b", imported=True),
        ),
    )
    html = _render(view=view)
    assert "this only puts 2 resources under management" in _norm(html)


def test_adopt_only_blast_line_is_reframed_with_cannot_touch_note():
    view = _view()
    view.change_summary = _import_summary()
    html = _render(
        view=view,
        blast_phrase="1 Cloud Storage bucket",
        cannot_touch_note=BLAST_CANNOT_TOUCH_NOTE,
    )
    text = _norm(html)
    assert 'data-testid="blast-radius"' in html
    assert "puts under management at most: 1 Cloud Storage bucket" in text
    assert "It modifies nothing — the live resource is only recorded in OpenTofu state" in text
    # The cannot-touch note renders in the adopt variant too (honesty contract).
    # Jinja HTML-escapes the apostrophe in "DriftScribe's", so assert on a
    # distinctive apostrophe-free fragment rather than the raw constant.
    assert "cannot change IAM anywhere, cannot delete, replace, or un-manage" in text
    # The non-adopt phrasing must NOT appear for an adopt-only plan.
    assert "can affect at most" not in text


def test_import_plus_update_plan_keeps_existing_copy_no_adopt_note():
    # adopt_only is False for a mixed import+update plan → the generic green
    # note and the standard "can affect at most" blast copy render unchanged.
    view = _view()
    view.change_summary = _summary(
        entries=(ChangeEntry(
            verb="update", rtype="google_storage_bucket",
            type_label="Cloud Storage bucket", name="b",
            address="google_storage_bucket.b", imported=True,
        ),),
        n_create=0, n_update=1, n_import=0,
    )
    html = _render(
        view=view,
        blast_phrase="1 Cloud Storage bucket",
        cannot_touch_note=BLAST_CANNOT_TOUCH_NOTE,
    )
    text = _norm(html)
    assert 'data-testid="adopt-note"' not in html
    assert 'data-testid="no-destroy-note"' in html
    assert "can affect at most: 1 Cloud Storage bucket" in text
    assert "puts under management at most" not in text


def test_destructive_plan_unchanged_no_adopt_note():
    # The destructive red warning stays FIRST and wins outright — never an
    # adopt-note, even if the (impossible-in-practice) plan also had imports.
    view = _view()
    view.change_summary = _summary(
        entries=(ChangeEntry(
            verb="destroy", rtype="google_pubsub_topic", type_label="Pub/Sub topic",
            name="orders", address="google_pubsub_topic.orders",
        ),),
        n_create=0, n_destroy=1, n_import=1,
    )
    html = _render(view=view)
    assert 'data-testid="destroy-warning"' in html
    assert 'data-testid="adopt-note"' not in html
    assert 'data-testid="no-destroy-note"' not in html


# --------------------------------------------------------------------------- #
# Task 5 (Wave-4 item 13) — heuristic cost estimate card
# --------------------------------------------------------------------------- #


def _cost(headline="Adds no always-on cost — ¥0/month until it is used.",
          entries=(), n_hidden=0):
    return SimpleNamespace(
        headline=headline, entries=entries, n_hidden=n_hidden,
        by_address={e.address: e for e in entries},
        disclaimer=("Cost figures are heuristic estimates from Google Cloud "
                    "list prices (Tokyo region, fetched 2026-06-11) — not a "
                    "quote. Usage-based charges (storage, messages, requests, "
                    "network) depend entirely on how much you use."),
    )


def test_cost_headline_entry_and_disclaimer_render():
    v = _view()
    v.change_summary = _summary()
    ec = SimpleNamespace(address="google_storage_bucket.assets",
                         kind="usage", monthly_jpy=None,
                         note="¥0/month while empty — storage billed at about "
                              "¥3.67/GiB-month (Standard, Tokyo list price)")
    v.cost_summary = _cost(entries=(ec,))
    html = _render(view=v, show_summary=True)
    assert 'data-testid="cost-estimate"' in html
    assert "Adds no always-on cost" in html
    assert 'data-testid="cost-entry"' in html and "¥3.67/GiB-month" in html
    assert 'data-testid="cost-disclaimer"' in html and "not a quote" in html


def test_cost_absent_when_cost_summary_none():
    v = _view()
    v.change_summary = _summary()
    v.cost_summary = None
    html = _render(view=v, show_summary=True)
    assert 'data-testid="cost-estimate"' not in html
    assert 'data-testid="cost-disclaimer"' not in html


def test_cost_never_renders_outside_trust_gate():
    # unverifiable / integrity-fail / denylist-blocked all suppress the whole
    # card today — cost must die with it (H1).
    for break_it in (
        lambda v: setattr(v, "unverifiable", True),
        lambda v: setattr(v, "integrity_ok", False),
        lambda v: setattr(v, "denylist_violations", [("import-forbidden-v1", "x")]),
    ):
        v = _view()
        v.change_summary = _summary()
        v.cost_summary = _cost()
        break_it(v)
        html = _render(view=v, show_summary=True)
        assert 'data-testid="cost-estimate"' not in html


def test_cost_entry_skipped_for_deposed_row():
    # A deposed entry shares its address with the main row — the join must
    # not put a cost line on it. Build a summary with a deposed entry plus a
    # normal one at the same address; assert exactly ONE cost-entry div.
    shared_address = "google_storage_bucket.assets"
    normal_entry = ChangeEntry(
        verb="create", rtype="google_storage_bucket",
        type_label="Cloud Storage bucket", name="assets",
        address=shared_address, location="asia-northeast1",
    )
    deposed_entry = ChangeEntry(
        verb="destroy", rtype="google_storage_bucket",
        type_label="Cloud Storage bucket", name="assets",
        address=shared_address, deposed="abc123",
    )
    v = _view()
    v.change_summary = _summary(entries=(normal_entry, deposed_entry), n_create=1, n_destroy=1)
    ec = SimpleNamespace(
        address=shared_address,
        kind="usage", monthly_jpy=None,
        note="¥0/month while empty — storage billed at about ¥3.67/GiB-month (Standard, Tokyo list price)",
    )
    v.cost_summary = _cost(entries=(ec,))
    html = _render(view=v, show_summary=True)
    assert html.count('data-testid="cost-entry"') == 1
