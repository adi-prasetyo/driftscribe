// Workload selection contract for the operator UI.
//
// The option VALUES (drift/upgrade/explore/provision) are the /chat API
// contract sent to the coordinator; the LABELS are the operator-facing text.
// This re-homes the contract previously guarded in the Jinja template via
// tests/integration/test_ui_transparency.py:59-62 — see plan §3 and Appendix B.

export type Workload = 'drift' | 'upgrade' | 'explore' | 'provision';

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
