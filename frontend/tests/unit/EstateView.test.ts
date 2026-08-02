import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import EstateView from '../../src/components/EstateView.svelte';
import type { InfraGraph, InfraGroup, InfraNode, PendingApproval } from '../../src/lib/infra_graph';
import { investigateUnmatchedPrefill } from '../../src/lib/infra_graph';
import type { Decision } from '../../src/lib/types';

afterEach(cleanup);

const BUCKET = 'storage.googleapis.com/Bucket';
const RUN = 'run.googleapis.com/Service';

function node(over: Partial<InfraNode> = {}): InfraNode {
  return {
    id: 'n0',
    label: 'demo-node',
    asset_type: BUCKET,
    managed: false,
    location: 'asia-northeast1',
    ...over,
  };
}

function group(over: Partial<InfraGroup> = {}): InfraGroup {
  return {
    asset_type: BUCKET,
    label: 'Storage bucket',
    count: 1,
    managed: 0,
    drift: 1,
    sensitive: false,
    adoptable: true,
    nodes: [node()],
    ...over,
  };
}

function graph(over: Partial<InfraGraph> = {}): InfraGraph {
  return {
    generated_at: '2026-07-28T06:00:00Z',
    project: 'demo-proj',
    caveat: '',
    // ds-1vn: a healthy deployment's snapshot matches. Set EXPLICITLY, because
    // omitting it now means "unverified", which suppresses adoption — every
    // unrelated adopt test would then be exercising the suppressed path and
    // proving nothing about the one it is named for.
    iac_snapshot_stale: false,
    degraded: false,
    degraded_reason: null,
    totals: { resources: 10, managed: 5, drift: 5 },
    groups: [group()],
    edges: [],
    ...over,
  };
}

function pending(over: Partial<PendingApproval> = {}): PendingApproval {
  return {
    pr_number: 268,
    title: 'Adopt receipts',
    url: 'https://github.com/x/y/pull/268',
    asset_type: BUCKET,
    resource_name: 'demo-node',
    ...over,
  };
}

/** A PRIMARY card (managed > 0) of a NON-adoptable type, so its unmanaged node
 *  buckets as `untracked` rather than drift — the fixture the fold needs. */
function untrackedGraph(): InfraGraph {
  return graph({
    groups: [
      group({
        asset_type: RUN,
        label: 'Cloud Run',
        count: 2,
        managed: 1,
        drift: 1,
        adoptable: false,
        nodes: [
          node({ id: 'r0', label: 'checkout', asset_type: RUN, managed: true }),
          node({ id: 'r1', label: 'legacy-worker', asset_type: RUN, managed: false }),
        ],
      }),
    ],
  });
}

function baseProps(over: Record<string, unknown> = {}) {
  return {
    graph: graph(),
    decisions: [] as Decision[],
    pendingApprovals: [] as PendingApproval[],
    ...over,
  };
}

describe('EstateView — loading/degraded honesty', () => {
  it('renders an honest loading line when the graph has not loaded yet', () => {
    // `settled: false` is what "has not loaded yet" now means precisely — a
    // null graph on a SETTLED cycle is a finished failure, not a load in
    // progress, and takes the degraded branch below (Codex review of #258).
    const { getByTestId, queryByTestId } = render(EstateView, {
      props: baseProps({ graph: null, settled: false }),
    });
    expect(getByTestId('estate-loading')).toBeTruthy();
    expect(queryByTestId('estate-row')).toBeNull();
  });

  it('renders an honest degraded line, never a fake-empty "all clear"', () => {
    const { getByTestId, queryByTestId } = render(EstateView, {
      props: baseProps({ graph: graph({ degraded: true }) }),
    });
    expect(getByTestId('estate-degraded')).toBeTruthy();
    expect(queryByTestId('estate-row')).toBeNull();
  });
});

describe('EstateView — rows', () => {
  it('renders a drift row (hollow dot) and a managed row (filled dot), grouped by status', () => {
    const g = graph({
      groups: [
        group({
          asset_type: RUN,
          label: 'Cloud Run',
          count: 2,
          managed: 1,
          drift: 1,
          adoptable: true,
          nodes: [
            node({ id: 'r0', label: 'checkout', asset_type: RUN, managed: true }),
            node({ id: 'r1', label: 'storefront', asset_type: RUN, managed: false }),
          ],
        }),
      ],
    });
    const { getByTestId, getAllByTestId } = render(EstateView, { props: baseProps({ graph: g }) });
    expect(getByTestId('estate-group-drift').textContent).toContain('1');
    expect(getByTestId('estate-group-managed').textContent).toContain('1');
    const rows = getAllByTestId('estate-row');
    expect(rows).toHaveLength(2);
    expect(rows.some((r) => r.textContent?.includes('storefront'))).toBe(true);
    expect(rows.some((r) => r.textContent?.includes('checkout'))).toBe(true);
  });

  it('collapses the untracked group into a fold, count in the summary', () => {
    const { getByTestId, getAllByTestId } = render(EstateView, {
      props: baseProps({ graph: untrackedGraph() }),
    });
    const fold = getByTestId('estate-untracked-fold');
    expect(fold.tagName).toBe('DETAILS');
    expect(fold.hasAttribute('open')).toBe(false);
    // The count is the information; the rows are detail-on-demand.
    expect(fold.querySelector('summary')?.textContent).toContain('1');
    // Rows still render (inside the fold) under the unchanged testid.
    const inFold = getAllByTestId('estate-row').filter((r) => fold.contains(r));
    expect(inFold).toHaveLength(1);
    expect(inFold[0].textContent).toContain('legacy-worker');
  });

  it('omits the untracked fold entirely when nothing is untracked', () => {
    const { queryByTestId } = render(EstateView, { props: baseProps() });
    expect(queryByTestId('estate-untracked-fold')).toBeNull();
  });
});

// 2026-07-31 desk+estate merge — this is a SECTION of the desk page now, not a
// screen of its own. The desk owns the single instrument band, and the way back
// to the desk is "you are already there".
describe('EstateView — a section, not a screen', () => {
  it('renders no instrument band of its own (the desk owns the band)', () => {
    const { queryByTestId } = render(EstateView, { props: baseProps({ settled: true }) });
    expect(queryByTestId('instrument-band-managed')).toBeNull();
    expect(queryByTestId('instrument-band-drift')).toBeNull();
    expect(queryByTestId('instrument-band-awaiting')).toBeNull();
  });

  it('renders no back-to-desk button (it lives on the desk now)', () => {
    const { queryByTestId } = render(EstateView, { props: baseProps({ settled: true }) });
    expect(queryByTestId('estate-back-desk')).toBeNull();
  });

  it('carries id="estate" and tabindex="-1" on the section root — the band scroll target', () => {
    // tabindex is what lets scrollToEstate() MOVE FOCUS with the scroll, so a
    // keyboard user does not stay parked on the band button above the viewport.
    const { getByTestId } = render(EstateView, { props: baseProps({ settled: true }) });
    const root = getByTestId('estate-view');
    expect(root.id).toBe('estate');
    expect(root.getAttribute('tabindex')).toBe('-1');
  });
});

describe('EstateView — adopt chip vs. PR-open chip', () => {
  it('an adoptable drift row with no open PR shows the adopt chip and fires onAdopt with the exact prefill', async () => {
    const onAdopt = vi.fn();
    const g = graph({
      groups: [
        group({
          nodes: [node({ id: 'b0', label: 'shipping-topic', location: 'asia-northeast1' })],
        }),
      ],
    });
    const { getByTestId, queryByTestId } = render(EstateView, {
      props: baseProps({ graph: g, onAdopt }),
    });
    expect(queryByTestId('estate-pr-chip')).toBeNull();
    const btn = getByTestId('estate-adopt-btn');
    await fireEvent.click(btn);
    expect(onAdopt).toHaveBeenCalledTimes(1);
    expect(onAdopt.mock.calls[0][0]).toContain('shipping-topic');
    expect(onAdopt.mock.calls[0][0]).toContain('asia-northeast1');
  });

  it('a drift row with an open adoption PR shows the PR-review chip INSTEAD of the adopt button (button absent)', () => {
    const g = graph({
      groups: [
        group({
          nodes: [node({ id: 'b0', label: 'receipts' })],
        }),
      ],
    });
    const approvals = [pending({ asset_type: BUCKET, resource_name: 'receipts', pr_number: 268 })];
    const { getByTestId, queryByTestId } = render(EstateView, {
      props: baseProps({ graph: g, pendingApprovals: approvals }),
    });
    expect(queryByTestId('estate-adopt-btn')).toBeNull();
    expect(getByTestId('estate-pr-chip').textContent).toContain('268');
  });

  it('respects adoptDisabled — the adopt button is disabled and the click is a no-op', async () => {
    const onAdopt = vi.fn();
    const { getByTestId } = render(EstateView, {
      props: baseProps({ onAdopt, adoptDisabled: true }),
    });
    const btn = getByTestId('estate-adopt-btn') as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    await fireEvent.click(btn);
    expect(onAdopt).not.toHaveBeenCalled();
  });
});

// The unmatched-declarations group, moved here from InfraDiagram's chat panel
// (ds-zld). The band's own semantics were settled in #244 and are re-pinned
// here rather than assumed: what carries the count, that a declaration is NOT a
// live resource, and that Investigate hands the SAME string InfraDiagram did.
describe('EstateView — declared in IaC, not found live', () => {
  const DECL = {
    id: 'u0',
    asset_type: RUN,
    type_label: 'Cloud Run service',
    label: 'storefront-old',
    address: 'google_cloud_run_v2_service.storefront_old',
  };

  function declGraph(over: Partial<InfraGraph> = {}, decls = [DECL], count = 1, truncated = 0) {
    return graph({
      unmatched_declarations: { count, truncated, entries: decls },
      ...over,
    });
  }

  it('renders a row per declaration, with its type and HCL address', () => {
    const { getByTestId, getAllByTestId } = render(EstateView, {
      props: baseProps({ graph: declGraph() }),
    });
    expect(getByTestId('estate-group-unmatched').textContent).toContain('1');
    const rows = getAllByTestId('estate-unmatched-row');
    expect(rows).toHaveLength(1);
    expect(rows[0].textContent).toContain('storefront-old');
    expect(rows[0].textContent).toContain('google_cloud_run_v2_service.storefront_old');
    expect(rows[0].textContent).toContain('Cloud Run service');
    // Evidence, not proof — the lead saying so travelled with the rows.
    expect(getByTestId('estate-unmatched-lead').textContent).toContain(
      'did not match the latest Cloud Asset Inventory snapshot',
    );
  });

  it('is absent for an old DTO with no unmatched_declarations field, and for an empty one', () => {
    const { queryByTestId } = render(EstateView, { props: baseProps() });
    expect(queryByTestId('estate-group-unmatched')).toBeNull();
    expect(queryByTestId('estate-unmatched-row')).toBeNull();
    cleanup();

    const empty = render(EstateView, {
      props: baseProps({
        graph: graph({ unmatched_declarations: { count: 0, truncated: 0, entries: [] } }),
      }),
    });
    expect(empty.queryByTestId('estate-group-unmatched')).toBeNull();
  });

  it('is NOT a resource row — it carries no status dot and never joins drift or managed', () => {
    // The whole reason these live in their own group: the dot coding in this
    // list means "live resource, managed or drifted", and a declaration with no
    // resource is neither. A dot here would assert something about a thing that
    // does not exist. The CELL is still there so the names line up (jsdom can't
    // see that; the class is the proxy the visual spec measures).
    const { getByTestId, queryAllByTestId } = render(EstateView, {
      props: baseProps({ graph: declGraph() }),
    });
    const row = getByTestId('estate-unmatched-row');
    expect(row.querySelector('.estate-view__dot')).toBeTruthy();
    expect(row.querySelector('.estate-view__dot--none')).toBeTruthy();
    // And it did not leak into the live-resource groups' row list.
    for (const r of queryAllByTestId('estate-row')) {
      expect(r.textContent).not.toContain('storefront-old');
    }
  });

  it('Investigate fires onInvestigate with the same prefill InfraDiagram built', async () => {
    // Byte-for-byte the shared builder's output, not a lookalike: this is the
    // string that reaches Provision, and the move must not have reworded it.
    const onInvestigate = vi.fn();
    const onAdopt = vi.fn();
    const g = declGraph();
    const { getByTestId } = render(EstateView, {
      props: baseProps({ graph: g, onInvestigate, onAdopt }),
    });
    await fireEvent.click(getByTestId('estate-unmatched-investigate'));
    expect(onInvestigate).toHaveBeenCalledTimes(1);
    expect(onInvestigate.mock.calls[0][0]).toBe(investigateUnmatchedPrefill(DECL, g));
    // A separate errand from Adopt, on a separate callback.
    expect(onAdopt).not.toHaveBeenCalled();
  });

  it('respects adoptDisabled — Investigate is disabled and the click is a no-op', async () => {
    const onInvestigate = vi.fn();
    const { getByTestId } = render(EstateView, {
      props: baseProps({ graph: declGraph(), onInvestigate, adoptDisabled: true }),
    });
    const btn = getByTestId('estate-unmatched-investigate') as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    await fireEvent.click(btn);
    expect(onInvestigate).not.toHaveBeenCalled();
  });

  it('heading shows the TRUE count and the trailer reports what the cap hid', () => {
    // Same discipline as driftHidden and systemManagedTotal: the count is the
    // server's, the rows are a capped sample, and the difference is stated
    // rather than silently dropped.
    const { getByTestId } = render(EstateView, {
      props: baseProps({ graph: declGraph({}, [DECL], 3, 2) }),
    });
    expect(getByTestId('estate-group-unmatched').textContent).toContain('3');
    expect(getByTestId('estate-unmatched-more').textContent).toContain('2');
  });

  it('a degraded graph reports no declarations at all', () => {
    // A degraded read's declaration list is exactly as untrustworthy as its
    // resource list. This must not become the one figure that survives a failed
    // fetch and reads as fact (ds-eh6).
    // What delivers this is the group's POSITION — inside the same loaded-only
    // branch every other group renders in — not a `degraded` term in the
    // component's own derived. That distinction is the assertion: hoist this
    // group out of that branch and this test goes red, which is precisely the
    // mistake a later edit could make while the rows still "look" guarded.
    const { getByTestId, queryByTestId } = render(EstateView, {
      props: baseProps({ graph: declGraph({ degraded: true }) }),
    });
    expect(getByTestId('estate-degraded')).toBeTruthy();
    expect(queryByTestId('estate-group-unmatched')).toBeNull();
    expect(queryByTestId('estate-unmatched-row')).toBeNull();
  });
});

describe('EstateView — system-managed fold', () => {
  it('renders the fold with the TRUE control-plane total, not just the sampled rows', () => {
    const g = graph({
      groups: [
        group({
          asset_type: RUN,
          label: 'Cloud Run',
          count: 3,
          managed: 0,
          drift: 2,
          adoptable: true,
          drift_adoptable: 0, // both unmanaged nodes are control-plane — 0 actionable
          nodes: [
            node({ id: 'r0', label: 'driftscribe-agent', asset_type: RUN, managed: false, control_plane: true }),
            node({ id: 'r1', label: 'infra-reader', asset_type: RUN, managed: false, control_plane: true }),
          ],
        }),
      ],
    });
    const { getByTestId } = render(EstateView, { props: baseProps({ graph: g }) });
    const fold = getByTestId('estate-system-fold');
    expect(fold.textContent).toContain('2');
  });

  it('omits the fold entirely when there is nothing system-managed', () => {
    const { queryByTestId } = render(EstateView, { props: baseProps() });
    expect(queryByTestId('estate-system-fold')).toBeNull();
  });
});

describe('EstateView — other resources note', () => {
  it('renders the "other resources" note when a type is out of scope, omits it otherwise', () => {
    const g = graph({
      groups: [
        group(),
        {
          asset_type: RUN + '.revision',
          label: 'Cloud Run revision',
          count: 2,
          managed: 0,
          drift: 2,
          sensitive: false,
          adoptable: false,
          nodes: [
            node({ id: 'v0', label: 'demo-00018-abc', asset_type: RUN + '.revision', managed: false }),
            node({ id: 'v1', label: 'demo-00017-abc', asset_type: RUN + '.revision', managed: false }),
          ],
        } as unknown as InfraGroup,
      ],
    });
    const withOther = render(EstateView, { props: baseProps({ graph: g }) });
    expect(withOther.getByTestId('estate-other').textContent).toContain('2');
    cleanup();

    const { queryByTestId } = render(EstateView, { props: baseProps() });
    expect(queryByTestId('estate-other')).toBeNull();
  });
});

describe('EstateView — tour target', () => {
  it('data-tour="adopt-target" lands on the first adoptable row', () => {
    const g = graph({
      groups: [
        group({ asset_type: BUCKET, adopt_rank: 1, nodes: [node({ id: 'b0', label: 'bucket-a' })] }),
        group({
          asset_type: RUN,
          adopt_rank: 2,
          nodes: [node({ id: 'r0', label: 'run-a', asset_type: RUN })],
        }),
      ],
    });
    const { container, getAllByTestId } = render(EstateView, { props: baseProps({ graph: g }) });
    const marked = container.querySelector('[data-tour="adopt-target"]');
    expect(marked?.textContent).toContain('bucket-a');
    // Exactly one row carries the marker.
    const rows = getAllByTestId('estate-row');
    const markedRows = rows.filter((r) => r.getAttribute('data-tour') === 'adopt-target');
    expect(markedRows).toHaveLength(1);
  });

  it('no row carries the marker when there is no adoptable target', () => {
    const { container } = render(EstateView, { props: baseProps({ graph: graph({ groups: [] }) }) });
    expect(container.querySelector('[data-tour="adopt-target"]')).toBeNull();
  });
});

// Codex re-review of #258. The band cases moved to the desk with the band
// (2026-07-31 merge); the section's OWN unknown-vs-empty discipline stays here,
// and stays independent of the hero's state machine.
describe('EstateView — unknown figures match the desk (ds-eh6)', () => {
  it('a SETTLED null graph reads unavailable, not "Loading the estate…"', () => {
    // The fetch finished and failed. Saying "loading" claims something is still
    // in progress, and it would sit there until the next 45s poll.
    const { getByTestId, queryByTestId } = render(EstateView, {
      props: baseProps({ graph: null, settled: true }),
    });
    expect(queryByTestId('estate-loading')).toBeNull();
    expect(getByTestId('estate-degraded')).toBeTruthy();
  });
});

// Codex round 3 of #258 — the most consequential instance of the same defect:
// this one drives an ACTION. On a pending-approvals soft failure the store
// writes `pendingApprovals: []`, and the Adopt button appears exactly when no
// open adoption PR was found for a row. That emptiness means "we could not ask
// GitHub", so offering Adopt asserts something the app just failed to establish.
describe('EstateView — unreliable approvals must not imply "safe to adopt" (ds-eh6)', () => {
  it('suppresses the Adopt button and says why', () => {
    const { getByTestId, queryByTestId } = render(EstateView, {
      props: baseProps({ settled: true, approvalsStale: true }),
    });
    expect(queryByTestId('estate-adopt-btn')).toBeNull();
    expect(getByTestId('estate-adopt-unknown').textContent).toContain('adoption status unknown');
  });

  it('still renders a POSITIVELY observed PR chip', () => {
    // Only the absence claim is unsupported. A PR we actually saw is still a
    // fact, and hiding it would be its own information loss.
    const { getByTestId, queryByTestId } = render(EstateView, {
      props: baseProps({
        pendingApprovals: [pending({ resource_name: 'demo-node', asset_type: BUCKET })],
        settled: true,
        approvalsStale: true,
      }),
    });
    expect(getByTestId('estate-pr-chip')).toBeTruthy();
    expect(queryByTestId('estate-adopt-unknown')).toBeNull();
  });

  it('offers Adopt normally when the approvals lane is reliable', () => {
    const { getByTestId, queryByTestId } = render(EstateView, {
      props: baseProps({ settled: true, approvalsStale: false }),
    });
    expect(getByTestId('estate-adopt-btn')).toBeTruthy();
    expect(queryByTestId('estate-adopt-unknown')).toBeNull();
  });

  it('clears the tour adopt spotlight while approvals are unreliable', () => {
    const { container } = render(EstateView, {
      props: baseProps({ settled: true, approvalsStale: true }),
    });
    expect(container.querySelector('[data-tour="adopt-target"]')).toBeNull();
  });
});


// --------------------------------------------------------------------------- //
// ds-1vn — the estate discloses when its IaC snapshot is not this deployment's.
//
// The incident: infra-reader was baked at f72ef29 (07-29); PR #168 added
// iac/adopt_topic_adopt_probe_topic.tf on 07-31. The worker parsed an iac/ tree
// without the declaration, so the topic listed as undeclared and offered an
// Adopt button for a resource already declared and merged. The page said
// nothing about the snapshot's age.
//
// ⚠️ Why this cannot ride the existing channels, both of which were the obvious
// first choice and both of which fail:
//
//   * `graph.caveat` — InfraDiagram renders it only `{#if graph && !degraded}`,
//     so the disclosure disappears exactly when things are worst.
//   * `graph.degraded` — EstateView (above) replaces the ENTIRE estate with a
//     generic unavailable line. That hides the resources the warning is about,
//     which is worse than the bug.
//
// So it gets its own visible state, tested here rather than assumed.
// --------------------------------------------------------------------------- //

describe('EstateView — IaC snapshot freshness disclosure (ds-1vn)', () => {
  it('shows a specific notice when the snapshot is stale', () => {
    const { getByTestId } = render(EstateView, {
      props: baseProps({ graph: graph({ iac_snapshot_stale: true }) }),
    });
    const el = getByTestId('estate-snapshot-stale');
    expect(el.textContent?.trim()).toBeTruthy();
  });

  it('still renders the estate itself when stale — the warning must not hide its subject', () => {
    const { getByTestId, getAllByTestId, queryByTestId } = render(EstateView, {
      props: baseProps({ graph: graph({ iac_snapshot_stale: true }) }),
    });
    expect(getByTestId('estate-snapshot-stale')).toBeTruthy();
    expect(getAllByTestId('estate-row').length).toBeGreaterThan(0);
    expect(queryByTestId('estate-degraded')).toBeNull();
  });

  it('says nothing when the snapshot matches this deployment', () => {
    const { queryByTestId } = render(EstateView, {
      props: baseProps({ graph: graph({ iac_snapshot_stale: false }) }),
    });
    expect(queryByTestId('estate-snapshot-stale')).toBeNull();
    expect(queryByTestId('estate-snapshot-unverified')).toBeNull();
  });

  // The state prod is in until infra-reader is redeployed, and the one most
  // easily rendered as silence. "Could not check" is not "fine".
  it('shows a DISTINCT, quieter notice when freshness could not be verified', () => {
    const { getByTestId, queryByTestId } = render(EstateView, {
      props: baseProps({ graph: graph({ iac_snapshot_stale: null }) }),
    });
    expect(getByTestId('estate-snapshot-unverified')).toBeTruthy();
    expect(queryByTestId('estate-snapshot-stale')).toBeNull();
  });

  it('treats a graph with no freshness field at all as unverified, not as fresh', () => {
    // A build serving a payload from before these fields existed. Absent must
    // not read as false — that is the same conflation as `phase: null` meaning
    // "applied" (ds-2mc). Written by DELETING the key, not by passing
    // undefined: the fixture now defaults to a healthy `false`, so a test for
    // the absent case has to actually produce absence.
    const g = graph();
    delete (g as Partial<InfraGraph>).iac_snapshot_stale;
    const { getByTestId } = render(EstateView, { props: baseProps({ graph: g }) });
    expect(getByTestId('estate-snapshot-unverified')).toBeTruthy();
  });

  it('renders no freshness notice at all on a degraded graph', () => {
    // Degraded already replaces the estate with its own line. Stacking a
    // freshness note onto a panel that is showing nothing would be noise about
    // the currency of data that is not on screen.
    const { queryByTestId, getByTestId } = render(EstateView, {
      props: baseProps({ graph: graph({ degraded: true, iac_snapshot_stale: null }) }),
    });
    expect(getByTestId('estate-degraded')).toBeTruthy();
    expect(queryByTestId('estate-snapshot-stale')).toBeNull();
    expect(queryByTestId('estate-snapshot-unverified')).toBeNull();
  });

  it('renders no freshness notice while the graph is still loading', () => {
    const { queryByTestId } = render(EstateView, {
      props: baseProps({ graph: null, settled: false }),
    });
    expect(queryByTestId('estate-snapshot-stale')).toBeNull();
    expect(queryByTestId('estate-snapshot-unverified')).toBeNull();
  });

  // The notice sits ABOVE the rows it qualifies. An operator who has already
  // scrolled to an Adopt button and clicked it was never warned.
  it('places the notice before the first estate row', () => {
    const { getByTestId, getAllByTestId } = render(EstateView, {
      props: baseProps({ graph: graph({ iac_snapshot_stale: true }) }),
    });
    const notice = getByTestId('estate-snapshot-stale');
    const firstRow = getAllByTestId('estate-row')[0];
    expect(notice.compareDocumentPosition(firstRow) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});

// --------------------------------------------------------------------------- //
// ds-1vn, round 2 (Codex review). Two holes in the first cut, both of the same
// shape the whole bead is about: an unsupported claim surviving into an ACTION.
// --------------------------------------------------------------------------- //

describe('EstateView — a stale snapshot suppresses adoption (ds-1vn r2)', () => {
  // The incident, exactly. `adopt-probe-topic` was declared and merged on
  // 07-31; infra-reader was baked 07-29; the estate listed it as drift and
  // offered an Adopt button for a resource already declared. Disclosing that in
  // prose while leaving the button armed fixes the sentence, not the defect —
  // "not declared" is read off a snapshot we have just proved is a different
  // tree, and unlike a wrong figure that absence drives an action.
  it('replaces the Adopt button with a reason when the snapshot is stale', () => {
    const { queryByTestId, getByTestId } = render(EstateView, {
      props: baseProps({ graph: graph({ iac_snapshot_stale: true }) }),
    });
    expect(queryByTestId('estate-adopt-btn')).toBeNull();
    expect(getByTestId('estate-adopt-stale').textContent?.trim()).toBeTruthy();
  });

  it('a suppressed row is still LISTED — the estate never hides its subject', () => {
    const { getAllByTestId } = render(EstateView, {
      props: baseProps({ graph: graph({ iac_snapshot_stale: true }) }),
    });
    expect(getAllByTestId('estate-row').length).toBeGreaterThan(0);
  });

  it('drops the tour spotlight while adoption is suppressed', () => {
    // firstAdoptableRow picks the row whose button this state just removed, so
    // an un-nulled target would spotlight a row and point at nothing.
    const { container } = render(EstateView, {
      props: baseProps({ graph: graph({ iac_snapshot_stale: true }) }),
    });
    expect(container.querySelector('[data-tour="adopt-target"]')).toBeNull();
  });

  // Suppressed on UNVERIFIED too (Codex r3). An earlier cut spared it, arguing
  // absence of evidence is not evidence of a mismatch. The counterexample is
  // the very rollout that produces `unverified`: deploy the coordinator ahead
  // of the worker, and the old worker has no hash AND is genuinely missing the
  // new declaration — the ds-1vn incident exactly, wearing "unknown" instead
  // of "mismatch". Only `fresh` may drive an adoption.
  it('suppresses Adopt on an UNVERIFIED snapshot too, and says currency is unconfirmed', () => {
    const { queryByTestId, getByTestId } = render(EstateView, {
      props: baseProps({ graph: graph({ iac_snapshot_stale: null }) }),
    });
    expect(queryByTestId('estate-adopt-btn')).toBeNull();
    expect(getByTestId('estate-adopt-unverified')).toBeTruthy();
    expect(getByTestId('estate-snapshot-unverified')).toBeTruthy();
  });

  // Codex r4: the first cut of the widened rule reused the STALE chip for both
  // states, so an unverified row said "out of step" — asserting a mismatch that
  // was never established, while the notice above it said only "could not
  // confirm". The earlier test checked the chip EXISTED and not what it
  // claimed, which is how it passed. Widening a suppression is not licence to
  // widen its claim.
  it('an unverified row does not claim a mismatch was found', () => {
    const { getByTestId, queryByTestId } = render(EstateView, {
      props: baseProps({ graph: graph({ iac_snapshot_stale: null }) }),
    });
    expect(queryByTestId('estate-adopt-stale')).toBeNull();
    const chip = getByTestId('estate-adopt-unverified').textContent ?? '';
    expect(chip).toContain('not verified');
    expect(chip).not.toMatch(/out of step|mismatch/i);
  });

  it('a stale row DOES claim the mismatch, because that one was established', () => {
    const { getByTestId, queryByTestId } = render(EstateView, {
      props: baseProps({ graph: graph({ iac_snapshot_stale: true }) }),
    });
    expect(queryByTestId('estate-adopt-unverified')).toBeNull();
    expect(getByTestId('estate-adopt-stale').textContent).toContain('out of step');
  });

  it('suppresses Adopt when a failed refresh retired the freshness claim', () => {
    // graphStale degrades a retained `false` to unverified — and the action
    // derived from that assurance must go with it. Leaving Adopt armed while
    // retiring the assurance is the exact split Codex r3 called out.
    const { queryByTestId, getByTestId } = render(EstateView, {
      props: baseProps({ graph: graph({ iac_snapshot_stale: false }), graphStale: true }),
    });
    expect(queryByTestId('estate-adopt-btn')).toBeNull();
    // ...and says "not verified", not "out of step": a failed refresh is no
    // evidence of a mismatch.
    expect(getByTestId('estate-adopt-unverified')).toBeTruthy();
  });

  it('keeps Adopt when the snapshot matches', () => {
    const { getAllByTestId, queryByTestId } = render(EstateView, {
      props: baseProps({ graph: graph({ iac_snapshot_stale: false }) }),
    });
    expect(getAllByTestId('estate-adopt-btn').length).toBeGreaterThan(0);
    expect(queryByTestId('estate-adopt-stale')).toBeNull();
  });

  // The two suppressions have DIFFERENT reasons and must not be conflated: one
  // means "we could not ask GitHub whether a PR is open", the other "the
  // declared set this row's status came from is not this deployment's".
  it('a stale snapshot and an unreliable approvals lane give different reasons', () => {
    const { getByTestId, queryByTestId } = render(EstateView, {
      props: baseProps({ graph: graph({ iac_snapshot_stale: true }), approvalsStale: true }),
    });
    expect(getByTestId('estate-adopt-stale')).toBeTruthy();
    expect(queryByTestId('estate-adopt-unknown')).toBeNull();
  });
});

describe('EstateView — a retained graph does not keep vouching for itself (ds-1vn r2)', () => {
  // `runCycle` preserves the previous graph when /infra/graph fails
  // (overviewStore.ts). Right for the NUMBERS — a stale count beats a blank
  // panel — but `iac_snapshot_stale: false` is an ASSURANCE, and retaining it
  // reports "checked, current" about a check that did not run this cycle.
  // A coordinator deploy can change iac/ while the refresh is failing.
  it('degrades a retained "fresh" to unverified when the last graph fetch failed', () => {
    const { getByTestId } = render(EstateView, {
      props: baseProps({ graph: graph({ iac_snapshot_stale: false }), graphStale: true }),
    });
    expect(getByTestId('estate-snapshot-unverified')).toBeTruthy();
  });

  it('does NOT invent staleness from a failed fetch', () => {
    // A failed refresh is no evidence of a MISMATCH either. Unverified, not stale.
    const { queryByTestId } = render(EstateView, {
      props: baseProps({ graph: graph({ iac_snapshot_stale: false }), graphStale: true }),
    });
    expect(queryByTestId('estate-snapshot-stale')).toBeNull();
  });

  it('a failed refresh does not retire a mismatch already observed', () => {
    // Order matters: `true` is checked before `graphStale`. Softening a
    // positive mismatch to "unverified" because a LATER fetch failed would
    // retire a warning on no evidence — and re-arm the Adopt buttons.
    const { getByTestId, queryByTestId } = render(EstateView, {
      props: baseProps({ graph: graph({ iac_snapshot_stale: true }), graphStale: true }),
    });
    expect(getByTestId('estate-snapshot-stale')).toBeTruthy();
    expect(queryByTestId('estate-adopt-btn')).toBeNull();
  });

  it('graphStale is irrelevant when the graph never claimed freshness', () => {
    const { getByTestId } = render(EstateView, {
      props: baseProps({ graph: graph({ iac_snapshot_stale: null }), graphStale: true }),
    });
    expect(getByTestId('estate-snapshot-unverified')).toBeTruthy();
  });
});
