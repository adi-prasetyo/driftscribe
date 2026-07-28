import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import SealStamp from '../../src/components/SealStamp.svelte';

// SealStamp is the 判子 (hanko) approval stamp — vermilion (--ds-seal), the
// only red in the palette, reserved for this one moment: a human approved
// something. Two sizes (desk hero vs ledger-row mini); `animate` is opt-in
// (default false) so ledger rows don't all fire the stamp-in keyframe on
// every render — only the single freshly-approved instance on the desk does.

afterEach(cleanup);

describe('SealStamp', () => {
  it('renders the 承認 glyph', () => {
    const { getByText } = render(SealStamp);
    expect(getByText('承認')).toBeTruthy();
  });

  it('has role="img" with a non-empty accessible name', () => {
    const { getByRole } = render(SealStamp);
    const el = getByRole('img');
    expect(el.getAttribute('aria-label')?.trim()).toBeTruthy();
  });

  it('defaults to the "lg" size class', () => {
    const { getByRole } = render(SealStamp);
    const el = getByRole('img');
    expect(el.className).toContain('lg');
    expect(el.className).not.toContain('sm');
  });

  it('size="sm" is expressed as a distinct class', () => {
    const { getByRole } = render(SealStamp, { props: { size: 'sm' } });
    const el = getByRole('img');
    expect(el.className).toContain('sm');
    expect(el.className).not.toContain('lg');
  });

  it('defaults to NOT animating', () => {
    const { getByRole } = render(SealStamp);
    const el = getByRole('img');
    expect(el.className).not.toMatch(/animate/);
  });

  it('animate=true marks the animating state', () => {
    const { getByRole } = render(SealStamp, { props: { animate: true } });
    const el = getByRole('img');
    expect(el.className).toMatch(/animate/);
  });

  it('animate=false explicitly stays un-animated', () => {
    const { getByRole } = render(SealStamp, { props: { animate: false } });
    const el = getByRole('img');
    expect(el.className).not.toMatch(/animate/);
  });

  it('animate is valid on the sm size too (both markers present)', () => {
    // The resting opacity for sm (.8) differs from lg (.85) in the mockup —
    // both are driven off the same --seal-rest-opacity variable so the
    // stampIn keyframe's endpoint always agrees with the base, whichever
    // size is animating. This just pins that sm + animate is a supported,
    // renderable combination, not a desk-only convention baked into markup.
    const { getByRole } = render(SealStamp, { props: { size: 'sm', animate: true } });
    const el = getByRole('img');
    expect(el.className).toContain('sm');
    expect(el.className).toMatch(/animate/);
  });
});
