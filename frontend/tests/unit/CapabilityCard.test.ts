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
// categories, at least one rule per category. Tool/worker LISTS are trimmed
// for brevity, but every string is VERBATIM from the real GET /capabilities
// DTO (descriptions truncated only at sentence boundaries) — regenerate with:
//   .venv/bin/python -c "from agent.capabilities import build_capabilities;
//   import json; print(json.dumps(build_capabilities(), indent=2))"
// ---------------------------------------------------------------------------
const FIXTURE: Capabilities = {
  version: 1,
  provenance: 'Generated from the same constants the enforcement code imports, not hand-written documentation.',
  iam_note: 'Each worker runs as its own service account with least-privilege IAM, codified in infra/scripts/. The only identity that can change live infrastructure is the apply worker\'s service account, and only after an operator approves the exact plan.',
  workloads: [
    {
      name: 'drift',
      display_name: 'Anchor',
      descriptor: 'Cloud Run config',
      description: 'Detect drift between a Cloud Run service\'s live env vars and the team\'s declared ops-contract.yaml.',
      autonomous: true,
      tools: [
        { name: 'drift_read_live_env', description: 'Reads the live Cloud Run environment: deployed image, revision, environment variables, and service configuration.', write_capable: false },
        { name: 'notify', description: 'Sends a notification via the notifier worker (counted as write-capable because it rides a sending credential).', write_capable: true },
      ],
      workers: [{ name: 'drift_reader', description: 'Reads the live Cloud Run service state for drift detection. Read-only by the scope of calls it makes.' }],
      actions: [
        { name: 'rollback', display_name: 'Rollback (HITL)', requires_approval: true },
        { name: 'no_op', display_name: 'No action needed', requires_approval: false },
      ],
    },
    {
      name: 'upgrade',
      display_name: 'Patch',
      descriptor: 'dependencies',
      description: 'Checks the repo\'s package.json on demand for outdated dependencies (or vulnerable versions per advisory feeds) and proposes upgrade PRs.',
      // Phase 17.G: Patch is NOT autonomous — /recheck upgrade 503s, no wired
      // trigger. The DTO's autonomous flag now reflects that (was wrongly true).
      autonomous: false,
      tools: [
        { name: 'upgrade_read_dependencies', description: 'Reads the target repo\'s dependency lockfile to identify outdated packages.', write_capable: false },
        { name: 'upgrade_propose_pr', description: 'Opens a dependency-upgrade pull request in the target repo.', write_capable: true },
      ],
      workers: [{ name: 'upgrade_reader', description: 'Reads the target repo\'s dependency lockfile. Read-only by the scope of calls it makes.' }],
      actions: [],
    },
    {
      name: 'explore',
      display_name: 'Explore',
      descriptor: 'read-only',
      description: 'Read-only investigation across infra and code. Inspects a Cloud Run service\'s live env vars, the repo\'s declared ops-contract, the dependency lockfile, and authoritative developer docs — then reports. It cannot change anything: no PR, no rollback, no notification.',
      autonomous: false,
      tools: [
        { name: 'drift_read_live_env', description: 'Reads the live Cloud Run environment: deployed image, revision, environment variables, and service configuration.', write_capable: false },
      ],
      workers: [{ name: 'drift_reader', description: 'Reads the live Cloud Run service state for drift detection. Read-only by the scope of calls it makes.' }],
      actions: [],
    },
    {
      name: 'provision',
      display_name: 'Provision',
      descriptor: 'infra edits',
      description: 'Author OpenTofu (IaC) changes from a chat request and open ONE iac/-only pull request for the gated apply pipeline to plan, approve, and apply.',
      autonomous: false,
      tools: [
        { name: 'drift_read_live_env', description: 'Reads the live Cloud Run environment: deployed image, revision, environment variables, and service configuration.', write_capable: false },
        { name: 'provision_open_infra_pr', description: 'Authors OpenTofu files under iac/ and opens ONE pull request. Never applies anything; applying happens only through the gated approve-then-apply pipeline.', write_capable: true },
        { name: 'provision_propose_adoption', description: 'Adopt an existing resource into IaC management via a zero-change import PR. Renders the config deterministically; cannot modify live infrastructure.', write_capable: true },
      ],
      workers: [{ name: 'infra_reader', description: 'Reads the whole-project GCP asset inventory. Read-only by IAM (asset viewer only).' }],
      actions: [],
    },
  ],
  human_gates: [
    {
      id: 'iac_apply',
      title: 'IaC plan apply',
      description: 'Before the apply worker runs ``tofu apply``, an operator must approve the exact stored plan via the approval page. The approval is bound to the specific plan by a plan-bound HMAC with a signed expiry window. Approving one plan cannot approve another.',
      route: '/iac-approvals/{pr_number}',
      method: 'POST',
    },
    {
      id: 'rollback',
      title: 'Rollback',
      description: 'The rollback worker requires a valid operator approval token before it will execute any Cloud Run rollback. The approval is single-use with a 15-minute TTL and bound to the specific rollback request by HMAC. The worker re-verifies the token at execution time.',
      route: '/approvals/{approval_id}',
      method: 'POST',
    },
  ],
  denylist: {
    summary: 'Before any apply, the plan is checked against a fail-closed denylist. A violation blocks the apply; operator approval cannot override it.',
    enforced_at: [
      'the trusted plan-builder CI, before a plan is ever stored',
      'the approval page, as an advisory check before you approve',
      'the tofu-apply worker, immediately before apply (final gate)',
    ],
    rules: [
      { id: 'control-plane-service', description: 'The Cloud Run services.', category: 'control-plane' },
      { id: 'control-plane-sa', description: 'The service accounts.', category: 'control-plane' },
      { id: 'iam-change-forbidden-v1', description: 'Any IAM change at all, even on unrelated resources (v1 floor).', category: 'iam' },
      { id: 'delete-action-forbidden-v1', description: 'Deleting any resource (v1 floor).', category: 'global-v1' },
      { id: 'plan-json-unparseable', description: 'The plan file isn\'t valid JSON (fail-closed).', category: 'structural' },
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
    expect(denylist.textContent).toContain('The Cloud Run services.');
    expect(denylist.textContent).toContain('Its own control plane is off-limits');

    // Workloads section — all four crew identities AND their descriptors
    const workloads = getByTestId('cap-workloads');
    for (const crew of ['Anchor', 'Patch', 'Explore', 'Provision']) {
      expect(workloads.textContent).toContain(crew);
    }
    expect(workloads.textContent).toContain('Cloud Run config');
    expect(workloads.textContent).toContain('infra edits');

    // Provision shows the "On-demand · chat only" pill. GLUED-EXACT-STRING PIN
    // on the seam (Svelte 5 whitespace gotcha, PR #83 lesson): the component
    // glues name → descriptor → pill with explicit {' '} so the rendered text
    // is exactly "<name> — <descriptor> <pill>". If a {' '} is dropped the
    // strings glue and this assertion FAILS.
    const provisionSummary = workloads.querySelector('[data-testid="cap-workload-provision-summary"]');
    expect(provisionSummary).not.toBeNull();
    expect(provisionSummary!.textContent).toContain('Provision — infra edits On-demand · chat only');
    // And the pill must be the on-demand one, never the autonomous label:
    expect(provisionSummary!.textContent).not.toContain('Autonomous');

    // Same glued pin on the autonomous side of the seam (Anchor/drift):
    const driftSummary = workloads.querySelector('[data-testid="cap-workload-drift-summary"]');
    expect(driftSummary).not.toBeNull();
    expect(driftSummary!.textContent).toContain('Anchor — Cloud Run config Autonomous · also chat');
  });

  it('3b. each workload summary leads with its verb glyph (decorative, aria-hidden, keyed on the symbolic value)', async () => {
    const paths: string[] = [];
    const { getByTestId } = render(CapabilityCard, {
      props: { call: makeCall(paths) },
    });
    const card = getByTestId('capability-card') as HTMLDetailsElement;
    card.open = true;
    await fireEvent(card, new Event('toggle'));

    const workloads = await waitFor(() => getByTestId('cap-workloads'));
    // One glyph per workload, keyed on the FROZEN symbolic value (wl.name),
    // never the display name. The glyph is the first child of the summary so it
    // does not disturb the glued name → descriptor → pill text seams.
    for (const verb of ['drift', 'upgrade', 'explore', 'provision']) {
      const summary = workloads.querySelector(
        `[data-testid="cap-workload-${verb}-summary"]`,
      ) as HTMLElement;
      expect(summary).not.toBeNull();
      const glyph = summary.querySelector(`[data-testid="crew-glyph-${verb}"]`);
      expect(glyph).not.toBeNull();
      // Decorative only — meaning lives in the card text, not the SVG.
      expect(glyph!.getAttribute('aria-hidden')).toBe('true');
      // First element child of the summary (leads the flex row, before the name).
      expect(summary.firstElementChild).toBe(glyph);
    }
  });

  it('3c. each workload body carries its stewardship-loop role line (honest, no auto-remediation claim)', async () => {
    const paths: string[] = [];
    const { getByTestId } = render(CapabilityCard, {
      props: { call: makeCall(paths) },
    });
    const el = getByTestId('capability-card') as HTMLDetailsElement;
    el.open = true;
    await fireEvent(el, new Event('toggle'));

    await waitFor(() => getByTestId('cap-workloads'));
    // The loop line lives in the BODY, not the summary (the summary has a
    // glued-exact-string pin). One per crew, keyed on the symbolic value.
    const provLoop = getByTestId('cap-workload-provision-loop');
    expect(provLoop.textContent).toContain('In the loop ·');
    expect(provLoop.textContent).toContain('Stands infrastructure up');
    const driftLoop = getByTestId('cap-workload-drift-loop');
    expect(driftLoop.textContent).toContain('Guards what is live');
    // Honesty: Anchor's loop line reacts to/detects drift; it must never claim
    // it fixes drift on its own (remediation is gated, shown in the gates section).
    expect(driftLoop.textContent?.toLowerCase()).not.toContain('fixes');
    expect(driftLoop.textContent?.toLowerCase()).not.toContain('auto');
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
      expect(getByRole('heading', { name: /always blocked/i })).toBeTruthy();
      expect(getByRole('heading', { name: /what each workload can use/i })).toBeTruthy();
    });
  });

  it('7. malformed 200 (valid JSON, missing structure) → cap-error + working retry', async () => {
    // A 200 whose body parses but lacks the load-bearing keys must route to
    // the error/retry path, NOT set the cache flag: Svelte 5 has no error
    // boundary, so without the structural check the template would throw on
    // the missing arrays and leave a blank panel with no way to re-attempt.
    let callCount = 0;
    const paths: string[] = [];
    const call = async (path: string): Promise<Response> => {
      paths.push(path);
      callCount++;
      if (callCount === 1) {
        return new Response(JSON.stringify({ version: 1 }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        });
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

    // Error row appears — the malformed body must not blank the panel
    await waitFor(() => {
      expect(getByTestId('cap-error')).toBeTruthy();
    });
    expect(paths).toHaveLength(1);

    // Retry refetches and renders the good response
    await fireEvent.click(getByTestId('cap-retry'));
    await waitFor(() => {
      expect(getByTestId('cap-gates')).toBeTruthy();
    });
    expect(paths).toHaveLength(2);
  });

  it('8. adoptable_resource_types field present → labels render in denylist section', async () => {
    const withAdoptable: Capabilities = {
      ...FIXTURE,
      denylist: {
        ...FIXTURE.denylist,
        adoptable_resource_types: [
          { type: 'google_storage_bucket', label: 'Cloud Storage bucket' },
          { type: 'google_pubsub_topic', label: 'Pub/Sub topic' },
        ],
      },
    };
    const paths: string[] = [];
    const call = makeCall(
      paths,
      new Response(JSON.stringify(withAdoptable), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    );
    const { container } = render(CapabilityCard, { props: { call } });
    const el = container.querySelector('[data-testid="capability-card"]') as HTMLDetailsElement;
    el.open = true;
    await fireEvent(el, new Event('toggle'));

    await waitFor(() => {
      const p = container.querySelector('.cap-denylist__adoptable');
      expect(p).toBeTruthy();
      expect(p!.textContent).toContain('Cloud Storage bucket');
      expect(p!.textContent).toContain('Pub/Sub topic');
    });
  });

  it('9. adoptable_resource_types field absent → card renders without adoptable line (no crash)', async () => {
    // FIXTURE has no adoptable_resource_types — the {#if} block must be absent, not throw.
    const paths: string[] = [];
    const { container } = render(CapabilityCard, { props: { call: makeCall(paths) } });
    const el = container.querySelector('[data-testid="capability-card"]') as HTMLDetailsElement;
    el.open = true;
    await fireEvent(el, new Event('toggle'));

    await waitFor(() => {
      // The denylist section renders (the enforced_at line is always present)
      expect(container.querySelector('.cap-denylist__enforced')).toBeTruthy();
    });
    // The adoptable line must NOT be present
    expect(container.querySelector('.cap-denylist__adoptable')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Autonomy-mode note — now supplied as a prop (App derives it from the shared
// autonomyStore via autonomyNoteFor). The copy + best-effort silence
// (loading/unknown/propose_apply → null) are pinned in autonomyStore.test.ts;
// here we only verify the card renders the prop or stays silent on null.
// ---------------------------------------------------------------------------

describe('CapabilityCard — autonomy note (via prop)', () => {
  function okJson(b: unknown): Response {
    return new Response(JSON.stringify(b), { status: 200, headers: { 'content-type': 'application/json' } });
  }

  it('renders the note when autonomyNote is provided', async () => {
    const { getByTestId } = render(CapabilityCard, {
      props: { call: async () => okJson(FIXTURE), autonomyNote: 'NOTE-TEXT-XYZ' },
    });
    const el = getByTestId('capability-card') as HTMLDetailsElement;
    el.open = true;
    await fireEvent(el, new Event('toggle'));
    await waitFor(() =>
      expect(getByTestId('capability-autonomy-note').textContent).toContain('NOTE-TEXT-XYZ'),
    );
  });

  it('omits the note when autonomyNote is null (default)', async () => {
    const { getByTestId, queryByTestId } = render(CapabilityCard, {
      props: { call: async () => okJson(FIXTURE) },
    });
    const el = getByTestId('capability-card') as HTMLDetailsElement;
    el.open = true;
    await fireEvent(el, new Event('toggle'));
    await waitFor(() => expect(getByTestId('cap-gates')).toBeTruthy());
    expect(queryByTestId('capability-autonomy-note')).toBeNull();
  });

  it('never fetches /autonomy itself (single source of truth is the store)', async () => {
    const paths: string[] = [];
    const { getByTestId } = render(CapabilityCard, {
      props: {
        call: async (p: string) => { paths.push(p); return okJson(FIXTURE); },
        autonomyNote: 'X',
      },
    });
    const el = getByTestId('capability-card') as HTMLDetailsElement;
    el.open = true;
    await fireEvent(el, new Event('toggle'));
    await waitFor(() => expect(getByTestId('cap-gates')).toBeTruthy());
    expect(paths).not.toContain('/autonomy');
  });
});

// ---------------------------------------------------------------------------
// Task 4 — per-crew lazy prompt disclosure in CapabilityCard
// ---------------------------------------------------------------------------

describe('CapabilityCard — per-crew prompt disclosure (Task 4)', () => {
  it('lazy-loads and renders a crew prompt with distinct chat prompt', async () => {
    const PROMPTS = {
      workload: 'drift', display_name: 'Anchor', descriptor: 'Cloud Run config',
      recheck_prompt: 'RECHECK-PROMPT-TEXT', chat_prompt: 'CHAT-PROMPT-TEXT',
      chat_prompt_distinct: true, source_dir: 'workloads/drift',
      revision: 'driftscribe-agent-00094-7cr', demo_note: 'Demo: prompts are soft guidance.',
    };
    const call = async (path: string) => {
      if (path === '/capabilities') return new Response(JSON.stringify(FIXTURE), { status: 200, headers: { 'content-type': 'application/json' } });
      if (path === '/autonomy') return new Response(JSON.stringify({ mode: 'propose_apply' }), { status: 200, headers: { 'content-type': 'application/json' } });
      if (path === '/workloads/drift/prompts') return new Response(JSON.stringify(PROMPTS), { status: 200, headers: { 'content-type': 'application/json' } });
      return new Response('not found', { status: 404 });
    };
    const { getByTestId } = render(CapabilityCard, { props: { call } });
    const card = getByTestId('capability-card') as HTMLDetailsElement;
    card.open = true; await fireEvent(card, new Event('toggle'));
    await waitFor(() => getByTestId('cap-workload-drift-summary'));
    const promptsDetails = getByTestId('cap-workload-drift-prompts') as HTMLDetailsElement;
    promptsDetails.open = true; await fireEvent(promptsDetails, new Event('toggle'));
    await waitFor(() => {
      expect(getByTestId('cap-workload-drift-prompts').textContent).toContain('RECHECK-PROMPT-TEXT');
      expect(getByTestId('cap-workload-drift-prompts').textContent).toContain('CHAT-PROMPT-TEXT');
      expect(getByTestId('cap-workload-drift-prompts').textContent).toContain('Demo: prompts are soft guidance.');
    });
  });

  it('renders single-prompt crew branch (chat_prompt_distinct=false): recheck renders, no-separate-chat copy shown, exactly one <pre>', async () => {
    const PROMPTS = {
      workload: 'explore', display_name: 'Explore', descriptor: 'read-only',
      recheck_prompt: 'EXPLORE-PROMPT-TEXT', chat_prompt: null,
      chat_prompt_distinct: false, source_dir: 'workloads/explore',
      revision: 'driftscribe-agent-00094-7cr', demo_note: 'Demo: prompts are soft guidance.',
    };
    const call = async (path: string) => {
      if (path === '/capabilities') return new Response(JSON.stringify(FIXTURE), { status: 200, headers: { 'content-type': 'application/json' } });
      if (path === '/autonomy') return new Response(JSON.stringify({ mode: 'propose_apply' }), { status: 200, headers: { 'content-type': 'application/json' } });
      if (path === '/workloads/explore/prompts') return new Response(JSON.stringify(PROMPTS), { status: 200, headers: { 'content-type': 'application/json' } });
      return new Response('not found', { status: 404 });
    };
    const { getByTestId } = render(CapabilityCard, { props: { call } });
    const card = getByTestId('capability-card') as HTMLDetailsElement;
    card.open = true; await fireEvent(card, new Event('toggle'));
    await waitFor(() => getByTestId('cap-workload-explore-summary'));
    const promptsDetails = getByTestId('cap-workload-explore-prompts') as HTMLDetailsElement;
    promptsDetails.open = true; await fireEvent(promptsDetails, new Event('toggle'));
    await waitFor(() => {
      // (a) recheck prompt text renders
      expect(promptsDetails.textContent).toContain('EXPLORE-PROMPT-TEXT');
      // (b) no-separate-chat-prompt copy shown
      expect(promptsDetails.textContent).toContain('no separate chat prompt');
      // (c) exactly one <pre> — no chat prompt leaks
      expect(promptsDetails.querySelectorAll('pre')).toHaveLength(1);
    });
  });

  it('fails soft when the prompt fetch errors (no red error, no throw)', async () => {
    const call = async (path: string) => {
      if (path === '/capabilities') return new Response(JSON.stringify(FIXTURE), { status: 200, headers: { 'content-type': 'application/json' } });
      if (path === '/autonomy') return new Response(JSON.stringify({ mode: 'propose_apply' }), { status: 200, headers: { 'content-type': 'application/json' } });
      return new Response('boom', { status: 500 });   // prompts fetch fails
    };
    const { getByTestId } = render(CapabilityCard, { props: { call } });
    const card = getByTestId('capability-card') as HTMLDetailsElement;
    card.open = true; await fireEvent(card, new Event('toggle'));
    await waitFor(() => getByTestId('cap-workload-drift-summary'));
    const promptsDetails = getByTestId('cap-workload-drift-prompts') as HTMLDetailsElement;
    promptsDetails.open = true; await fireEvent(promptsDetails, new Event('toggle'));
    await waitFor(() => expect(promptsDetails.textContent?.toLowerCase()).toContain('unavailable'));
  });
});
