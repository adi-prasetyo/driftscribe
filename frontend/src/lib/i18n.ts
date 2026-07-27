// i18n.ts — hand-rolled, dependency-free localization for the operator SPA.
//
// Idiom matches the repo's other stores (pauseStore/autonomyStore): a classic
// `writable` consumed via `$`-auto-subscription. Templates translate with the
// reactive `{$t('key')}`; anything that needs the locale for Intl reads the
// `$locale` store and maps it through `localeTag`. See
// docs/plans/2026-07-11-i18n-japanese-localization-design.md for the contract.
import { writable, derived, type Readable } from 'svelte/store';
import { messages, type MessageKey } from '../locales';

export type Locale = 'en' | 'ja';
export type { MessageKey };

/** Toggle order (JA first — this is a Japanese-audience app). */
export const LOCALES: readonly Locale[] = ['ja', 'en'] as const;

const STORAGE_KEY = 'driftscribe.locale';

export function isLocale(v: unknown): v is Locale {
  return v === 'en' || v === 'ja';
}

/**
 * Concrete BCP-47 tag for `Intl` / `toLocaleString`. NEVER pass `undefined` to
 * Intl for localized output — it uses the host default and won't respond to the
 * toggle. Callers pass `localeTag($locale)`.
 */
export function localeTag(l: Locale): 'ja-JP' | 'en-US' {
  return l === 'ja' ? 'ja-JP' : 'en-US';
}

/**
 * Initial locale: a persisted choice wins; otherwise Japanese (first-time
 * visitors to this Japanese-audience app default to JA regardless of browser).
 * Pure + independently testable (does not touch the singleton store).
 */
export function detectInitial(): Locale {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (isLocale(stored)) return stored;
  } catch {
    /* storage unavailable (locked/SSR) — fall through to the JA default */
  }
  return 'ja';
}

type Params = Record<string, string | number>;

function interpolate(s: string, params?: Params): string {
  if (!params) return s;
  return s.replace(/\{(\w+)\}/g, (m, k) =>
    Object.prototype.hasOwnProperty.call(params, k) ? String(params[k]) : m,
  );
}

// Loud in dev/test (a typo'd/undefined key throws), quiet in prod (degrades to
// EN, then to the key text) so a missing translation can never blank the UI.
const LOUD_ON_MISSING =
  typeof import.meta !== 'undefined' &&
  !!import.meta.env &&
  (import.meta.env.DEV === true || import.meta.env.MODE === 'test');

/**
 * Resolve a message for `loc`. JA falls back to EN; a key absent from BOTH
 * catalogs throws in dev/test and returns the key in prod. Pure — the reactive
 * wrapper is `t` below.
 */
export function translate(loc: Locale, key: MessageKey | string, params?: Params): string {
  const raw = (loc === 'ja' ? messages.ja[key] : messages.en[key]) ?? messages.en[key];
  if (raw === undefined) {
    if (LOUD_ON_MISSING) throw new Error(`i18n: unknown message key "${key}"`);
    return key;
  }
  return interpolate(raw, params);
}

export type TranslateFn = (key: MessageKey, params?: Params) => string;

/** The active locale. Consume as `$locale`. */
export const locale = writable<Locale>(detectInitial());

// Persist the choice + reflect it on <html lang> and document.title on every
// change (Codex review: keep storage, the store, and the document in sync).
locale.subscribe((l) => {
  try {
    localStorage.setItem(STORAGE_KEY, l);
  } catch {
    /* ignore */
  }
  if (typeof document !== 'undefined') {
    document.documentElement.lang = l;
    document.title = translate(l, 'common.documentTitle');
  }
});

export function setLocale(l: Locale): void {
  locale.set(l);
}

export function toggleLocale(): void {
  locale.update((l) => (l === 'ja' ? 'en' : 'ja'));
}

/** Reactive translator for markup + `$derived`: `{$t('key')}` / `{$t('key', { n })}`. */
export const t: Readable<TranslateFn> = derived(
  locale,
  ($l): TranslateFn =>
    (key: MessageKey, params?: Params) =>
      translate($l, key, params),
);

/** Locale-aware number formatting (replaces `n.toLocaleString('en-US')`). */
export function fmtNumber(n: number, l: Locale): string {
  return n.toLocaleString(localeTag(l));
}

/**
 * Pluralization: EN picks `.one` / `.other` by count; JA carries identical
 * `.one` / `.other` (no grammatical plural) so the same call works. Catalog both
 * forms. Usage: `plural($t, 'conversations.messageCount', n)`.
 */
export function plural(tf: TranslateFn, base: string, n: number): string {
  const key = (n === 1 ? `${base}.one` : `${base}.other`) as MessageKey;
  return tf(key, { n });
}
