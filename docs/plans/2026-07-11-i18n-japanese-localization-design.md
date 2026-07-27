# Japanese localization + EN/JA toggle — design & implementation plan

**Date:** 2026-07-11
**Goal:** Localize every user-facing text in the DriftScribe operator SPA for Japanese
users, with a language toggle so English users can switch back. Default to Japanese
(this is a Japanese-audience submission), persisted, with a header toggle.

## Scope (decided with the user)

- **In scope:** ALL static frontend text — buttons, labels, headings, help hints,
  tooltips, placeholders, aria-labels, empty/error states, the tour, every panel,
  and the display strings that live in `frontend/src/lib/*.ts`.
- **Out of scope:** server/LLM-generated content — crew chat replies, PR bodies,
  decision-log row data, trace event payloads. These come from the Python
  coordinator/workers and stay in their original language. (We DO localize the
  static chrome that frames them: section headings, meta-line labels, status
  tokens the frontend maps from backend enums, buttons, etc.)
- **Default language:** first-time visitor gets **Japanese**; choice persisted in
  `localStorage['driftscribe.locale']`; header toggle flips EN/JA and re-persists.

## Architecture

Hand-rolled, dependency-free, matching the repo's ethos (like `markdown.ts`). No
`svelte-i18n` / `typesafe-i18n` dependency.

### Core — `frontend/src/lib/i18n.ts`

- `export type Locale = 'en' | 'ja'`
- `locale` = a Svelte `writable<Locale>` (same store idiom as `pauseStore`/
  `autonomyStore`, consumed via `$`-auto-subscription).
- `detectInitial()`: `localStorage['driftscribe.locale']` if `'en'|'ja'`, else
  `'ja'` (Japanese-audience default). Wrapped in try/catch for SSR/jsdom safety.
- A `locale.subscribe` side-effect persists to localStorage and sets
  `document.documentElement.lang`.
- `setLocale(l)` / `toggleLocale()`.
- `translate(loc, key, params?)`: `messages[loc][key] ?? messages.en[key] ?? key`,
  then `{param}` interpolation. English is the guaranteed fallback so a missing JA
  key degrades to English, never to a raw key in the UI.
- `t` = `derived(locale, $l => (key, params?) => translate($l, key, params))`.
  Templates use `{$t('key')}` / `{$t('key', { n })}`. Because `$t` is a reactive
  store value, any markup / `$derived` that calls it re-runs on toggle.

### Catalog — `frontend/src/locales/`

- One file per namespace (component/area), each exporting `{ en: {...}, ja: {...} }`
  with **flat dotted keys** (`composer.placeholder`, `header.brandSub`, ...).
  Per-namespace files = parallel-safe (fan-out agents never touch a shared file).
- `frontend/src/locales/index.ts` merges all namespaces into
  `messages: Record<Locale, Record<string,string>>`.
- **Key-parity unit test** (`locales.test.ts`): for every namespace, `en` and `ja`
  key sets are identical; no empty values; no key defined twice across namespaces.

### Toggle — `frontend/src/components/LocaleToggle.svelte`

Compact segmented control `EN | 日本語` in `App.svelte` header actions
(`app-header__actions`, beside `DemoNoticeBell` / `AutonomyPill`). Calls
`setLocale`. `aria-label` localized; `lang`/`hreflang` semantics correct.

### `lib/*.ts` string-producing helpers — reactive strategy

Helpers that compose human text (often with data, e.g. `superseded by #${pr}`) take
a `t: TranslateFn` parameter; components pass `$t`. Keeps them **pure + unit-testable**
(tests pass an `en`-bound `translate`) and **reactive** (component `$derived`/markup
tracking `$t` re-runs on toggle). Affected: `format.ts` (`iacApplyMeta`,
`iacStatusLabel/Help`, `decisionActionLabel/Help`, `fmtTokens` "tok", `fmtWhen` via
`Intl` locale), `approval.ts` (`iacApproveLabel`), `autonomy.ts` (`MODE_LABELS`,
`MODE_BLURBS`, explainer), `autonomyStore.ts` (`autonomyNoteFor`), `labels.ts`
(`workerLabel`), `decision.ts` (`decisionFields` labels + `ACTION_LABEL`),
`capabilities.ts` (`CATEGORY_HEADINGS`), `workloads.ts` (`CREW_LIFECYCLE`,
prefill text). Proper nouns (Anchor/Patch/Provision/Explore, DriftScribe) are NOT
translated. `Intl.DateTimeFormat`/`toLocaleString` switch to the active locale.

### Constraint: `workloads.catalog.json` is backend-pinned

`tests/unit/test_capabilities.py::test_frontend_catalog_matches_backend` pins the
JSON's `name`/`descriptor` to backend YAML. Do NOT edit the JSON. The JSON stays the
**EN source**; JA crew descriptors/summaries live in a separate `crews` namespace map
keyed by workload value, applied at render time.

## Test strategy

- `frontend/tests/unit/setup.ts`: force `localStorage['driftscribe.locale']='en'`
  before each test so the ~40 existing English-asserting component/App tests keep
  passing unchanged (EN catalog values are byte-identical to the current inline text).
- Helper-signature tests (`format/approval/labels/decision/capabilities/
  autonomyStore` .test.ts): thread an `en`-bound `t`; assertions stay English.
- New tests: key-parity; `i18n.ts` (default JA, persistence, fallback, interpolation);
  `LocaleToggle`; one JA-render smoke per major surface.

## Terminology consistency (user ask) — `docs/i18n-glossary.md`

Canonical EN→JA glossary for domain terms so wording is identical everywhere:
drift, adopt, approve/approval, propose, apply, plan, crew, workload, trace,
reasoning, coverage, rollback, denylist, autonomy (observe/propose/propose+apply),
infrastructure/IaC, pull request, superseded, provision, steward, live/managed, etc.
**Codex back-and-forth** reviews (a) the glossary term choices and (b) the assembled
JA catalog against the glossary for consistency across UI + explanatory copy.

## Execution phases

0. **Core**: `i18n.ts`, `locales/index.ts` + scaffold, `LocaleToggle.svelte`, wire
   into header, `<html lang>`, test `setup.ts` en-pin, key-parity test. Build green.
1. **Glossary**: author `docs/i18n-glossary.md`; Codex review of terms.
2. **Fan-out extraction+translation**: parallel subagents, each owns disjoint
   components + its namespace file + (for lib owners) helper signature + that helper's
   test. Follow the glossary. Extract EN verbatim, add JA, swap inline text → `$t`.
3. **Reconcile + build**: merge namespaces, `svelte-check`, full `vitest`, fix.
4. **Terminology + translation review**: Codex reviews JA catalog vs glossary; iterate.
5. **Visual verify**: Playwright — JA default screenshot, toggle → EN screenshot,
   assert no cross-locale leakage; run smoke tests.
6. **Ship**: commit on a feature branch, open PR. Because this changes the DEFAULT
   language of the live public demo (English demo video in flight), CONFIRM with the
   user before merge/deploy rather than auto-deploying.

## Revisions after Codex review (2026-07-11) — FROZEN CONVENTIONS

These are the contract every fan-out agent must follow. Codex thread
`019f4fce-99a2-7610-88fe-cf7084d93559`.

- **Typed keys.** `type MessageKey = keyof typeof enMessages` (inferred from the
  merged EN catalog). `TranslateFn = (k: MessageKey, params?: Record<string,
  string|number>) => string`. A `$t('typo')` is then a compile error.
- **Missing-key behavior.** `translate` returns `messages[loc][k] ?? messages.en[k]
  ?? <throw in dev/test | return k in prod>`. Import.meta.env.DEV/`MODE==='test'`
  → throw so a missing/typo'd key fails loudly; prod degrades to EN then to the key.
- **Interpolation:** `{name}` placeholders only. No ICU.
- **Plurals:** catalog `.one` + `.other` keys (JA both identical); components pick
  `n === 1 ? '.one' : '.other'`. Small `plural(t, base, n)` helper.
- **Whole-sentence keys.** Never concatenate translated fragments across markup —
  JA word order differs. A sentence with an embedded link/number/`<strong>` is ONE
  key with `{placeholders}`; if markup truly must split it, use a component-level
  structured variant, not string concat.
- **EN verbatim.** Each agent first moves the EXACT current English into the `en`
  catalog (byte-for-byte — the en-pinned tests must still pass), THEN writes `ja`.
- **Intl needs locale, not just `$t`.** Export the `locale` store; a
  `localeTag(l): 'ja-JP'|'en-US'` helper; `fmtNumber(n, l)` and date formatters that
  take the tag. Replace `Intl.DateTimeFormat(undefined,…)` / `toLocaleString('en-US')`
  at: `ConversationsRail.svelte:59`, `PauseBanner.svelte:63`, `Timeline.svelte:185`,
  `AutonomyPill.svelte:244`, `DecisionsRail.svelte:158`, `format.ts:35,306`.
- **Hybrid, not `t`-everywhere.** Pure view-model helpers return SEMANTIC IDS where
  natural (status enum, conversation bucket, autonomy mode, action, lifecycle) and
  the component translates the id; only helpers composing a sentence from several
  runtime values take `t`. `conversations.ts` `Today/Yesterday/Older` become ids, not
  rendered strings. `WORKLOADS`/`MODE_LABELS`/`groupRules` are built once at module
  eval — they must NOT read the store; expose their text as keys/ids resolved at render.
- **Backend capability prose.** Localize STABLE IDENTIFIERS via frontend JA maps with
  EN fallback: `gate.id`, `rule.id`+`category`, tool/worker `name`, `workload` value,
  action `name`. Arbitrary prose stays pass-through (English) and is DOCUMENTED:
  `InfraDiagram` `degraded_reason`/`caveat`, backend error strings, and any
  free-text description with no stable id. CapabilityCard sites:
  `:170,:178,:235,:321`. This keeps the panel mostly-JA without backend changes.
- **Both HTML shells:** `frontend/index.html` + `agent/templates/transparency.html`
  → initial `lang="ja"`, JA `<title>`, JA `<noscript>`. Runtime syncs
  `document.documentElement.lang` + `document.title` from the store (via `<svelte:head>`
  or a store subscription). `hreflang` omitted (same URL, client state). Toggle labels
  carry their own `lang` ("EN"→`lang="en"`, "日本語"→`lang="ja"`).
- **Fonts:** append JA-native fallbacks to `--ds-font` (Hiragino Sans, "Hiragino Kaku
  Gothic ProN", "Yu Gothic", YuGothic, Meiryo, "Noto Sans JP"); visually verify weights.
- **`nowrap` audit:** header, crew picker, CTAs, rail metadata pills — verify at JA.
- **Test isolation:** `setup.ts` seeds `driftscribe.locale='en'` at MODULE EVAL (top
  level, before app modules import `i18n.ts`) AND resets the `locale` store +
  `document.documentElement.lang` in an `afterEach`. Smoke/visual suites seed EN via
  `page.addInitScript`; separate JA smoke variants added. `detectInitial`/`translate`
  stay pure + independently unit-tested (don't re-import the global singleton to test
  default-JA).
- **Fan out by VERTICAL surface**, not component count. Foundation batch (shared lib
  helpers + shared components HelpHint/Modal/Group + `i18n.ts` API freeze + all
  namespace stub files + `locales/index.ts`) lands and is verified FIRST; then surface
  agents run in parallel, each integrating with its own EN+JA tests (small batches, not
  one big end reconciliation). `locales/index.ts` is pre-wired centrally so no agent
  edits it concurrently.
- **Residual-string audit** uses an allowlist (DriftScribe, crew names, GCP, IaC, PR#,
  code/ids, backend/user content) and scans known chrome containers — a naive
  Latin-run scan is too noisy.
- **Server-rendered approval pages** (`agent/templates/*` beyond the shell, e.g.
  iac-approval) are OUT of this SPA pass (no client toggle there); noted as a
  follow-up. The SPA is the deliverable.

## Risks

- Cross-locale leakage (a missed inline string). Mitigation: JA-mode Playwright scan
  for Latin-script runs in visible text; grep for residual quoted English in templates.
- Layout: Japanese is often more compact but some labels wrap differently; verify the
  header, crew picker, rail, and buttons at both locales.
- Reactivity in `<script>` blocks (not just markup): use `$derived` over `$t` /
  `$locale`, never a one-time `get(locale)`.
- Test breakage from default JA: neutralized by the en-pin in `setup.ts`.
