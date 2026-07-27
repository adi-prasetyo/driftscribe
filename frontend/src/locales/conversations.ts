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
      'Each conversation stays with the crew that started it. ' +
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

    // Pluralized "N messages" meta line — counts the OPERATOR's own turns (see
    // turnsLabel's ceil(turn_count/2) note in the component).
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
  },
  ja: {
    'conversations.rail.title': 'チャット履歴',
    'conversations.rail.helpAriaLabel': 'チャットについて',
    'conversations.rail.helpText':
      'チャットはここに保存されるので、いつでもスレッドを再開し、続きから進められます。' +
      '各会話は、開始時のエージェントチームに固定されます。' +
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
  },
};
