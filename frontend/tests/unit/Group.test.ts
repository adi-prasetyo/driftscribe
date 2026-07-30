import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import Group from '../../src/components/Group.svelte';

// Group is the generic reasoning-group <details>. These pin the opt-in `hint`
// affordance: when set, it renders a help-circle icon next to the title whose
// title + aria-label carry the explanatory copy (used by Timeline only for the
// coordinator group, to self-document the `global`-region latency). When unset,
// nothing extra renders — the other groups (tools/mcp) stay clean.

afterEach(cleanup);

describe('Group hint affordance', () => {
  const HINT = 'inference is routed to the global region';

  it('renders a help-circle hint icon with title + aria-label + role when hint is set', () => {
    const { container } = render(Group, {
      props: { key: 'coordinator', title: 'Coordinator reasoning', hint: HINT },
    });
    const hint = container.querySelector('.group__hint');
    expect(hint).not.toBeNull();
    expect(hint?.getAttribute('title')).toBe(HINT);
    expect(hint?.getAttribute('aria-label')).toBe(HINT);
    expect(hint?.getAttribute('role')).toBe('img');
    // The help-circle icon renders an inline <svg> (with a circle) inside.
    const svg = hint?.querySelector('svg');
    expect(svg).not.toBeNull();
    expect(svg?.querySelector('circle')).not.toBeNull();
  });

  it('renders no hint affordance when hint is absent', () => {
    const { container } = render(Group, {
      props: { key: 'tools', title: 'Tools & workers' },
    });
    expect(container.querySelector('.group__hint')).toBeNull();
  });
});

// ds-7ag.5 — the chat page carried six boxed cards of equal weight, so nothing
// on it read as the substance. `quiet` demotes a metadata drawer to a plain
// disclosure: no box when closed, a muted label row, and its content on a well
// when open. The `card` default is unchanged, so every existing consumer keeps
// its look.
describe('Group quiet variant', () => {
  it('quiet renders the demoted chrome and keeps the e2e DOM contract', () => {
    const { container } = render(Group, {
      props: { key: 'tools', title: 'Tools & workers', variant: 'quiet' },
    });
    const details = container.querySelector('details');
    expect(details?.classList.contains('group--quiet')).toBe(true);
    // The Playwright e2e sets `.open = true` on #group-tools and asserts
    // [data-group="tools"] becomes visible — both variants must satisfy it.
    expect(details?.id).toBe('group-tools');
    expect(container.querySelector('[data-group="tools"]')).not.toBeNull();
  });

  it('card is the default, and it is untouched', () => {
    const { container } = render(Group, {
      props: { key: 'coordinator', title: 'Coordinator reasoning' },
    });
    const details = container.querySelector('details');
    expect(details?.classList.contains('group')).toBe(true);
    expect(details?.classList.contains('group--quiet')).toBe(false);
  });
});

// Plan Task 11 — an empty state that says what comes next. "No coordinator
// reasoning yet." reports an absence; on the page's primary group that is a
// wasted opportunity to tell the operator what produces one.
describe('Group empty state', () => {
  it('renders emptyText verbatim when given', () => {
    const GUIDANCE = "Send a question and the coordinator's reasoning will stream here.";
    const { getByText } = render(Group, {
      props: {
        key: 'coordinator',
        title: 'Coordinator reasoning',
        empty: true,
        emptyText: GUIDANCE,
      },
    });
    expect(getByText(GUIDANCE)).toBeTruthy();
  });

  it('falls back to the generic absence line when emptyText is absent', () => {
    const { container } = render(Group, {
      props: { key: 'tools', title: 'Tools & workers', empty: true },
    });
    expect(container.querySelector('.group__empty')?.textContent).toBe('No tools & workers yet.');
  });
});
