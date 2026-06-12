import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import Icon from '../../src/components/Icon.svelte';
import { ICON_PATHS, type IconName } from '../../src/lib/icons';

afterEach(cleanup);

// ---------------------------------------------------------------------------
// Documented 21-icon set — drift-pin
// ---------------------------------------------------------------------------
const EXPECTED_ICON_NAMES = new Set<string>([
  'eye',
  'git-pull-request',
  'zap',
  'pause',
  'play',
  'radar',
  'compass',
  'key-round',
  'send',
  'check',
  'x',
  'history',
  'boxes',
  'shield',
  'brain',
  'wrench',
  'cable',
  'copy',
  'rotate-ccw',
  'git-merge',
  'alert-triangle',
  'file-text',
]);

// ---------------------------------------------------------------------------
// Registry safety: allowlisted tags and attributes
// ---------------------------------------------------------------------------
const ALLOWED_TAGS = new Set(['path', 'circle', 'rect', 'line', 'polyline', 'polygon']);
const ALLOWED_ATTRS = new Set([
  'd', 'cx', 'cy', 'r', 'x', 'y', 'x1', 'y1', 'x2', 'y2',
  'width', 'height', 'rx', 'ry', 'points', 'fill',
]);

function parseIconMarkup(markup: string): { elements: Element[] } {
  // Use DOMParser via jsdom (available in the test environment).
  const doc = new DOMParser().parseFromString(`<svg>${markup}</svg>`, 'image/svg+xml');
  const parserError = doc.querySelector('parsererror');
  if (parserError) {
    throw new Error(`SVG parse error for markup: ${markup}\n${parserError.textContent}`);
  }
  const svgRoot = doc.documentElement;
  const elements: Element[] = [];
  function walk(node: Element) {
    for (const child of Array.from(node.children)) {
      elements.push(child);
      walk(child);
    }
  }
  walk(svgRoot);
  return { elements };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('Icon registry — drift-pin', () => {
  it('contains exactly the documented 21 icon names', () => {
    const registryNames = new Set(Object.keys(ICON_PATHS));
    const missing = [...EXPECTED_ICON_NAMES].filter((n) => !registryNames.has(n));
    const extra = [...registryNames].filter((n) => !EXPECTED_ICON_NAMES.has(n));
    expect(missing, `Missing icons: ${missing.join(', ')}`).toHaveLength(0);
    expect(extra, `Extra icons not in plan: ${extra.join(', ')}`).toHaveLength(0);
  });
});

describe('Icon registry — markup safety', () => {
  it('every icon value uses only allowlisted tags', () => {
    for (const [name, markup] of Object.entries(ICON_PATHS)) {
      const { elements } = parseIconMarkup(markup);
      for (const el of elements) {
        const tag = el.tagName.toLowerCase();
        expect(
          ALLOWED_TAGS.has(tag),
          `Icon "${name}" contains disallowed tag <${tag}>`,
        ).toBe(true);
      }
    }
  });

  it('every icon value uses only allowlisted attributes', () => {
    for (const [name, markup] of Object.entries(ICON_PATHS)) {
      const { elements } = parseIconMarkup(markup);
      for (const el of elements) {
        for (const attr of Array.from(el.attributes)) {
          expect(
            ALLOWED_ATTRS.has(attr.name.toLowerCase()),
            `Icon "${name}" element <${el.tagName}> has disallowed attribute "${attr.name}"`,
          ).toBe(true);
        }
      }
    }
  });

  it('no icon value contains <script', () => {
    for (const [name, markup] of Object.entries(ICON_PATHS)) {
      expect(markup, `Icon "${name}" contains <script`).not.toMatch(/<script/i);
    }
  });

  it('no icon value contains inline event handlers (on*=)', () => {
    for (const [name, markup] of Object.entries(ICON_PATHS)) {
      expect(markup, `Icon "${name}" contains inline event handler`).not.toMatch(/on[a-z]+=\s*["']/i);
    }
  });

  it('no icon value contains href= or xlink:', () => {
    for (const [name, markup] of Object.entries(ICON_PATHS)) {
      expect(markup, `Icon "${name}" contains href=`).not.toMatch(/href=/i);
      expect(markup, `Icon "${name}" contains xlink:`).not.toMatch(/xlink:/i);
    }
  });
});

describe('Icon component — rendering', () => {
  it('renders an <svg> with aria-hidden="true" and focusable="false"', () => {
    const { container } = render(Icon, { props: { name: 'check' as IconName } });
    const svg = container.querySelector('svg');
    expect(svg).not.toBeNull();
    expect(svg!.getAttribute('aria-hidden')).toBe('true');
    expect(svg!.getAttribute('focusable')).toBe('false');
  });

  it('defaults to width and height of 16', () => {
    const { container } = render(Icon, { props: { name: 'check' as IconName } });
    const svg = container.querySelector('svg')!;
    expect(svg.getAttribute('width')).toBe('16');
    expect(svg.getAttribute('height')).toBe('16');
  });

  it('respects the size prop', () => {
    const { container } = render(Icon, { props: { name: 'check' as IconName, size: 24 } });
    const svg = container.querySelector('svg')!;
    expect(svg.getAttribute('width')).toBe('24');
    expect(svg.getAttribute('height')).toBe('24');
  });

  it('has class "ds-icon" when no extraClass is passed', () => {
    const { container } = render(Icon, { props: { name: 'x' as IconName } });
    const svg = container.querySelector('svg')!;
    expect(svg.getAttribute('class')).toBe('ds-icon');
  });

  it('has class "ds-icon foo" when extraClass="foo"', () => {
    const { container } = render(Icon, { props: { name: 'x' as IconName, extraClass: 'foo' } });
    const svg = container.querySelector('svg')!;
    expect(svg.getAttribute('class')).toBe('ds-icon foo');
  });

  it('class never contains the string "undefined"', () => {
    // Without extraClass
    const { container: c1 } = render(Icon, { props: { name: 'check' as IconName } });
    expect(c1.querySelector('svg')!.getAttribute('class')).not.toContain('undefined');

    // With empty string extraClass
    const { container: c2 } = render(Icon, { props: { name: 'check' as IconName, extraClass: '' } });
    expect(c2.querySelector('svg')!.getAttribute('class')).not.toContain('undefined');
  });

  it('renders inner path markup for a known icon', () => {
    const { container } = render(Icon, { props: { name: 'check' as IconName } });
    const svg = container.querySelector('svg')!;
    // check icon should have a <path> element
    expect(svg.querySelector('path')).not.toBeNull();
  });
});
