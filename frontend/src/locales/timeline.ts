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

    // Self-documents why responses can feel slow (hover-help on the
    // coordinator group). See Timeline.svelte's COORDINATOR_HINT.

    // The reasoning group's own empty line (plan Task 11). The generic
    // "No coordinator reasoning yet." reports an absence; on the page's primary
    // group that wastes the chance to say what produces one. Tools/MCP keep the
    // generic copy — they are demoted drawers now, and guidance there would just
    // be more text to read.
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
    'timeline.status.error': 'error',

    // Timeline's tool-call pair chrome.
    'timeline.pair.ok': 'ok',
    'timeline.pair.toolArgs': 'tool_args',
    'timeline.pair.resultPreview': 'result_preview',
    'timeline.pair.emptyPreview': '(empty)',

    // Coordinator group's llm_usage row — the small caption label ahead of
    // the fmtTokens value (format.ts's own "{n} tok" suffix is separate).

    // Tools/MCP subgroup meta.
    'timeline.latencyMs': '{ms} ms',

    // TraceBadge — copy-to-clipboard affordance.

    // HistoricalBanner.

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

    // --- Inline reasoning disclosure (ds-jns) ---
    // The collapsed line under each crew reply, and the expanded detail it
    // opens. Keys are `disclosure.*` rather than `timeline.disclosure.*`: the
    // desk decision record (PR 2) mounts the same components off the chat view,
    // so the namespace names the SURFACE, not where it first appeared.
    'disclosure.showReasoning': 'View reasoning',
    // Shown while the stream is live and no thought summary has arrived yet.
    // Vertex can omit summaries entirely, so this can be the whole run's label.
    'disclosure.thinking': 'Thinking…',
    'disclosure.toggleAria': 'Reasoning for this reply',
    // The run itself failed. Distinct from loadError below: whatever streamed
    // before the failure is still shown.
    'disclosure.streamError': 'The run was interrupted',
    'disclosure.loading': 'Loading reasoning…',
    'disclosure.loadError': "This reasoning couldn't be loaded.",
    'disclosure.retry': 'Try again',
    'disclosure.copyLink': 'Copy link',
    'disclosure.copied': 'Copied',
    'disclosure.copyTitle': 'Copy a link to this reasoning',
    'disclosure.traceLabel': 'Trace',
    // Fail-soft: the trace is complete, only the PR description is missing.
    // Run accounting. Both rendered in the deleted page-level Timeline and
    // nowhere else, so ds-jns Task 3.3 took them off the only surface named
    // "transparency" — the tokens a run spent, and how much grounding its MCP
    // calls actually consulted.
    'disclosure.tokens': '{tokens} spent',
    'disclosure.docs': '{n} docs',
    'disclosure.prBodyMissing': "The PR description couldn't be loaded.",
    'disclosure.mcpLabel': 'MCP',
  },
  ja: {


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
    'timeline.status.error': 'エラー',


    'timeline.pair.ok': 'OK',
    'timeline.pair.toolArgs': 'ツール引数',
    'timeline.pair.resultPreview': '結果プレビュー',
    'timeline.pair.emptyPreview': '（空）',

    'timeline.latencyMs': '{ms} ms',



    'timeline.worker.read_live_env_tool': 'リーダー（ドリフト）',
    'timeline.worker.patch_docs_tool': 'ドキュメント（ドリフト）',
    'timeline.worker.propose_rollback_tool': 'ロールバック（ドリフト、人による確認・承認）',
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

    // --- インライン推論ディスクロージャー (ds-jns) ---
    'disclosure.showReasoning': '推論を表示',
    'disclosure.thinking': '推論中…',
    'disclosure.toggleAria': 'この返信の推論',
    'disclosure.streamError': '実行が中断されました',
    'disclosure.loading': '推論を読み込んでいます…',
    'disclosure.loadError': 'この推論を読み込めませんでした。',
    'disclosure.retry': '再試行',
    'disclosure.copyLink': 'リンクをコピー',
    'disclosure.copied': 'コピーしました',
    'disclosure.copyTitle': 'この推論へのリンクをコピー',
    'disclosure.traceLabel': 'トレース',
    'disclosure.tokens': '{tokens} 消費',
    'disclosure.docs': 'ドキュメント {n} 件',
    'disclosure.prBodyMissing': 'PR の説明を読み込めませんでした。',
    'disclosure.mcpLabel': 'MCP',
  },
};
