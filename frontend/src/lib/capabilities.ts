// capabilities.ts — types mirroring the GET /capabilities DTO (version 1) +
// display helpers for the CapabilityCard panel.
//
// The DTO is generated server-side from the same constants the enforcement
// code imports (TOOL_REGISTRY, WORKER_REGISTRY, ACTION_REGISTRY,
// MUTATION_TOOL_NAMES, RULE_DESCRIPTIONS) — it is never hand-written.
// These types mirror that shape exactly.

/** A single tool available to a workload. */
export interface CapTool {
  name: string;
  description: string;
  /** True when the tool is in MUTATION_TOOL_NAMES: "writes OR rides a
   *  write-capable credential". The per-tool description carries the nuance
   *  for tools that are included for credential-containment only. */
  write_capable: boolean;
}

/** A worker that a workload can invoke. */
export interface CapWorker {
  name: string;
  description: string;
}

/** An action a workload can take. */
export interface CapAction {
  name: string;
  display_name: string;
  requires_approval: boolean;
}

/** A human-gated operation — the gate is the POST (not the GET form page). */
export interface CapGate {
  id: string;
  title: string;
  description: string;
  route: string;
  method: string;
}

/** A single denylist rule. */
export interface CapRule {
  id: string;
  description: string;
  /** Category from CATEGORY_ORDER: typically "control-plane" | "service-managed" | "iam" | "global-v1" | "structural".
   *  A future server may emit additional categories — callers must not drop unknown ones.
   *  Typed as string so the runtime unknown-category path is never unreachable. */
  category: string;
}

/** A workload as serialized by the server. */
export interface CapWorkload {
  name: string;
  /** Crew identity (e.g. "Anchor"). */
  display_name: string;
  /** Domain subtitle (e.g. "Cloud Run config"). */
  descriptor: string;
  description: string;
  /** True only when the workload has a LIVE autonomous trigger — derived
   *  server-side from AUTONOMOUS_TRIGGER_WORKLOADS (only `drift`/Anchor
   *  today), NOT from observation_kind. */
  autonomous: boolean;
  tools: CapTool[];
  workers: CapWorker[];
  actions: CapAction[];
}

/** The full GET /capabilities DTO (version 1). */
export interface Capabilities {
  version: 1;
  provenance: string;
  iam_note: string;
  workloads: CapWorkload[];
  human_gates: CapGate[];
  denylist: {
    summary: string;
    enforced_at: string[];
    rules: CapRule[];
    /** Optional — present from Phase-2 import-admission; absent on older payloads. */
    adoptable_resource_types?: { type: string; label: string }[];
  };
}

// ---------------------------------------------------------------------------
// Display helpers
// ---------------------------------------------------------------------------

/** Human-readable headings for the known denylist categories. */
export const CATEGORY_HEADINGS: Record<
  'control-plane' | 'service-managed' | 'iam' | 'global-v1' | 'structural',
  string
> = {
  'control-plane': 'Its own control plane is off-limits:',
  'service-managed': 'It leaves Google-created buckets alone',
  iam: 'It cannot change who has access',
  'global-v1': 'It cannot destroy or replace anything',
  structural: 'Malformed plans are rejected outright',
};

/** A single group returned by groupRules. */
export interface RuleGroup {
  /** Category string — one of the known literals or a raw unknown string. */
  category: string;
  heading: string;
  rules: CapRule[];
}

/**
 * Group rules by category, preserving server order within and across groups.
 *
 * Unknown-category behavior (safety invariant): a rule whose `category` is
 * not in `CATEGORY_HEADINGS` is NEVER dropped — it is appended as a trailing
 * group whose heading is the raw category string. Safety information must not
 * silently disappear because the server grew a category before the frontend
 * learned its heading.
 */
export function groupRules(rules: CapRule[]): RuleGroup[] {
  // First pass: collect groups in insertion order (preserving server order).
  const knownCategories = Object.keys(CATEGORY_HEADINGS) as Array<keyof typeof CATEGORY_HEADINGS>;
  const knownMap = new Map<string, CapRule[]>();
  const unknownMap = new Map<string, CapRule[]>();

  for (const rule of rules) {
    if (knownCategories.includes(rule.category as keyof typeof CATEGORY_HEADINGS)) {
      if (!knownMap.has(rule.category)) knownMap.set(rule.category, []);
      knownMap.get(rule.category)!.push(rule);
    } else {
      if (!unknownMap.has(rule.category)) unknownMap.set(rule.category, []);
      unknownMap.get(rule.category)!.push(rule);
    }
  }

  const groups: RuleGroup[] = [];

  for (const [cat, catRules] of knownMap) {
    groups.push({
      category: cat,
      heading: CATEGORY_HEADINGS[cat as keyof typeof CATEGORY_HEADINGS],
      rules: catRules,
    });
  }

  for (const [cat, catRules] of unknownMap) {
    groups.push({
      category: cat,
      // Raw category string as heading — unknown categories are never dropped.
      heading: cat,
      rules: catRules,
    });
  }

  return groups;
}
