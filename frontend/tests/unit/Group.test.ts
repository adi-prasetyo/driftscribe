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
