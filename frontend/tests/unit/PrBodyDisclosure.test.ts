// frontend/tests/unit/PrBodyDisclosure.test.ts
import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import PrBodyDisclosure from '../../src/components/PrBodyDisclosure.svelte';

afterEach(cleanup);

describe('PrBodyDisclosure', () => {
  it('renders nothing when body is null', () => {
    const { queryByTestId } = render(PrBodyDisclosure, { props: { body: null } });
    expect(queryByTestId('pr-body-disclosure')).toBeNull();
  });

  it('renders nothing when body is an empty string', () => {
    const { queryByTestId } = render(PrBodyDisclosure, { props: { body: '' } });
    expect(queryByTestId('pr-body-disclosure')).toBeNull();
  });

  it('renders nothing when body is whitespace-only (no blocks)', () => {
    const { queryByTestId } = render(PrBodyDisclosure, { props: { body: '   \n\n  ' } });
    expect(queryByTestId('pr-body-disclosure')).toBeNull();
  });

  it('renders **bold** as a <strong> element (not literal asterisks)', () => {
    const { getByTestId } = render(PrBodyDisclosure, {
      props: { body: 'this is **important** stuff' },
    });
    const md = getByTestId('pr-body-md');
    const strong = md.querySelector('strong');
    expect(strong?.textContent).toBe('important');
    expect(md.textContent).not.toContain('**');
  });

  it('renders `code` as a <code> element', () => {
    const { getByTestId } = render(PrBodyDisclosure, {
      props: { body: 'set `service_account` here' },
    });
    const code = getByTestId('pr-body-md').querySelector('code');
    expect(code?.textContent).toBe('service_account');
  });

  it('renders a heading without showing the ## marker', () => {
    const { getByTestId } = render(PrBodyDisclosure, {
      props: { body: '## A title\n\nbody text' },
    });
    const md = getByTestId('pr-body-md');
    expect(md.textContent).toContain('A title');
    expect(md.textContent).not.toContain('##');
    // headings are styled, NOT real <h*> (keeps the page heading outline clean)
    expect(md.querySelector('h1,h2,h3,h4,h5,h6')).toBeNull();
    expect(md.querySelector('.md-heading')).toBeTruthy();
  });

  it('renders a bullet list as <li> items without the - marker', () => {
    const { getByTestId } = render(PrBodyDisclosure, {
      props: { body: '- first\n- second' },
    });
    const items = getByTestId('pr-body-md').querySelectorAll('li');
    expect(items).toHaveLength(2);
    expect(items[0].textContent).toBe('first');
    expect(getByTestId('pr-body-md').textContent).not.toContain('- first');
  });

  it('renders a safe https link as an anchor with noopener noreferrer', () => {
    const { getByTestId } = render(PrBodyDisclosure, {
      props: { body: 'see [the PR](https://github.com/a/b/pull/9)' },
    });
    const a = getByTestId('pr-body-md').querySelector('a');
    expect(a?.getAttribute('href')).toBe('https://github.com/a/b/pull/9');
    expect(a?.getAttribute('rel')).toBe('noopener noreferrer');
    expect(a?.getAttribute('target')).toBe('_blank');
    expect(a?.textContent).toBe('the PR');
  });

  it('renders an ordered list as <ol> and a fenced code block as <pre><code>', () => {
    const { getByTestId } = render(PrBodyDisclosure, {
      props: { body: '1. one\n2. two\n\n```\nconst x = 1;\n```' },
    });
    const md = getByTestId('pr-body-md');
    expect(md.querySelector('ol')).toBeTruthy();
    expect(md.querySelectorAll('ol > li')).toHaveLength(2);
    const codeblock = md.querySelector('pre > code');
    expect(codeblock?.textContent).toContain('const x = 1;');
  });

  it('renders a GFM table with scoped column headers', () => {
    const { getByTestId } = render(PrBodyDisclosure, {
      props: { body: '| Var | Live |\n|---|---|\n| A | 1 |' },
    });
    const md = getByTestId('pr-body-md');
    const table = md.querySelector('table');
    expect(table).toBeTruthy();
    const ths = md.querySelectorAll('th');
    expect(ths).toHaveLength(2);
    ths.forEach((th) => expect(th.getAttribute('scope')).toBe('col'));
    expect(md.querySelectorAll('tbody td')).toHaveLength(2);
  });

  it('never renders a javascript: link (degrades to plain text)', () => {
    const { getByTestId } = render(PrBodyDisclosure, {
      props: { body: 'click [here](javascript:alert(1))' },
    });
    const md = getByTestId('pr-body-md');
    expect(md.querySelector('a')).toBeNull();
    expect(md.textContent).toContain('here');
    expect(md.textContent).not.toContain('javascript:');
  });

  it('escapes HTML — no injected element, no {@html} surface', () => {
    const { getByTestId } = render(PrBodyDisclosure, {
      props: { body: '<img src=x onerror=alert(1)> and <script>alert(2)</script>' },
    });
    const md = getByTestId('pr-body-md');
    expect(md.querySelector('img')).toBeNull();
    expect(md.querySelector('script')).toBeNull();
    // the markup survives as visible literal text
    expect(md.textContent).toContain('<img src=x onerror=alert(1)>');
  });

  it('preserves a non-ASCII emoji as visible text (no longer dropped by mono font)', () => {
    const { getByTestId } = render(PrBodyDisclosure, {
      props: { body: 'Generated as the C5g end-to-end test. 🤖' },
    });
    expect(getByTestId('pr-body-md').textContent).toContain('🤖');
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
