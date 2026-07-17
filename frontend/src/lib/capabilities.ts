// capabilities.ts — types mirroring the GET /capabilities DTO (version 1) +
// display helpers for the CapabilityCard panel.
//
// The DTO is generated server-side from the same constants the enforcement
// code imports (TOOL_REGISTRY, WORKER_REGISTRY, ACTION_REGISTRY,
// MUTATION_TOOL_NAMES, RULE_DESCRIPTIONS) — it is never hand-written.
// These types mirror that shape exactly.

import type { TranslateFn, MessageKey } from './i18n';

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
// Denylist category headings
// ---------------------------------------------------------------------------

/** i18n key for each known denylist category's heading. Not exported — callers
 *  go through `categoryHeading()`, which is the safe (never-throws-on-unknown)
 *  entry point. */
const CATEGORY_HEADING_KEYS: Record<string, MessageKey> = {
  'control-plane': 'capability.category.controlPlane',
  'service-managed': 'capability.category.serviceManaged',
  iam: 'capability.category.iam',
  'global-v1': 'capability.category.globalV1',
  structural: 'capability.category.structural',
};

/**
 * Human-readable heading for a denylist category. A static Record built at
 * module eval (like the old `CATEGORY_HEADINGS`) can't react to a locale
 * toggle, so this resolves at RENDER time via the passed `t` — callers must
 * invoke it reactively (`categoryHeading(group.category, $t)`).
 *
 * Unknown-category behavior (safety invariant, unchanged from the old static
 * CATEGORY_HEADINGS map): a category with no key in CATEGORY_HEADING_KEYS
 * returns the raw category string rather than throwing or dropping the rule
 * group — safety information must not silently disappear because the server
 * grew a category before the frontend learned its heading.
 */
export function categoryHeading(category: string, t: TranslateFn): string {
  const key = CATEGORY_HEADING_KEYS[category];
  return key ? t(key) : category;
}

/** A single group returned by groupRules. */
export interface RuleGroup {
  /** Category string — one of the known literals or a raw unknown string.
   *  Resolve the display heading via `categoryHeading(category, t)` at render. */
  category: string;
  rules: CapRule[];
}

/**
 * Group rules by category, preserving server order within and across groups.
 *
 * Unknown-category behavior (safety invariant): a rule whose `category` is
 * not a known category is NEVER dropped — it is appended as a trailing group.
 * `categoryHeading()` resolves its display heading to the raw category string.
 * Safety information must not silently disappear because the server grew a
 * category before the frontend learned its heading.
 */
export function groupRules(rules: CapRule[]): RuleGroup[] {
  // First pass: collect groups in insertion order (preserving server order).
  const knownCategories = Object.keys(CATEGORY_HEADING_KEYS);
  const knownMap = new Map<string, CapRule[]>();
  const unknownMap = new Map<string, CapRule[]>();

  for (const rule of rules) {
    if (knownCategories.includes(rule.category)) {
      if (!knownMap.has(rule.category)) knownMap.set(rule.category, []);
      knownMap.get(rule.category)!.push(rule);
    } else {
      if (!unknownMap.has(rule.category)) unknownMap.set(rule.category, []);
      unknownMap.get(rule.category)!.push(rule);
    }
  }

  const groups: RuleGroup[] = [];

  for (const [cat, catRules] of knownMap) {
    groups.push({ category: cat, rules: catRules });
  }

  for (const [cat, catRules] of unknownMap) {
    groups.push({ category: cat, rules: catRules });
  }

  return groups;
}

// ---------------------------------------------------------------------------
// Backend capability metadata — stable-id JA maps with EN (DTO) fallback
// ---------------------------------------------------------------------------
//
// GET /capabilities carries backend-generated English prose. Per the i18n
// design doc's "Backend capability prose" rule: localize STABLE IDENTIFIERS
// (gate.id, rule.id, tool/worker name, action name) via a frontend JA map,
// falling back to the DTO's own English string when the id isn't in the map
// (an id the map hasn't caught up to yet, or a genuinely new one — never a
// blank render, never a thrown error). Arbitrary free prose with no stable id
// (denylist.summary, denylist.enforced_at[], iam_note, provenance, the raw
// system-prompt text from /workloads/{name}/prompts) is intentionally left
// English — see CapabilityCard.svelte for those pass-through sites.
//
// Each map is `Record<id, MessageKey>` (not `Record<id, string>`) so lookups
// go through the same `t()` your static UI strings do — dev/test mode's
// "missing key throws" guard still catches a typo'd id→key mapping here, and
// JA→EN fallback for an unfinished translation is automatic. The id NOT being
// in the map (a case this file must always handle) is a different situation
// from a mapped id whose key is missing — that's guarded by the `key ?`
// check before ever calling `t()`.

const GATE_TITLE_KEYS: Record<string, MessageKey> = {
  iac_apply: 'capability.gate.iacApply.title',
  rollback: 'capability.gate.rollback.title',
};
export function gateTitle(gate: CapGate, t: TranslateFn): string {
  const key = GATE_TITLE_KEYS[gate.id];
  return key ? t(key) : gate.title;
}

const GATE_DESCRIPTION_KEYS: Record<string, MessageKey> = {
  iac_apply: 'capability.gate.iacApply.description',
  rollback: 'capability.gate.rollback.description',
};
export function gateDescription(gate: CapGate, t: TranslateFn): string {
  const key = GATE_DESCRIPTION_KEYS[gate.id];
  return key ? t(key) : gate.description;
}

// Keyed by the exact rule IDs in driftscribe_lib/iac_plan_denylist.py::RULE_DESCRIPTIONS.
const RULE_DESCRIPTION_KEYS: Record<string, MessageKey> = {
  'plan-json-unparseable': 'capability.rule.planJsonUnparseable',
  'plan-json-missing-resource-changes': 'capability.rule.planJsonMissingResourceChanges',
  'plan-json-malformed-change': 'capability.rule.planJsonMalformedChange',
  'control-plane-service': 'capability.rule.controlPlaneService',
  'control-plane-sa': 'capability.rule.controlPlaneSa',
  'control-plane-bucket': 'capability.rule.controlPlaneBucket',
  'service-managed-bucket': 'capability.rule.serviceManagedBucket',
  'service-managed-pubsub': 'capability.rule.serviceManagedPubsub',
  'control-plane-secret': 'capability.rule.controlPlaneSecret',
  'control-plane-kms': 'capability.rule.controlPlaneKms',
  'wif-config-change': 'capability.rule.wifConfigChange',
  'iam-change-forbidden-v1': 'capability.rule.iamChangeForbiddenV1',
  'import-with-changes-forbidden-v1': 'capability.rule.importWithChangesForbiddenV1',
  'import-type-not-adoptable-v1': 'capability.rule.importTypeNotAdoptableV1',
  'import-mixed-plan-forbidden-v1': 'capability.rule.importMixedPlanForbiddenV1',
  'import-batch-forbidden-v1': 'capability.rule.importBatchForbiddenV1',
  'delete-action-forbidden-v1': 'capability.rule.deleteActionForbiddenV1',
  'forget-action-forbidden-v1': 'capability.rule.forgetActionForbiddenV1',
  'replace-action-forbidden-v1': 'capability.rule.replaceActionForbiddenV1',
  'unknown-action-forbidden-v1': 'capability.rule.unknownActionForbiddenV1',
};
export function ruleDescription(rule: CapRule, t: TranslateFn): string {
  const key = RULE_DESCRIPTION_KEYS[rule.id];
  return key ? t(key) : rule.description;
}

// Keyed by the exact tool names in agent/capabilities.py::TOOL_DESCRIPTIONS.
const TOOL_DESCRIPTION_KEYS: Record<string, MessageKey> = {
  drift_read_live_env: 'capability.tool.driftReadLiveEnv',
  read_project_inventory: 'capability.tool.readProjectInventory',
  drift_patch_docs: 'capability.tool.driftPatchDocs',
  drift_propose_rollback: 'capability.tool.driftProposeRollback',
  notify: 'capability.tool.notify',
  load_contract: 'capability.tool.loadContract',
  search_recent_prs: 'capability.tool.searchRecentPrs',
  load_iac_plan: 'capability.tool.loadIacPlan',
  read_team_log: 'capability.tool.readTeamLog',
  read_conversations: 'capability.tool.readConversations',
  upgrade_read_dependencies: 'capability.tool.upgradeReadDependencies',
  upgrade_propose_pr: 'capability.tool.upgradeProposePr',
  upgrade_close_pr: 'capability.tool.upgradeClosePr',
  upgrade_merge_pr: 'capability.tool.upgradeMergePr',
  search_developer_docs: 'capability.tool.searchDeveloperDocs',
  retrieve_developer_doc: 'capability.tool.retrieveDeveloperDoc',
  provision_open_infra_pr: 'capability.tool.provisionOpenInfraPr',
  provision_propose_adoption: 'capability.tool.provisionProposeAdoption',
  get_session_state: 'capability.tool.getSessionState',
  set_session_state: 'capability.tool.setSessionState',
};
export function toolDescription(tool: CapTool, t: TranslateFn): string {
  const key = TOOL_DESCRIPTION_KEYS[tool.name];
  return key ? t(key) : tool.description;
}

// Keyed by the exact worker names in agent/capabilities.py::WORKER_DESCRIPTIONS.
const WORKER_DESCRIPTION_KEYS: Record<string, MessageKey> = {
  drift_reader: 'capability.worker.driftReader',
  drift_docs: 'capability.worker.driftDocs',
  drift_rollback: 'capability.worker.driftRollback',
  infra_reader: 'capability.worker.infraReader',
  notifier: 'capability.worker.notifier',
  upgrade_reader: 'capability.worker.upgradeReader',
  upgrade_docs: 'capability.worker.upgradeDocs',
  tofu_editor: 'capability.worker.tofuEditor',
};
export function workerDescription(worker: CapWorker, t: TranslateFn): string {
  const key = WORKER_DESCRIPTION_KEYS[worker.name];
  return key ? t(key) : worker.description;
}

// Keyed by the exact action names in agent/workloads/registry.py::ACTION_REGISTRY.
// `no_op`'s display_name ("No action needed") is byte-identical to the decision-log
// label already carried in shared.decision.noOp — reuse that key instead of a
// second copy of the same sentence.
const ACTION_DISPLAY_NAME_KEYS: Record<string, MessageKey> = {
  docs_pr: 'capability.action.docsPr',
  drift_issue: 'capability.action.driftIssue',
  escalation: 'capability.action.escalation',
  no_op: 'shared.decision.noOp',
  rollback: 'capability.action.rollback',
  upgrade_pr: 'capability.action.upgradePr',
};
export function actionDisplayName(action: CapAction, t: TranslateFn): string {
  const key = ACTION_DISPLAY_NAME_KEYS[action.name];
  return key ? t(key) : action.display_name;
}

// Keyed by the exact resource types in agent/capabilities.py::ADOPTABLE_TYPE_LABELS.
const ADOPTABLE_TYPE_LABEL_KEYS: Record<string, MessageKey> = {
  google_storage_bucket: 'capability.adoptableType.googleStorageBucket',
  google_pubsub_topic: 'capability.adoptableType.googlePubsubTopic',
  google_pubsub_subscription: 'capability.adoptableType.googlePubsubSubscription',
  google_cloud_run_v2_service: 'capability.adoptableType.googleCloudRunV2Service',
};
export function adoptableTypeLabel(entry: { type: string; label: string }, t: TranslateFn): string {
  const key = ADOPTABLE_TYPE_LABEL_KEYS[entry.type];
  return key ? t(key) : entry.label;
}
