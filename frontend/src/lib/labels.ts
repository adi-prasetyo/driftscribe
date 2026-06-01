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
 */
export const WORKER_LABELS: Record<string, string> = {
  // Drift workload
  read_live_env_tool: 'Reader (drift)',
  patch_docs_tool: 'Docs (drift)',
  propose_rollback_tool: 'Rollback (drift) — HITL',
  // Upgrade workload — tool names must match the exposed surface in
  // ``agent/adk_tools.py`` (the ``_tool`` suffix is part of the function
  // name the ADK runner sees).
  upgrade_read_dependencies_tool: 'Upgrade Reader',
  upgrade_propose_pr_tool: 'Upgrade Docs',
  upgrade_close_pr_tool: 'Upgrade Docs — close PR',
  upgrade_merge_pr_tool: 'Upgrade Docs — merge PR',
  // Provision workload (Phase D) — the callable __name__ is
  // ``open_infra_pr_tool`` (the symbolic workload name is
  // ``provision_open_infra_pr``; the timeline keys on the callable).
  open_infra_pr_tool: 'Open infra PR',
  // Shared
  notify_tool: 'Notifier',
  // MCP — Google Developer Knowledge. Per-tool labels (keyed on ``mcp_tool``)
  // plus the bare server-identity key (``mcp_server`` fallback) yielding the
  // unsuffixed friendly label.
  developer_knowledge: 'Developer Knowledge MCP',
  answer_query: 'Developer Knowledge MCP — answer',
  search_documents: 'Developer Knowledge MCP — search',
  get_documents: 'Developer Knowledge MCP — get',
};

/** Friendly label for a tool/MCP key, falling back to the raw key. */
export function workerLabel(key: string): string {
  return WORKER_LABELS[key] ?? key;
}
