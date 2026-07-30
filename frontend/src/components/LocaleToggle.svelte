<script lang="ts">
  // EN / 日本語 language toggle for the header. A compact two-segment control:
  // the active language is filled, the other is a quiet button. The whole app
  // re-renders reactively because every translated string reads the `$t`/`$locale`
  // stores this button writes via setLocale. Each label carries its own `lang`
  // (the endonym is in that language, not the current document language).
  import { locale, setLocale, type Locale, t } from '../lib/i18n';

  const options: {
    value: Locale;
    label: string;
    lang: string;
    ariaKey: 'common.localeToggle.selectJapanese' | 'common.localeToggle.selectEnglish';
    testid: string;
  }[] = [
    {
      value: 'ja',
      label: '日本語',
      lang: 'ja',
      ariaKey: 'common.localeToggle.selectJapanese',
      testid: 'locale-ja',
    },
    {
      value: 'en',
      label: 'EN',
      lang: 'en',
      ariaKey: 'common.localeToggle.selectEnglish',
      testid: 'locale-en',
    },
  ];
</script>

<div class="locale-toggle" role="group" aria-label={$t('common.localeToggle.aria')}>
  {#each options as opt (opt.value)}
    <button
      type="button"
      class="locale-toggle__seg"
      class:is-active={$locale === opt.value}
      aria-pressed={$locale === opt.value}
      lang={opt.lang}
      aria-label={$t(opt.ariaKey)}
      data-testid={opt.testid}
      onclick={() => setLocale(opt.value)}>{opt.label}</button
    >
  {/each}
</div>

<style>
  /* ds-7ag.3 — de-emphasized to a plain inline pair. This was a segmented pill
     with its own border, surface and shadowed active fill, which put it at the
     same visual weight as the view nav and the autonomy control; the header read
     as six competing chips (見づらい). Refactoring UI's rule is to quiet the
     secondaries, not to shout the primary louder. Weight now carries the state:
     the active language is ink, the other is faint. */
  .locale-toggle {
    display: inline-flex;
    align-items: stretch;
    gap: var(--ds-sp-2);
  }
  .locale-toggle__seg {
    appearance: none;
    border: 0;
    background: transparent;
    color: var(--ds-faint);
    font-family: inherit;
    font-size: var(--ds-fs-1);
    font-weight: var(--ds-fw-medium);
    line-height: 1.2;
    /* Keeps the hit area ≥ 24px tall despite the 13px text — the chrome went
       away, the target must not. */
    padding: 0.35em 0.2em;
    border-radius: var(--ds-radius-sm);
    cursor: pointer;
    white-space: nowrap;
    transition: color var(--ds-dur) var(--ds-ease);
  }
  .locale-toggle__seg:hover {
    color: var(--ds-fg);
  }
  .locale-toggle__seg.is-active {
    color: var(--ds-fg);
    font-weight: var(--ds-fw-semibold);
  }
  .locale-toggle__seg:active {
    transform: translateY(1px);
  }
</style>
