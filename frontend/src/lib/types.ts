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

/** One row in the past-decisions rail (GET /decisions). Open shape — only the
 *  fields the rail renders are typed; the rest flow through the index sig. */
export interface Decision extends Record<string, unknown> {
  decision_id: string;
  trace_id?: string;
  action: string;
  created_at?: string;
  approval?: DecisionApproval | null;
}

/** GET /trace/{id} response (historical replay + post-`done` backfill). */
export interface TraceResponse {
  trace_id: string;
  events: TraceEvent[];
  decision?: Decision | null;
  complete: boolean;
  fetched_from_cache?: boolean;
}
