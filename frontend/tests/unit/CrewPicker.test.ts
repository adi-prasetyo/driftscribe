import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import CrewPicker from '../../src/components/CrewPicker.svelte';
import { WORKLOADS } from '../../src/lib/workloads';

// CrewPicker is the chat-composer workload selector: four small crew cards
// (native radios under the hood) where the SELECTED card's glyph loops and the
// rest are static. Each card's domain descriptor surfaces as a tooltip on
// hover/focus and as the radio's accessible description. We assert the DOM
// contract — radio identity, the selection-driven `crew-glyph--animated` class,
// the descriptor wiring, and disabled inertness. jsdom can't play keyframes, so
// motion is asserted via the class the CrewGlyph gate toggles (proven
// separately in CrewGlyph.test.ts).

afterEach(cleanup);

const VALUES = ['drift', 'upgrade', 'provision', 'explore'] as const;

function glyphAnimated(container: HTMLElement, verb: string): boolean {
  const svg = container.querySelector(`[data-testid="crew-glyph-${verb}"]`);
  return !!svg && svg.getAttribute('class')!.includes('crew-glyph--animated');
}

describe('CrewPicker — card group', () => {
  it('renders one radio card per workload, in catalog order', () => {
    const { container } = render(CrewPicker, { props: { value: 'drift' } });
    for (const v of VALUES) {
      const card = container.querySelector(`[data-testid="crew-card-${v}"]`);
      expect(card, `card for ${v} should render`).not.toBeNull();
      const radio = card!.querySelector('input[type="radio"]') as HTMLInputElement;
      expect(radio).not.toBeNull();
      expect(radio.value).toBe(v);
    }
  });

  it('groups its radios under a unique name so two pickers cannot cross-wire', () => {
    const a = render(CrewPicker, { props: { value: 'drift' } });
    const b = render(CrewPicker, { props: { value: 'upgrade' } });
    const nameA = (a.container.querySelector('input[type="radio"]') as HTMLInputElement).name;
    const nameB = (b.container.querySelector('input[type="radio"]') as HTMLInputElement).name;
    expect(nameA).not.toBe('');
    expect(nameA).not.toBe(nameB);
  });

  it('loops ONLY the selected card; the rest rest on their static frame', () => {
    const { container } = render(CrewPicker, { props: { value: 'drift' } });
    expect(glyphAnimated(container, 'drift')).toBe(true);
    for (const v of VALUES.filter((x) => x !== 'drift')) {
      expect(glyphAnimated(container, v), `${v} must be static when drift is selected`).toBe(false);
    }
  });

  it('moves the animation to whichever card the operator selects', async () => {
    const { container } = render(CrewPicker, { props: { value: 'drift' } });
    const exploreRadio = container.querySelector(
      '[data-testid="crew-card-explore"] input',
    ) as HTMLInputElement;
    await fireEvent.click(exploreRadio);
    await waitFor(() => expect(glyphAnimated(container, 'explore')).toBe(true));
    expect(glyphAnimated(container, 'drift')).toBe(false);
    expect(exploreRadio.checked).toBe(true);
  });

  it('exposes each card\'s summary as a tooltip AND the radio\'s accessible description', () => {
    const { container } = render(CrewPicker, { props: { value: 'drift' } });
    for (const wl of WORKLOADS) {
      const card = container.querySelector(`[data-testid="crew-card-${wl.value}"]`)!;
      const radio = card.querySelector('input[type="radio"]') as HTMLInputElement;
      const hintId = radio.getAttribute('aria-describedby');
      expect(hintId, `${wl.value} radio should be described by its hint`).toBeTruthy();
      const hint = card.querySelector(`#${CSS.escape(hintId!)}`) as HTMLElement;
      expect(hint, `${wl.value} hint element should exist`).not.toBeNull();
      expect(hint.getAttribute('role')).toBe('tooltip');
      expect(hint.textContent).toBe(wl.summary);
      // The accessible NAME is pinned to just the crew name (so the summary
      // isn't duplicated into name + description) — Codex review 019ed108.
      const nameId = radio.getAttribute('aria-labelledby');
      const nameEl = card.querySelector(`#${CSS.escape(nameId!)}`) as HTMLElement;
      expect(nameEl?.textContent).toBe(wl.name);
    }
  });

  it('does NOT render an autonomy marker on the cards (that lives in the capability card)', () => {
    const { container } = render(CrewPicker, { props: { value: 'drift' } });
    // The Anchor (drift) card carries no "auto"/"autonomous" text — only its
    // name + descriptor (which is "Cloud Run config", not an autonomy word).
    const drift = container.querySelector('[data-testid="crew-card-drift"]')!;
    expect(drift.textContent?.toLowerCase()).not.toContain('auto');
  });
});

describe('CrewPicker — disabled (historical replay)', () => {
  it('disables the radios and stops the selected glyph from looping', () => {
    const { container } = render(CrewPicker, { props: { value: 'drift', disabled: true } });
    const radio = container.querySelector('input[type="radio"]') as HTMLInputElement;
    expect(radio.disabled).toBe(true);
    // Even the selected card is static when the composer is inert.
    expect(glyphAnimated(container, 'drift')).toBe(false);
  });
});

describe('CrewPicker — crew-locked (open thread)', () => {
  it('refuses to switch to a locked card on click', async () => {
    const { container } = render(CrewPicker, { props: { value: 'drift', lockedTo: 'drift' } });
    const explore = container.querySelector(
      '[data-testid="crew-card-explore"] input',
    ) as HTMLInputElement;
    await fireEvent.click(explore);
    // jsdom may transiently report the clicked radio checked inside the
    // cancelled activation; assert the SETTLED state (post-rerender), and pin
    // that the locked-to radio is still the checked one.
    await waitFor(() => {
      expect(explore.checked).toBe(false);
      const drift = container.querySelector(
        '[data-testid="crew-card-drift"] input',
      ) as HTMLInputElement;
      expect(drift.checked).toBe(true);
    });
  });

  it('marks locked cards aria-disabled; the thread crew stays operable', () => {
    const { container } = render(CrewPicker, { props: { value: 'drift', lockedTo: 'drift' } });
    for (const v of VALUES) {
      const radio = container.querySelector(`[data-testid="crew-card-${v}"] input`)!;
      if (v === 'drift') expect(radio.getAttribute('aria-disabled')).toBeNull();
      else expect(radio.getAttribute('aria-disabled')).toBe('true');
    }
  });

  it("swaps locked cards' tooltip to the lock explanation naming the thread crew", () => {
    const { container } = render(CrewPicker, { props: { value: 'drift', lockedTo: 'drift' } });
    const exploreHint = container.querySelector(
      '[data-testid="crew-card-explore"] [role="tooltip"]',
    )!;
    expect(exploreHint.textContent).toContain('This thread is with Anchor');
    expect(exploreHint.textContent?.toLowerCase()).toContain('new chat');
    // The thread's own card keeps its normal summary.
    const driftHint = container.querySelector(
      '[data-testid="crew-card-drift"] [role="tooltip"]',
    )!;
    expect(driftHint.textContent).toBe(WORKLOADS.find((w) => w.value === 'drift')!.summary);
  });

  it('blocks arrow-key radio navigation while locked', async () => {
    const { container } = render(CrewPicker, { props: { value: 'drift', lockedTo: 'drift' } });
    const drift = container.querySelector(
      '[data-testid="crew-card-drift"] input',
    ) as HTMLInputElement;
    // fireEvent resolves false when preventDefault() was called…
    const notPrevented = await fireEvent.keyDown(drift, { key: 'ArrowRight' });
    expect(notPrevented).toBe(false);
    // …and the selection must actually stay put (the behavior, not the API).
    await waitFor(() => expect(drift.checked).toBe(true));
  });

  it('snaps a programmatic value change back to the locked crew (belt)', async () => {
    const { container } = render(CrewPicker, { props: { value: 'explore', lockedTo: 'drift' } });
    await waitFor(() => {
      const drift = container.querySelector(
        '[data-testid="crew-card-drift"] input',
      ) as HTMLInputElement;
      expect(drift.checked).toBe(true);
    });
  });

  it('force-shows the tooltip on a blocked click (nudge feedback)', async () => {
    const { container } = render(CrewPicker, { props: { value: 'drift', lockedTo: 'drift' } });
    const card = container.querySelector('[data-testid="crew-card-explore"]')!;
    await fireEvent.click(card.querySelector('input')!);
    expect(card.classList.contains('crew-card--nudged')).toBe(true);
  });

  it('does not lock anything when lockedTo is null (fresh composer)', async () => {
    const { container } = render(CrewPicker, { props: { value: 'drift', lockedTo: null } });
    const explore = container.querySelector(
      '[data-testid="crew-card-explore"] input',
    ) as HTMLInputElement;
    await fireEvent.click(explore);
    expect(explore.checked).toBe(true);
    expect(explore.getAttribute('aria-disabled')).toBeNull();
  });
});
