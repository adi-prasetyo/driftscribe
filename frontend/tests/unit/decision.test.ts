import { describe, it, expect } from 'vitest';
import { decisionFields } from '../../src/lib/decision';
import type { Decision } from '../../src/lib/types';

// A realistic iac_apply decision doc as GET /trace returns it (verified against
// live Firestore doc fb18148c-…: fields {action, pr_number, apply_status,
// merge_state, approver, head_sha, applied_at, …}). No rationale/rendered_body.
const IAC_APPLY: Decision = {
  decision_id: 'fb18148c-c207-4f78-a94d-a01e1fcb4b0d',
  event_key: 'iac-apply-47-bfb30e2824d67fb3f68de36fdeba1d4d',
  trace_id: '88908d9b2d244dd6b8f952a6d799851f',
  action: 'iac_apply',
  pr_number: 47,
  apply_status: 'applied',
  merge_state: 'merged',
  approver: 'theghostsquad00@gmail.com',
  head_sha: '89f2d4e093f2fa15fab0d86b21c1e98d45845418',
  applied_at: '2026-05-31T08:27:45.434428+00:00',
  created_at: '2026-05-31T08:27:45.450000+00:00',
} as Decision;

function byLabel(d: Decision, label: string) {
  return decisionFields(d).find((f) => f.label === label);
}

describe('decisionFields — iac_apply (curated)', () => {
  it('renders the full curated row set in order', () => {
    const labels = decisionFields(IAC_APPLY).map((f) => f.label);
    expect(labels).toEqual(['Action', 'Pull request', 'Apply', 'Merge', 'Head SHA', 'Approver', 'When']);
  });

  it('maps the action to a friendly label', () => {
    expect(byLabel(IAC_APPLY, 'Action')?.value).toBe('Infra apply');
  });

  it('formats the PR number with a leading #', () => {
    expect(byLabel(IAC_APPLY, 'Pull request')?.value).toBe('#47');
  });

  it('truncates head_sha to 12 chars and keeps the full value in title', () => {
    const sha = byLabel(IAC_APPLY, 'Head SHA');
    expect(sha?.value).toBe('89f2d4e093f2');
    expect(sha?.value.length).toBe(12);
    expect(sha?.code).toBe(true);
    expect(sha?.title).toBe('89f2d4e093f2fa15fab0d86b21c1e98d45845418');
  });

  it('shows the approver verbatim', () => {
    expect(byLabel(IAC_APPLY, 'Approver')?.value).toBe('theghostsquad00@gmail.com');
  });

  it('prefers applied_at for the When row and formats it (not raw ISO)', () => {
    const when = byLabel(IAC_APPLY, 'When');
    // Exact string is locale/tz-dependent; assert it parsed (year present, no ISO "T").
    expect(when?.value).toContain('2026');
    expect(when?.value).not.toContain('T08:27');
  });
});

describe('decisionFields — badge variants', () => {
  it('apply_status applied → ok', () => {
    expect(byLabel(IAC_APPLY, 'Apply')?.badge).toBe('ok');
  });
  it('apply_status failed / failed_state_suspect → danger', () => {
    expect(byLabel({ ...IAC_APPLY, apply_status: 'failed' } as Decision, 'Apply')?.badge).toBe('danger');
    expect(byLabel({ ...IAC_APPLY, apply_status: 'failed_state_suspect' } as Decision, 'Apply')?.badge).toBe('danger');
  });
  it('apply_status ambiguous → warn', () => {
    expect(byLabel({ ...IAC_APPLY, apply_status: 'ambiguous' } as Decision, 'Apply')?.badge).toBe('warn');
  });
  it('unknown apply_status → muted', () => {
    expect(byLabel({ ...IAC_APPLY, apply_status: 'weird' } as Decision, 'Apply')?.badge).toBe('muted');
  });
  it('merge_state merged → ok, failed → danger, pending → warn, unknown → muted', () => {
    expect(byLabel({ ...IAC_APPLY, merge_state: 'merged' } as Decision, 'Merge')?.badge).toBe('ok');
    expect(byLabel({ ...IAC_APPLY, merge_state: 'failed' } as Decision, 'Merge')?.badge).toBe('danger');
    expect(byLabel({ ...IAC_APPLY, merge_state: 'pending' } as Decision, 'Merge')?.badge).toBe('warn');
    expect(byLabel({ ...IAC_APPLY, merge_state: 'mystery' } as Decision, 'Merge')?.badge).toBe('muted');
  });
});

describe('decisionFields — safety (no dynamic field dump)', () => {
  it('never renders a non-allowlisted field, even a sensitive-looking one', () => {
    const d = {
      decision_id: 'x',
      action: 'iac_apply',
      // These MUST NOT surface — /trace returns the decision unredacted.
      github_token: 'ghp_supersecretvalue',
      approval_url: 'https://coord/approvals/ap-1?t=secrettoken',
      internal_note: 'sensitive',
    } as unknown as Decision;
    const fields = decisionFields(d);
    const blob = JSON.stringify(fields);
    expect(blob).not.toContain('ghp_supersecretvalue');
    expect(blob).not.toContain('secrettoken');
    expect(blob).not.toContain('sensitive');
    // Only the safe Action row is produced for this minimal doc.
    expect(fields.map((f) => f.label)).toEqual(['Action']);
  });

  it('clamps an oversized allowlisted value', () => {
    const long = 'a'.repeat(500);
    const d = { decision_id: 'x', action: 'iac_apply', approver: long } as Decision;
    const approver = byLabel(d, 'Approver');
    expect(approver!.value.length).toBeLessThanOrEqual(257); // 256 + ellipsis
    expect(approver!.value.endsWith('…')).toBe(true);
  });
});

describe('decisionFields — generic / edge cases', () => {
  it('returns [] for null/undefined', () => {
    expect(decisionFields(null)).toEqual([]);
    expect(decisionFields(undefined)).toEqual([]);
  });

  it('an unknown action with only created_at shows Action + When', () => {
    const d = { decision_id: 'x', action: 'mystery_action', created_at: '2026-01-02T03:04:05Z' } as Decision;
    expect(decisionFields(d).map((f) => f.label)).toEqual(['Action', 'When']);
    expect(byLabel(d, 'Action')?.value).toBe('mystery_action');
  });

  it('falls back to created_at when applied_at is absent', () => {
    const d = { decision_id: 'x', action: 'iac_apply', created_at: '2026-01-02T03:04:05Z' } as Decision;
    expect(byLabel(d, 'When')?.value).toContain('2026');
  });
});
