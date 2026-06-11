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
  // Autonomy dial fields (ClickOps item 11). Present on decisions created while
  // the dial is configured; absent on pre-dial decisions (stale-coordinator
  // fail-quiet: the rail renders nothing when absent).
  autonomy_mode?: string;
  suppressed_by_autonomy?: boolean;
}

/** GET /trace/{id} response (historical replay + post-`done` backfill). */
export interface TraceResponse {
  trace_id: string;
  events: TraceEvent[];
  decision?: Decision | null;
  complete: boolean;
  fetched_from_cache?: boolean;
}
