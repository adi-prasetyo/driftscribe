// header namespace — App.svelte: the header (brand/title + actions) AND the
// rest of App.svelte's own body strings (chat-turn error copy, the chat-area
// landmark, and the two lib helpers App.svelte drives — autonomyStore's
// autonomyNoteFor and workloads' askAboutPrPrefill/initialChatPrefill).
export const header = {
  en: {
    // Brand mark — "DriftScribe" itself is a proper noun and stays untranslated
    // in the <h1>/<a> text; only the surrounding copy is keyed here.
    'header.brand.ariaLabel': 'DriftScribe — go to home',
    // Suffix rendered right after the literal "DriftScribe" text node (note the
    // leading ". " — the two concatenate into one sentence).
    'header.brand.tagline': '. The agent proposes, you approve.',
    'header.tourButton': 'Tour',
    'header.chatArea.ariaLabel': 'Chat and reasoning timeline',

    // submitChat's terminal error copy (network/HTTP/stream failures).
    'header.chatError.network': 'Network error contacting the coordinator.',
    'header.chatError.rateLimit':
      'Rate limit reached. The demo allows a few chat runs per minute per visitor. ' +
      'Please wait a moment and try again.',
    'header.chatError.requestFailed': 'Request failed ({status}).',
    'header.chatError.malformed': 'Malformed response.',
    'header.chatError.coordinatorError': 'The coordinator returned an error.',
    'header.chatError.streamInterrupted':
      'The reasoning stream was interrupted. Showing the recovered reasoning.',
    'header.chatError.streamEnded':
      'The reasoning stream ended before a final reply arrived.',

    // autonomyStore.ts — autonomyNoteFor's 3 sentences (CapabilityCard note).
    'header.autonomyNote.readError':
      'Autonomy state could not be read. The effective mode is Observe ' +
      '(failing closed) until the dial can be read again.',
    'header.autonomyNote.observe':
      'The autonomy dial is currently set to Observe. Tools that open pull ' +
      'requests, issues, or approvals, and anything that merges or applies, ' +
      'are disabled until you raise the dial.',
    'header.autonomyNote.propose':
      'The autonomy dial is currently set to Propose. Pull requests and issues ' +
      'are enabled; anything that merges or applies is disabled until you raise the dial.',

    // workloads.ts — askAboutPrPrefill (the ?ask_pr composer prefill).
    'header.prefill.askPr':
      "I'm reviewing infrastructure change PR #{pr} before deciding on it. " +
      'Load its plan and explain what it would change in plain language.',
  },
  ja: {
    'header.brand.ariaLabel': 'DriftScribe のホームへ移動',
    'header.brand.tagline': '：エージェントが提案し、あなたが承認します。',
    'header.tourButton': 'ツアー',
    'header.chatArea.ariaLabel': 'チャットと推論のタイムライン',

    'header.chatError.network': 'コーディネーターへの接続でネットワークエラーが発生しました。',
    'header.chatError.rateLimit':
      'リクエスト数の上限に達しました。デモでは訪問者ごとに1分あたり数回までチャットを' +
      '実行できます。しばらく待ってから再試行してください。',
    'header.chatError.requestFailed': 'リクエストに失敗しました（{status}）。',
    'header.chatError.malformed': '応答の形式が正しくありません。',
    'header.chatError.coordinatorError': 'コーディネーターがエラーを返しました。',
    'header.chatError.streamInterrupted':
      '推論のストリームが中断されました。復元された推論を表示しています。',
    'header.chatError.streamEnded':
      '最終的な返信が届く前に、推論のストリームが終了しました。',

    'header.autonomyNote.readError':
      '自律動作レベルの状態を読み込めませんでした。状態を再び読み込めるように' +
      'なるまで、有効なモードは監視のみ（フェイルクローズ）です。',
    'header.autonomyNote.observe':
      '自律動作レベルは現在「監視のみ」に設定されています。プルリクエストや ' +
      'Issue、承認リクエストを作成するツール、およびマージや適用を行うものはすべて、' +
      '自律動作レベルを上げるまで無効です。',
    'header.autonomyNote.propose':
      '自律動作レベルは現在「提案」に設定されています。プルリクエストと Issue ' +
      'の作成は有効ですが、マージや適用を行うものは、自律動作レベルを上げるまで無効です。',

    'header.prefill.askPr':
      'インフラ変更の PR #{pr} について判断する前に、内容を確認したいです。IaC プランを' +
      '読み込み、何が変更されるのかを平易な言葉で説明してください。',
  },
};
