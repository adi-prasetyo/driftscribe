import { describe, it, expect } from 'vitest';
import { WORKER_LABELS, workerLabel } from '../../src/lib/labels';

describe('WORKER_LABELS', () => {
  // The five required entries asserted by plan §3 / the integration test
  // tests/integration/test_ui_transparency.py:112-127 that this module
  // re-homes from the legacy single-file renderer.
  it('maps read_live_env_tool to "Reader (drift)"', () => {
    expect(WORKER_LABELS['read_live_env_tool']).toBe('Reader (drift)');
  });

  it('maps the developer_knowledge MCP key to "Developer Knowledge MCP"', () => {
    // The MCP group keys on mcp_tool falling back to mcp_server
    // (``developer_knowledge``); the server-identity key must yield the
    // bare friendly label.
    expect(WORKER_LABELS['developer_knowledge']).toBe('Developer Knowledge MCP');
  });

  it('maps upgrade_read_dependencies_tool to "Upgrade Reader"', () => {
    expect(WORKER_LABELS['upgrade_read_dependencies_tool']).toBe('Upgrade Reader');
  });

  it('maps upgrade_propose_pr_tool to "Upgrade Docs"', () => {
    expect(WORKER_LABELS['upgrade_propose_pr_tool']).toBe('Upgrade Docs');
  });

  it('maps open_infra_pr_tool to "Open infra PR"', () => {
    expect(WORKER_LABELS['open_infra_pr_tool']).toBe('Open infra PR');
  });

  // Full legacy map preserved verbatim (drift reader/docs/rollback,
  // upgrade reader/docs/close/merge, provision, notifier, MCP per-tool).
  it('preserves the full legacy drift-workload labels', () => {
    expect(WORKER_LABELS['patch_docs_tool']).toBe('Docs (drift)');
    expect(WORKER_LABELS['propose_rollback_tool']).toBe('Rollback (drift) — HITL');
  });

  it('preserves the full legacy upgrade-workload labels', () => {
    expect(WORKER_LABELS['upgrade_close_pr_tool']).toBe('Upgrade Docs — close PR');
    expect(WORKER_LABELS['upgrade_merge_pr_tool']).toBe('Upgrade Docs — merge PR');
  });

  it('preserves the shared notifier label', () => {
    expect(WORKER_LABELS['notify_tool']).toBe('Notifier');
  });

  it('preserves the legacy per-tool MCP labels', () => {
    expect(WORKER_LABELS['answer_query']).toBe('Developer Knowledge MCP — answer');
    expect(WORKER_LABELS['search_documents']).toBe('Developer Knowledge MCP — search');
    expect(WORKER_LABELS['get_documents']).toBe('Developer Knowledge MCP — get');
  });
});

describe('workerLabel', () => {
  it('returns the mapped label for a known key', () => {
    expect(workerLabel('read_live_env_tool')).toBe('Reader (drift)');
    expect(workerLabel('open_infra_pr_tool')).toBe('Open infra PR');
    expect(workerLabel('developer_knowledge')).toBe('Developer Knowledge MCP');
  });

  it('falls back to the raw key verbatim for an unknown key', () => {
    expect(workerLabel('some_unknown_tool')).toBe('some_unknown_tool');
    expect(workerLabel('')).toBe('');
    expect(workerLabel('（未知）')).toBe('（未知）');
  });

  it('uses ?? semantics: a real entry wins over the fallback', () => {
    // Ensure the fallback only triggers on missing keys, not falsy values.
    for (const [key, value] of Object.entries(WORKER_LABELS)) {
      expect(workerLabel(key)).toBe(value);
    }
  });
});
