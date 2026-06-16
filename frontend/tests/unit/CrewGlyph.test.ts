import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import CrewGlyph from '../../src/components/CrewGlyph.svelte';

// Component tests for CrewGlyph — the per-agent "one estate, four verbs"
// looping glyph. We assert STRUCTURE and accessibility, not animation playback
// (jsdom does not run CSS @keyframes, and the global base.css reduced-motion
// reset — mirrored by setup.ts's matchMedia mock returning matches:true for
// 'reduce' — means the animated branch never actually plays here anyway).
// What matters and is testable: the right verb renders the right elements, the
// SVG is decorative (aria-hidden), and an unknown verb degrades to a static
// node rather than a broken/blank glyph.

afterEach(cleanup);

// The frozen symbolic workload values that map to each verb's animation.
const VERBS = ['drift', 'upgrade', 'provision', 'explore'] as const;

// Verb -> a class that ONLY that verb's markup contains, proving the right
// branch rendered (and, by omission, that the others did not).
const SIGNATURE: Record<string, string> = {
  drift: '.anchor-home',
  upgrade: '.patch-crack',
  provision: '.prov-branch',
  explore: '.scan-band',
};

describe('CrewGlyph — accessibility + SVG contract', () => {
  it('renders an <svg> that is decorative (aria-hidden) and unfocusable for every verb', () => {
    for (const verb of VERBS) {
      const { container } = render(CrewGlyph, { props: { verb } });
      const svg = container.querySelector('svg');
      expect(svg, `verb ${verb} should render an <svg>`).not.toBeNull();
      expect(svg!.getAttribute('aria-hidden')).toBe('true');
      expect(svg!.getAttribute('focusable')).toBe('false');
      // No text node anywhere — meaning must live in the card text, not the SVG.
      expect(svg!.textContent?.trim()).toBe('');
      cleanup();
    }
  });

  it('mirrors the icon-system SVG idiom: 0 0 64 64 viewBox, no fill, currentColor stroke', () => {
    const { container } = render(CrewGlyph, { props: { verb: 'drift' } });
    const svg = container.querySelector('svg')!;
    expect(svg.getAttribute('viewBox')).toBe('0 0 64 64');
    expect(svg.getAttribute('fill')).toBe('none');
    expect(svg.getAttribute('stroke')).toBe('currentColor');
    expect(svg.getAttribute('stroke-linecap')).toBe('round');
    expect(svg.getAttribute('stroke-linejoin')).toBe('round');
  });

  it('defaults to a 24px render size and respects the size prop', () => {
    const def = render(CrewGlyph, { props: { verb: 'explore' } });
    const svg = def.container.querySelector('svg')!;
    expect(svg.getAttribute('width')).toBe('24');
    expect(svg.getAttribute('height')).toBe('24');
    cleanup();

    const big = render(CrewGlyph, { props: { verb: 'explore', size: 40 } });
    const svg2 = big.container.querySelector('svg')!;
    expect(svg2.getAttribute('width')).toBe('40');
    expect(svg2.getAttribute('height')).toBe('40');
  });
});

describe('CrewGlyph — verb routing', () => {
  it('tags the root with a stable per-verb test id and modifier class', () => {
    for (const verb of VERBS) {
      const { container } = render(CrewGlyph, { props: { verb } });
      const svg = container.querySelector('svg')!;
      expect(svg.getAttribute('data-testid')).toBe(`crew-glyph-${verb}`);
      expect(svg.getAttribute('class')).toContain(`crew-glyph--${verb}`);
      cleanup();
    }
  });

  it('renders ONLY the selected verb\'s signature markup', () => {
    for (const verb of VERBS) {
      const { container } = render(CrewGlyph, { props: { verb } });
      // The chosen verb's signature element is present...
      expect(
        container.querySelector(SIGNATURE[verb]),
        `verb ${verb} should render ${SIGNATURE[verb]}`,
      ).not.toBeNull();
      // ...and no OTHER verb's signature leaked in.
      for (const other of VERBS) {
        if (other === verb) continue;
        expect(
          container.querySelector(SIGNATURE[other]),
          `verb ${verb} must not render ${SIGNATURE[other]}`,
        ).toBeNull();
      }
      cleanup();
    }
  });
});

describe('CrewGlyph — unknown verb fallback', () => {
  it('degrades an unrecognized verb to a static node, not a blank/broken glyph', () => {
    const { container } = render(CrewGlyph, { props: { verb: 'totally-unknown' } });
    const svg = container.querySelector('svg')!;
    // Stable, non-leaky identity for the fallback.
    expect(svg.getAttribute('data-testid')).toBe('crew-glyph-unknown');
    expect(svg.getAttribute('aria-hidden')).toBe('true');
    // A node is still drawn (the shared service-node rect) so the row never
    // shows an empty box — and it is the GENUINELY-STATIC node, never the
    // animated `.anchor-node`, so an unknown workload is not misrepresented as
    // Anchor drifting.
    expect(container.querySelector('.static-node')).not.toBeNull();
    expect(container.querySelector('.anchor-node')).toBeNull();
    // None of the verb-specific animated parts are present.
    for (const sig of Object.values(SIGNATURE)) {
      expect(container.querySelector(sig)).toBeNull();
    }
  });
});
