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
