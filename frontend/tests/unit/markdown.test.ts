// frontend/tests/unit/markdown.test.ts
//
// Unit tests for the hand-rolled Markdown SUBSET parser that backs the
// open-trace "What this change did (from the PR)" disclosure. The parser is a
// PURE function producing an AST; PrBodyDisclosure.svelte renders that AST
// through Svelte's auto-escaping native elements (no {@html}). These tests pin
// the parse, the link-href security allowlist, and graceful degradation of the
// syntax we deliberately DON'T fully support.

import { describe, it, expect } from 'vitest';
import {
  parseMarkdown,
  safeMarkdownLinkHref,
  type InlineNode,
  type BlockNode,
} from '../../src/lib/markdown';

// Flatten an inline tree to its visible text (code + text + line breaks),
// so a test can assert "the words survived" without pinning nesting.
function plainInline(nodes: InlineNode[]): string {
  return nodes
    .map((n) => {
      if (n.type === 'text') return n.value;
      if (n.type === 'code') return n.value;
      if (n.type === 'br') return '\n';
      if (n.type === 'strong' || n.type === 'em' || n.type === 'link') {
        return plainInline(n.children);
      }
      return '';
    })
    .join('');
}

describe('safeMarkdownLinkHref', () => {
  it('accepts http and https, returning a normalized href', () => {
    expect(safeMarkdownLinkHref('https://github.com/a/b/pull/1')).toBe(
      'https://github.com/a/b/pull/1',
    );
    expect(safeMarkdownLinkHref('http://example.com')).toBe('http://example.com/');
  });

  it('rejects dangerous and non-web schemes', () => {
    expect(safeMarkdownLinkHref('javascript:alert(1)')).toBeNull();
    expect(safeMarkdownLinkHref('JavaScript:alert(1)')).toBeNull();
    expect(safeMarkdownLinkHref('data:text/html,<script>')).toBeNull();
    expect(safeMarkdownLinkHref('vbscript:msgbox(1)')).toBeNull();
    expect(safeMarkdownLinkHref('file:///etc/passwd')).toBeNull();
    // mailto is deliberately NOT supported (no template emits it).
    expect(safeMarkdownLinkHref('mailto:a@b.com')).toBeNull();
  });

  it('does not decode HTML entities into a scheme', () => {
    // jav&#x61;script: must NOT become javascript:
    expect(safeMarkdownLinkHref('jav&#x61;script:alert(1)')).toBeNull();
  });

  it('rejects control chars, whitespace, backslash and angle brackets', () => {
    expect(safeMarkdownLinkHref('https://e.com/a b')).toBeNull();
    expect(safeMarkdownLinkHref('https://e.com/\tx')).toBeNull();
    expect(safeMarkdownLinkHref('https://e.com/\nx')).toBeNull();
    expect(safeMarkdownLinkHref('https://good.com\\@evil.com')).toBeNull();
    expect(safeMarkdownLinkHref('https://e.com/<x>')).toBeNull();
  });

  it('rejects embedded userinfo (open-redirect / spoof guard)', () => {
    expect(safeMarkdownLinkHref('https://good.com@evil.com')).toBeNull();
    expect(safeMarkdownLinkHref('https://user:pass@evil.com')).toBeNull();
  });

  it('rejects non-strings and empties', () => {
    expect(safeMarkdownLinkHref(null)).toBeNull();
    expect(safeMarkdownLinkHref(undefined)).toBeNull();
    expect(safeMarkdownLinkHref(42)).toBeNull();
    expect(safeMarkdownLinkHref('')).toBeNull();
  });

  it('rejects other non-web schemes, bare hosts and relative paths', () => {
    expect(safeMarkdownLinkHref('ftp://files.example.com/x')).toBeNull();
    expect(safeMarkdownLinkHref('blob:https://e.com/uuid')).toBeNull();
    expect(safeMarkdownLinkHref('/relative/path')).toBeNull();
    expect(safeMarkdownLinkHref('example.com')).toBeNull();
    expect(safeMarkdownLinkHref('//protocol-relative.example')).toBeNull();
  });
});

describe('parseMarkdown — blocks', () => {
  it('returns [] for empty / non-string input', () => {
    expect(parseMarkdown('')).toEqual([]);
    // @ts-expect-error guarding runtime misuse
    expect(parseMarkdown(null)).toEqual([]);
    expect(parseMarkdown('   \n  \n')).toEqual([]);
  });

  it('parses ATX headings with their level', () => {
    const blocks = parseMarkdown('## Title here\n\n###### Deep');
    expect(blocks[0]).toMatchObject({ type: 'heading', level: 2 });
    expect(plainInline((blocks[0] as Extract<BlockNode, { type: 'heading' }>).children)).toBe(
      'Title here',
    );
    expect(blocks[1]).toMatchObject({ type: 'heading', level: 6 });
  });

  it('does not treat 7+ hashes as a heading', () => {
    const blocks = parseMarkdown('####### not a heading');
    expect(blocks[0].type).toBe('paragraph');
  });

  it('groups blank-line-separated paragraphs and turns single newlines into <br>', () => {
    const blocks = parseMarkdown('line one\nline two\n\nsecond para');
    expect(blocks).toHaveLength(2);
    expect(blocks[0].type).toBe('paragraph');
    const p0 = blocks[0] as Extract<BlockNode, { type: 'paragraph' }>;
    expect(p0.children.some((n) => n.type === 'br')).toBe(true);
    expect(plainInline(p0.children)).toBe('line one\nline two');
    expect(blocks[1].type).toBe('paragraph');
  });

  it('parses unordered lists with -, * and + markers', () => {
    for (const m of ['-', '*', '+']) {
      const blocks = parseMarkdown(`${m} first\n${m} second`);
      expect(blocks[0]).toMatchObject({ type: 'list', ordered: false });
      const list = blocks[0] as Extract<BlockNode, { type: 'list' }>;
      expect(list.items).toHaveLength(2);
      expect(plainInline(list.items[0])).toBe('first');
    }
  });

  it('parses ordered lists', () => {
    const blocks = parseMarkdown('1. alpha\n2. beta');
    expect(blocks[0]).toMatchObject({ type: 'list', ordered: true });
    expect((blocks[0] as Extract<BlockNode, { type: 'list' }>).items).toHaveLength(2);
  });

  it('ends a list at a blank line before a following paragraph', () => {
    const blocks = parseMarkdown('- a\n- b\n\nafter');
    expect(blocks[0].type).toBe('list');
    expect(blocks[1]).toMatchObject({ type: 'paragraph' });
    expect(plainInline((blocks[1] as Extract<BlockNode, { type: 'paragraph' }>).children)).toBe(
      'after',
    );
  });

  it('parses fenced code blocks verbatim (no inline parsing inside)', () => {
    const blocks = parseMarkdown('```\nconst x = **not bold**;\n```');
    expect(blocks[0]).toEqual({ type: 'codeblock', value: 'const x = **not bold**;' });
  });

  it('parses a fence with a language tag and keeps content literal', () => {
    const blocks = parseMarkdown('```hcl\nservice_account = "x"\n```');
    expect(blocks[0]).toEqual({ type: 'codeblock', value: 'service_account = "x"' });
  });

  it('treats an unclosed fence as a code block running to the end', () => {
    const blocks = parseMarkdown('```\nstill code\nmore');
    expect(blocks[0]).toEqual({ type: 'codeblock', value: 'still code\nmore' });
  });

  it('parses a GFM table (header + delimiter + rows)', () => {
    const md = '| Var | Live |\n|---|---|\n| `A` | 1 |\n| B | 2 |';
    const blocks = parseMarkdown(md);
    expect(blocks[0].type).toBe('table');
    const t = blocks[0] as Extract<BlockNode, { type: 'table' }>;
    expect(t.header.map(plainInline)).toEqual(['Var', 'Live']);
    expect(t.rows).toHaveLength(2);
    expect(t.rows[0].map(plainInline)).toEqual(['A', '1']);
  });

  it('does NOT treat a single piped line (no delimiter row) as a table', () => {
    const blocks = parseMarkdown('a | b | c is just prose');
    expect(blocks[0].type).toBe('paragraph');
  });

  it('splits a marker-type change into separate lists (no latching)', () => {
    const blocks = parseMarkdown('- a\n- b\n1. c\n2. d');
    expect(blocks.map((b) => b.type)).toEqual(['list', 'list']);
    expect(blocks[0]).toMatchObject({ type: 'list', ordered: false });
    expect(blocks[1]).toMatchObject({ type: 'list', ordered: true });
  });

  it('drops an empty heading (just "# ") rather than emitting a blank heading', () => {
    const blocks = parseMarkdown('# \n\nreal body');
    // no empty heading block; the body paragraph survives
    expect(blocks.every((b) => b.type !== 'heading')).toBe(true);
    expect(blocks[0]).toMatchObject({ type: 'paragraph' });
  });
});

describe('parseMarkdown — inline', () => {
  function para(md: string): InlineNode[] {
    const b = parseMarkdown(md);
    return (b[0] as Extract<BlockNode, { type: 'paragraph' }>).children;
  }

  it('parses **strong** and __strong__', () => {
    expect(para('a **bold** b').find((n) => n.type === 'strong')).toBeTruthy();
    expect(para('a __bold__ b').find((n) => n.type === 'strong')).toBeTruthy();
  });

  it('parses *em* and _em_', () => {
    expect(para('a *it* b').find((n) => n.type === 'em')).toBeTruthy();
    expect(para('a _it_ b').find((n) => n.type === 'em')).toBeTruthy();
  });

  it('parses inline `code`, including multi-backtick runs', () => {
    const single = para('use `service_account` here').find((n) => n.type === 'code');
    expect(single).toMatchObject({ type: 'code', value: 'service_account' });
    const dbl = para('use ``a`b`` here').find((n) => n.type === 'code');
    expect(dbl).toMatchObject({ type: 'code', value: 'a`b' });
  });

  it('renders ***x*** as bold+italic without leaking asterisks', () => {
    const nodes = para('say ***x*** now');
    // em wrapping strong (or strong wrapping em); the visible text has no stray *
    const hasStrong = JSON.stringify(nodes).includes('"strong"');
    const hasEm = JSON.stringify(nodes).includes('"em"');
    expect(hasStrong && hasEm).toBe(true);
    expect(plainInline(nodes)).toBe('say x now');
  });

  it('does not leak asterisks for an asymmetric **x* run', () => {
    // degrades cleanly: a literal * plus an <em>x</em>, never "*x*" via a broken strong
    const nodes = para('**x*');
    expect(nodes.some((n) => n.type === 'strong')).toBe(false);
    expect(plainInline(nodes)).toBe('*x');
  });

  it('does NOT emphasise intraword DOUBLE underscores (snake/dunder identifiers)', () => {
    for (const id of ['a__b__c', 'MY__CONST__VALUE', 'path__double__under']) {
      const nodes = para(`x ${id} y`);
      expect(nodes.every((n) => n.type !== 'em' && n.type !== 'strong')).toBe(true);
      expect(plainInline(nodes)).toBe(`x ${id} y`);
    }
  });

  it('still treats word-boundary __bold__ as bold (faithful to GitHub)', () => {
    const nodes = para('the __init__ method');
    expect(nodes.find((n) => n.type === 'strong')).toMatchObject({ type: 'strong' });
  });

  it('renders an empty-text link using the href as its visible text (no empty anchor)', () => {
    const link = para('[](https://example.com/x)').find((n) => n.type === 'link') as Extract<
      InlineNode,
      { type: 'link' }
    >;
    expect(link).toBeTruthy();
    expect(link.href).toBe('https://example.com/x');
    expect(plainInline(link.children).length).toBeGreaterThan(0);
  });

  it('drops an angle-wrapped dangerous link destination', () => {
    const nodes = para('[evil](<javascript:alert(1)>)');
    expect(nodes.every((n) => n.type !== 'link')).toBe(true);
    expect(plainInline(nodes)).not.toContain('javascript:');
  });

  it('does not emphasise whitespace-flanked asterisks (e.g. multiplication)', () => {
    // (none of these start the line, so `* ` isn't read as a bullet marker)
    for (const s of ['2 * 3 * 4', 'a * b', 'see * foo* here', '**x **']) {
      const nodes = para(s);
      expect(nodes.every((n) => n.type !== 'em' && n.type !== 'strong')).toBe(true);
      expect(plainInline(nodes)).toBe(s);
    }
  });

  it('does not close emphasis on a * inside an inner code span', () => {
    const nodes = para('**use `*.tf` globs**');
    const strong = nodes.find((n) => n.type === 'strong') as Extract<
      InlineNode,
      { type: 'strong' }
    >;
    expect(strong).toBeTruthy();
    // the code span (with its literal *) survives intact inside the bold
    const code = strong.children.find((n) => n.type === 'code');
    expect(code).toMatchObject({ type: 'code', value: '*.tf' });
    expect(plainInline(nodes)).toBe('use *.tf globs');
  });

  it('does not close emphasis on an escaped \\* delimiter', () => {
    const nodes = para('*a \\* b*');
    expect(nodes.find((n) => n.type === 'em')).toBeTruthy();
    expect(plainInline(nodes)).toBe('a * b');
  });

  it('does not close emphasis on a delimiter inside an inner link label', () => {
    const nodes = para('*a [x*y](https://e.com) b*');
    const em = nodes.find((n) => n.type === 'em') as Extract<InlineNode, { type: 'em' }>;
    expect(em).toBeTruthy();
    const link = em.children.find((n) => n.type === 'link') as Extract<
      InlineNode,
      { type: 'link' }
    >;
    expect(link?.href).toBe('https://e.com/');
    expect(plainInline(link.children)).toBe('x*y');
    expect(plainInline(nodes)).toBe('a x*y b');
  });

  it('does not throw and degrades on pathologically deep emphasis nesting', () => {
    const deep = '*'.repeat(40) + 'x' + '*'.repeat(40);
    expect(() => parseMarkdown(deep)).not.toThrow();
    const nodes = (parseMarkdown(deep)[0] as Extract<BlockNode, { type: 'paragraph' }>).children;
    expect(plainInline(nodes)).toContain('x');
  });

  it('closes a code span on a same-length run, skipping a longer run inside', () => {
    // `` opens a 2-tick span; the 4-tick run inside is literal; the 2-tick closes.
    const code = para('`` text ```` more `` end').find((n) => n.type === 'code');
    expect(code).toMatchObject({ type: 'code', value: 'text ```` more' });
  });

  it('does NOT italicise intraword underscores (protects identifiers)', () => {
    // service_account in PROSE must stay literal text, not <em>account</em>.
    const nodes = para('the service_account field and ignore_changes too');
    expect(nodes.every((n) => n.type !== 'em')).toBe(true);
    expect(plainInline(nodes)).toBe('the service_account field and ignore_changes too');
  });

  it('renders a valid [text](https://…) as a link node', () => {
    const link = para('see [the PR](https://github.com/a/b/pull/3) now').find(
      (n) => n.type === 'link',
    ) as Extract<InlineNode, { type: 'link' }>;
    expect(link).toBeTruthy();
    expect(link.href).toBe('https://github.com/a/b/pull/3');
    expect(plainInline(link.children)).toBe('the PR');
  });

  it('degrades a [text](javascript:…) link to plain text (no anchor, no scheme leak)', () => {
    const nodes = para('click [here](javascript:alert(1)) please');
    expect(nodes.every((n) => n.type !== 'link')).toBe(true);
    // The visible link text survives; the dangerous href does not appear.
    const text = plainInline(nodes);
    expect(text).toContain('here');
    expect(text).not.toContain('javascript:');
  });

  it('honours backslash escapes of markdown punctuation', () => {
    const nodes = para('literal \\*stars\\* and \\`ticks\\`');
    expect(nodes.every((n) => n.type === 'text' || n.type === 'br')).toBe(true);
    expect(plainInline(nodes)).toBe('literal *stars* and `ticks`');
  });

  it('treats an unclosed ** as literal text', () => {
    const nodes = para('a **dangling bold');
    expect(nodes.every((n) => n.type !== 'strong')).toBe(true);
    expect(plainInline(nodes)).toBe('a **dangling bold');
  });

  it('treats an unclosed `code` backtick as literal text', () => {
    const nodes = para('a `dangling code');
    expect(nodes.every((n) => n.type !== 'code')).toBe(true);
    expect(plainInline(nodes)).toContain('`dangling code');
  });

  it('does NOT decode HTML entities or create elements (kept as literal text)', () => {
    const nodes = para('<img src=x onerror=alert(1)> &lt;b&gt;');
    expect(plainInline(nodes)).toBe('<img src=x onerror=alert(1)> &lt;b&gt;');
  });
});

describe('parseMarkdown — nested links + table-cell escapes', () => {
  function para(md: string): InlineNode[] {
    const b = parseMarkdown(md);
    return (b[0] as Extract<BlockNode, { type: 'paragraph' }>).children;
  }
  function collectLinks(nodes: InlineNode[]): Extract<InlineNode, { type: 'link' }>[] {
    const out: Extract<InlineNode, { type: 'link' }>[] = [];
    for (const n of nodes) {
      if (n.type === 'link') {
        out.push(n);
        out.push(...collectLinks(n.children));
      } else if (n.type === 'strong' || n.type === 'em') {
        out.push(...collectLinks(n.children));
      }
    }
    return out;
  }

  it('never nests an anchor inside an anchor (link labels carry no link node)', () => {
    const nodes = para('[[inner](https://a.example)](https://b.example)');
    const links = collectLinks(nodes);
    expect(links).toHaveLength(1);
    expect(links[0].href).toBe('https://b.example/');
    // the inner link is flattened to literal text inside the outer label
    expect(links[0].children.some((n) => n.type === 'link')).toBe(false);
  });

  it('an unsafe outer link does not leak an inner anchor from its label', () => {
    const nodes = para('click [[ok](https://safe.example)](javascript:bad)');
    expect(collectLinks(nodes)).toHaveLength(0);
    expect(plainInline(nodes)).toContain('ok');
    expect(plainInline(nodes)).not.toContain('javascript:bad');
  });

  it('treats an escaped pipe in a table cell as a literal pipe, not a cell break', () => {
    const md = '| col |\n|---|\n| a \\| b |';
    const t = parseMarkdown(md)[0] as Extract<BlockNode, { type: 'table' }>;
    expect(t.rows[0]).toHaveLength(1);
    expect(plainInline(t.rows[0][0])).toBe('a | b');
  });
});

describe('parseMarkdown — golden (PR #32 body)', () => {
  const BODY = [
    '## C5g positive smoke — first real in-place UPDATE through the gated pipeline',
    '',
    'Repoints `payment-demo` to run as the dedicated minimal **`payment-demo-runtime@`** service account (the C5f hardening SA) instead of the default compute SA `1079423440495-compute@`.',
    '',
    '- **Resource:** `google_cloud_run_v2_service.payment_demo` — adds `template.service_account` (in-place UPDATE).',
    '- **Why:** completes the C5f isolation.',
    '- **Gate path:** C2 plan-builder → operator opens CF-Access `/iac-approvals` → apply → merge.',
    '',
    'Generated as the C5g end-to-end test. 🤖',
  ].join('\n');

  it('parses into heading + paragraph + 3-item list + paragraph', () => {
    const blocks = parseMarkdown(BODY);
    expect(blocks.map((b) => b.type)).toEqual([
      'heading',
      'paragraph',
      'list',
      'paragraph',
    ]);
    const list = blocks[2] as Extract<BlockNode, { type: 'list' }>;
    expect(list.ordered).toBe(false);
    expect(list.items).toHaveLength(3);
  });

  it('preserves the 🤖 emoji as text and keeps identifiers intact', () => {
    const blocks = parseMarkdown(BODY);
    const last = blocks[3] as Extract<BlockNode, { type: 'paragraph' }>;
    expect(plainInline(last.children)).toContain('🤖');
    // the dotted, underscored identifier survives intact inside the list item's
    // `code` span (the underscore must NOT have been treated as emphasis).
    const list = blocks[2] as Extract<BlockNode, { type: 'list' }>;
    const codeNode = list.items[0].find((n) => n.type === 'code');
    expect(codeNode).toMatchObject({ type: 'code', value: 'google_cloud_run_v2_service.payment_demo' });
    expect(plainInline(list.items[0])).toContain('template.service_account');
  });
});
