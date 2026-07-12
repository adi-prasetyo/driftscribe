"""Approval-page localization (agent/approval_i18n.py + the two templates).

Three contracts:

1. **English is untouched.** No ``lang`` in the render context (every
   pre-existing caller, link, test and probe) must produce the exact English
   strings the pages always had — the JA pass is additive-only.
2. **``?lang=ja`` renders the decision copy in Japanese** on both templates,
   including the gate notes, buttons and change-summary vocabulary.
3. **Exact-match maps cannot go silently stale.** The gate reasons live in
   approval_i18n.REASON_EN itself (main.py assigns the constants — single
   source), and every EN key still copied out of another module (the two
   autonomy_apply_blocked_detail variants, the blast/demo-note constants,
   the outcome banners) is pinned against its live source, so rewording the
   source fails HERE instead of quietly demoting the JA page to mixed
   English.
"""
from __future__ import annotations

from types import SimpleNamespace

from agent import approval_i18n
from agent.autonomy import autonomy_apply_blocked_detail
from agent.main import _IAC_SOURCE_DEMO_NOTE, _TEMPLATES
from driftscribe_lib.iac_plan_summary import (
    BLAST_CANNOT_TOUCH_NOTE,
    ChangeEntry,
    PlanSummary,
)


def _view() -> SimpleNamespace:
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


def _render_iac(**ctx) -> str:
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


def _rollback_approval() -> SimpleNamespace:
    return SimpleNamespace(
        approval_id="ap-123",
        target_revision="payment-demo-00042-abc",
        reason="env drift detected",
        created_by="rollback-worker",
        expires_at="2026-07-12T23:59:00Z",
        status="pending",
    )


def _render_rollback(**ctx) -> str:
    base = {
        "approval_id": "ap-123",
        "approval": _rollback_approval(),
        "token": "tok",
        "token_missing": False,
        "expired": False,
        "paused": False,
        "autonomy_blocked": False,
        "autonomy_detail": "",
    }
    base.update(ctx)
    return _TEMPLATES.env.get_template("approval.html").render(base)


# --------------------------------------------------------------------------- #
# 1. No-lang renders stay byte-for-byte English.
# --------------------------------------------------------------------------- #
def test_iac_page_without_lang_is_english():
    html = _render_iac(can_approve=True, form_token="signed-token")
    assert 'lang="en"' in html
    assert "DriftScribe Infra Approval" in html
    assert "Approve infra apply" in html
    assert "Human-in-the-loop gate for the OpenTofu apply worker" in html
    assert "IaC 適用" not in html


def test_rollback_page_without_lang_is_english():
    html = _render_rollback()
    assert 'lang="en"' in html
    assert "DriftScribe Approval" in html
    assert "Approve rollback" in html
    assert "ロールバック" not in html


# --------------------------------------------------------------------------- #
# 2. lang='ja' renders the decision copy in Japanese.
# --------------------------------------------------------------------------- #
def test_iac_page_ja_chrome_and_form():
    html = _render_iac(lang="ja", can_approve=True, form_token="signed-token")
    assert 'lang="ja"' in html
    assert "DriftScribe IaC 適用の承認" in html
    assert "IaC 適用を承認" in html
    assert "却下" in html
    # The POST keeps the language.
    assert 'action="/iac-approvals/42?lang=ja"' in html
    # Verdict card labels.
    assert "整合性" in html and "検証済み" in html
    assert "拒否リスト（自己保護）" in html and "なし" in html


def test_iac_page_ja_pending_note_wraps_localized_reason():
    reason = approval_i18n.localize_reason(
        approval_i18n.REASON_EN["dry_run"], "ja"
    )
    html = _render_iac(lang="ja", reason_blocked=reason, reason_severity="pending")
    assert "承認はまだ利用できません" in html
    assert "dry-run モード" in html
    assert "Approval not available yet" not in html


def test_iac_page_ja_change_summary_vocabulary():
    summary = PlanSummary(
        entries=(
            ChangeEntry(
                verb="import",
                rtype="google_storage_bucket",
                type_label="Cloud Storage bucket",
                name="my-old-uploads",
                address="google_storage_bucket.adopt_my_old_uploads",
                location="asia-northeast1",
                imported=True,
            ),
        ),
        n_import=1,
        type_counts=(("Cloud Storage bucket", 1),),
    )
    view = _view()
    view.change_summary = summary
    html = _render_iac(
        lang="ja",
        view=view,
        show_summary=True,
        blast_phrase=approval_i18n.blast_phrase_ja(summary),
        cannot_touch_note=approval_i18n.localize_const(BLAST_CANNOT_TOUCH_NOTE, "ja"),
    )
    assert "この変更で行われること" in html
    assert "ストレージバケット" in html  # type label via ja_type_label
    assert "インポート 1件" in html  # counts line
    assert "IaC 管理に取り込み" in html  # imported gloss
    assert "ストレージバケット 1件" in html  # blast_phrase_ja
    assert "コントロールプレーン" in html  # cannot_touch_note (JA)


def test_rollback_page_ja():
    html = _render_rollback(lang="ja")
    assert 'lang="ja"' in html
    assert "DriftScribe 承認" in html
    assert "ロールバックを承認" in html
    assert 'action="/approvals/ap-123?lang=ja"' in html
    assert "対象リビジョン" in html
    # Crew-authored reason stays pass-through.
    assert "env drift detected" in html


# --------------------------------------------------------------------------- #
# 3. Exact-match maps pinned against their live sources.
# --------------------------------------------------------------------------- #
def test_reason_map_covers_live_autonomy_strings():
    # If autonomy_apply_blocked_detail is ever reworded, the JA mapping would
    # silently stop matching — fail here instead.
    for mode in ("observe", "propose"):
        live = autonomy_apply_blocked_detail(mode)
        assert live in approval_i18n.REASON_JA, mode
        assert approval_i18n.localize_reason(live, "ja") != live


def test_every_reason_constant_has_a_ja_rendering():
    # REASON_EN is the single source main.py assigns from, so staleness vs
    # main.py is structural — this guards the EN↔JA key parity inside the
    # module itself (a new REASON_EN entry must ship with its JA text).
    for key, en in approval_i18n.REASON_EN.items():
        ja = approval_i18n.localize_reason(en, "ja")
        assert ja != en, f"REASON_EN[{key!r}] has no JA rendering"


def test_const_map_covers_live_constants():
    for const in (BLAST_CANNOT_TOUCH_NOTE, _IAC_SOURCE_DEMO_NOTE):
        assert approval_i18n.localize_const(const, "ja") != const


def test_localize_reason_identity_fallbacks():
    assert approval_i18n.localize_reason("some new reason", "ja") == "some new reason"
    known = "artifact unverifiable"
    assert approval_i18n.localize_reason(known, "en") == known


def test_outcome_helpers_en_byte_identical_to_pre_i18n_strings():
    assert approval_i18n.outcome_superseded(221, "en") == (
        "Superseded by PR #221, which is applied and merged. "
        "This plan is stale (its resource already exists) — nothing to "
        "approve here."
    )
    assert approval_i18n.outcome_already_applied("en") == (
        "Already applied and merged — nothing more to approve here."
    )
    assert approval_i18n.outcome_terminal("failed_state_suspect", "en") == (
        "Terminal state recorded: apply_status='failed_state_suspect'. "
        "The failed apply could not be proven to have left state clean "
        "— run the apply-failure recovery runbook (state reconcile) "
        "before any retry; this will NOT be retried automatically."
    )
    assert approval_i18n.outcome_terminal("failed", "en") == (
        "Terminal state recorded: apply_status='failed'. "
        "Manual verification required; this will NOT be retried automatically."
    )


def test_outcome_helpers_ja():
    assert "PR #221 に置き換え済み" in approval_i18n.outcome_superseded(221, "ja")
    assert "すでに適用・マージ済み" in approval_i18n.outcome_already_applied("ja")
    assert "apply_status='failed'" in approval_i18n.outcome_terminal("failed", "ja")


def test_resolve_lang_allowlist():
    def req(**params):
        return SimpleNamespace(query_params=params)

    assert approval_i18n.resolve_lang(req()) == "en"
    assert approval_i18n.resolve_lang(req(lang="ja")) == "ja"
    assert approval_i18n.resolve_lang(req(lang="en")) == "en"
    assert approval_i18n.resolve_lang(req(lang="fr")) == "en"
    assert approval_i18n.resolve_lang(req(lang="<script>")) == "en"


def test_blast_phrase_ja_shapes():
    assert approval_i18n.blast_phrase_ja(None) == ""
    empty = PlanSummary(entries=(), type_counts=())
    assert approval_i18n.blast_phrase_ja(empty) == ""
    multi = PlanSummary(
        entries=(),
        type_counts=(("Pub/Sub topic", 1), ("Cloud Storage bucket", 2)),
    )
    assert (
        approval_i18n.blast_phrase_ja(multi)
        == "Pub/Sub トピック 1件、ストレージバケット 2件"
    )
    unknown = PlanSummary(entries=(), type_counts=(("quantum widget", 3),))
    assert approval_i18n.blast_phrase_ja(unknown) == "quantum widget 3件"
