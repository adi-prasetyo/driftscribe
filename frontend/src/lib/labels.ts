import type { MessageKey } from '../locales';
import type { TranslateFn } from './i18n';

/**
 * Friendly worker / tool / MCP labels for the transparency timeline.
 *
 * Ported verbatim from the legacy single-file renderer
 * (``agent/templates/transparency.html`` — ``_WORKER_LABELS`` / ``workerLabel``)
 * so the SPA shows the same human-friendly text the old UI did. Re-homes the
 * labels guarded by ``tests/integration/test_ui_transparency.py:112-127``.
 *
 * Keyed by the tool's callable ``__name__`` (the string the ADK runner /
 * timeline carries) or, for MCP entries, by the ``mcp_tool`` name with the
 * ``mcp_server`` identity (``developer_knowledge``) as a fallback key — the
 * MCP group sub-keys on ``mcp_tool`` falling back to ``mcp_server`` when the
 * tool name isn't carried on older event shapes.
 *
 * i18n: the English label text itself now lives in the `timeline.worker.*`
 * catalog (frontend/src/locales/timeline.ts, byte-for-byte the same strings
 * this map used to hold directly) — this map only carries the KNOWN key set,
 * mapping a tool/MCP key to its catalog key.
 */
const WORKER_LABEL_KEYS: Record<string, MessageKey> = {
  // Drift workload
  read_live_env_tool: 'timeline.worker.read_live_env_tool',
  patch_docs_tool: 'timeline.worker.patch_docs_tool',
  propose_rollback_tool: 'timeline.worker.propose_rollback_tool',
  // Upgrade workload — tool names must match the exposed surface in
  // ``agent/adk_tools.py`` (the ``_tool`` suffix is part of the function
  // name the ADK runner sees).
  upgrade_read_dependencies_tool: 'timeline.worker.upgrade_read_dependencies_tool',
  upgrade_propose_pr_tool: 'timeline.worker.upgrade_propose_pr_tool',
  upgrade_close_pr_tool: 'timeline.worker.upgrade_close_pr_tool',
  upgrade_merge_pr_tool: 'timeline.worker.upgrade_merge_pr_tool',
  // Provision workload (Phase D) — the callable __name__ is
  // ``open_infra_pr_tool`` (the symbolic workload name is
  // ``provision_open_infra_pr``; the timeline keys on the callable).
  open_infra_pr_tool: 'timeline.worker.open_infra_pr_tool',
  // Adoption tool (adopt design Phase 3) — callable ``propose_adoption_tool``;
  // symbolic workload name is ``provision_propose_adoption``.
  propose_adoption_tool: 'timeline.worker.propose_adoption_tool',
  // Shared
  notify_tool: 'timeline.worker.notify_tool',
  // Item 12 — pending-infra-PR plan Q&A (explore workload).
  load_iac_plan_tool: 'timeline.worker.load_iac_plan_tool',
  // MCP — Google Developer Knowledge. Per-tool labels (keyed on ``mcp_tool``)
  // plus the bare server-identity key (``mcp_server`` fallback) yielding the
  // unsuffixed friendly label.
  developer_knowledge: 'timeline.worker.developer_knowledge',
  answer_query: 'timeline.worker.answer_query',
  search_documents: 'timeline.worker.search_documents',
  get_documents: 'timeline.worker.get_documents',
};

/** Friendly label for a tool/MCP key, falling back to the raw key. */
export function workerLabel(key: string, t: TranslateFn): string {
  const catalogKey = WORKER_LABEL_KEYS[key];
  return catalogKey ? t(catalogKey) : key;
}
