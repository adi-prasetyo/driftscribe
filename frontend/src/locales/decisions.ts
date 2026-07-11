// decisions namespace — DecisionsRail, DecisionSummary, and decision.ts row labels.
//
// EN values are moved BYTE-FOR-BYTE from the code they used to live in (see
// the call sites in components/DecisionsRail.svelte, components/
// DecisionSummary.svelte, and lib/decision.ts) — the EN catalog is the app's
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
    'decisions.action.iacApply': 'Infra apply',
    'decisions.action.rollback': 'Rollback',
    'decisions.action.recheck': 'Re-check',
    // decision.ts — the Apply row's composed supersession value (distinct
    // from approval.ts's `shared.approve.supersededBy`, which is a link label
    // with a trailing arrow — this is a plain field value, no arrow).
    'decisions.field.apply.supersededBy': 'superseded by #{pr}',

    // DecisionsRail.svelte — rail chrome.
    'decisions.rail.title': 'Past decisions',
    'decisions.rail.prHint.ariaLabel': 'About these pull-request numbers',
    'decisions.rail.prHint.text':
      'These are real GitHub pull-request numbers, and only infrastructure ' +
      'changes show up here. Pull requests for UI, docs, and other code are ' +
      'left out, so the numbers can skip values.',
    'decisions.rail.empty': 'No decisions yet.',
    'decisions.rail.searchOpen': 'Search decisions ({n}) →',

    // DecisionsRail.svelte — search modal.
    'decisions.search.title': 'Search decisions',
    'decisions.search.inputAriaLabel': 'Search decisions by PR, crew, action, or status',
    'decisions.search.placeholder': 'Search by PR, title, crew, action, or status…',
    'decisions.search.count': '{shown} of {total}',
    'decisions.search.noMatch': 'No decisions match “{query}”.',

    // DecisionsRail.svelte — row face.
    'decisions.row.prLink': 'PR #{n} →',
    'decisions.row.githubLink.viewIssue': 'View issue →',
    'decisions.row.githubLink.viewPr': 'View PR →',
    'decisions.row.githubLink.viewOnGithub': 'View on GitHub →',
    'decisions.row.approve': 'Approve →',
    'decisions.row.expired': 'expired',

    // DecisionsRail.svelte — no_op headline meta.
    'decisions.noOp.lead': 'Checked · all clear',
    'decisions.noOp.helpAriaLabel': 'What “No action needed” means',

    // DecisionsRail.svelte — Observe-mode suppressed token.
    'decisions.autonomy.suppressed': 'not executed in {mode} mode',
    'decisions.autonomy.observeLabel': 'Observe',

    // DecisionsRail.svelte — dry-run preview pill.
    'decisions.dryRun.pill': 'dry run, not created on GitHub',

    // DecisionsRail.svelte — lifecycle step fallback (no apply_status recorded).
    'decisions.lifecycle.statusNotRecorded': 'status not recorded',

    // DecisionSummary.svelte.
    'decisions.summary.ariaLabel': 'Decision summary',
    'decisions.summary.label': 'Decision',
  },
  ja: {
    'decisions.field.action': '操作',
    'decisions.field.pullRequest': 'プルリクエスト',
    'decisions.field.apply': '適用',
    'decisions.field.merge': 'マージ',
    'decisions.field.headSha': 'HEAD SHA',
    'decisions.field.approver': '承認者',
    'decisions.field.when': '日時',
    'decisions.action.iacApply': 'IaC 適用',
    'decisions.action.rollback': 'ロールバック',
    'decisions.action.recheck': '再チェック',
    'decisions.field.apply.supersededBy': '#{pr} に置き換え済み',

    'decisions.rail.title': '判断履歴',
    'decisions.rail.prHint.ariaLabel': 'このプルリクエスト番号について',
    'decisions.rail.prHint.text':
      'これらは実際の GitHub のプルリクエスト番号で、ここにはインフラの変更のみが' +
      '表示されます。UI やドキュメント、その他のコードのプルリクエストは対象外のため、' +
      '番号が飛ぶことがあります。',
    'decisions.rail.empty': 'まだ判断はありません。',
    'decisions.rail.searchOpen': '判断を検索（{n}件）→',

    'decisions.search.title': '判断を検索',
    'decisions.search.inputAriaLabel': 'PR、エージェントチーム、操作、状態で判断を検索',
    'decisions.search.placeholder': 'PR、タイトル、エージェントチーム、操作、状態で検索…',
    'decisions.search.count': '{total}件中{shown}件',
    'decisions.search.noMatch': '「{query}」に一致する判断はありません。',

    'decisions.row.prLink': 'PR #{n} →',
    'decisions.row.githubLink.viewIssue': 'Issue を見る →',
    'decisions.row.githubLink.viewPr': 'PR を見る →',
    'decisions.row.githubLink.viewOnGithub': 'GitHub で見る →',
    'decisions.row.approve': '承認 →',
    'decisions.row.expired': '期限切れ',

    'decisions.noOp.lead': 'チェック済み・問題なし',
    'decisions.noOp.helpAriaLabel': '「対応不要」の意味',

    'decisions.autonomy.suppressed': '「{mode}」モードのため実行されていません',
    'decisions.autonomy.observeLabel': '監視のみ',

    'decisions.dryRun.pill': 'ドライラン（GitHub には未作成）',

    'decisions.lifecycle.statusNotRecorded': '状態未記録',

    'decisions.summary.ariaLabel': '判断内容',
    'decisions.summary.label': '判断',
  },
};
