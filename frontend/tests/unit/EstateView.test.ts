import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import EstateView from '../../src/components/EstateView.svelte';
import type { InfraGraph, InfraGroup, InfraNode, PendingApproval } from '../../src/lib/infra_graph';
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

function baseProps(over: Record<string, unknown> = {}) {
  return {
    graph: graph(),
    decisions: [] as Decision[],
    pendingApprovals: [] as PendingApproval[],
    onNavigate: vi.fn(),
    ...over,
  };
}

describe('EstateView — loading/degraded honesty', () => {
  it('renders an honest loading line when the graph has not loaded yet', () => {
    const { getByTestId, queryByTestId } = render(EstateView, {
      props: baseProps({ graph: null }),
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

  it('the InstrumentBand numbers are driven by resourceCards()+scopeTotals(), matching the desk', () => {
    const g = graph({
      totals: { resources: 999, managed: 111, drift: 222 }, // sentinel — must NOT surface
      groups: [
        {
          asset_type: RUN,
          label: 'Cloud Run',
          count: 2,
          managed: 1,
          drift: 1,
          sensitive: false,
          adoptable: true,
          nodes: [],
        } as unknown as InfraGroup,
      ],
    });
    const { getByTestId } = render(EstateView, { props: baseProps({ graph: g }) });
    expect(getByTestId('instrument-band-managed').textContent).toContain('1');
    expect(getByTestId('instrument-band-drift').textContent).toContain('1');
    expect(getByTestId('instrument-band-managed').textContent).not.toContain('111');
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
