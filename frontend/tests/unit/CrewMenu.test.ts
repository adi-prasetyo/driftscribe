import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import CrewMenu from '../../src/components/CrewMenu.svelte';
import { setLocale } from '../../src/lib/i18n';

// ── ds-uyo: the crew stops being a hidden setting ───────────────────────────
//
// The incident: Adopt on an unmanaged bucket armed Provision, the operator
// edited the prefill into a drift question, and Provision answered — correctly,
// since it holds `drift_read_live_env` by design. Nothing on screen had said
// Provision was the crew. The lock was right; the silence was the defect.
//
// So the interesting assertions here are not "the menu opens". They are the
// ones that keep the control from becoming a NEW way to lose something: it goes
// inert while a stream is live, it says what a click will cost before the
// click, and every description belongs to the row it describes.

afterEach(() => {
  cleanup();
  setLocale('en');
});

const noop = () => {};

/** Render with the menu already open, which is where most of this lives. */
async function openMenu(props: Record<string, unknown> = {}) {
  const view = render(CrewMenu, {
    props: { value: 'explore', onSelect: noop, ...props },
  });
  await fireEvent.click(view.getByTestId('crew-menu-trigger'));
  await waitFor(() => expect(view.getByTestId('crew-menu-popup')).toBeTruthy());
  return view;
}

const option = (v: string) => document.querySelector(`[data-testid="crew-menu-option-${v}"]`)!;

describe('CrewMenu — the trigger reports the crew', () => {
  it('shows the crew name at rest, with no menu open', () => {
    const { getByTestId, queryByTestId } = render(CrewMenu, {
      props: { value: 'provision', onSelect: noop },
    });
    expect(getByTestId('crew-menu-trigger').textContent).toContain('Provision');
    expect(queryByTestId('crew-menu-popup')).toBeNull();
  });

  it('carries the listbox contract on the trigger', async () => {
    const { getByTestId } = render(CrewMenu, {
      props: { value: 'explore', onSelect: noop },
    });
    const trigger = getByTestId('crew-menu-trigger');
    expect(trigger.getAttribute('aria-haspopup')).toBe('listbox');
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    // The visible text is the crew name alone, so the accessible name has to
    // supply the other half — that this control CHANGES the crew.
    const label = trigger.getAttribute('aria-label') ?? '';
    expect(label).toContain('Explore');
    expect(label.toLowerCase()).toContain('change');

    await fireEvent.click(trigger);
    await waitFor(() => expect(trigger.getAttribute('aria-expanded')).toBe('true'));
    // aria-controls has to name a node that is actually there, or it is a
    // pointer to nothing.
    const controls = trigger.getAttribute('aria-controls')!;
    expect(document.getElementById(controls)?.getAttribute('role')).toBe('listbox');
  });
});

describe('CrewMenu — the open list', () => {
  it('lists all four crews and marks the current one selected', async () => {
    await openMenu({ value: 'drift' });
    for (const v of ['drift', 'upgrade', 'provision', 'explore']) {
      expect(option(v)).toBeTruthy();
    }
    // Anchor is in the list deliberately: it is the only crew that can rule on
    // drift and act either way.
    expect(option('drift').getAttribute('aria-selected')).toBe('true');
    expect(option('explore').getAttribute('aria-selected')).toBe('false');
  });

  it('opens focus on the SELECTED option, not the first', async () => {
    // `drift` is first in catalog order, so opening on Provision is what tells
    // "the selected one" apart from "the first one".
    await openMenu({ value: 'provision' });
    await waitFor(() => expect(document.activeElement).toBe(option('provision')));
  });

  it('gives every option its OWN description node, all present at once', async () => {
    // Not one shared panel whose text swaps on focus: a screen reader can
    // announce the lifecycle line belonging to the previously focused row. The
    // proof is that all four resolve simultaneously to four different texts.
    await openMenu();
    const texts = new Set<string>();
    for (const v of ['drift', 'upgrade', 'provision', 'explore']) {
      const id = option(v).getAttribute('aria-describedby')!;
      const node = document.getElementById(id);
      expect(node, `option ${v} describes itself with a node that is not there`).not.toBeNull();
      expect(node!.textContent!.trim().length).toBeGreaterThan(0);
      texts.add(node!.textContent!.trim());
    }
    expect(texts.size).toBe(4);
  });

  it('renders the rows from the shared locale catalog, in the active locale', async () => {
    // Descriptor and lifecycle already exist in both languages (`shared.crew.*`),
    // so no row string is authored here. This is what proves it: switching the
    // locale changes what renders, which hardcoded English could not do.
    const { getByTestId, unmount } = await openMenu({ value: 'explore' });
    expect(getByTestId('crew-menu-popup').textContent).toContain('read-only');
    unmount();

    setLocale('ja');
    const ja = await openMenu({ value: 'explore' });
    expect(ja.getByTestId('crew-menu-popup').textContent).toContain('読み取り専用');
  });
});

describe('CrewMenu — keyboard', () => {
  it('moves with the arrows and wraps at both ends', async () => {
    await openMenu({ value: 'drift' }); // first in catalog order
    await waitFor(() => expect(document.activeElement).toBe(option('drift')));

    await fireEvent.keyDown(option('drift'), { key: 'ArrowDown' });
    expect(document.activeElement).toBe(option('upgrade'));

    // Backwards off the front wraps to the end, so no row is a dead end.
    await fireEvent.keyDown(option('upgrade'), { key: 'ArrowUp' });
    await fireEvent.keyDown(option('drift'), { key: 'ArrowUp' });
    expect(document.activeElement).toBe(option('explore'));
  });

  it('jumps with Home and End', async () => {
    await openMenu({ value: 'provision' });
    await fireEvent.keyDown(option('provision'), { key: 'End' });
    expect(document.activeElement).toBe(option('explore'));
    await fireEvent.keyDown(option('explore'), { key: 'Home' });
    expect(document.activeElement).toBe(option('drift'));
  });

  it.each([['Enter'], [' ']])('chooses with %s', async (key) => {
    // A listbox option is not a button, so the browser synthesises no click for
    // either key — both have to be handled, and Space is the one that gets
    // forgotten.
    const onSelect = vi.fn();
    await openMenu({ value: 'explore', onSelect });
    await fireEvent.keyDown(option('provision'), { key });
    expect(onSelect).toHaveBeenCalledWith('provision');
  });

  it('closes on Escape and puts focus back on the trigger', async () => {
    const { getByTestId, queryByTestId } = await openMenu({ value: 'explore' });
    await fireEvent.keyDown(option('explore'), { key: 'Escape' });
    await waitFor(() => expect(queryByTestId('crew-menu-popup')).toBeNull());
    expect(document.activeElement).toBe(getByTestId('crew-menu-trigger'));
  });

  it('moves the tab stop with focus, not with the selection', async () => {
    // jsdom has no tab order, so this cannot press Tab — the browser half is in
    // crew-menu.smoke.ts, and that is where the defect was actually found. What
    // this pins is the mechanism underneath it: exactly one option is tabbable,
    // and it is the FOCUSED one. Pinned to the selection instead, Tab finds the
    // selected option still tabbable further down the list, focus never leaves
    // the menu, and the operator is thrown back to the row they just left.
    await openMenu({ value: 'explore' }); // last in catalog order
    const tabbable = () =>
      ['drift', 'upgrade', 'provision', 'explore'].filter(
        (v) => option(v).getAttribute('tabindex') === '0',
      );
    await waitFor(() => expect(tabbable()).toEqual(['explore']));

    await fireEvent.keyDown(option('explore'), { key: 'ArrowDown' }); // wraps
    await waitFor(() => expect(tabbable()).toEqual(['drift']));
    expect(document.activeElement).toBe(option('drift'));
  });

  it('leaves the tab stop alone when the POINTER wanders', async () => {
    // `active` (which line is showing) follows hover; the tab stop must not, or
    // a passing mouse relocates a keyboard operator's exit.
    await openMenu({ value: 'explore' });
    await fireEvent.mouseEnter(option('upgrade'));
    expect(option('explore').getAttribute('tabindex')).toBe('0');
    expect(option('upgrade').getAttribute('tabindex')).toBe('-1');
  });

  it('opens from the trigger with ArrowDown', async () => {
    const { getByTestId } = render(CrewMenu, {
      props: { value: 'upgrade', onSelect: noop },
    });
    await fireEvent.keyDown(getByTestId('crew-menu-trigger'), { key: 'ArrowDown' });
    await waitFor(() => expect(getByTestId('crew-menu-popup')).toBeTruthy());
    await waitFor(() => expect(document.activeElement).toBe(option('upgrade')));
  });
});

describe('CrewMenu — what a click costs, said before the click', () => {
  it('marks the current row and offers the others a new chat, when there is something to clear', async () => {
    await openMenu({ value: 'explore', occupied: true });
    expect(option('explore').textContent).toContain('current');
    // Never "switch" or "hand over": a handoff continues THIS thread with its
    // context and this does not.
    for (const v of ['drift', 'upgrade', 'provision']) {
      const text = option(v).textContent ?? '';
      expect(text).toContain('starts new chat');
      expect(text.toLowerCase()).not.toContain('switch');
      expect(text.toLowerCase()).not.toContain('hand over');
    }
  });

  it('says nothing about a new chat on a screen with nothing on it', async () => {
    // `occupied`, not "a thread is open". A paused / one-shot / failed turn is
    // rendered with no conversation behind it and newChat() clears that too —
    // and unlike a persisted thread it cannot be reopened from the rail
    // afterwards, so it is the case that most needed the warning. App passes its
    // own `chatOccupied`; the App suite covers the wiring.
    await openMenu({ value: 'explore', occupied: false });
    expect(option('drift').textContent).not.toContain('starts new chat');
    expect(option('explore').textContent).toContain('current');
  });

  it('fires onSelect for the current crew too, leaving the no-op to the caller', async () => {
    // Deliberate: only the caller can start a new chat, so only the caller can
    // decide that this one should not. Splitting that decision across two files
    // would leave it owned by neither.
    const onSelect = vi.fn();
    await openMenu({ value: 'explore', onSelect });
    await fireEvent.click(option('explore'));
    expect(onSelect).toHaveBeenCalledWith('explore');
  });
});

describe('CrewMenu — inert means inert', () => {
  it('disables the trigger and refuses to open', async () => {
    const { getByTestId, queryByTestId } = render(CrewMenu, {
      props: { value: 'explore', onSelect: noop, disabled: true },
    });
    expect((getByTestId('crew-menu-trigger') as HTMLButtonElement).disabled).toBe(true);
    await fireEvent.click(getByTestId('crew-menu-trigger'));
    expect(queryByTestId('crew-menu-popup')).toBeNull();
  });

  it('closes an ALREADY OPEN menu when it goes disabled', async () => {
    // The one that matters. A selection runs newChat(), which cancels an
    // in-flight live stream — so a menu left standing when a turn starts can
    // discard a reply the operator is watching, before its conversation has
    // reached the rail. Disabling only the trigger would not have caught this:
    // the rows were already on screen and already clickable.
    const onSelect = vi.fn();
    const { queryByTestId, rerender } = await openMenu({ value: 'explore', onSelect });
    await rerender({ value: 'explore', onSelect, disabled: true });
    await waitFor(() => expect(queryByTestId('crew-menu-popup')).toBeNull());
    expect(onSelect).not.toHaveBeenCalled();
  });
});
