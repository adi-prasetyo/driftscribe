// frontend/tests/unit/DemoNoticeBell.test.ts
import { describe, it, expect, afterEach, beforeEach } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import DemoNoticeBell, {
  DEMO_NOTICE_DISMISSED_KEY,
  shouldAutoOpenNotice,
} from '../../src/components/DemoNoticeBell.svelte';
import { HEADER_POPOVER_EVENT } from '../../src/lib/headerPopover';

afterEach(cleanup);
beforeEach(() => window.localStorage.clear());

describe('shouldAutoOpenNotice', () => {
  it('opens on a plain first visit', () => {
    expect(shouldAutoOpenNotice('', false)).toBe(true);
  });
  it('never opens once dismissed', () => {
    expect(shouldAutoOpenNotice('', true)).toBe(false);
  });
  it('yields to errand deep links', () => {
    expect(shouldAutoOpenNotice('?ask_pr=12', false)).toBe(false);
    expect(shouldAutoOpenNotice('?preview_pr=12', false)).toBe(false);
    expect(shouldAutoOpenNotice('?reasoning=eba334f9211d46cabc79e50ed200a5a1', false)).toBe(false);
  });
});

describe('DemoNoticeBell', () => {
  it('auto-opens with the load-bearing copy and shows the unread badge', async () => {
    const { findByTestId, getByTestId } = render(DemoNoticeBell, { props: { search: '' } });
    const popover = await findByTestId('demo-notice-popover');
    expect(popover.textContent).toContain('This is a live sandbox.');
    expect(popover.textContent).toContain('heals itself every couple of hours');
    expect(popover.textContent).toContain('upgrade demo resets within a couple of hours of being fixed');
    expect(getByTestId('demo-notice-badge')).toBeTruthy();
  });

  it('Got it closes, persists the flag, and clears the badge; the bell stays', async () => {
    const { findByTestId, getByTestId, queryByTestId } = render(DemoNoticeBell, {
      props: { search: '' },
    });
    await findByTestId('demo-notice-popover');
    await fireEvent.click(getByTestId('demo-notice-dismiss'));
    await waitFor(() => expect(queryByTestId('demo-notice-popover')).toBeNull());
    expect(queryByTestId('demo-notice-badge')).toBeNull();
    expect(getByTestId('demo-notice-bell')).toBeTruthy();
    expect(window.localStorage.getItem(DEMO_NOTICE_DISMISSED_KEY)).toBe('1');
  });

  it('click-anywhere-outside dismisses and persists', async () => {
    const { findByTestId, queryByTestId } = render(DemoNoticeBell, { props: { search: '' } });
    await findByTestId('demo-notice-popover');
    await fireEvent.pointerDown(document.body);
    await waitFor(() => expect(queryByTestId('demo-notice-popover')).toBeNull());
    expect(window.localStorage.getItem(DEMO_NOTICE_DISMISSED_KEY)).toBe('1');
  });

  it('Escape dismisses and persists', async () => {
    const { findByTestId, queryByTestId } = render(DemoNoticeBell, { props: { search: '' } });
    await findByTestId('demo-notice-popover');
    await fireEvent.keyDown(window, { key: 'Escape' });
    await waitFor(() => expect(queryByTestId('demo-notice-popover')).toBeNull());
    expect(window.localStorage.getItem(DEMO_NOTICE_DISMISSED_KEY)).toBe('1');
  });

  it('closing a boot auto-open does not steal focus to the bell', async () => {
    // Codex blocking finding #1: only user-opened closes may return focus.
    const outside = document.createElement('button');
    document.body.appendChild(outside);
    try {
      const { findByTestId, getByTestId, queryByTestId } = render(DemoNoticeBell, {
        props: { search: '' },
      });
      await findByTestId('demo-notice-popover');
      outside.focus(); // where the user "is" — mount must not have moved it, and close must not either
      await fireEvent.keyDown(window, { key: 'Escape' });
      await waitFor(() => expect(queryByTestId('demo-notice-popover')).toBeNull());
      expect(document.activeElement).toBe(outside);
      expect(document.activeElement).not.toBe(getByTestId('demo-notice-bell'));
    } finally {
      outside.remove();
    }
  });

  it('a foreign header popover opening closes the notice (bus mutual exclusion)', async () => {
    const { findByTestId, queryByTestId } = render(DemoNoticeBell, { props: { search: '' } });
    await findByTestId('demo-notice-popover'); // listener registered + popover open
    window.dispatchEvent(
      new CustomEvent(HEADER_POPOVER_EVENT, { detail: { id: 'autonomy' } }),
    );
    await waitFor(() => expect(queryByTestId('demo-notice-popover')).toBeNull());
    expect(window.localStorage.getItem(DEMO_NOTICE_DISMISSED_KEY)).toBe('1');
  });

  it('does not auto-open when dismissed; bell reopens it and close returns focus to the bell', async () => {
    window.localStorage.setItem(DEMO_NOTICE_DISMISSED_KEY, '1');
    const { findByTestId, getByTestId, queryByTestId } = render(DemoNoticeBell, {
      props: { search: '' },
    });
    await tick(); // let onMount run — it must NOT open
    expect(queryByTestId('demo-notice-popover')).toBeNull();
    expect(queryByTestId('demo-notice-badge')).toBeNull();
    await fireEvent.click(getByTestId('demo-notice-bell'));
    const popover = await findByTestId('demo-notice-popover');
    expect(popover.textContent).toContain('This is a live sandbox.');
    // user-opened close DOES return focus to the bell
    await fireEvent.keyDown(window, { key: 'Escape' });
    await waitFor(() => expect(queryByTestId('demo-notice-popover')).toBeNull());
    expect(document.activeElement).toBe(getByTestId('demo-notice-bell'));
  });

  it('errand deep link suppresses auto-open but keeps the badge', async () => {
    const { getByTestId, queryByTestId } = render(DemoNoticeBell, {
      props: { search: '?ask_pr=12' },
    });
    await tick(); // let onMount run — it must NOT open
    expect(queryByTestId('demo-notice-popover')).toBeNull();
    expect(getByTestId('demo-notice-badge')).toBeTruthy();
    expect(window.localStorage.getItem(DEMO_NOTICE_DISMISSED_KEY)).toBeNull();
  });

  it('bell toggle closes an open popover and persists', async () => {
    const { findByTestId, getByTestId, queryByTestId } = render(DemoNoticeBell, {
      props: { search: '' },
    });
    await findByTestId('demo-notice-popover');
    await fireEvent.click(getByTestId('demo-notice-bell'));
    await waitFor(() => expect(queryByTestId('demo-notice-popover')).toBeNull());
    expect(window.localStorage.getItem(DEMO_NOTICE_DISMISSED_KEY)).toBe('1');
  });
});

// ds-odt — the popover clamps to the viewport's left edge at desktop widths,
// which on the chat view is where the conversations rail sits. A notice the
// visitor did not ask for must not intercept the click meant for what it
// happened to land on.
describe('DemoNoticeBell — a passive notice does not take the pointer (ds-odt)', () => {
  it('a boot auto-open is pointer-transparent', () => {
    const { getByTestId } = render(DemoNoticeBell, { props: { search: '' } });
    const popover = getByTestId('demo-notice-popover');
    expect(popover.getAttribute('data-passive')).toBe('true');
    expect(popover.className).toContain('demo-bell__popover--passive');
  });

  it('a bell-CLICK open is fully interactive', () => {
    // The visitor summoned it; a popover that ignores the pointer it was
    // opened with would be its own bug.
    const { getByTestId } = render(DemoNoticeBell, { props: { search: '' } });
    fireEvent.click(getByTestId('demo-notice-bell')); // closes the auto-open
    fireEvent.click(getByTestId('demo-notice-bell')); // reopens, user-initiated
    const popover = getByTestId('demo-notice-popover');
    expect(popover.getAttribute('data-passive')).toBeNull();
    expect(popover.className).not.toContain('demo-bell__popover--passive');
  });

  it('"Got it" still works while passive', () => {
    // The one control the notice owns opts back into pointer events, so it is
    // a real button rather than a decoration clicks fall through.
    const { getByTestId, queryByTestId } = render(DemoNoticeBell, { props: { search: '' } });
    expect(getByTestId('demo-notice-popover').getAttribute('data-passive')).toBe('true');
    fireEvent.click(getByTestId('demo-notice-dismiss'));
    expect(queryByTestId('demo-notice-popover')).toBeNull();
  });
});
