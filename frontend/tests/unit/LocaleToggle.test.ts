import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import { tick } from 'svelte';
import { get } from 'svelte/store';
import LocaleToggle from '../../src/components/LocaleToggle.svelte';
import { locale, setLocale } from '../../src/lib/i18n';

afterEach(() => {
  cleanup();
  setLocale('en');
});

describe('LocaleToggle', () => {
  it('renders both language segments with the active one pressed (EN by default in tests)', () => {
    const { getByTestId } = render(LocaleToggle);
    const en = getByTestId('locale-en');
    const ja = getByTestId('locale-ja');
    expect(en.textContent).toBe('EN');
    expect(ja.textContent).toBe('日本語');
    expect(en.getAttribute('aria-pressed')).toBe('true');
    expect(ja.getAttribute('aria-pressed')).toBe('false');
  });

  it('labels each segment in its own language for a11y', () => {
    const { getByTestId } = render(LocaleToggle);
    expect(getByTestId('locale-en').getAttribute('lang')).toBe('en');
    expect(getByTestId('locale-ja').getAttribute('lang')).toBe('ja');
  });

  it('switches the locale store on click', async () => {
    const { getByTestId } = render(LocaleToggle);
    await fireEvent.click(getByTestId('locale-ja'));
    expect(get(locale)).toBe('ja');
    expect(getByTestId('locale-ja').getAttribute('aria-pressed')).toBe('true');
    expect(getByTestId('locale-en').getAttribute('aria-pressed')).toBe('false');

    await fireEvent.click(getByTestId('locale-en'));
    expect(get(locale)).toBe('en');
  });

  it('localizes the group aria-label with the active locale', async () => {
    const { getByRole } = render(LocaleToggle);
    expect(getByRole('group').getAttribute('aria-label')).toBe('Language');
    setLocale('ja');
    await tick(); // aria-label is reactive via $t; wait for Svelte to flush.
    expect(getByRole('group').getAttribute('aria-label')).toBe('言語');
  });
});
