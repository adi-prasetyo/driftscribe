// Shared view types for the operator UI. The per-event shapes live in sse.ts
// (stream) and timeline.ts (TraceEvent superset). This module adds the
// /decisions + /trace response shapes the components render.

import type { TraceEvent } from './timeline';

/** Approval sidecar on a rollback decision (GET /decisions → decision.approval). */
export interface DecisionApproval {
  approval_id?: string;
  /** Server-minted absolute URL (`{COORDINATOR_URL}/approvals/{id}?t=…`). Always
   *  routed through `safeApprovalHref` before it becomes an anchor href. */
  approval_url?: string;
  expires_at?: string | null;
  /** Serve-time join (Task 3.0b) of the LIVE approval doc's status, from
   *  `driftscribe_lib/approvals.py`'s `ApprovalStore._claim` transitions.
   *  Only `pending`/`used`/`denied` are ever written — `_claim` is the sole
   *  writer of the two terminal transitions and always passes exactly one of
   *  `"used"` or `"denied"` as `new_status`; an `"approved"` value never
   *  occurs despite an older code comment suggesting it. Absent when the
   *  approval doc could not be read (missing doc, or a soft-failed store
   *  read) — the row is served un-enriched rather than 500ing. */
  status?: 'pending' | 'used' | 'denied';
  /** The transition timestamp. `null`/absent means "resolved, but we don't
   *  know when". NEVER falls back to the decision's own `created_at` (that's
   *  proposal time, not resolution time) — treat `null` as genuinely unknown,
   *  not "just now".
   *
   *  Since ds-2mc this is written ONLY for a confirmed terminal outcome: a
   *  `denied` (terminal at the moment of the flip), or a `used` whose traffic
   *  shift was observed to succeed. A rollback that is still applying, failed,
   *  or whose outcome could not be confirmed carries `null` — as does any
   *  `used`/`denied` doc written before the field existed. */
  resolved_at?: string | null;
  /** What actually happened to a claimed rollback.
   *
   *  `status: 'used'` means only that the single-use credential was SPENT —
   *  the transactional flip is the anti-replay claim and by construction runs
   *  BEFORE the Cloud Run traffic shift, so it can never testify that the
   *  rollback happened. This field is what can:
   *
   *  - `claimed` / `applying` — in flight; outcome not yet established
   *  - `applied`  — confirmed success. The ONLY value that may render a seal.
   *  - `failed`   — definitely did not apply
   *  - `outcome_unknown` — the mutation may have landed and we could not
   *    confirm it (poll expiry, lost response). Must be rendered as
   *    unconfirmed, NEVER as failed; `/reconcile` resolves it later.
   *
   *  `null`/absent = outcome unknown, including for any rollback resolved
   *  before ds-2mc. Consumers must treat absence as unknown, never as
   *  success — that conflation is the bug this field exists to fix. */
  phase?: 'claimed' | 'applying' | 'applied' | 'failed' | 'outcome_unknown' | null;
  /** When the CURRENT `phase` was observed. Distinct from `resolved_at`, which
   *  exists only on a confirmed success — so it is the only timestamp available
   *  for ordering failed/unconfirmed rows against each other, and the only
   *  signal separating a rollback applying right now from one whose worker died
   *  mid-wait an hour ago. `null`/absent on pre-ds-2mc docs. */
  phase_at?: string | null;
}

/** The PR/issue side-channel on a drift/docs/upgrade decision
 *  (GET /decisions → decision.github). `url` is an absolute github.com URL or
 *  null (dry-run / no-op); always routed through `safeGithubHref` before href. */
export interface DecisionGithub {
  url?: string | null;
  dry_run?: boolean;
}

/** Mirrors agent/models.py:ContractStatus. The per-var verdict of the live env
 *  against ops-contract.yaml. Rendered as a status pill on the env-diff card. */
export type ContractStatus =
  | 'absent'
  | 'present_allow_manual'
  | 'present_disallow_manual'
  | 'match';

/** One env-var drift row (GET /trace → decision.diffs[]). Mirrors
 *  agent/models.py:EnvDiff. `expected`/`live` are RAW env-var values and may be
 *  secrets (the decision doc is unredacted) — never render them directly; route
 *  every value through `displayDiffValue` (lib/diff.ts). Only the fields the
 *  card renders are typed; the backend also ships debug_config_value /
 *  recent_pr_match, intentionally omitted (YAGNI). */
export interface EnvDiff {
  name: string;
  expected?: string | null;
  live?: string | null;
  contract_status?: ContractStatus | string;
}

/** One row in the past-decisions rail (GET /decisions). Open shape — only the
 *  fields the rail renders are typed; the rest flow through the index sig. */
export interface Decision extends Record<string, unknown> {
  decision_id: string;
  trace_id?: string;
  action: string;
  created_at?: string;
  approval?: DecisionApproval | null;
  github?: DecisionGithub | null;
  diffs?: EnvDiff[];
  // iac_apply rows: pr_number + head_sha are persisted; pr_title is the as-applied
  // GitHub PR title (write-time snapshot, absent on pre-backfill rows). The rail
  // renders PR # as a linked title, pr_title as the subtitle, head_sha in the meta.
  pr_number?: number;
  head_sha?: string;
  pr_title?: string;
  // iac_apply lifecycle status (applied / waiting_for_rebake / failed /
  // failed_state_suspect / ambiguous). The rail renders it as a meta-line token
  // and uses it to retire the stale "Review & approve →" CTA on superseded rows.
  apply_status?: string;
  // Set by the recovery runbook when a merged-but-stale iac_apply plan was
  // re-expressed in a NEW PR (that new PR carries the real `applied` row). Its
  // presence retires this row's actionable "Review & approve →" CTA and points
  // the operator at the superseding PR. Positive integer PR number.
  superseded_by_pr?: number;
  // iac_apply apply moment (ISO 8601), recorded when apply_status==="applied".
  // Distinct from created_at (the doc's last-activity time): a merge-only reconcile
  // re-records the merged outcome with a fresh created_at but carries the ORIGINAL
  // applied_at forward (agent/main.py _record_iac_decision). The rail shows an
  // "applied {date}" cue when the two diverge; the trace card's "When" prefers it.
  applied_at?: string;
  // iac_apply merge state (merged / failed / pending / n/a). May be reconciled
  // at serve time: when the PR was merged out-of-band, the coordinator promotes
  // a stale merge_state="failed" to "merged" and sets merge_reconciled (a
  // cosmetic marker — the SPA can note "confirmed on GitHub"). See GET /decisions
  // / /trace reconcile_merge_state.
  merge_state?: string;
  merge_reconciled?: boolean;
  // Autonomy dial fields (ClickOps item 11). Present on decisions created while
  // the dial is configured; absent on pre-dial decisions (stale-coordinator
  // fail-quiet: the rail renders nothing when absent).
  autonomy_mode?: string;
  suppressed_by_autonomy?: boolean;
}

/** One persisted turn in a multi-turn conversation (P2). Mirrors a Firestore
 *  `conversations/{id}/turns/{seq}` doc. `role` is the AUTHOR axis — `"user"`
 *  for the operator's prompt, `"crew"` for the agent reply (NOT the ADK
 *  `model` role; the backend stores the human-facing label). `text` is rendered
 *  as ESCAPED PLAIN TEXT in the thread (deliberate XSS stance — see the chat
 *  reply-plain-text decision); never route it through a Markdown renderer. */
export interface ConversationTurn {
  seq: number;
  role: 'user' | 'crew' | string;
  text: string;
  workload?: string;
  trace_id?: string | null;
  created_at?: string;
  // Crew turns only: present when that turn opened an infrastructure PR.
  iac_pr?: { pr_number: number; pr_url: string } | null;
  tool_calls?: string[];
  // Live/optimistic turns ONLY — the backend never sets these. Drive the
  // chat-native live exchange (App.svelte `displayTurns`): `optimistic` marks a
  // transient turn not yet persisted into the thread (its action links are
  // suppressed until it settles), and `pending` marks the crew turn while the
  // reply is still streaming (renders a typing indicator instead of text).
  optimistic?: boolean;
  pending?: boolean;
}

/** One conversation's metadata row in the history rail (GET /conversations).
 *  Turns are NOT embedded — the rail only needs title/crew/timestamps; fetch a
 *  single conversation's full turns via GET /conversations/{id}. */
export interface Conversation {
  conversation_id: string;
  /** Crew lock — every turn in this thread runs against this one workload. */
  workload: string;
  /** Truncated first prompt (no LLM summary). May be "(untitled)". */
  title: string;
  created_at?: string;
  updated_at?: string;
  turn_count?: number;
  last_trace_id?: string | null;
}

/** GET /conversations/{id} response: the conversation doc + its ordered turns
 *  (oldest-first by seq), used to rehydrate the thread on resume. */
export interface ConversationDetail extends Conversation {
  turns: ConversationTurn[];
}

/** GET /conversations response shape. */
export interface ConversationsResponse {
  conversations: Conversation[];
}

/** GET /trace/{id} response (historical replay + post-`done` backfill). */
export interface TraceResponse {
  trace_id: string;
  events: TraceEvent[];
  decision?: Decision | null;
  complete: boolean;
  fetched_from_cache?: boolean;
}

/** GET /trace/{id}/pr-body response — the agent-authored PR description for the
 *  open-trace "what this change did" disclosure (iac_apply only). `body` is the
 *  scrubbed description or null (no description / fail-soft GitHub miss). */
export interface PrBody {
  pr_number: number;
  head_sha: string;
  body: string | null;
  body_truncated: boolean;
  cached: boolean;
}
