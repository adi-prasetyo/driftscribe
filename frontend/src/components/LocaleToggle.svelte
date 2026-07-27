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
  .locale-toggle {
    display: inline-flex;
    align-items: stretch;
    gap: 2px;
    padding: 2px;
    background: var(--ds-surface-2);
    border: 1px solid var(--ds-border);
    border-radius: var(--ds-radius-pill);
  }
  .locale-toggle__seg {
    appearance: none;
    border: 0;
    background: transparent;
    color: var(--ds-muted);
    font-family: inherit;
    font-size: var(--ds-fs-1);
    font-weight: var(--ds-fw-semibold);
    line-height: 1.2;
    padding: 0.28em 0.7em;
    border-radius: var(--ds-radius-pill);
    cursor: pointer;
    white-space: nowrap;
    transition:
      background-color var(--ds-dur) var(--ds-ease),
      color var(--ds-dur) var(--ds-ease);
  }
  .locale-toggle__seg:hover {
    color: var(--ds-fg);
  }
  .locale-toggle__seg.is-active {
    background: var(--ds-surface);
    color: var(--ds-fg);
    box-shadow: var(--ds-shadow-sm);
  }
  .locale-toggle__seg:active {
    transform: translateY(1px);
  }
</style>
