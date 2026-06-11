// frontend/tests/unit/TourCard.test.ts
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import TourCard from '../../src/components/TourCard.svelte';
import type { InfraGraph } from '../../src/lib/infra_graph';

// jsdom does not implement scrollIntoView — the spotlight effect calls it.
// Fresh mock per test (Codex should-fix: a shared beforeAll mock leaks call
// history across cases).
beforeEach(() => {
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
});
afterEach(cleanup);

function graphWithTarget(): InfraGraph {
  return {
    generated_at: null,
    project: 'driftscribe-hack-2026',
    caveat: '',
    degraded: false,
    degraded_reason: null,
    totals: { resources: 12, managed: 9, drift: 3 },
    groups: [
      {
        asset_type: 'storage.googleapis.com/Bucket',
        label: 'Storage bucket',
        count: 1,
        managed: 0,
        drift: 1,
        sensitive: false,
        adoptable: true,
        adopt_rank: 1,
        adopt_hint: 'Buckets are the simplest first adoption.',
        nodes: [
          {
            id: 'g0n0',
            label: 'demo-bucket',
            asset_type: 'storage.googleapis.com/Bucket',
            managed: false,
            location: 'asia-northeast1',
          },
        ],
      },
    ],
    edges: [],
  };
}

async function advanceTo(getByTestId: (id: string) => HTMLElement, clicks: number) {
  for (let i = 0; i < clicks; i++) await fireEvent.click(getByTestId('tour-next'));
}

describe('TourCard — navigation', () => {
  it('starts at step 1 of 5 with the welcome copy and a disabled Back', () => {
    const { getByTestId } = render(TourCard, { props: { graph: graphWithTarget() } });
    expect(getByTestId('tour-progress').textContent).toContain('1 of 5');
    expect(getByTestId('tour-body').textContent).toContain(
      'the GCP project driftscribe-hack-2026',
    );
    expect((getByTestId('tour-back') as HTMLButtonElement).disabled).toBe(true);
  });

  it('Next/Back walk the steps; the estate step shows live totals', async () => {
    const { getByTestId } = render(TourCard, { props: { graph: graphWithTarget() } });
    await advanceTo(getByTestId, 1);
    expect(getByTestId('tour-progress').textContent).toContain('2 of 5');
    expect(getByTestId('tour-body').textContent).toContain('12 resources indexed');
    await fireEvent.click(getByTestId('tour-back'));
    expect(getByTestId('tour-progress').textContent).toContain('1 of 5');
  });

  it('the last step shows Finish (no Next) and Finish fires onClose', async () => {
    const onClose = vi.fn();
    const { getByTestId, queryByTestId } = render(TourCard, {
      props: { graph: graphWithTarget(), onClose },
    });
    await advanceTo(getByTestId, 4);
    expect(queryByTestId('tour-next')).toBeNull();
    await fireEvent.click(getByTestId('tour-finish'));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('the close button fires onClose from any step', async () => {
    const onClose = vi.fn();
    const { getByTestId } = render(TourCard, {
      props: { graph: graphWithTarget(), onClose },
    });
    await fireEvent.click(getByTestId('tour-close'));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe('TourCard — adopt step (T4: prefill, never send)', () => {
  it('offers the prefill button and fires onAdoptPrefill with the exact prefill, then advances', async () => {
    const onAdoptPrefill = vi.fn();
    const { getByTestId } = render(TourCard, {
      props: { graph: graphWithTarget(), onAdoptPrefill },
    });
    await advanceTo(getByTestId, 3);
    expect(getByTestId('tour-progress').textContent).toContain('4 of 5');
    expect(getByTestId('tour-body').textContent).toContain('demo-bucket');
    await fireEvent.click(getByTestId('tour-adopt-btn'));
    expect(onAdoptPrefill).toHaveBeenCalledWith(
      'Adopt the Storage bucket `demo-bucket` in asia-northeast1 into IaC management.',
    );
    expect(getByTestId('tour-progress').textContent).toContain('5 of 5');
  });

  it('respects adoptDisabled (same condition as the panel buttons)', async () => {
    const onAdoptPrefill = vi.fn();
    const { getByTestId } = render(TourCard, {
      props: { graph: graphWithTarget(), adoptDisabled: true, onAdoptPrefill },
    });
    await advanceTo(getByTestId, 3);
    expect((getByTestId('tour-adopt-btn') as HTMLButtonElement).disabled).toBe(true);
    await fireEvent.click(getByTestId('tour-adopt-btn'));
    expect(onAdoptPrefill).not.toHaveBeenCalled();
  });

  it('shows no button when there is nothing to adopt (T5)', async () => {
    const g = graphWithTarget();
    g.totals = { resources: 9, managed: 9, drift: 0 };
    g.groups = [];
    const { getByTestId, queryByTestId } = render(TourCard, { props: { graph: g } });
    await advanceTo(getByTestId, 3);
    expect(queryByTestId('tour-adopt-btn')).toBeNull();
    expect(getByTestId('tour-body').textContent).toContain('already under IaC management');
  });

  it('stays honest when the graph never loaded (T3)', async () => {
    const { getByTestId, queryByTestId } = render(TourCard, { props: { graph: null } });
    await advanceTo(getByTestId, 3);
    expect(queryByTestId('tour-adopt-btn')).toBeNull();
    expect(getByTestId('tour-body').textContent).toContain('not available yet');
  });

  it('final step shows the busy note only while chat is disabled (Codex MF3)', async () => {
    const busy = render(TourCard, {
      props: { graph: graphWithTarget(), adoptDisabled: true },
    });
    await advanceTo(busy.getByTestId, 4);
    expect(busy.getByTestId('tour-busy-note').textContent).toContain('busy');
    cleanup();

    const idle = render(TourCard, { props: { graph: graphWithTarget() } });
    await advanceTo(idle.getByTestId, 4);
    expect(idle.queryByTestId('tour-busy-note')).toBeNull();
  });
});

describe('TourCard — spotlight', () => {
  it('toggles .tour-spotlight on the matching [data-tour] element per step', async () => {
    const estate = document.createElement('div');
    estate.setAttribute('data-tour', 'estate');
    document.body.appendChild(estate);
    try {
      const { getByTestId } = render(TourCard, { props: { graph: graphWithTarget() } });
      // step 1 (welcome): no target
      expect(estate.classList.contains('tour-spotlight')).toBe(false);
      await fireEvent.click(getByTestId('tour-next')); // → estate
      expect(estate.classList.contains('tour-spotlight')).toBe(true);
      expect(window.HTMLElement.prototype.scrollIntoView).toHaveBeenCalled();
      await fireEvent.click(getByTestId('tour-next')); // → controls (absent in DOM)
      expect(estate.classList.contains('tour-spotlight')).toBe(false);
    } finally {
      estate.remove();
    }
  });

  it('removes the spotlight on unmount', async () => {
    const estate = document.createElement('div');
    estate.setAttribute('data-tour', 'estate');
    document.body.appendChild(estate);
    try {
      const { getByTestId, unmount } = render(TourCard, {
        props: { graph: graphWithTarget() },
      });
      await fireEvent.click(getByTestId('tour-next'));
      expect(estate.classList.contains('tour-spotlight')).toBe(true);
      unmount();
      expect(estate.classList.contains('tour-spotlight')).toBe(false);
    } finally {
      estate.remove();
    }
  });
});
