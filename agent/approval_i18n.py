"""Render-time Japanese localization for the server-rendered approval pages.

Scope contract (2026-07-12, operator decision "decision copy only"):

- Locale is an explicit, allowlisted ``?lang=`` query param appended by the
  SPA's approval-link builders (``frontend/src/lib/approval.ts``). NO param —
  every pre-existing link, test, e2e probe, PR comment — renders every
  English STRING it always did (rendered bytes may differ only in inert
  inter-tag whitespace from the added Jinja branches; nothing pins page
  bytes). There is no Accept-Language sniffing and no cookie: the pages must
  stay deterministic for probes and stable for the EN-pinned test suites.
- Localized: everything an operator reads to DECIDE (page chrome, verdict
  labels, change-summary copy, warnings, buttons, gate notes) plus the
  enumerable Python-side strings below. Deliberately NOT localized (kept
  English even under ``lang=ja``): the 16 provenance-field tooltips (literal
  metadata keys), cost-estimate strings, ``e.action_reason`` (OpenTofu
  wording), denylist violation details (policy engine output), and the POST
  outcome strings minted inside the POST handlers — the POST re-render keeps
  its Japanese chrome but reports the outcome in English.
- Every ``localize_*`` helper is exact-match with an English-identity
  fallback: an unmapped string renders as-is rather than raising, so a new
  backend reason/label can never break the page. The flip side is pinned by
  tests/unit/test_approval_i18n.py: if a mapped SOURCE string is reworded,
  the JA mapping goes silently stale, so that test asserts each mapping key
  still equals the live constant it was copied from.

Terminology follows docs/i18n-glossary.md and the SPA catalogs
(frontend/src/locales/*.ts) — e.g. 承認/却下, 拒否リスト, IaC の適用,
IaC 管理に取り込む, 自律動作レベル（監視のみ／提案／提案＋適用）, 判断履歴.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent.autonomy import MODE_LABELS

if TYPE_CHECKING:  # pragma: no cover — typing only, avoids a runtime import
    from fastapi import Request

    from driftscribe_lib.iac_plan_summary import PlanSummary

APPROVAL_LANGS = ("en", "ja")


def resolve_lang(request: "Request") -> str:
    """Allowlisted ``?lang=`` resolution; anything else is English."""
    lang = request.query_params.get("lang", "en")
    return lang if lang in APPROVAL_LANGS else "en"


# --------------------------------------------------------------------------- #
# reason_blocked / autonomy_detail — exact-match EN → JA.
#
# REASON_EN is the SINGLE SOURCE for the gate-reason literals: agent/main.py's
# gate rungs assign these constants (they are not duplicated there), so the JA
# map cannot go stale against a reworded reason — a new rung that skips this
# module simply renders English (identity fallback), never breaks. The only
# strings still duplicated from another module are the two
# autonomy_apply_blocked_detail() variants, pinned by test_approval_i18n.py.
# --------------------------------------------------------------------------- #
REASON_EN: dict[str, str] = {
    "no_artifact": "No verifiable plan artifact.",
    "unverifiable": "artifact unverifiable",
    "integrity_mismatch": "plan.json integrity mismatch",
    "denylist": "denylist violations (self-protection policy)",
    "pr_mismatch": "artifact does not match this PR",
    "operator_only": (
        "approving requires a signed-in operator identity"
        " (Cloudflare Access), which this request does not carry"
    ),
    "not_configured": "approvals not configured (server token unset)",
    "dry_run": "infra apply disabled (coordinator in dry-run mode)",
    "paused": "DriftScribe is paused (operator kill switch active)",
    "paused_unreadable": (
        "DriftScribe is paused (pause state unreadable — failing closed)"
    ),
    "autonomy_unreadable": (
        "autonomy state could not be read — the effective mode is Observe "
        "(failing closed). Applying changes is disabled until the dial can "
        "be read again."
    ),
}
_AUTONOMY_DIAL_JA = (
    "自律動作レベルが「{label}」に設定されているため、変更の適用は無効です。"
    "この承認を有効にするには、オペレーター UI でダイヤルを「提案＋適用」に"
    "上げてください。提案自体は有効なまま待機しています。"
)


def _autonomy_blocked_en(mode: str) -> str:
    # Byte-identical to agent.autonomy.autonomy_apply_blocked_detail — the
    # import stays one-way (approval_i18n ← autonomy) and the pinning test
    # asserts equality against the real function.
    label = MODE_LABELS.get(mode, mode)
    return (
        f"autonomy is set to {label} — applying changes is disabled. "
        "Raise the dial to Propose + Apply in the operator UI to enable this "
        "approval. The proposal itself remains valid and waiting."
    )


_MODE_LABELS_JA = {"observe": "監視のみ", "propose": "提案"}

_REASON_JA_BY_KEY: dict[str, str] = {
    "no_artifact": "検証可能なプランアーティファクトがありません。",
    "unverifiable": "アーティファクトを検証できません",
    "integrity_mismatch": "plan.json の整合性チェックに失敗しました",
    "denylist": "拒否リスト違反（自己保護ポリシー）",
    "pr_mismatch": "アーティファクトがこの PR と一致しません",
    "operator_only": (
        "承認にはサインイン済みのオペレーターの本人確認（Cloudflare Access）が"
        "必要ですが、このリクエストには含まれていません"
    ),
    "not_configured": "承認が設定されていません（サーバートークン未設定）",
    "dry_run": "IaC の適用は無効です（コーディネーターが dry-run モード）",
    "paused": "DriftScribe は一時停止中です（オペレーターの停止スイッチが有効）",
    "paused_unreadable": (
        "DriftScribe は一時停止中です（一時停止状態を読み取れないため、安全側に倒しています）"
    ),
    "autonomy_unreadable": (
        "自律動作レベルの状態を読み取れません。有効なモードは「監視のみ」です"
        "（安全側に倒しています）。ダイヤルが再び読み取れるようになるまで、"
        "変更の適用は無効です。"
    ),
}

REASON_JA: dict[str, str] = {
    **{REASON_EN[key]: ja for key, ja in _REASON_JA_BY_KEY.items()},
    _autonomy_blocked_en("observe"): _AUTONOMY_DIAL_JA.format(
        label=_MODE_LABELS_JA["observe"]
    ),
    _autonomy_blocked_en("propose"): _AUTONOMY_DIAL_JA.format(
        label=_MODE_LABELS_JA["propose"]
    ),
}


def localize_reason(reason: str, lang: str) -> str:
    """JA rendering of a gate reason; English (identity) for everything else."""
    if lang != "ja":
        return reason
    return REASON_JA.get(reason, reason)


# --------------------------------------------------------------------------- #
# Terminal-state outcome banners (GET decision-state awareness). The EN
# branches are byte-identical to the strings iac_approval_get built inline
# before this module existed (pinned by test_approval_i18n.py).
# --------------------------------------------------------------------------- #
def outcome_superseded(superseded_by: int, lang: str) -> str:
    if lang == "ja":
        return (
            f"PR #{superseded_by} に置き換え済みです（適用・マージ済み）。"
            "このプランは古くなっています（対象のリソースはすでに存在します）。"
            "ここで承認するものはありません。"
        )
    return (
        f"Superseded by PR #{superseded_by}, which is applied and merged. "
        "This plan is stale (its resource already exists) — nothing to "
        "approve here."
    )


def outcome_already_applied(lang: str) -> str:
    if lang == "ja":
        return "すでに適用・マージ済みです。ここで承認するものはもうありません。"
    return "Already applied and merged — nothing more to approve here."


def outcome_terminal(status: str, lang: str) -> str:
    if lang == "ja":
        note = (
            "失敗した適用が state を汚していないことを証明できませんでした。"
            "再試行の前に、適用失敗リカバリーランブック（state の突合）を実行"
            "してください。自動では再試行されません。"
            if status == "failed_state_suspect"
            else "手動での確認が必要です。自動では再試行されません。"
        )
        return f"終了状態が記録されています：apply_status={status!r}。{note}"
    note = (
        "The failed apply could not be proven to have left state clean "
        "— run the apply-failure recovery runbook (state reconcile) "
        "before any retry; this will NOT be retried automatically."
        if status == "failed_state_suspect"
        else "Manual verification required; this will NOT be retried "
        "automatically."
    )
    return f"Terminal state recorded: apply_status={status!r}. {note}"


# --------------------------------------------------------------------------- #
# Shared page constants (exact-match, identity fallback).
# --------------------------------------------------------------------------- #
_CONST_JA: dict[str, str] = {
    # driftscribe_lib.iac_plan_summary.BLAST_CANNOT_TOUCH_NOTE — the honesty
    # contract (may claim only what iac_plan_denylist enforces) carries over
    # verbatim in meaning; the pinning test guards the EN source.
    (
        "It cannot touch DriftScribe's own control plane (its services, "
        "service accounts, state/artifact buckets, secrets, or encryption "
        "keys), cannot change IAM anywhere, cannot delete, replace, or "
        "un-manage any resource, and can adopt (import) an existing resource "
        "only one at a time, from a small allowlist of types, and only when "
        "nothing would be modified — denylist-enforced, re-checked by the "
        "apply worker before apply."
    ): (
        "この変更は、DriftScribe 自身のコントロールプレーン（サービス、"
        "サービスアカウント、state・アーティファクトのバケット、シークレット、"
        "暗号鍵）には触れられず、IAM の変更は一切できず、リソースの削除・"
        "置換・IaC 管理からの除外もできません。既存リソースの取り込み（インポート）は、"
        "一度に 1 件、許可された少数のタイプに限り、かつ何も変更されない場合に"
        "のみ可能です。これらは拒否リストで強制され、適用前に適用ワーカーが"
        "再チェックします。"
    ),
    # agent.main._IAC_SOURCE_DEMO_NOTE
    (
        "Demo: the generated OpenTofu source is shown to everyone here so judges can "
        "inspect exactly what the agent authored. Outside the demo this view would be "
        "operator-only."
    ): (
        "デモ：生成された OpenTofu ソースは、エージェントが実際に何を書いたかを"
        "審査員が確認できるよう、ここでは誰でも閲覧できます。デモ期間外で"
        "あれば、この表示はオペレーター限定になります。"
    ),
}


def localize_const(text: str, lang: str) -> str:
    """JA rendering of a shared page constant; identity for everything else."""
    if lang != "ja":
        return text
    return _CONST_JA.get(text, text)


# --------------------------------------------------------------------------- #
# Change-summary vocabulary. Type labels follow the SPA's `infra.type.*`
# JA catalog (frontend/src/locales/infra.ts) where the concepts overlap, so
# the approval page and the resource map name a resource the same way.
# Unknown labels (new types, the google_-strip fallback) pass through in EN.
# --------------------------------------------------------------------------- #
_TYPE_LABEL_JA: dict[str, str] = {
    "Cloud Storage bucket": "ストレージバケット",
    "Pub/Sub topic": "Pub/Sub トピック",
    "Pub/Sub subscription": "Pub/Sub サブスクリプション",
    "Cloud Run service": "Cloud Run サービス",
    "service account": "サービスアカウント",
    "Secret Manager secret": "シークレット",
    "Secret Manager secret version": "シークレットバージョン",
    "Eventarc trigger": "Eventarc トリガー",
    "Artifact Registry repository": "Artifact Registry リポジトリ",
    "project IAM member binding": "プロジェクト IAM メンバーバインディング",
    "project IAM binding": "プロジェクト IAM バインディング",
    "custom IAM role": "カスタム IAM ロール",
    "Cloud Run IAM member binding": "Cloud Run IAM メンバーバインディング",
    "Firestore database": "Firestore データベース",
    "VPC network": "VPC ネットワーク",
    "VPC subnetwork": "サブネット",
    "firewall rule": "ファイアウォールルール",
}

# OpenTofu plan verbs (ChangeEntry.verb) as displayed text. The chip's CSS
# class keeps the raw enum (`ds-verb--{{ e.verb }}`); only the visible text
# localizes. `change` is the summary's catch-all bucket (rendered "other" in
# EN by the counts line).
_VERB_JA: dict[str, str] = {
    "create": "作成",
    "update": "変更",
    "replace": "置換",
    "destroy": "削除",
    "import": "インポート",
    # Matches the SPA's established forget verb (infra.graph.verb.forget =
    # 「IaC 管理から除外予定」) minus the ghost 〜予定.
    "forget": "IaC 管理から除外",
    "change": "その他",
}


def ja_type_label(label: str) -> str:
    """JA resource-type label for a plan-summary EN label (identity fallback)."""
    return _TYPE_LABEL_JA.get(label, label)


def ja_verb_label(verb: str) -> str:
    """JA display text for a plan verb chip (identity fallback)."""
    return _VERB_JA.get(verb, verb)


def blast_phrase_ja(summary: "PlanSummary | None") -> str:
    """「Pub/Sub トピック 1件、ストレージバケット 2件」 — the JA counterpart of
    driftscribe_lib.iac_plan_summary.blast_radius_phrase (same suppression
    contract: '' when there are no type_counts). Counts use the SPA's 〜件
    form; labels fall back to English for unmapped types."""
    if summary is None or not summary.type_counts:
        return ""
    return "、".join(
        f"{ja_type_label(label)} {n}件" for label, n in summary.type_counts
    )
