import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import TokenStatus from '../../src/components/TokenStatus.svelte';

// The operator auth indicator. ds-7ag.3 split its three states by VOLUME rather
// than styling all of them as filled chips: a green "token ok" pill announced
// "everything is fine" as loudly as a real problem, on a header that already
// carried five other filled chips. Problems stay loud; the healthy state steps
// back. This file pins that split — it is the difference between a de-emphasis
// and an accidental loss of a warning.

afterEach(cleanup);

describe('TokenStatus', () => {
  it('the healthy state is quiet: no filled pill, muted label, still announced', () => {
    const { container, getByText } = render(TokenStatus, {
      props: { state: 'ok', onChange: vi.fn() },
    });
    const pill = container.querySelector('#token-status') as HTMLElement;
    expect(pill.className).toContain('token-status__quiet');
    // The green fill is what went away — the state must not carry ds-pill--ok.
    expect(pill.className).not.toContain('ds-pill--ok');
    // The accessible content and the live announcement are untouched: this was
    // a restyle, not a downgrade of what a screen reader is told.
    expect(getByText('token ok')).toBeTruthy();
    expect(pill.getAttribute('aria-live')).toBe('polite');
    // The key icon stays and becomes the status signal (recolored --ds-ok in CSS).
    expect(pill.querySelector('svg')).toBeTruthy();
  });

  it('a rejected token stays loud (danger emphasis, never quieted)', () => {
    const { container, getByText } = render(TokenStatus, {
      props: { state: 'invalid', onChange: vi.fn() },
    });
    const pill = container.querySelector('#token-status') as HTMLElement;
    expect(pill.className).toContain('ds-pill--danger');
    expect(pill.className).not.toContain('token-status__quiet');
    expect(getByText('token rejected')).toBeTruthy();
  });

  it('a missing token keeps its muted-pill emphasis', () => {
    const { container, getByText } = render(TokenStatus, {
      props: { state: 'missing', onChange: vi.fn() },
    });
    const pill = container.querySelector('#token-status') as HTMLElement;
    expect(pill.className).toContain('ds-pill--muted');
    expect(pill.className).not.toContain('token-status__quiet');
    expect(getByText('no token')).toBeTruthy();
  });

  it('the change-token affordance still calls back', async () => {
    const onChange = vi.fn();
    const { container } = render(TokenStatus, { props: { state: 'ok', onChange } });
    await fireEvent.click(container.querySelector('#change-token-btn') as HTMLElement);
    expect(onChange).toHaveBeenCalledTimes(1);
  });
});
