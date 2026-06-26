// frontend/tests/unit/PrBodyDisclosure.test.ts
import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import PrBodyDisclosure from '../../src/components/PrBodyDisclosure.svelte';

afterEach(cleanup);

describe('PrBodyDisclosure', () => {
  it('renders the body in a <pre> when present', () => {
    const { getByTestId } = render(PrBodyDisclosure, {
      props: { body: '## Repoints SA\nWhy: isolation' },
    });
    const pre = getByTestId('pr-body-disclosure').querySelector('pre');
    expect(pre?.textContent).toBe('## Repoints SA\nWhy: isolation');
  });

  it('renders nothing when body is null', () => {
    const { queryByTestId } = render(PrBodyDisclosure, { props: { body: null } });
    expect(queryByTestId('pr-body-disclosure')).toBeNull();
  });

  it('renders nothing when body is an empty string', () => {
    const { queryByTestId } = render(PrBodyDisclosure, { props: { body: '' } });
    expect(queryByTestId('pr-body-disclosure')).toBeNull();
  });

  it('escapes HTML in the body (no injection surface)', () => {
    const { getByTestId } = render(PrBodyDisclosure, {
      props: { body: '<img src=x onerror=alert(1)>' },
    });
    const el = getByTestId('pr-body-disclosure');
    // Rendered as literal text — no real <img> element is created.
    expect(el.querySelector('img')).toBeNull();
    expect(el.querySelector('pre')?.textContent).toContain('<img src=x onerror=alert(1)>');
  });

  it('shows the truncated note when truncated', () => {
    const { getByTestId } = render(PrBodyDisclosure, {
      props: { body: 'x', truncated: true },
    });
    expect(getByTestId('pr-body-truncated')).toBeTruthy();
  });

  it('omits the truncated note by default', () => {
    const { queryByTestId } = render(PrBodyDisclosure, { props: { body: 'x' } });
    expect(queryByTestId('pr-body-truncated')).toBeNull();
  });
});
