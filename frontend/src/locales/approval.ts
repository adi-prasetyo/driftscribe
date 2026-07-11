// approval namespace — ApprovalCta, IacApprovalCta, PrBodyDisclosure,
// approval.ts CTA labels, and format.ts iac_apply status labels/help.
// Filled by the i18n fan-out.
export const approval = {
  en: {
    // ApprovalCta — the inline rollback HITL CTA.
    'approval.rollbackCta.title': 'HITL approval required',
    'approval.rollbackCta.approve': 'Approve →',

    // IacApprovalCta — the first-authoring infra-apply CTA.
    'approval.iacCta.title': 'Infra apply needs your approval (PR #{pr})',
    'approval.iacCta.reviewApprove': 'Review & approve →',
    'approval.iacCta.cageNote':
      'Before anything applies, this change must pass the self-protection ' +
      'denylist and get your explicit approval. The denylist blocks any ' +
      'DriftScribe control-plane changes, any IAM changes, and any deletes, ' +
      'replacements, or un-managing.',

    // PrBodyDisclosure — the "what this change did" PR-body panel chrome. The
    // rendered PR body itself is agent-authored pass-through, never translated.
    // Trailing space matches the original template's literal space before the
    // hint span.
    'approval.prBody.summary': 'What this change did ',
    'approval.prBody.hint': '(from the PR)',
    'approval.prBody.ariaLabel': 'Pull request description',
    'approval.prBody.truncated': 'Description truncated. Open the PR on GitHub for the full text.',
    'approval.prBody.tableScrollable': 'Table (scrollable)',
  },
  ja: {
    'approval.rollbackCta.title': '人による確認・承認が必要です',
    'approval.rollbackCta.approve': '承認 →',

    'approval.iacCta.title': 'インフラの適用には承認が必要です（PR #{pr}）',
    'approval.iacCta.reviewApprove': '確認して承認 →',
    'approval.iacCta.cageNote':
      '適用の前に、この変更は自己防御のための拒否リストを通過し、あなたの明示的な承認を' +
      '得る必要があります。拒否リストは、DriftScribe 自身のコントロールプレーンの変更、' +
      'アクセス権（IAM）の変更、リソースの削除・置換、IaC 管理から外すことのいずれも' +
      'ブロックします。',

    'approval.prBody.summary': 'この変更で行われたこと',
    'approval.prBody.hint': '（PR より）',
    'approval.prBody.ariaLabel': 'PR 本文',
    'approval.prBody.truncated': '説明は省略されています。全文は GitHub の PR で確認してください。',
    'approval.prBody.tableScrollable': '表（横スクロール可）',
  },
};
