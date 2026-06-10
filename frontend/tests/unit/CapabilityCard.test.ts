import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import CapabilityCard from '../../src/components/CapabilityCard.svelte';
import type { Capabilities } from '../../src/lib/capabilities';

// Component tests for CapabilityCard — the lazy-fetch, collapsed <details>
// panel that shows the agent's safety cage.
//
// jsdom keeps closed-<details> content in the DOM, so we can assert on the
// body without opening the panel (for the "no fetch on mount" test). When we
// need to simulate opening, we set detailsEl.open = true then dispatch a
// 'toggle' event (jsdom does not reliably fire ontoggle from a summary click).

afterEach(cleanup);

// ---------------------------------------------------------------------------
// Representative fixture — all four workloads, both gates, all four rule
// categories, at least one rule per category. Tool lists trimmed for brevity.
// ---------------------------------------------------------------------------
const FIXTURE: Capabilities = {
  version: 1,
  provenance: 'Generated from the same constants the enforcement code imports — not hand-written documentation.',
  iam_note: 'Each worker runs as its own service account with least-privilege IAM, codified in infra/scripts/.',
  workloads: [
    {
      name: 'drift',
      display_name: 'Cloud Run config',
      description: 'Detect drift between a Cloud Run service\'s live env vars and the team\'s declared ops-contract.yaml.',
      autonomous: true,
      tools: [
        { name: 'drift_read_live_env', description: 'Reads the live Cloud Run environment.', write_capable: false },
        { name: 'notify', description: 'Sends a notification via the notifier worker (counted as write-capable because it rides a sending credential).', write_capable: true },
      ],
      workers: [{ name: 'drift_reader', description: 'Reads the live Cloud Run service state.' }],
      actions: [
        { name: 'rollback', display_name: 'Rollback (HITL)', requires_approval: true },
        { name: 'no_op', display_name: 'No action needed', requires_approval: false },
      ],
    },
    {
      name: 'upgrade',
      display_name: 'Dependencies',
      description: 'Watch the repo\'s package.json for outdated dependencies.',
      autonomous: true,
      tools: [
        { name: 'upgrade_read_dependencies', description: 'Reads the target repo\'s dependency lockfile.', write_capable: false },
        { name: 'upgrade_propose_pr', description: 'Opens a dependency-upgrade pull request.', write_capable: true },
      ],
      workers: [{ name: 'upgrade_reader', description: 'Reads dependency lockfiles.' }],
      actions: [],
    },
    {
      name: 'explore',
      display_name: 'Explore (chat)',
      description: 'Answer questions about the project\'s infrastructure using read-only tools.',
      autonomous: false,
      tools: [
        { name: 'drift_read_live_env', description: 'Reads the live Cloud Run environment.', write_capable: false },
      ],
      workers: [{ name: 'drift_reader', description: 'Reads the live Cloud Run service state.' }],
      actions: [],
    },
    {
      name: 'provision',
      display_name: 'Provision (infra edits)',
      description: 'Author OpenTofu (IaC) changes from a chat request and open ONE iac/-only pull request.',
      autonomous: false,
      tools: [
        { name: 'drift_read_live_env', description: 'Reads the live Cloud Run environment.', write_capable: false },
        { name: 'provision_open_infra_pr', description: 'Authors OpenTofu files under iac/ and opens ONE pull request — never applies anything; applying happens only through the gated approve-then-apply pipeline.', write_capable: true },
      ],
      workers: [{ name: 'infra_reader', description: 'Reads the whole-project GCP asset inventory. Read-only by IAM (asset viewer only).' }],
      actions: [],
    },
  ],
  human_gates: [
    {
      id: 'iac_apply',
      title: 'IaC plan apply',
      description: 'Before the apply worker runs tofu apply, an operator must approve the exact stored plan. The approval is bound to the specific plan by a plan-bound HMAC with a signed expiry window.',
      route: '/iac-approvals/{pr_number}',
      method: 'POST',
    },
    {
      id: 'rollback',
      title: 'Rollback',
      description: 'The rollback worker requires a valid operator approval token. The approval is single-use with a 15-minute TTL and bound by HMAC — the worker re-verifies the token at execution time.',
      route: '/approvals/{approval_id}',
      method: 'POST',
    },
  ],
  denylist: {
    summary: 'Before any apply, the plan is checked against a fail-closed denylist. A violation blocks the apply — operator approval cannot override it.',
    enforced_at: [
      'the trusted plan-builder CI, before a plan is ever stored',
      'the approval page, as an advisory check before you approve',
      'the tofu-apply worker, immediately before apply (final gate)',
    ],
    rules: [
      { id: 'control-plane-service', description: 'No change may touch DriftScribe\'s own Cloud Run services.', category: 'control-plane' },
      { id: 'control-plane-sa', description: 'No change may touch DriftScribe\'s own service accounts.', category: 'control-plane' },
      { id: 'iam-change-forbidden-v1', description: 'All IAM changes are refused — even on unrelated resources (v1 floor).', category: 'iam' },
      { id: 'delete-action-forbidden-v1', description: 'All deletes are refused — the agent cannot destroy any resource (v1 floor).', category: 'global-v1' },
      { id: 'plan-json-unparseable', description: 'The plan file is not valid JSON — rejected outright (fail-closed).', category: 'structural' },
    ],
  },
};

// ---------------------------------------------------------------------------
// call stub factory — records paths; returns Response with FIXTURE by default.
// ---------------------------------------------------------------------------
function makeCall(
  paths: string[],
  response: Response = new Response(JSON.stringify(FIXTURE), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  }),
): (path: string) => Promise<Response> {
  return async (path: string) => {
    paths.push(path);
    return response;
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('CapabilityCard', () => {
  it('1. renders collapsed with no fetch performed', () => {
    const paths: string[] = [];
    const { getByTestId } = render(CapabilityCard, {
      props: { call: makeCall(paths) },
    });
    const el = getByTestId('capability-card') as HTMLDetailsElement;
    expect(el.open).toBe(false);
    expect(paths).toHaveLength(0);
  });

  it('2. opening fetches /capabilities exactly once; re-toggle does not refetch', async () => {
    const paths: string[] = [];
    const { getByTestId } = render(CapabilityCard, {
      props: { call: makeCall(paths) },
    });
    const el = getByTestId('capability-card') as HTMLDetailsElement;

    // First open
    el.open = true;
    await fireEvent(el, new Event('toggle'));
    await waitFor(() => expect(paths).toContain('/capabilities'));
    expect(paths.filter(p => p === '/capabilities')).toHaveLength(1);

    // Close and re-open — must NOT refetch
    el.open = false;
    await fireEvent(el, new Event('toggle'));
    el.open = true;
    await fireEvent(el, new Event('toggle'));
    // Still only one fetch
    expect(paths.filter(p => p === '/capabilities')).toHaveLength(1);
  });

  it('3. renders all four sections with correct content from DTO fixture', async () => {
    const paths: string[] = [];
    const { getByTestId, getByText } = render(CapabilityCard, {
      props: { call: makeCall(paths) },
    });
    const el = getByTestId('capability-card') as HTMLDetailsElement;
    el.open = true;
    await fireEvent(el, new Event('toggle'));

    // Gates section — both gate titles present
    await waitFor(() => {
      const gates = getByTestId('cap-gates');
      expect(gates.textContent).toContain('IaC plan apply');
      expect(gates.textContent).toContain('Rollback');
    });

    // Denylist section — a control-plane rule description AND its category heading
    const denylist = getByTestId('cap-denylist');
    expect(denylist.textContent).toContain('No change may touch DriftScribe\'s own Cloud Run services.');
    expect(denylist.textContent).toContain('Its own control plane is untouchable');

    // Workloads section — all four display names
    const workloads = getByTestId('cap-workloads');
    expect(workloads.textContent).toContain('Cloud Run config');
    expect(workloads.textContent).toContain('Dependencies');
    expect(workloads.textContent).toContain('Explore (chat)');
    expect(workloads.textContent).toContain('Provision (infra edits)');

    // Provision shows "chat-only" pill — exact-string pin on the pill seam.
    // The pill text must be "chat-only" (not "autonomous + chat").
    // Svelte 5 whitespace gotcha: the seam uses {' '} so the rendered text
    // is exactly "<display_name> · chat-only".
    const provisionSummary = workloads.querySelector('[data-testid="cap-workload-provision-summary"]');
    expect(provisionSummary).not.toBeNull();
    expect(provisionSummary!.textContent).toContain('Provision (infra edits)');
    expect(provisionSummary!.textContent).toContain('chat-only');
    // Glued-exact-string pin on the autonomous pill seam:
    expect(provisionSummary!.textContent).not.toContain('autonomous');

    // Drift should show "autonomous + chat"
    const driftSummary = workloads.querySelector('[data-testid="cap-workload-drift-summary"]');
    expect(driftSummary).not.toBeNull();
    expect(driftSummary!.textContent).toContain('autonomous + chat');
  });

  it('4. write_capable badge: provision_open_infra_pr shows "write-capable", read tool shows "read"', async () => {
    const paths: string[] = [];
    const { getByTestId } = render(CapabilityCard, {
      props: { call: makeCall(paths) },
    });
    const el = getByTestId('capability-card') as HTMLDetailsElement;
    el.open = true;
    await fireEvent(el, new Event('toggle'));

    await waitFor(() => {
      const workloads = getByTestId('cap-workloads');
      // provision_open_infra_pr is write_capable: true → badge "write-capable"
      const writeRow = workloads.querySelector('[data-testid="cap-tool-provision_open_infra_pr"]');
      expect(writeRow).not.toBeNull();
      expect(writeRow!.textContent).toContain('write-capable');

      // drift_read_live_env is write_capable: false → badge "read"
      // (first occurrence in drift workload)
      const readRow = workloads.querySelector('[data-testid="cap-tool-drift_read_live_env"]');
      expect(readRow).not.toBeNull();
      expect(readRow!.textContent).toContain('read');
    });
  });

  it('5. fetch failure → cap-error visible; cap-retry refetches and renders on success', async () => {
    // First call returns 500; second returns FIXTURE
    let callCount = 0;
    const paths: string[] = [];
    const call = async (path: string): Promise<Response> => {
      paths.push(path);
      callCount++;
      if (callCount === 1) {
        return new Response('Server error', { status: 500 });
      }
      return new Response(JSON.stringify(FIXTURE), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    };

    const { getByTestId } = render(CapabilityCard, { props: { call } });
    const el = getByTestId('capability-card') as HTMLDetailsElement;
    el.open = true;
    await fireEvent(el, new Event('toggle'));

    // Error row should appear
    await waitFor(() => {
      expect(getByTestId('cap-error')).toBeTruthy();
    });
    expect(paths).toHaveLength(1);

    // Click Retry
    const retryBtn = getByTestId('cap-retry');
    await fireEvent.click(retryBtn);

    // Second fetch → renders gates
    await waitFor(() => {
      expect(getByTestId('cap-gates')).toBeTruthy();
    });
    expect(paths).toHaveLength(2);
  });

  it('6. accessibility: sections are headed elements', async () => {
    const paths: string[] = [];
    const { getByRole } = render(CapabilityCard, {
      props: { call: makeCall(paths) },
    });
    const el = document.querySelector('[data-testid="capability-card"]') as HTMLDetailsElement;
    el.open = true;
    await fireEvent(el, new Event('toggle'));

    await waitFor(() => {
      // Each of the three main sections must have a heading
      expect(getByRole('heading', { name: /always needs your approval/i })).toBeTruthy();
      expect(getByRole('heading', { name: /blocked outright/i })).toBeTruthy();
      expect(getByRole('heading', { name: /what each workload can use/i })).toBeTruthy();
    });
  });
});
