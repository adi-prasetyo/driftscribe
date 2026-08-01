// decisions namespace — DecisionSummary, DecisionRecord and decision.ts row
// labels. It was named for DecisionsRail, which ds-jns Task 3.3 deleted along
// with two thirds of these keys (its chrome, its search modal, its row face,
// its lifecycle steps); what is left is what the DESK renders.
//
// EN values are moved BYTE-FOR-BYTE from the code they used to live in (see
// the call sites in components/DecisionSummary.svelte and lib/decision.ts, and
// formerly the deleted rail) — the EN catalog is the app's
// original inline text, so the unit-test suite (pinned to EN via
// tests/unit/setup.ts) keeps asserting the same strings.
//
// NOT translated here (stay verbatim in both locales, per the glossary):
// the literal `iac_apply` action tag on the rail meta line (a raw enum
// value), PR numbers, SHAs, and the `title={d.action}` hover tooltip.
export const decisions = {
  en: {
    // decision.ts — DecisionSummary row labels (decisionFields).
    'decisions.field.action': 'Action',
    'decisions.field.pullRequest': 'Pull request',
    'decisions.field.apply': 'Apply',
    'decisions.field.merge': 'Merge',
    'decisions.field.headSha': 'Head SHA',
    'decisions.field.approver': 'Approver',
    'decisions.field.when': 'When',
    // decision.ts — ACTION_LABEL (the Action row's value). The unrecognised-
    // action fallback (`clamp(action)`, raw enum) and the 'decision' default
    // stay untranslated in decision.ts itself — only these three get a label.
    // decision.ts — the Apply row's composed supersession value (distinct
    // from approval.ts's `shared.approve.supersededBy`, which is a link label
    // with a trailing arrow — this is a plain field value, no arrow).
    'decisions.field.apply.supersededBy': 'superseded by #{pr}',

    // Rail chrome. (was the rail's; the desk renders these now).

    // Search modal. (was the rail's; the desk renders these now).

    // Row face. (was the rail's; the desk renders these now).
    'decisions.row.githubLink.viewIssue': 'View issue →',
    'decisions.row.githubLink.viewPr': 'View PR →',

    // No_op headline meta. (was the rail's; the desk renders these now).

    // Observe-mode suppressed token. The {mode} label (was the rail's; the desk renders these now).
    // comes from the shared autonomy modeLabel (capability.mode.*.label).
    'decisions.autonomy.suppressed': 'not executed in {mode} mode',

    // Dry-run preview pill. (was the rail's; the desk renders these now).
    'decisions.dryRun.pill': 'dry run, not created on GitHub',

    // Lifecycle step fallback (no apply_status recorded). (was the rail's; the desk renders these now).

    // DecisionSummary.svelte.
    'decisions.summary.ariaLabel': 'Decision summary',
    'decisions.summary.label': 'Decision',
  },
  ja: {
    'decisions.field.action': 'アクション',
    'decisions.field.pullRequest': 'プルリクエスト',
    'decisions.field.apply': '適用',
    'decisions.field.merge': 'マージ',
    'decisions.field.headSha': 'HEAD SHA',
    'decisions.field.approver': '承認者',
    'decisions.field.when': '日時',
    'decisions.field.apply.supersededBy': '#{pr} に置き換え済み',



    'decisions.row.githubLink.viewIssue': 'Issue を見る →',
    'decisions.row.githubLink.viewPr': 'PR を見る →',


    'decisions.autonomy.suppressed': '「{mode}」モードのため実行されていません',

    'decisions.dryRun.pill': 'ドライラン（GitHub には未作成）',


    'decisions.summary.ariaLabel': '判断内容',
    'decisions.summary.label': '判断',
  },
};
