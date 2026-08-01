// misc namespace — HelpHint, Modal, ReplyPending, DemoNoticeBell,
// HistoricalBanner, CrewGlyph and other small shared components.
// Filled by the i18n fan-out.
export const misc = {
  en: {
    // HelpHint — own chrome only; `text`/`label` are caller-owned props and
    // are NOT translated here.
    'misc.helpHint.explainStatus': 'Explain this status',
    'misc.helpHint.explainLabeledStatus': 'Explain the “{label}” status',
    // Modal — `title` is caller-owned.
    'misc.modal.close': 'Close',
    // DemoNoticeBell — the public judging-window notice.
    'misc.demoBell.ariaLabel': 'Live sandbox notice',
    'misc.demoBell.ariaLabelUnread': 'Live sandbox notice, 1 unread',
    'misc.demoBell.lead': 'This is a live sandbox.',
    'misc.demoBell.body':
      'Ask a crew to investigate drift, propose a fix, or roll back the payment-demo service and watch it happen. Everything you can reach from here is a demo fixture, so nothing you do lands on real infrastructure.',
    'misc.demoBell.gotIt': 'Got it',
    // ReplyPending + FinalResponse hero chrome. The reply body itself is
    // backend/LLM content and is NOT translated.
    'misc.coordinatorReply.label': 'Coordinator reply',
    'misc.replyPending.sr': 'Generating the coordinator’s reply…',
    'misc.finalResponse.error': 'Error',
    // Group — the generic reasoning-group empty state. `title` is caller-owned
    // (lowercased by the component under EN only, matching the prior inline
    // `title.toLowerCase()`; localized JA titles pass through unchanged).
    'misc.group.emptyState': 'No {title} yet.',
  },
  ja: {
    'misc.helpHint.explainStatus': 'このステータスの説明',
    'misc.helpHint.explainLabeledStatus': '「{label}」ステータスの説明',
    'misc.modal.close': '閉じる',
    'misc.demoBell.ariaLabel': '稼働中のサンドボックスのお知らせ',
    'misc.demoBell.ariaLabelUnread': '稼働中のサンドボックスのお知らせ、未読1件',
    'misc.demoBell.lead': 'これは実際に操作できるサンドボックス環境です。',
    'misc.demoBell.body':
      'エージェントチームにドリフトの調査、修正の提案、payment-demo サービスのロールバックを依頼すると、その場で実行される様子を確認できます。ここから操作できるものはすべてデモ用のフィクスチャなので、実際のインフラに変更が及ぶことはありません。',
    'misc.demoBell.gotIt': 'わかりました',
    'misc.coordinatorReply.label': 'コーディネーターの返信',
    'misc.replyPending.sr': 'コーディネーターの返信を生成しています…',
    'misc.finalResponse.error': 'エラー',
    'misc.group.emptyState': '「{title}」はまだありません。',
  },
};
