// Shared canned data + selector constants for the mock-Playwright smoke. The
// smoke boots the REAL FastAPI app (serving the built shell + /static) and mocks
// the JSON/SSE data endpoints with page.route, so it stands in for the
// dispatch-only cloud e2e (tests/e2e/ui) without a deployed coordinator.

// Selector contract — kept here so the smoke and the deployed e2e spec agree.
export const TESTIDS = {
  chatPrompt: 'chat-prompt',
  chatSubmit: 'chat-submit',
  finalResponse: 'final-response',
  replyPending: 'reply-pending',
  pastDecisionsPane: 'past-decisions-pane',
  pastDecisionItem: 'past-decision-item',
  openTraceButton: 'open-trace-button',
  historicalBanner: 'historical-banner',
  infraPanel: 'infra-panel',
  infraToggle: 'infra-toggle',
  infraCards: 'infra-cards',
  infraOther: 'infra-other',
  infraOtherCards: 'infra-other-cards',
  infraDriftBadge: 'infra-drift-badge',
  infraRefresh: 'infra-refresh',
  conversationsPane: 'conversations-pane',
  conversationOpen: 'conversation-open',
  conversationThread: 'conversation-thread',
  threadTyping: 'thread-typing',
} as const;

export const TRACE_ID = 'abcdef0123456789abcdef0123456789';

// A persisted multi-turn conversation for the resume-from-rail smoke (P2).
export const CONVERSATION_ID = 'conv-smoke-0001';

// GET /conversations — the history rail's metadata list (no turns embedded).
export function conversationsListResponse() {
  return {
    conversations: [
      {
        conversation_id: CONVERSATION_ID,
        workload: 'explore',
        title: 'prior chat about drift',
        created_at: '2026-06-27T10:00:00Z',
        updated_at: '2026-06-27T10:05:00Z',
        turn_count: 2,
        last_trace_id: TRACE_ID,
      },
    ],
  };
}

// GET /conversations/{id} — the full ordered turns used to rehydrate the thread.
export function conversationDetailResponse() {
  return {
    conversation_id: CONVERSATION_ID,
    workload: 'explore',
    title: 'prior chat about drift',
    created_at: '2026-06-27T10:00:00Z',
    updated_at: '2026-06-27T10:05:00Z',
    turn_count: 2,
    last_trace_id: TRACE_ID,
    turns: [
      { seq: 0, role: 'user', text: 'what changed on payment-demo?', workload: 'explore' },
      {
        seq: 1,
        role: 'crew',
        text: 'the env var EXTRA drifted from the contract',
        workload: 'explore',
        trace_id: TRACE_ID,
      },
    ],
  };
}

// A historical iac_apply trace: produced by the HITL approval handler, NOT the
// reasoning loop — so its /trace has a decision doc but ZERO `event` entries.
// Exercises the historical-label + DecisionSummary + empty-timeline path.
export const IAC_TRACE_ID = '88908d9b2d244dd6b8f952a6d799851f';

// d-drift-1's trace_id (see decisionsResponse). Its /trace carries a decision
// doc with env diffs so the smoke can exercise the DriftDiffCard.
export const DRIFT_CARD_TRACE_ID = 'aa11bb22cc33dd44ee55ff6600112233';

// Distinctive raw secret values — the smoke asserts NONE of these appear in the
// DOM (the card redacts them; the rationale below never quotes them).
export const SECRET_TOKEN_VALUE_OLD = 'sk-old-DEADBEEF0000';
export const SECRET_TOKEN_VALUE_NEW = 'sk-new-CAFEBABE1111';
export const SECRET_URL_VALUE_OLD = 'https://admin:hunter2OLD@svc.internal/api';
export const SECRET_URL_VALUE_NEW = 'https://admin:s3cr3tNEW@svc.internal/api';

// One SSE chat turn: meta → thought → tool_call → tool_result → done.
// NB: mcp_call is intentionally ABSENT from the stream (it only arrives via the
// /trace backfill — see TRACE_RESPONSE).
//
// `opts.conversationId`: when set, the done frame echoes it — the chat-native
// path then settles the reply into the thread's crew bubble (the persisted
// path). Omit it to exercise the one-shot fallback (reply stays in the hero).
export function sseBody(traceId = TRACE_ID, opts: { conversationId?: string } = {}): string {
  const done: Record<string, unknown> = {
    reply: 'Found 3 drifted env vars.',
    tool_calls: ['read_live_env_tool'],
    session_id: 's1',
  };
  if (opts.conversationId) done.conversation_id = opts.conversationId;
  const frames = [
    `event: meta\ndata: ${JSON.stringify({ trace_id: traceId })}`,
    `data: ${JSON.stringify({ event: 'llm_thought', trace_id: traceId, workload: 'drift', thought_text: 'Comparing live env to the ops contract.' })}`,
    `data: ${JSON.stringify({ event: 'tool_call', trace_id: traceId, workload: 'drift', tool_name: 'read_live_env_tool', tool_args: { service: 'payment-demo' } })}`,
    `data: ${JSON.stringify({ event: 'tool_result', trace_id: traceId, workload: 'drift', tool_name: 'read_live_env_tool', result_preview: '{"drift":3}', result_ok: true })}`,
    `event: done\ndata: ${JSON.stringify(done)}`,
  ];
  return frames.join('\n\n') + '\n\n';
}

// /trace backfill: same events as the stream PLUS a side-channel mcp_call (which
// is how the MCP group gets populated — the stream never carries it).
export function traceResponse(traceId = TRACE_ID) {
  return {
    trace_id: traceId,
    events: [
      { event: 'llm_thought', trace_id: traceId, workload: 'drift', thought_text: 'Comparing live env to the ops contract.', insert_id: 'i1', timestamp: '2026-06-02T00:00:01Z' },
      { event: 'tool_call', trace_id: traceId, workload: 'drift', tool_name: 'read_live_env_tool', tool_args: { service: 'payment-demo' }, insert_id: 'i2', timestamp: '2026-06-02T00:00:02Z' },
      { event: 'tool_result', trace_id: traceId, workload: 'drift', tool_name: 'read_live_env_tool', result_preview: '{"drift":3}', result_ok: true, insert_id: 'i3', timestamp: '2026-06-02T00:00:03Z' },
      { event: 'mcp_call', trace_id: traceId, workload: 'drift', mcp_tool: 'search_documents', mcp_server: 'developer_knowledge', latency_ms: 120, doc_count: 2, insert_id: 'i4', timestamp: '2026-06-02T00:00:04Z' },
    ],
    decision: null,
    complete: true,
    fetched_from_cache: false,
  };
}

// /trace for the d-drift-1 drift_issue: a decision doc carrying env diffs.
// LOG_LEVEL is non-secret (values shown); API_TOKEN is secret by NAME; ENDPOINT
// is secret by VALUE (credentialed URL) despite a non-secret name. The secret
// raw values live ONLY in diffs[] (not the rationale), so the "no raw secret in
// DOM" assertion isolates the CARD's client-side redaction (PR 1's concern).
// The raw-rationale scrub is PR 2 (backend) and is not exercised here.
export function driftCardTraceResponse() {
  return {
    trace_id: DRIFT_CARD_TRACE_ID,
    events: [],
    decision: {
      decision_id: 'd-drift-1',
      trace_id: DRIFT_CARD_TRACE_ID,
      action: 'drift_issue',
      rationale: 'Three variables drifted from the ops contract; secret values are redacted in the table below.',
      github: { url: 'https://github.com/acme/ops/issues/99', dry_run: false },
      diffs: [
        { name: 'LOG_LEVEL', expected: 'info', live: 'debug', contract_status: 'present_allow_manual' },
        { name: 'API_TOKEN', expected: SECRET_TOKEN_VALUE_OLD, live: SECRET_TOKEN_VALUE_NEW, contract_status: 'present_disallow_manual' },
        { name: 'ENDPOINT', expected: SECRET_URL_VALUE_OLD, live: SECRET_URL_VALUE_NEW, contract_status: 'absent' },
      ],
    },
    complete: true,
  };
}

// The iac_apply decision doc (mirrors the live Firestore shape). Used both as a
// rail row and as the /trace decision for IAC_TRACE_ID.
export function iacDecision() {
  return {
    decision_id: 'd-iac',
    trace_id: IAC_TRACE_ID,
    action: 'iac_apply',
    pr_number: 47,
    apply_status: 'applied',
    merge_state: 'merged',
    approver: 'op@example.com',
    head_sha: '89f2d4e093f2fa15fab0d86b21c1e98d45845418',
    applied_at: '2026-05-31T08:27:45Z',
    created_at: '2026-05-31T08:27:45Z',
  };
}

// /trace for an iac_apply: a real decision doc, but NO reasoning events and
// complete:false (no final_response is ever emitted for this path). The UI must
// still label it "historical" (not "streaming") and render the DecisionSummary.
export function iacTraceResponse() {
  return {
    trace_id: IAC_TRACE_ID,
    events: [],
    decision: iacDecision(),
    complete: false,
  };
}

// GET /infra/graph — the resource-map DTO (build_graph shape). A Cloud Run group
// (one managed, one drift) + a counts-only secret group. totals.drift = 2 so the
// glanceable badge reads "2 drift"; the secret group carries NO name.
export function infraGraphResponse() {
  return {
    generated_at: '2026-06-03T00:00:00Z',
    project: 'driftscribe-hack-2026',
    caveat: 'CAI is eventually consistent — may lag a recent apply.',
    iac_snapshot_sha: 'cafef00d',
    degraded: false,
    degraded_reason: null,
    totals: { resources: 3, managed: 1, drift: 2 },
    groups: [
      {
        asset_type: 'run.googleapis.com/Service',
        label: 'Cloud Run service',
        adoptable: true,
        count: 2,
        managed: 1,
        drift: 1,
        sensitive: false,
        nodes: [
          { id: 'g0n0', label: 'payment-demo', asset_type: 'run.googleapis.com/Service', managed: true, location: 'asia-northeast1' },
          { id: 'g0n1', label: 'storefront', asset_type: 'run.googleapis.com/Service', managed: false, location: 'asia-northeast1' },
        ],
      },
      {
        asset_type: 'secretmanager.googleapis.com/Secret',
        label: 'Secret',
        count: 1,
        managed: 0,
        drift: 1,
        sensitive: true,
        nodes: [],
      },
    ],
    edges: [],
    truncated: { per_type_sample: 10 },
  };
}

// /decisions rail. Includes a rollback decision with a SAME-ORIGIN approval_url
// (built per-test against the page origin), an off-origin "malicious" one, and a
// historical iac_apply (no prose, no reasoning timeline).
export function decisionsResponse(origin: string) {
  return {
    decisions: [
      {
        decision_id: 'd-rollback',
        trace_id: TRACE_ID,
        action: 'rollback',
        created_at: '2026-06-02T00:00:00Z',
        approval: {
          approval_id: 'ap-1',
          approval_url: `${origin}/approvals/ap-1?t=tok`,
          expires_at: '2099-01-01T00:00:00Z',
        },
      },
      {
        decision_id: 'd-evil',
        trace_id: 'ffffffffffffffffffffffffffffffff',
        action: 'rollback',
        created_at: '2026-06-01T00:00:00Z',
        approval: {
          approval_id: 'ap-evil',
          approval_url: 'https://evil.example/approvals/ap-evil?t=tok',
          expires_at: '2099-01-01T00:00:00Z',
        },
      },
      iacDecision(),
      {
        decision_id: 'd-drift-1',
        trace_id: 'aa11bb22cc33dd44ee55ff6600112233',
        action: 'drift_issue',
        created_at: '2026-06-08T01:00:00+00:00',
        github: { url: 'https://github.com/acme/ops/issues/99', dry_run: false },
      },
      {
        decision_id: 'd-drift-evil',
        trace_id: 'bb11bb22cc33dd44ee55ff6600112233',
        action: 'drift_issue',
        created_at: '2026-06-08T01:01:00+00:00',
        github: { url: 'javascript:alert(document.cookie)', dry_run: false },
      },
    ],
  };
}
