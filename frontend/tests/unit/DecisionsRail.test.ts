import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent, within } from '@testing-library/svelte';
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

/** Build an iac_apply decision row with sane defaults. The GitHub link tracks
 *  the effective pr_number (real /decisions rows are written that way), so the
 *  link-gated header hint sees each distinct number faithfully. */
function iacRow(over: Partial<Decision>): Decision {
  const prNumber = over.pr_number ?? 68;
  return {
    decision_id: `d-${Math.random().toString(36).slice(2)}`,
    action: 'iac_apply',
    pr_number: 68,
    github: { url: `https://github.com/adi-prasetyo/driftscribe/pull/${prNumber}` },
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
    // ONE rail row, ONE approval CTA for the whole lifecycle. The face is an
    // `applied` row with no confirmed merge → neutral "Go to approval page →".
    expect(getAllByTestId('past-decision-item')).toHaveLength(1);
    const link = getByTestId('iac-approve-link');
    expect(link.textContent?.trim()).toBe('Go to approval page →');
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

  it('renders the apply_status token on the meta line (applied + awaiting rebuild)', () => {
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
    // The HelpHint panel is collapsed by default (icon-only button, no text),
    // so both meta lines are the exact token string: action · status · ⎇ sha.
    expect(metas).toContain('iac_apply· applied· ⎇ 0496b30');
    expect(metas).toContain('iac_apply· awaiting rebuild· ⎇ 0496b30');
    const applied = metas.find((t) => t?.includes('applied'))!;
    expect(applied.indexOf('applied')).toBeGreaterThan(applied.indexOf('iac_apply'));
    expect(applied.indexOf('applied')).toBeLessThan(applied.indexOf('⎇'));
  });
});

describe('DecisionsRail — merge-aware "done" affordance', () => {
  it('applied + merged reads "applied & merged" (done) and the CTA is a history link', () => {
    const decisions: Decision[] = [
      iacRow({
        decision_id: 'done-68',
        apply_status: 'applied',
        merge_state: 'merged',
        pr_number: 68,
      }),
    ];
    const { getByTestId, container } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    const status = getByTestId('iac-status');
    expect(status.textContent).toContain('applied & merged');
    // The done token carries the ✓ check icon + the ok tone class.
    expect(status.classList.contains('iac-status--ok')).toBe(true);
    expect(container.querySelector('.iac-status-check')).not.toBeNull();
    // CTA is worded as a record, not a pending action.
    expect(getByTestId('iac-approve-link').textContent?.trim()).toBe('View approval history →');
    // A done row needs no help affordance is wrong — it DOES carry a "done" help ⓘ.
    expect(getByTestId('status-help')).toBeTruthy();
  });

  it('applied + merge_state=failed reads "merge pending" (warn) and keeps an actionable CTA', () => {
    const decisions: Decision[] = [
      iacRow({
        decision_id: 'pending-71',
        apply_status: 'applied',
        merge_state: 'failed',
        pr_number: 71,
      }),
    ];
    const { getByTestId, container } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    const status = getByTestId('iac-status');
    expect(status.textContent).toContain('merge pending');
    expect(status.classList.contains('iac-status--warn')).toBe(true);
    expect(container.querySelector('.iac-status-check')).toBeNull(); // not done
    expect(getByTestId('iac-approve-link').textContent?.trim()).toBe('Go to approval page →');
  });

  it('surfaces an "applied {date}" cue when applied_at predates created_at materially', () => {
    const decisions: Decision[] = [
      iacRow({
        decision_id: 'reconciled-32',
        apply_status: 'applied',
        merge_state: 'merged',
        pr_number: 32,
        applied_at: '2026-05-30T11:16:12Z', // real apply, a month before...
        created_at: '2026-06-26T16:03:27Z', // ...the merge-only reconcile (row time)
      }),
    ];
    const { getByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    expect(getByTestId('applied-cue').textContent).toContain('applied');
  });

  it('omits the cue when applied_at and created_at are the same moment', () => {
    const decisions: Decision[] = [
      iacRow({
        decision_id: 'fresh-68',
        apply_status: 'applied',
        merge_state: 'merged',
        pr_number: 68,
        applied_at: '2026-06-26T16:03:27Z',
        created_at: '2026-06-26T16:03:27Z',
      }),
    ];
    const { queryByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    expect(queryByTestId('applied-cue')).toBeNull();
  });
});

describe('DecisionsRail — status help affordance', () => {
  it('renders a focusable help button only for cryptic statuses; click reveals the explanation', async () => {
    const decisions: Decision[] = [
      iacRow({ decision_id: 'applied-68', apply_status: 'applied', pr_number: 68 }),
      iacRow({ decision_id: 'wait-71', apply_status: 'waiting_for_rebake', pr_number: 71 }),
    ];
    const { getAllByTestId, getByTestId, queryByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    // Exactly ONE help button — on the awaiting-rebuild row, not the applied one.
    const hints = getAllByTestId('status-help');
    expect(hints).toHaveLength(1);
    // Reachable by keyboard/touch: a real <button>, not a title-only span.
    expect(hints[0].tagName).toBe('BUTTON');
    expect(hints[0].getAttribute('aria-expanded')).toBe('false');
    // Collapsed by default — no panel in the DOM until activated.
    expect(queryByTestId('status-help-panel')).toBeNull();

    await fireEvent.click(hints[0]);
    expect(hints[0].getAttribute('aria-expanded')).toBe('true');
    const panel = getByTestId('status-help-panel');
    // The explanation says rebuild-of-what (the worker), not a circular "re-bake".
    expect(panel.textContent?.toLowerCase()).toContain('worker');
    expect(panel.textContent?.toLowerCase()).not.toContain('re-bake');
    // aria-controls points at the revealed panel.
    expect(hints[0].getAttribute('aria-controls')).toBe(panel.getAttribute('id'));
  });

  it('renders the help button on a failed row and reveals the state-proven-clean note', async () => {
    const decisions: Decision[] = [
      iacRow({ decision_id: 'failed-71', apply_status: 'failed', pr_number: 71 }),
    ];
    const { getAllByTestId, getByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    const hints = getAllByTestId('status-help');
    expect(hints).toHaveLength(1);
    await fireEvent.click(hints[0]);
    const panel = getByTestId('status-help-panel');
    // The distinguishing reassurance: live state was left untouched...
    expect(panel.textContent?.toLowerCase()).toContain('unchanged');
    // ...and it must NOT point the operator at the trace for the OpenTofu error.
    expect(panel.textContent?.toLowerCase()).not.toContain('open the trace');
  });

  it('renders no help button when every row is self-evident (applied)', () => {
    // `applied` is the only self-evident apply_status now that `failed` carries
    // help (state-proven-clean note); both rows must produce zero status-help ⓘ.
    const decisions: Decision[] = [
      iacRow({ decision_id: 'applied-68', apply_status: 'applied', pr_number: 68 }),
      iacRow({ decision_id: 'applied-71', apply_status: 'applied', pr_number: 71 }),
    ];
    const { queryAllByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    expect(queryAllByTestId('status-help')).toHaveLength(0);
  });
});

describe('DecisionsRail — no_op headline + help affordance', () => {
  /** A plain (non-iac) decision row — no pr_number, so it renders via the
   *  `{:else}` headline branch, not the PR-link branch. */
  function plainRow(over: Partial<Decision>): Decision {
    return {
      decision_id: `d-${Math.random().toString(36).slice(2)}`,
      action: 'no_op',
      created_at: '2026-05-30T01:00:00Z',
      ...over,
    } as Decision;
  }

  it('renders the friendly "No action needed" headline, not the bare no_op token', () => {
    const { getByTestId } = render(DecisionsRail, {
      props: { decisions: [plainRow({})], activeTraceId: null, onOpenTrace: noop },
    });
    const row = getByTestId('past-decision-item');
    const headline = row.querySelector('.row-action');
    expect(headline?.textContent?.trim()).toBe('No action needed');
    // The raw enum stays available as the hover tooltip for the curious.
    expect(headline?.getAttribute('title')).toBe('no_op');
    // The faint meta lead is crew-neutral (not drift-specific) so it stays
    // accurate if a non-drift crew ever writes a no_op row to this rail.
    const meta = row.querySelector('.row-meta');
    expect(meta?.textContent).toContain('Checked · all clear');
    expect(meta?.textContent?.toLowerCase()).not.toContain('drift');
  });

  it('shows a help button on a no_op row; click reveals the "checked, all clear" explanation', async () => {
    const { getAllByTestId, getByTestId, queryByTestId } = render(DecisionsRail, {
      props: { decisions: [plainRow({})], activeTraceId: null, onOpenTrace: noop },
    });
    const hints = getAllByTestId('action-help');
    expect(hints).toHaveLength(1);
    expect(hints[0].tagName).toBe('BUTTON');
    // Collapsed by default — panel absent until activated.
    expect(queryByTestId('action-help-panel')).toBeNull();

    await fireEvent.click(hints[0]);
    const panel = getByTestId('action-help-panel');
    expect(panel.textContent?.toLowerCase()).toContain('matched');
    expect(panel.textContent?.toLowerCase()).toContain('nothing');
    expect(hints[0].getAttribute('aria-controls')).toBe(panel.getAttribute('id'));
  });

  it('renders no action-help button on a non-no_op plain row (e.g. docs_pr)', () => {
    const { queryAllByTestId } = render(DecisionsRail, {
      props: {
        decisions: [plainRow({ action: 'docs_pr', github: { url: 'https://github.com/x/y/pull/9' } })],
        activeTraceId: null,
        onOpenTrace: noop,
      },
    });
    expect(queryAllByTestId('action-help')).toHaveLength(0);
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
    expect(meta).not.toContain('awaiting rebuild');

    // The summary carries the status COMPOSITION (exact single-expression
    // string — lifecycleSummaryLabel), never a bare count that hides state.
    const summary = getByTestId('iac-lifecycle-summary');
    expect(summary.textContent?.trim()).toBe('2 earlier steps · awaiting rebuild ×2');

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
    for (const s of steps) expect(s.textContent).toContain('awaiting rebuild');
    // Each cryptic step also carries the focusable help affordance.
    expect(getAllByTestId('status-help')).toHaveLength(2);

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
// traceButtonLabel wiring — iac_apply rows read "view details →" (recorded
// directly, no coordinator reasoning run); everything else keeps the
// reasoning-backed "view reasoning →".
// ---------------------------------------------------------------------------

describe('DecisionsRail — open-trace button label', () => {
  it('an iac_apply row shows "view details →"', () => {
    const decisions: Decision[] = [
      iacRow({ decision_id: 'applied-68', apply_status: 'applied', pr_number: 68, trace_id: 't-applied' }),
    ];
    const { getByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    expect(getByTestId('open-trace-button').textContent?.trim()).toBe('view details →');
  });

  it('an iac_apply lifecycle step also shows "view details →"', () => {
    const decisions: Decision[] = [
      iacRow({ decision_id: 'applied-68', apply_status: 'applied', pr_number: 68, trace_id: 't-new' }),
      iacRow({ decision_id: 'wait-68', apply_status: 'waiting_for_rebake', pr_number: 68, trace_id: 't-old' }),
    ];
    const { getByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    expect(getByTestId('lifecycle-open-trace').textContent?.trim()).toBe('view details →');
  });

  it('a non-iac_apply row (rollback) with a trace_id shows "view reasoning →"', () => {
    const decisions: Decision[] = [
      { decision_id: 'rb-1', action: 'rollback', trace_id: 't-rb' } as Decision,
    ];
    const { getByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    expect(getByTestId('open-trace-button').textContent?.trim()).toBe('view reasoning →');
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
    expect(token.textContent?.trim()).toBe('not executed in Observe mode');
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
        'dry run, not created on GitHub',
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

// ---------------------------------------------------------------------------
// Header hint — "why aren't the PR numbers consecutive?"
// ---------------------------------------------------------------------------

// The header PR-numbering hint shows only once there are ≥2 distinct, linked
// iac_apply PR numbers on screen — explaining why the numbers skip values.
describe('DecisionsRail — PR-numbering header hint', () => {
  it('shows no hint when only one numbered row exists (no sequence to explain)', () => {
    const decisions: Decision[] = [iacRow({ decision_id: 'a', apply_status: 'applied', pr_number: 68 })];
    const { queryByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    expect(queryByTestId('rail-gap-help')).toBeNull();
  });

  it('reveals a GitHub-numbering explanation when ≥2 distinct PR numbers are shown', async () => {
    const decisions: Decision[] = [
      iacRow({ decision_id: 'a', apply_status: 'applied', pr_number: 102 }),
      iacRow({ decision_id: 'b', apply_status: 'applied', pr_number: 68 }),
    ];
    const { getByTestId, queryByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    const hint = getByTestId('rail-gap-help');
    // A real, keyboard/touch-reachable button with its own (non-"status") name.
    expect(hint.tagName).toBe('BUTTON');
    expect(hint.getAttribute('aria-label')?.toLowerCase()).toContain('pull-request');
    expect(hint.getAttribute('aria-label')?.toLowerCase()).not.toContain('status');
    // Collapsed by default — no panel until activated.
    expect(queryByTestId('rail-gap-help-panel')).toBeNull();
    expect(hint.getAttribute('aria-expanded')).toBe('false');

    await fireEvent.click(hint);
    expect(hint.getAttribute('aria-expanded')).toBe('true');
    const panel = getByTestId('rail-gap-help-panel');
    // The explanation grounds the gaps in real GitHub PR numbering.
    expect(panel.textContent?.toLowerCase()).toContain('github');
    expect(hint.getAttribute('aria-controls')).toBe(panel.getAttribute('id'));
  });

  it('counts DISTINCT PR numbers — a multi-doc lifecycle for one PR shows no hint', () => {
    const decisions: Decision[] = [
      iacRow({ decision_id: 'a', apply_status: 'applied', pr_number: 68 }),
      iacRow({ decision_id: 'w1', apply_status: 'waiting_for_rebake', pr_number: 68 }),
      iacRow({ decision_id: 'w2', apply_status: 'waiting_for_rebake', pr_number: 68 }),
    ];
    const { queryByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    expect(queryByTestId('rail-gap-help')).toBeNull();
  });

  it('the header hint uses its own testid — it never inflates the per-row status-help count', () => {
    // Two self-evident `applied` rows (no status-help) but two distinct PR
    // numbers (header hint present). Proves the two affordances are independent.
    const decisions: Decision[] = [
      iacRow({ decision_id: 'a', apply_status: 'applied', pr_number: 68 }),
      iacRow({ decision_id: 'b', apply_status: 'applied', pr_number: 71 }),
    ];
    const { getByTestId, queryAllByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    expect(getByTestId('rail-gap-help')).toBeTruthy();
    expect(queryAllByTestId('status-help')).toHaveLength(0);
  });
});

// N distinct-PR applied rows (one doc each → N `single` rail items), newest-first.
function manyRows(n = 14): Decision[] {
  return Array.from({ length: n }, (_, i) =>
    iacRow({
      decision_id: `d${i}`,
      pr_number: i + 1,
      apply_status: 'applied',
      merge_state: 'merged',
      trace_id: `t${i}`,
      pr_title: `change ${i}`,
    }),
  );
}

describe('DecisionsRail — cap + search', () => {
  it('caps the rail to `max` rows and shows the search affordance with the TOTAL row count', () => {
    const { getAllByTestId, getByTestId } = render(DecisionsRail, {
      props: { decisions: manyRows(14), activeTraceId: null, onOpenTrace: noop, max: 10 },
    });
    expect(getAllByTestId('past-decision-item')).toHaveLength(10);
    expect(getByTestId('decisions-search-open').textContent).toContain('(14)');
  });

  it('hides the affordance when the rows fit within `max`', () => {
    const { queryByTestId } = render(DecisionsRail, {
      props: { decisions: manyRows(6), activeTraceId: null, onOpenTrace: noop, max: 10 },
    });
    expect(queryByTestId('decisions-search-open')).toBeNull();
  });

  it('hides the affordance when active-pinning means every row is already shown (total = max+1)', () => {
    const { getAllByTestId, queryByTestId } = render(DecisionsRail, {
      props: { decisions: manyRows(11), activeTraceId: 't10', onOpenTrace: noop, max: 10 },
    });
    expect(getAllByTestId('past-decision-item')).toHaveLength(11); // 10 + pinned active = all
    expect(queryByTestId('decisions-search-open')).toBeNull(); // nothing hidden
  });

  it('pins the active (open-trace) row even when it falls outside the cap', () => {
    const { getAllByTestId } = render(DecisionsRail, {
      props: { decisions: manyRows(14), activeTraceId: 't13', onOpenTrace: noop, max: 10 },
    });
    const rows = getAllByTestId('past-decision-item');
    expect(rows).toHaveLength(11); // 10 newest + the pinned active row
    expect(rows.some((r) => r.classList.contains('active'))).toBe(true);
  });

  it('opens the modal with the full list, filters live, and shows a no-match note', async () => {
    const rows = manyRows(14);
    rows[7] = iacRow({ decision_id: 'd7', pr_number: 8, apply_status: 'applied', merge_state: 'merged', trace_id: 't7', pr_title: 'unique-needle' });
    const { getByTestId, queryByTestId } = render(DecisionsRail, {
      props: { decisions: rows, activeTraceId: null, onOpenTrace: noop, max: 10 },
    });
    expect(queryByTestId('decisions-search-input')).toBeNull();
    await fireEvent.click(getByTestId('decisions-search-open'));
    expect(getByTestId('decisions-search-count').textContent).toContain('14 of 14');
    await fireEvent.input(getByTestId('decisions-search-input'), { target: { value: 'unique-needle' } });
    expect(getByTestId('decisions-search-count').textContent).toContain('1 of 14');
    await fireEvent.input(getByTestId('decisions-search-input'), { target: { value: 'zzz-nope' } });
    expect(getByTestId('decisions-search-count').textContent).toContain('0 of 14');
    expect(getByTestId('decisions-search-input')).toBeTruthy(); // input persists on empty results
  });

  it('opening a trace from inside the modal closes the modal and calls onOpenTrace', async () => {
    const onOpenTrace = vi.fn();
    const rows = manyRows(14);
    rows[7] = iacRow({ decision_id: 'd7', pr_number: 8, apply_status: 'applied', merge_state: 'merged', trace_id: 'trace-8', pr_title: 'unique-needle' });
    const { getByTestId, queryByTestId, container } = render(DecisionsRail, {
      props: { decisions: rows, activeTraceId: null, onOpenTrace, max: 10 },
    });
    await fireEvent.click(getByTestId('decisions-search-open'));
    await fireEvent.input(getByTestId('decisions-search-input'), { target: { value: 'unique-needle' } });
    const dialog = container.querySelector('dialog')!;
    await fireEvent.click(within(dialog).getByTestId('open-trace-button'));
    expect(onOpenTrace).toHaveBeenCalledWith('trace-8');
    // Modal closed → its search input is gone.
    expect(queryByTestId('decisions-search-input')).toBeNull();
  });
});
