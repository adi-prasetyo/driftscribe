// timeline namespace — Timeline, TraceBadge, HistoricalBanner, and the
// labels.ts worker/tool/MCP labels. (Group.svelte's own chrome lives in
// `misc.group.*` — foundation-owned.) Keys prefixed `timeline.`.
//
// EN values are moved BYTE-FOR-BYTE from the code they used to live in (see
// the call sites in Timeline.svelte / TraceBadge.svelte / HistoricalBanner.svelte
// / labels.ts) — the EN catalog is the app's original inline text, so the
// unit-test suite (pinned to EN via tests/unit/setup.ts) keeps passing.
export const timeline = {
  en: {
    'timeline.group.coordinator': 'Coordinator reasoning',
    'timeline.group.tools': 'Tools & workers',
    'timeline.group.mcp': 'MCP traffic',

    // Self-documents why responses can feel slow (hover-help on the
    // coordinator group). See Timeline.svelte's COORDINATOR_HINT.
    'timeline.coordinatorHint':
      "Gemini's reasoning summaries are only returned by Vertex AI's 'global' " +
      'region, so this deployment routes inference there. Expect a little ' +
      'extra latency per turn. Under heavy load Vertex can also omit the ' +
      'summaries entirely, even though the coordinator still reasons; when ' +
      'that happens, a note appears here with the thinking-token count.',

    // Historical-empty state: two honest variants (directly-recorded vs. a
    // reasoning run whose trace just couldn't be loaded).
    'timeline.empty.directlyRecorded':
      'No reasoning timeline for this decision. It was recorded directly, not ' +
      'produced by an agent reasoning run.',
    'timeline.empty.notLoaded':
      "The reasoning timeline for this turn couldn't be loaded. The " +
      "coordinator's reasoning is stored separately from the conversation and " +
      'may be temporarily unavailable.',

    // "Reasoned but no summaries" note — {n} is the pre-formatted thinking-token
    // count (fmtNumber at the call site, locale-aware).
    'timeline.omittedNote':
      'The coordinator did reason on this turn ({n} thinking tokens), but ' +
      'Vertex AI omitted the reasoning summaries. Summaries are generated ' +
      'best-effort and can be dropped when the service is busy; the reply and ' +
      'tool calls are unaffected.',

    // Status vocabulary shared between TraceBadge's status pill and
    // Timeline's per-call tool_call/tool_result badge (same English word,
    // one canonical translation per the glossary).
    'timeline.status.pending': 'pending',
    'timeline.status.streaming': 'streaming',
    'timeline.status.complete': 'complete',
    'timeline.status.stalled': 'stalled · logs lagging',
    'timeline.status.error': 'error',
    'timeline.status.historical': 'historical',

    // Timeline's tool-call pair chrome.
    'timeline.pair.ok': 'ok',
    'timeline.pair.toolArgs': 'tool_args',
    'timeline.pair.resultPreview': 'result_preview',

    // Coordinator group's llm_usage row — the small caption label ahead of
    // the fmtTokens value (format.ts's own "{n} tok" suffix is separate).
    'timeline.usageLabel': 'tokens',

    // Tools/MCP subgroup meta.
    'timeline.subgroup.calls.one': '{n} call',
    'timeline.subgroup.calls.other': '{n} calls',
    'timeline.subgroup.docs': '{n} docs',
    'timeline.latencyMs': '{ms} ms',

    // TraceBadge — copy-to-clipboard affordance.
    'timeline.trace.copyTitle': 'click to copy trace id',
    'timeline.trace.copy': 'copy',
    'timeline.trace.copied': 'copied',

    // HistoricalBanner.
    'timeline.historicalBanner.label': 'viewing past reasoning',
    'timeline.historicalBanner.newChat': '← new chat',

    // labels.ts WORKER_LABELS, ported verbatim from the legacy single-file
    // renderer (agent/templates/transparency_legacy.html `_WORKER_LABELS`).
    // Drift workload.
    'timeline.worker.read_live_env_tool': 'Reader (drift)',
    'timeline.worker.patch_docs_tool': 'Docs (drift)',
    'timeline.worker.propose_rollback_tool': 'Rollback (drift) · HITL',
    // Upgrade workload.
    'timeline.worker.upgrade_read_dependencies_tool': 'Upgrade Reader',
    'timeline.worker.upgrade_propose_pr_tool': 'Upgrade Docs',
    'timeline.worker.upgrade_close_pr_tool': 'Upgrade Docs · close PR',
    'timeline.worker.upgrade_merge_pr_tool': 'Upgrade Docs · merge PR',
    // Provision workload.
    'timeline.worker.open_infra_pr_tool': 'Open infra PR',
    'timeline.worker.propose_adoption_tool': 'Adopt resource (import PR)',
    // Shared.
    'timeline.worker.notify_tool': 'Notifier',
    'timeline.worker.load_iac_plan_tool': 'IaC plan reader',
    // MCP — Google Developer Knowledge.
    'timeline.worker.developer_knowledge': 'Developer Knowledge MCP',
    'timeline.worker.answer_query': 'Developer Knowledge MCP · answer',
    'timeline.worker.search_documents': 'Developer Knowledge MCP · search',
    'timeline.worker.get_documents': 'Developer Knowledge MCP · get',
  },
  ja: {
    'timeline.group.coordinator': 'コーディネーターの推論',
    'timeline.group.tools': 'ツールとワーカー',
    'timeline.group.mcp': 'MCP 通信',

    'timeline.coordinatorHint':
      'Gemini の推論の要約は Vertex AI の「global」リージョンでのみ返されるため、' +
      'このデプロイでは推論をそちらにルーティングしています。そのため、ターンごとに' +
      '多少のレイテンシが発生します。負荷が高い場合、Vertex が要約を完全に省略する' +
      'ことがありますが、その場合でもコーディネーターは推論を行っています。そのような' +
      'ときは、思考トークン数とともにここに注記が表示されます。',

    'timeline.empty.directlyRecorded':
      'この判断には推論タイムラインがありません。エージェントによる推論ではなく、' +
      '判断として直接記録されたものです。',
    'timeline.empty.notLoaded':
      'このターンの推論タイムラインを読み込めませんでした。コーディネーターの推論は' +
      '会話とは別に保存されており、一時的に利用できない場合があります。',

    'timeline.omittedNote':
      'コーディネーターはこのターンで推論を行いました（思考トークン：{n}）。ただし、' +
      'Vertex AI が推論の要約を省略しました。要約はベストエフォートで生成されるため、' +
      'サービスが混雑していると省略されることがあります。返信やツール呼び出しへの' +
      '影響はありません。',

    'timeline.status.pending': '待機中',
    'timeline.status.streaming': 'リアルタイム',
    'timeline.status.complete': '完了',
    'timeline.status.stalled': '遅延中・ログ反映待ち',
    'timeline.status.error': 'エラー',
    'timeline.status.historical': '履歴',

    'timeline.usageLabel': 'トークン数',

    'timeline.pair.ok': 'OK',
    'timeline.pair.toolArgs': 'ツール引数',
    'timeline.pair.resultPreview': '結果プレビュー',

    'timeline.subgroup.calls.one': '{n}回の呼び出し',
    'timeline.subgroup.calls.other': '{n}回の呼び出し',
    'timeline.subgroup.docs': '{n}件のドキュメント',
    'timeline.latencyMs': '{ms} ms',

    'timeline.trace.copyTitle': 'クリックしてトレース ID をコピー',
    'timeline.trace.copy': 'コピー',
    'timeline.trace.copied': 'コピー済み',

    'timeline.historicalBanner.label': '過去の実行の推論を表示中',
    'timeline.historicalBanner.newChat': '← 新規チャット',

    'timeline.worker.read_live_env_tool': 'リーダー（ドリフト）',
    'timeline.worker.patch_docs_tool': 'ドキュメント（ドリフト）',
    'timeline.worker.propose_rollback_tool': 'ロールバック（ドリフト）・人による確認・承認',
    'timeline.worker.upgrade_read_dependencies_tool': 'アップグレードリーダー',
    'timeline.worker.upgrade_propose_pr_tool': 'アップグレードドキュメント',
    'timeline.worker.upgrade_close_pr_tool': 'アップグレードドキュメント・PR をクローズ',
    'timeline.worker.upgrade_merge_pr_tool': 'アップグレードドキュメント・PR をマージ',
    'timeline.worker.open_infra_pr_tool': 'インフラ PR を開く',
    'timeline.worker.propose_adoption_tool': 'リソースを IaC 管理に取り込む（取り込み PR）',
    'timeline.worker.notify_tool': '通知',
    'timeline.worker.load_iac_plan_tool': 'IaC プランリーダー',
    'timeline.worker.developer_knowledge': 'Developer Knowledge MCP',
    'timeline.worker.answer_query': 'Developer Knowledge MCP・回答',
    'timeline.worker.search_documents': 'Developer Knowledge MCP・検索',
    'timeline.worker.get_documents': 'Developer Knowledge MCP・取得',
  },
};
