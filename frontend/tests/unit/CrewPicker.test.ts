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

const VALUES = ['drift', 'upgrade', 'explore', 'provision'] as const;

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

  it('exposes each card\'s descriptor as a tooltip AND the radio\'s accessible description', () => {
    const { container } = render(CrewPicker, { props: { value: 'drift' } });
    for (const wl of WORKLOADS) {
      const card = container.querySelector(`[data-testid="crew-card-${wl.value}"]`)!;
      const radio = card.querySelector('input[type="radio"]') as HTMLInputElement;
      const hintId = radio.getAttribute('aria-describedby');
      expect(hintId, `${wl.value} radio should be described by its hint`).toBeTruthy();
      const hint = card.querySelector(`#${CSS.escape(hintId!)}`) as HTMLElement;
      expect(hint, `${wl.value} hint element should exist`).not.toBeNull();
      expect(hint.getAttribute('role')).toBe('tooltip');
      expect(hint.textContent).toBe(wl.descriptor);
      // The accessible NAME is pinned to just the crew name (so the descriptor
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
