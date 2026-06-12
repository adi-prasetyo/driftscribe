import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import DecisionsRail from '../../src/components/DecisionsRail.svelte';
import type { Decision } from '../../src/lib/types';

// First component-render test in the repo. Uses @testing-library/svelte (v5,
// Svelte-5-native render) on the jsdom environment configured in
// vitest.config.ts. We assert the supersession behaviour (a later `applied`
// iac_apply row retires the stale "Review & approve →" CTA on its
// `waiting_for_rebake` siblings) and the new meta-line status token end-to-end
// through the component, not just via the pure helpers.

afterEach(cleanup);

const noop = () => {};

/** Build an iac_apply decision row with sane defaults. */
function iacRow(over: Partial<Decision>): Decision {
  return {
    decision_id: `d-${Math.random().toString(36).slice(2)}`,
    action: 'iac_apply',
    pr_number: 68,
    github: { url: 'https://github.com/adi-prasetyo/driftscribe/pull/68' },
    ...over,
  } as Decision;
}

describe('DecisionsRail — iac_apply CTA supersession + status token', () => {
  // Case 1 (rewritten): the 3 same-PR docs now collapse into ONE row whose CTA
  // already reflects supersession (label-only downgrade, href intact).
  it('collapses a superseded lifecycle into one row with the view-only CTA', () => {
    const decisions: Decision[] = [
      iacRow({ decision_id: 'applied-68', apply_status: 'applied', pr_number: 68 }),
      iacRow({ decision_id: 'wait-68-a', apply_status: 'waiting_for_rebake', pr_number: 68 }),
      iacRow({ decision_id: 'wait-68-b', apply_status: 'waiting_for_rebake', pr_number: 68 }),
    ];
    const { getAllByTestId, getByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    // ONE rail row, ONE approval CTA for the whole lifecycle.
    expect(getAllByTestId('past-decision-item')).toHaveLength(1);
    const link = getByTestId('iac-approve-link');
    expect(link.textContent?.trim()).toBe('Open approval page →');
    expect(link.getAttribute('href')).toBe('/iac-approvals/68');
  });

  it('keeps "Review & approve →" on a lone waiting row with no applied sibling', () => {
    const decisions: Decision[] = [
      iacRow({ decision_id: 'wait-71', apply_status: 'waiting_for_rebake', pr_number: 71 }),
    ];

    const { getByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });

    const link = getByTestId('iac-approve-link');
    expect(link.textContent?.trim()).toBe('Review & approve →');
    expect(link.getAttribute('href')).toBe('/iac-approvals/71');
  });

  it('renders the apply_status token on the meta line (applied + awaiting re-bake)', () => {
    const decisions: Decision[] = [
      iacRow({ decision_id: 'applied-68', apply_status: 'applied', pr_number: 68, head_sha: '0496b305deadbeef' }),
      iacRow({ decision_id: 'wait-71', apply_status: 'waiting_for_rebake', pr_number: 71, head_sha: '0496b305deadbeef' }),
    ];

    const { container } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });

    // Svelte trims the whitespace at `{#if}` boundaries, so the literal
    // ` · {st}` markup renders the separator without a leading space — the
    // SAME established convention the SHA token already uses
    // ("iac_apply· ⎇ <sha>"). We assert the actual rendered token order:
    // action tag · status · ⎇ sha.
    const metas = Array.from(container.querySelectorAll('.row-meta')).map((n) =>
      n.textContent?.trim(),
    );
    expect(metas).toContain('iac_apply· applied· ⎇ 0496b30');
    expect(metas).toContain('iac_apply· awaiting re-bake· ⎇ 0496b30');
    // The status token sits BETWEEN the action tag and the SHA.
    const applied = metas.find((t) => t?.includes('applied'))!;
    expect(applied.indexOf('applied')).toBeGreaterThan(applied.indexOf('iac_apply'));
    expect(applied.indexOf('applied')).toBeLessThan(applied.indexOf('⎇'));
  });
});

describe('DecisionsRail — collapsed iac_apply lifecycle groups', () => {
  it('face shows the newest doc; expander (closed for a calm history) lists earlier steps oldest-first with status + per-step open trace', async () => {
    const decisions: Decision[] = [
      iacRow({ decision_id: 'applied-68', apply_status: 'applied', pr_number: 68,
               created_at: '2026-06-05T01:27:33Z', head_sha: '0496b305dead',
               trace_id: 'trace-applied', pr_title: 'infra(checkout): storefront + orders-worker' }),
      // Live-faithful: the two waiting docs share ONE trace_id.
      iacRow({ decision_id: 'wait-68-a', apply_status: 'waiting_for_rebake', pr_number: 68,
               created_at: '2026-06-04T14:53:36Z', trace_id: 'trace-waiting',
               pr_title: 'infra(checkout): storefront + orders-worker' }),
      iacRow({ decision_id: 'wait-68-b', apply_status: 'waiting_for_rebake', pr_number: 68,
               created_at: '2026-06-04T14:53:29Z', trace_id: 'trace-waiting',
               pr_title: 'infra(checkout): storefront + orders-worker' }),
    ];
    const opened: string[] = [];
    const { container, getByTestId, getAllByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: (t: string) => opened.push(t) },
    });

    // Face = newest doc: applied status on the meta line, PR link title.
    const meta = container.querySelector('.row-meta')?.textContent;
    expect(meta).toContain('applied');
    expect(meta).not.toContain('awaiting re-bake');

    // The summary carries the status COMPOSITION (exact single-expression
    // string — lifecycleSummaryLabel), never a bare count that hides state.
    const summary = getByTestId('iac-lifecycle-summary');
    expect(summary.textContent?.trim()).toBe('2 earlier steps · awaiting re-bake ×2');

    // Calm history (waiting steps only) ⇒ the expander defaults to CLOSED, and
    // the step nodes sit structurally INSIDE it so the native expander gates
    // their visibility. (jsdom can't pin the native summary-click toggle —
    // see CapabilityCard.test.ts:9-12 — the initial open-state is what's ours.)
    const details = container.querySelector('details.lifecycle') as HTMLDetailsElement;
    expect(details.open).toBe(false);

    // Earlier steps render oldest-first: wait-68-b (14:53:29) before wait-68-a
    // (14:53:36). Pin the actual order via the datetime attributes.
    const steps = getAllByTestId('iac-lifecycle-step');
    expect(steps).toHaveLength(2);
    expect(steps.every((s) => details.contains(s))).toBe(true);
    expect(steps.map((s) => s.querySelector('time')?.getAttribute('datetime'))).toEqual([
      '2026-06-04T14:53:29Z',
      '2026-06-04T14:53:36Z',
    ]);
    for (const s of steps) expect(s.textContent).toContain('awaiting re-bake');

    // Per-step open-trace works — both steps share the live-faithful trace id.
    const btns = getAllByTestId('lifecycle-open-trace');
    expect(btns).toHaveLength(2);
    await fireEvent.click(btns[0]);
    expect(opened).toEqual(['trace-waiting']);
  });

  it('an all-waiting group (no applied sibling) keeps the live "Review & approve →" CTA on ONE row', () => {
    // The highest-risk CTA case: collapsing must NOT eat the actionable label.
    const decisions: Decision[] = [
      iacRow({ decision_id: 'wait-90-a', apply_status: 'waiting_for_rebake', pr_number: 90 }),
      iacRow({ decision_id: 'wait-90-b', apply_status: 'waiting_for_rebake', pr_number: 90 }),
    ];
    const { getAllByTestId, getByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    expect(getAllByTestId('past-decision-item')).toHaveLength(1);
    const link = getByTestId('iac-approve-link');
    expect(link.textContent?.trim()).toBe('Review & approve →');
    expect(link.getAttribute('href')).toBe('/iac-approvals/90');
  });

  it('an anomalous history (prior failed step) is visible WITHOUT a click: composition in the summary + details defaults OPEN', () => {
    const decisions: Decision[] = [
      iacRow({ decision_id: 'a32', apply_status: 'applied', pr_number: 32, trace_id: 't-a' }),
      iacRow({ decision_id: 'f32', apply_status: 'failed', pr_number: 32, trace_id: 't-f' }),
    ];
    const { container, getByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    expect(getByTestId('iac-lifecycle-summary').textContent?.trim()).toBe('1 earlier step · failed');
    const details = container.querySelector('details.lifecycle') as HTMLDetailsElement;
    expect(details.open).toBe(true);
    expect(getByTestId('iac-lifecycle-step').textContent).toContain('failed');
  });

  it('marks the group row active when an EARLIER step trace is the active trace', () => {
    const decisions: Decision[] = [
      iacRow({ decision_id: 'a', apply_status: 'applied', pr_number: 68, trace_id: 't-new' }),
      iacRow({ decision_id: 'w', apply_status: 'waiting_for_rebake', pr_number: 68, trace_id: 't-old' }),
    ];
    const { getByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: 't-old', onOpenTrace: noop },
    });
    expect(getByTestId('past-decision-item').classList.contains('active')).toBe(true);
  });

  it('falls back to a sibling pr_title when the newest doc lacks one (fail-soft write)', () => {
    const decisions: Decision[] = [
      iacRow({ decision_id: 'a', apply_status: 'applied', pr_number: 68, pr_title: undefined }),
      iacRow({ decision_id: 'w', apply_status: 'waiting_for_rebake', pr_number: 68,
               pr_title: 'infra(checkout): storefront + orders-worker' }),
    ];
    const { container } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    expect(container.querySelector('.row-subtitle')?.textContent)
      .toBe('infra(checkout): storefront + orders-worker');
  });

  it("an operator's manual collapse of an anomalous expander survives a /decisions refresh", async () => {
    // `open={hasAnomalousStep(lifecycle)}` is a computed attribute. A real
    // summary click flips `details.open` OUTSIDE Svelte's knowledge — the
    // question is what a /decisions refresh (new array identity, identical
    // anomalous content) does to that manual state. Pin the actual behavior
    // instead of reasoning about Svelte's attribute effects.
    const mk = (title: string): Decision[] => [
      iacRow({ decision_id: 'a32', apply_status: 'applied', pr_number: 32, pr_title: title }),
      iacRow({ decision_id: 'f32', apply_status: 'failed', pr_number: 32 }),
    ];
    const { container, rerender } = render(DecisionsRail, {
      props: { decisions: mk('fix: retry apply'), activeTraceId: null, onOpenTrace: noop },
    });
    const details = container.querySelector('details.lifecycle') as HTMLDetailsElement;
    expect(details.open).toBe(true);

    // Operator collapses the anomalous expander (what a native click does).
    details.open = false;

    // Refresh: the {#each} key ('g:32') is stable so the <details> node is
    // reused, and Svelte dirty-checks the attribute effect — the computed
    // value is still `true`, unchanged since the last render, so it skips the
    // re-assignment and the operator's collapse stands. The changed pr_title
    // proves the refresh actually re-rendered (no vacuous pass).
    await rerender({ decisions: mk('fix: retry apply (amended)') });
    expect(container.querySelector('.row-subtitle')?.textContent).toBe('fix: retry apply (amended)');
    const after = container.querySelector('details.lifecycle') as HTMLDetailsElement;
    expect(after.open).toBe(false);
  });

  it('subtitle fallback scans the WHOLE group: only the oldest doc carries pr_title', () => {
    // 3-doc group where docs[0] AND docs[1] lack pr_title — kills a
    // `docs[1]?.pr_title` mutant that the 2-doc fallback test would let live.
    const decisions: Decision[] = [
      iacRow({ decision_id: 'a', apply_status: 'applied', pr_number: 68, pr_title: undefined }),
      iacRow({ decision_id: 'w1', apply_status: 'waiting_for_rebake', pr_number: 68, pr_title: undefined }),
      iacRow({ decision_id: 'w2', apply_status: 'waiting_for_rebake', pr_number: 68,
               pr_title: 'infra(checkout): storefront + orders-worker' }),
    ];
    const { container } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    expect(container.querySelector('.row-subtitle')?.textContent)
      .toBe('infra(checkout): storefront + orders-worker');
  });

  it('a lifecycle step with NO apply_status renders the neutral token, never the action string', () => {
    // A missing status is anomalous (fail-open ⇒ details defaults open); what
    // matters here is the step text: the deliberate `status not recorded`
    // token, NOT `iac_apply` leaking in where a status belongs.
    const decisions: Decision[] = [
      iacRow({ decision_id: 'a', apply_status: 'applied', pr_number: 68 }),
      iacRow({ decision_id: 'm', pr_number: 68 }),
    ];
    const { getByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    const step = getByTestId('iac-lifecycle-step');
    expect(step.textContent).toContain('status not recorded');
    expect(step.textContent).not.toContain('iac_apply');
  });

  it('a lone iac_apply doc renders exactly as before — single row, no expander', () => {
    const decisions: Decision[] = [
      iacRow({ decision_id: 'w71', apply_status: 'waiting_for_rebake', pr_number: 71 }),
    ];
    const { getByTestId, queryByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    expect(getByTestId('iac-approve-link').textContent?.trim()).toBe('Review & approve →');
    expect(queryByTestId('iac-lifecycle-summary')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Task 9 — autonomy-suppressed token
// ---------------------------------------------------------------------------

describe('DecisionsRail — Observe-mode suppressed decisions', () => {
  it('suppressed_by_autonomy:true renders autonomy-suppressed token with correct text', () => {
    const decisions: Decision[] = [
      {
        decision_id: 'sup-1',
        action: 'docs_pr',
        suppressed_by_autonomy: true,
        autonomy_mode: 'observe',
      },
    ];
    const { getByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    const token = getByTestId('autonomy-suppressed');
    expect(token.textContent?.trim()).toBe('not executed — Observe mode');
  });

  it('decision without suppressed_by_autonomy renders no autonomy-suppressed token (stale-coordinator fail-quiet)', () => {
    const decisions: Decision[] = [
      {
        decision_id: 'normal-1',
        action: 'docs_pr',
      },
    ];
    const { queryByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    expect(queryByTestId('autonomy-suppressed')).toBeNull();
  });

  it('suppressed_by_autonomy:false renders no autonomy-suppressed token', () => {
    const decisions: Decision[] = [
      {
        decision_id: 'not-sup-1',
        action: 'docs_pr',
        suppressed_by_autonomy: false,
        autonomy_mode: 'propose',
      },
    ];
    const { queryByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    expect(queryByTestId('autonomy-suppressed')).toBeNull();
  });
});

describe('DecisionsRail — dry-run preview pill', () => {
  /** A drift-class decision row (github sidecar shaped like agent/main.py). */
  function driftRow(over: Partial<Decision>): Decision {
    return {
      decision_id: `d-${Math.random().toString(36).slice(2)}`,
      action: 'drift_issue',
      ...over,
    } as Decision;
  }

  // Parameterized over the full GITHUB_LINK_LABEL action set (Codex plan-review
  // nit): pins the pill's action gate to exactly the actions that perform
  // GitHub side effects, incl. the upgrade_pr forward-compat entry.
  it.each(['drift_issue', 'escalation', 'docs_pr', 'upgrade_pr'])(
    'renders the pill on a %s row whose GitHub action was dry-run-skipped',
    (action) => {
      const decisions = [
        driftRow({ action, dry_run: true, github: { url: null, dry_run: true } }),
      ];
      const { getByTestId, queryByTestId } = render(DecisionsRail, {
        props: { decisions, activeTraceId: null, onOpenTrace: noop },
      });
      expect(getByTestId('decision-dry-run').textContent?.trim()).toBe(
        'dry run — not created on GitHub',
      );
      // url is null on a dry-run row, so no GitHub link renders beside the pill.
      expect(queryByTestId('decision-github-link')).toBeNull();
    },
  );

  it('no pill when the GitHub action really ran (github.dry_run false)', () => {
    const decisions = [
      driftRow({
        dry_run: false,
        github: { url: 'https://github.com/adi-prasetyo/driftscribe/issues/99', dry_run: false },
      }),
    ];
    const { queryByTestId, getByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    expect(queryByTestId('decision-dry-run')).toBeNull();
    expect(getByTestId('decision-github-link')).toBeTruthy();
  });

  it('no pill on a rollback row with top-level dry_run:true — a REAL approval was minted (agent/main.py dry_run_effective)', () => {
    const decisions: Decision[] = [
      {
        decision_id: 'rb-1',
        action: 'rollback',
        dry_run: true,
        dry_run_effective: false,
        approval: { approval_url: `${location.origin}/approvals/abc?t=x` },
      } as Decision,
    ];
    const { queryByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    expect(queryByTestId('decision-dry-run')).toBeNull();
  });

  it('no pill on a no_op row even though its sidecar mirrors the setting (nothing was skipped)', () => {
    const decisions = [
      driftRow({ action: 'no_op', dry_run: true, github: { url: null, dry_run: true } }),
    ];
    const { queryByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    expect(queryByTestId('decision-dry-run')).toBeNull();
  });

  it('Observe-suppressed row shows the autonomy token, never the dry-run pill (sidecar has no dry_run key)', () => {
    const decisions = [
      driftRow({
        dry_run: true,
        suppressed_by_autonomy: true,
        autonomy_mode: 'observe',
        github: { url: null }, // agent/main.py:1451-1456 — no dry_run key when suppressed
      }),
    ];
    const { getByTestId, queryByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    expect(getByTestId('autonomy-suppressed')).toBeTruthy();
    expect(queryByTestId('decision-dry-run')).toBeNull();
  });
});
