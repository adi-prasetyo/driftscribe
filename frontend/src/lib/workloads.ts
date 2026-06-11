// Workload selection contract for the operator UI.
//
// The option VALUES (drift/upgrade/explore/provision) are the /chat API
// contract sent to the coordinator; the LABELS are the operator-facing text.
// This re-homes the contract previously guarded in the Jinja template via
// tests/integration/test_ui_transparency.py:59-62 — see plan §3 and Appendix B.

export type Workload = 'drift' | 'upgrade' | 'explore' | 'provision';

/**
 * Parse `ask_pr` from a `location.search` string (the approval page's
 * "ask about this change" link). Same validation discipline as
 * infra_graph.ts::previewPrFromSearch: all-digits, positive, safe integer.
 */
export function askPrFromSearch(search: string): number | null {
  const raw = new URLSearchParams(search).get('ask_pr');
  if (raw === null || !/^\d+$/.test(raw)) return null;
  const n = Number(raw);
  return Number.isSafeInteger(n) && n > 0 ? n : null;
}

/**
 * Composer prefill text for an ask_pr arrival. PREFILLED, never auto-sent —
 * the operator reads and edits before anything happens (same contract as the
 * Adopt-button bridge). The PR number rides in the text; the explore agent
 * extracts it for load_iac_plan.
 */
export function askAboutPrPrefill(pr: number): string {
  return (
    `I'm reviewing infrastructure change PR #${pr} before deciding on it. ` +
    'Load its plan and explain what it would change in plain language.'
  );
}

export interface WorkloadOption {
  value: Workload;
  label: string;
}

export const WORKLOADS: WorkloadOption[] = [
  { value: 'drift', label: 'Cloud Run config' },
  { value: 'upgrade', label: 'Dependencies' },
  { value: 'explore', label: 'Explore (read-only)' },
  { value: 'provision', label: 'Provision (infra edits)' },
];

/**
 * Composer prefill (Phase-4 adopt-button bridge): App sets text + workload from an
 * Adopt click and ChatForm applies it WITHOUT sending. `epoch` lets the same/another
 * Adopt re-apply after the operator edits the draft. Shared so App and ChatForm can
 * never disagree on the shape.
 */
export interface ChatPrefill {
  text: string;
  workload: Workload;
  epoch: number;
}

/**
 * The whole ask_pr boot decision as a PURE function (App.svelte calls this
 * once at init) so the seeding rule — explore workload, epoch 1, prefill
 * only — is unit-testable without mounting App.
 */
export function initialChatPrefill(search: string): ChatPrefill | null {
  const pr = askPrFromSearch(search);
  return pr === null
    ? null
    : { text: askAboutPrPrefill(pr), workload: 'explore', epoch: 1 };
}
