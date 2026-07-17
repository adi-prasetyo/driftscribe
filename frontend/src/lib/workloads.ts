// Workload selection contract for the operator UI.
//
// The option VALUES (drift/upgrade/explore/provision) are the /chat API
// contract sent to the coordinator; the LABELS are the operator-facing text.
// This re-homes the contract previously guarded in the Jinja template via
// tests/integration/test_ui_transparency.py:59-62 — see plan §3 and Appendix B.

import workloadCatalog from './workloads.catalog.json';
import type { TranslateFn, MessageKey } from './i18n';

export type Workload = 'drift' | 'upgrade' | 'explore' | 'provision';

/**
 * Autonomy camp for the crew picker. ``autonomous`` = has a live trigger that
 * runs without being asked (only Anchor/`drift` in this build); ``on-demand`` =
 * runs only when the operator asks. This is the operator-facing distinction the
 * picker's optgroup + adjacent badge make unmistakable. The backend owns the
 * truth (agent/main.py::AUTONOMOUS_TRIGGER_WORKLOADS); the cross-surface
 * tests/unit/test_capabilities.py::test_frontend_catalog_matches_backend pins
 * this catalog's ``group`` to it so the two can never silently disagree.
 */
export type WorkloadGroup = 'autonomous' | 'on-demand';

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
export function askAboutPrPrefill(pr: number, t: TranslateFn): string {
  return t('header.prefill.askPr', { pr });
}

export interface WorkloadOption {
  value: Workload;
  /** Crew identity — the bold name, e.g. "Anchor". */
  name: string;
  /** Domain subtitle — the gray descriptor, e.g. "Cloud Run config". */
  descriptor: string;
  /** One-sentence summary of the workload's full description, surfaced as the
   *  crew-card hover/focus tooltip. Longer than ``descriptor`` (which stays the
   *  terse "Name — descriptor" label); a condensed form of the canonical
   *  backend ``description`` (rendered in full by CapabilityCard). */
  summary: string;
  /** Autonomy camp — drives the picker's optgroup + adjacent badge. */
  group: WorkloadGroup;
  /** Combined "Name — descriptor" label rendered in the native <option>. */
  label: string;
}

/**
 * The crew picker contract, derived from the single checked-in catalog
 * (workloads.catalog.json) so the SPA, the backend YAML display_name/
 * descriptor, and GET /capabilities can never silently disagree (the cross-
 * surface guard test reads that same JSON). The option VALUES
 * (drift/upgrade/explore/provision) are the /chat API contract sent to the
 * coordinator and are FROZEN; only the human-facing name/descriptor change.
 */
export const WORKLOADS: WorkloadOption[] = (
  workloadCatalog as ReadonlyArray<{
    value: string;
    name: string;
    descriptor: string;
    summary: string;
    group: string;
  }>
).map((e) => ({
  value: e.value as Workload,
  name: e.name,
  descriptor: e.descriptor,
  summary: e.summary,
  group: e.group as WorkloadGroup,
  label: `${e.name} — ${e.descriptor}`,
}));

/**
 * Where each crew sits in the stewardship loop (create → guard → maintain →
 * explain), as one operator-facing line. Surfaced in CapabilityCard so the four
 * crews read as one coherent system, not four separate tools. PURE COPY, not a
 * safety claim: Anchor reacts to/detects drift on its own, but its remediations
 * stay behind the human approval gate (see the gates section of the same card).
 * Keyed by the FROZEN symbolic workload value.
 */
export const CREW_LIFECYCLE: Record<Workload, string> = {
  provision: 'Stands infrastructure up. You describe a change; it opens the IaC PR.',
  drift: 'Guards what is live. Runs on its own, reacting when it detects drift.',
  upgrade: 'Keeps it current. Proposes dependency upgrades.',
  explore: 'Explains it. Read-only answers across the whole system.',
};

/**
 * i18n equivalents of the three catalog-shaped strings above (lifecycle line)
 * and in `WORKLOADS` (descriptor/summary), sourced from the `shared.crew.*`
 * catalog (frontend/src/locales/shared.ts) instead of the always-English
 * `CREW_LIFECYCLE` map / `workloads.catalog.json`. `value` is the frozen
 * symbolic workload, so the key is built the same way `plural()` (i18n.ts)
 * builds a dynamic key — cast through `MessageKey` since it isn't a literal.
 */
export function crewLifecycle(value: Workload, t: TranslateFn): string {
  return t(`shared.crew.${value}.lifecycle` as MessageKey);
}
export function crewDescriptor(value: Workload, t: TranslateFn): string {
  return t(`shared.crew.${value}.descriptor` as MessageKey);
}
export function crewSummary(value: Workload, t: TranslateFn): string {
  return t(`shared.crew.${value}.summary` as MessageKey);
}

/** value → crew display name, e.g. `drift` → `Anchor`. Built once from the
 *  catalog. */
const CREW_NAME = new Map<string, string>(WORKLOADS.map((w) => [w.value, w.name]));

/**
 * The operator-facing crew name for a workload value (`drift` → `Anchor`).
 * Falls back to the raw value, then to `"Crew"`, so an unknown/absent workload
 * never renders blank. Single source of truth for the byline (ConversationThread)
 * and the conversation search predicate.
 */
export function crewName(workload: string | undefined | null): string {
  return (workload && CREW_NAME.get(workload)) || workload || 'Crew';
}

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
export function initialChatPrefill(search: string, t: TranslateFn): ChatPrefill | null {
  const pr = askPrFromSearch(search);
  return pr === null
    ? null
    : { text: askAboutPrPrefill(pr, t), workload: 'explore', epoch: 1 };
}
