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
  infraDiagram: 'infra-diagram',
  infraDriftBadge: 'infra-drift-badge',
  infraRefresh: 'infra-refresh',
} as const;

export const TRACE_ID = 'abcdef0123456789abcdef0123456789';

// A historical iac_apply trace: produced by the HITL approval handler, NOT the
// reasoning loop — so its /trace has a decision doc but ZERO `event` entries.
// Exercises the historical-label + DecisionSummary + empty-timeline path.
export const IAC_TRACE_ID = '88908d9b2d244dd6b8f952a6d799851f';

// One SSE chat turn: meta → thought → tool_call → tool_result → done.
// NB: mcp_call is intentionally ABSENT from the stream (it only arrives via the
// /trace backfill — see TRACE_RESPONSE).
export function sseBody(traceId = TRACE_ID): string {
  const frames = [
    `event: meta\ndata: ${JSON.stringify({ trace_id: traceId })}`,
    `data: ${JSON.stringify({ event: 'llm_thought', trace_id: traceId, workload: 'drift', thought_text: 'Comparing live env to the ops contract.' })}`,
    `data: ${JSON.stringify({ event: 'tool_call', trace_id: traceId, workload: 'drift', tool_name: 'read_live_env_tool', tool_args: { service: 'payment-demo' } })}`,
    `data: ${JSON.stringify({ event: 'tool_result', trace_id: traceId, workload: 'drift', tool_name: 'read_live_env_tool', result_preview: '{"drift":3}', result_ok: true })}`,
    `event: done\ndata: ${JSON.stringify({ reply: 'Found 3 drifted env vars.', tool_calls: ['read_live_env_tool'], session_id: 's1' })}`,
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
    ],
  };
}
