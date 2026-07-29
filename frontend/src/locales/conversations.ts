// conversations namespace — ConversationsRail, ConversationThread, the
// conversations.ts date buckets, and rail.ts labels/pluralization.
export const conversations = {
  en: {
    // ConversationsRail.svelte — rail header. One key serves both the <aside>
    // landmark aria-label and the visible eyebrow heading (identical text).
    'conversations.rail.title': 'Conversations',
    'conversations.rail.helpAriaLabel': 'About conversations',
    'conversations.rail.helpText':
      'Your chats are saved here, so you can reopen any thread and pick up where you left off. ' +
      'One crew holds a conversation at a time, and it changes hands only when a crew ' +
      'suggests bringing in another and you confirm. ' +
      "Crews can also look back at redacted snippets of each other's recent chats as shared team memory.",
    'conversations.rail.empty':
      'No conversations yet. ' +
      'Chats you start are saved here, so you can reopen any thread and keep going.',
    'conversations.rail.searchOpen': 'Search chats ({n}) →',

    // Day-bucket headings. conversations.ts (lib) returns semantic ids
    // ('today'|'yesterday'|'older'); the component maps id → label here so the
    // bucket function itself stays pure/locale-free.
    'conversations.bucket.today': 'Today',
    'conversations.bucket.yesterday': 'Yesterday',
    'conversations.bucket.older': 'Older',

    // Pluralized "N messages" meta line — counts the OPERATOR's own turns, which
    // the backend now records directly (see turnsLabel in the component).
    'conversations.messageCount.one': '1 message',
    'conversations.messageCount.other': '{n} messages',

    // Search modal.
    'conversations.search.title': 'Search chats',
    'conversations.search.inputAriaLabel': 'Search chats by title or crew',
    'conversations.search.placeholder': 'Search by title or crew…',
    'conversations.search.count': '{matched} of {total}',
    'conversations.search.noMatch': 'No chats match “{query}”.',

    // ConversationThread.svelte
    'conversations.thread.ariaLabel': 'Conversation history',
    'conversations.thread.you': 'You',
    'conversations.thread.generatingReply': 'Generating reply…',
    'conversations.thread.viewReasoningAria': 'View reasoning for turn {n}',
    'conversations.thread.reviewPr': 'Review PR #{n} →',
    // Server-authored transition rows. Both name BOTH crews: a row that said
    // only "Provision joined" leaves the reader working out who left.
    'conversations.thread.crewChange': '{from} handed this conversation to {to}',
    'conversations.thread.crewChangeAria': '{from} handed this conversation to {to}',
    'conversations.thread.handoffDeclined': 'Staying with {from} — {to} was not brought in',
    'conversations.thread.handoffDeclinedAria':
      'Handoff to {to} declined; staying with {from}',

    // HandoffChip.svelte — the confirmation a crew's suggestion is waiting on.
    'conversations.handoff.ariaLabel': 'Crew suggestion awaiting your confirmation',
    'conversations.handoff.title': '{from} suggests bringing in {to}',
    // Confirming RUNS the joining crew immediately, so the button says what
    // will happen rather than a bare "OK".
    'conversations.handoff.confirm': 'Bring in {to}',
    'conversations.handoff.decline': 'Not now',
    'conversations.handoff.working': 'Bringing in {to}…',
    // Refusals. Every one of them leaves the conversation exactly as it was,
    // so the recovery is always the same: ask again.
    'conversations.handoff.error.gone':
      'This suggestion is no longer available. Ask again and the crew can offer it fresh.',
    'conversations.handoff.error.expired':
      'This suggestion expired. Ask again and the crew can offer it fresh.',
    'conversations.handoff.error.busy':
      'This conversation already has a turn running. Try again once it finishes.',
    'conversations.handoff.error.failed':
      "Could not bring in {to}. Nothing changed — you're still with {from}.",
    // Distinct from the above on purpose: the handover DID happen, and only the
    // joining crew's first reply failed. Saying "nothing changed" here would be
    // false, and would leave the operator expecting {from} to answer next.
    'conversations.handoff.error.joinFailed':
      '{to} has taken over this conversation, but its first reply failed. Ask again and {to} will pick it up.',
  },
  ja: {
    'conversations.rail.title': 'チャット履歴',
    'conversations.rail.helpAriaLabel': 'チャットについて',
    'conversations.rail.helpText':
      'チャットはここに保存されるので、いつでもスレッドを再開し、続きから進められます。' +
      '1つの会話を担当するエージェントチームは常に1つで、担当が変わるのは、' +
      '別のチームに引き継ぐようエージェントチームが提案し、あなたが承認したときだけです。' +
      'エージェントチームは、共有メモリとして、他のエージェントチームの最近のチャットから' +
      '一部を伏せた抜粋を参照することもあります。',
    'conversations.rail.empty':
      'チャットはまだありません。' +
      'ここで始めたチャットは保存されるので、いつでもスレッドを再開して続けられます。',
    'conversations.rail.searchOpen': 'チャットを検索（{n}件）→',

    'conversations.bucket.today': '今日',
    'conversations.bucket.yesterday': '昨日',
    'conversations.bucket.older': 'それ以前',

    'conversations.messageCount.one': '{n}件のメッセージ',
    'conversations.messageCount.other': '{n}件のメッセージ',

    'conversations.search.title': 'チャットを検索',
    'conversations.search.inputAriaLabel': 'タイトルまたはエージェントチームでチャットを検索',
    'conversations.search.placeholder': 'タイトルまたはエージェントチームで検索…',
    'conversations.search.count': '{total}件中{matched}件',
    'conversations.search.noMatch': '「{query}」に一致するチャットはありません。',

    'conversations.thread.ariaLabel': '会話の履歴',
    'conversations.thread.you': 'あなた',
    'conversations.thread.generatingReply': '返信を生成中…',
    'conversations.thread.viewReasoningAria': '第{n}ターンの推論を見る',
    'conversations.thread.reviewPr': 'PR #{n} を確認 →',
    'conversations.thread.crewChange': '{from}がこの会話を{to}に引き継ぎました',
    'conversations.thread.crewChangeAria': '{from}がこの会話を{to}に引き継ぎました',
    'conversations.thread.handoffDeclined': '{to}は加わらず、{from}が担当を継続します',
    'conversations.thread.handoffDeclinedAria':
      '{to}への引き継ぎは見送られ、{from}が担当を継続します',

    'conversations.handoff.ariaLabel': '承認待ちのエージェントチームからの提案',
    'conversations.handoff.title': '{from}が{to}への引き継ぎを提案しています',
    'conversations.handoff.confirm': '{to}に引き継ぐ',
    'conversations.handoff.decline': '今はしない',
    'conversations.handoff.working': '{to}に引き継いでいます…',
    'conversations.handoff.error.gone':
      'この提案は利用できなくなりました。もう一度尋ねると、エージェントチームが改めて提案できます。',
    'conversations.handoff.error.expired':
      'この提案は期限切れです。もう一度尋ねると、エージェントチームが改めて提案できます。',
    'conversations.handoff.error.busy':
      'この会話では現在ターンが実行中です。完了してからもう一度お試しください。',
    'conversations.handoff.error.failed':
      '{to}への引き継ぎに失敗しました。変更は何も行われておらず、引き続き{from}が担当します。',
    'conversations.handoff.error.joinFailed':
      '{to}がこの会話を引き継ぎましたが、最初の返信に失敗しました。もう一度尋ねると、{to}が対応します。',
  },
};
