import { describe, it, expect } from 'vitest';
import { workerLabel } from '../../src/lib/labels';
import { translate, type TranslateFn } from '../../src/lib/i18n';

// The whole suite asserts English (the timeline.en catalog is byte-for-byte
// the original inline text this module used to return), so workerLabel is
// called with an EN-bound translator.
const t: TranslateFn = (k, p) => translate('en', k, p);

describe('workerLabel', () => {
  // The five required entries asserted by plan §3 / the integration test
  // tests/integration/test_ui_transparency.py:112-127 that this module
  // re-homes from the legacy single-file renderer.
  it('maps read_live_env_tool to "Reader (drift)"', () => {
    expect(workerLabel('read_live_env_tool', t)).toBe('Reader (drift)');
  });

  it('maps the developer_knowledge MCP key to "Developer Knowledge MCP"', () => {
    // The MCP group keys on mcp_tool falling back to mcp_server
    // (``developer_knowledge``); the server-identity key must yield the
    // bare friendly label.
    expect(workerLabel('developer_knowledge', t)).toBe('Developer Knowledge MCP');
  });

  it('maps upgrade_read_dependencies_tool to "Upgrade Reader"', () => {
    expect(workerLabel('upgrade_read_dependencies_tool', t)).toBe('Upgrade Reader');
  });

  it('maps upgrade_propose_pr_tool to "Upgrade Docs"', () => {
    expect(workerLabel('upgrade_propose_pr_tool', t)).toBe('Upgrade Docs');
  });

  it('maps open_infra_pr_tool to "Open infra PR"', () => {
    expect(workerLabel('open_infra_pr_tool', t)).toBe('Open infra PR');
  });

  // Full legacy map preserved verbatim (drift reader/docs/rollback,
  // upgrade reader/docs/close/merge, provision, notifier, MCP per-tool).
  it('preserves the full legacy drift-workload labels', () => {
    expect(workerLabel('patch_docs_tool', t)).toBe('Docs (drift)');
    expect(workerLabel('propose_rollback_tool', t)).toBe('Rollback (drift) · HITL');
  });

  it('preserves the full legacy upgrade-workload labels', () => {
    expect(workerLabel('upgrade_close_pr_tool', t)).toBe('Upgrade Docs · close PR');
    expect(workerLabel('upgrade_merge_pr_tool', t)).toBe('Upgrade Docs · merge PR');
  });

  it('preserves the shared notifier label', () => {
    expect(workerLabel('notify_tool', t)).toBe('Notifier');
  });

  it('preserves the legacy per-tool MCP labels', () => {
    expect(workerLabel('answer_query', t)).toBe('Developer Knowledge MCP · answer');
    expect(workerLabel('search_documents', t)).toBe('Developer Knowledge MCP · search');
    expect(workerLabel('get_documents', t)).toBe('Developer Knowledge MCP · get');
  });

  it('falls back to the raw key verbatim for an unknown key', () => {
    expect(workerLabel('some_unknown_tool', t)).toBe('some_unknown_tool');
    expect(workerLabel('', t)).toBe('');
    expect(workerLabel('（未知）', t)).toBe('（未知）');
  });

  it('maps load_iac_plan_tool to "IaC plan reader"', () => {
    // Item 12 — pending-infra-PR plan Q&A (explore workload).
    expect(workerLabel('load_iac_plan_tool', t)).toBe('IaC plan reader');
  });
});
