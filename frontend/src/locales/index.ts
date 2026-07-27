// Catalog aggregator. Each surface owns a per-namespace file ({ en, ja } of flat
// dotted keys); this file merges them so the fan-out never edits a shared file.
// `MessageKey` is inferred from the merged EN catalog → `$t('typo')` is a compile
// error. EN is the guaranteed fallback; JA missing a key degrades to EN, never to
// a raw key in the UI. Key parity (EN⇔JA) is enforced by locales.test.ts.
import { common } from './common';
import { shared } from './shared';
import { header } from './header';
import { composer } from './composer';
import { conversations } from './conversations';
import { decisions } from './decisions';
import { timeline } from './timeline';
import { infra } from './infra';
import { capability } from './capability';
import { approval } from './approval';
import { tour } from './tour';
import { auth } from './auth';
import { misc } from './misc';

export type Locale = 'en' | 'ja';

// Per-namespace map, exposed for the parity test's precise diagnostics.
export const NAMESPACES = {
  common,
  shared,
  header,
  composer,
  conversations,
  decisions,
  timeline,
  infra,
  capability,
  approval,
  tour,
  auth,
  misc,
};

// Merge EN with literal key types preserved (so `keyof` yields the real union).
export const enMessages = {
  ...common.en,
  ...shared.en,
  ...header.en,
  ...composer.en,
  ...conversations.en,
  ...decisions.en,
  ...timeline.en,
  ...infra.en,
  ...capability.en,
  ...approval.en,
  ...tour.en,
  ...auth.en,
  ...misc.en,
};

export const jaMessages = {
  ...common.ja,
  ...shared.ja,
  ...header.ja,
  ...composer.ja,
  ...conversations.ja,
  ...decisions.ja,
  ...timeline.ja,
  ...infra.ja,
  ...capability.ja,
  ...approval.ja,
  ...tour.ja,
  ...auth.ja,
  ...misc.ja,
};

/** Every valid message key — inferred from the EN catalog. */
export type MessageKey = keyof typeof enMessages;

export const messages: { en: Record<string, string>; ja: Record<string, string> } = {
  en: enMessages,
  ja: jaMessages,
};
