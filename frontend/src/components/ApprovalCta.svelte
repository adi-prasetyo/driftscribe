<script lang="ts">
  import { safeApprovalHref } from '../lib/approval';
  import { t, locale } from '../lib/i18n';

  // Inline HITL call-to-action rendered INSIDE a rollback tool result. The
  // rollback worker may embed an `approval_url` in its JSON result payload; we
  // surface a same-origin "Approve →" button for it.
  //
  // SECURITY: this re-homes the same-origin CTA guard from the legacy
  // transparency renderer. We NEVER emit an `href` that did not pass
  // `safeApprovalHref` — an attacker-shaped result (off-origin URL,
  // `javascript:`/`data:` scheme, non-`/approvals/` path, malformed JSON) must
  // render NOTHING. The parse is wrapped in try/catch so a bad payload can
  // never throw at render time.
  let { resultPreview }: { resultPreview: string } = $props();

  const href = $derived.by<string | null>(() => {
    try {
      const obj: unknown = JSON.parse(resultPreview);
      const url =
        obj && typeof obj === 'object'
          ? (obj as Record<string, unknown>).approval_url
          : undefined;
      return typeof url === 'string'
        ? safeApprovalHref(url, undefined, $locale)
        : null;
    } catch {
      return null;
    }
  });
</script>

{#if href}
  <div class="approval-cta">
    <strong class="approval-cta__title">{$t('approval.rollbackCta.title')}</strong>
    <a class="approval-btn" {href} target="_blank" rel="noopener">{$t('approval.rollbackCta.approve')}</a>
  </div>
{/if}

<style>
  /* Amber call-to-action card — an operator action that is gating real work,
     so it reads as a warm, attention-drawing surface (not an error). */
  .approval-cta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--ds-sp-3) var(--ds-sp-4);
    margin: var(--ds-sp-4) 0;
    padding: var(--ds-sp-3) var(--ds-sp-4);
    background: var(--ds-warn-surface);
    border: 1px solid var(--ds-warn-border);
    border-left: 3px solid var(--ds-warn);
    border-radius: var(--ds-radius-sm);
  }

  .approval-cta__title {
    display: inline-flex;
    align-items: center;
    gap: var(--ds-sp-2);
    color: var(--ds-warn-ink);
    font-size: var(--ds-fs-2);
    font-weight: var(--ds-fw-semibold);
    line-height: var(--ds-lh-snug);
  }

  /* Small amber lozenge before the title for a touch of editorial polish. */
  .approval-cta__title::before {
    content: '';
    flex: 0 0 auto;
    width: 0.55em;
    height: 0.55em;
    border-radius: var(--ds-radius-pill);
    background: var(--ds-warn);
    box-shadow: 0 0 0 3px var(--ds-warn-surface), 0 0 0 4px var(--ds-warn-border);
  }

  /* Mirrors the shared `.ds-btn .ds-btn--approve` look (the approve-green
     primary action), kept component-scoped so it sits correctly in the amber
     card. */
  .approval-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: var(--ds-sp-2);
    flex: 0 0 auto;
    padding: 0.5em 1.15em;
    background: var(--ds-ok);
    border: 1px solid var(--ds-ok-ink);
    border-radius: var(--ds-radius-sm);
    color: #fff;
    font-size: var(--ds-fs-2);
    font-weight: var(--ds-fw-semibold);
    line-height: 1.2;
    text-decoration: none;
    white-space: nowrap;
    cursor: pointer;
    transition: background-color var(--ds-dur) var(--ds-ease),
      box-shadow var(--ds-dur) var(--ds-ease),
      transform var(--ds-dur-fast) var(--ds-ease);
  }

  .approval-btn:hover {
    background: var(--ds-ok-ink);
    text-decoration: none;
  }

  .approval-btn:active {
    transform: translateY(1px);
  }

  @media (max-width: 32rem) {
    .approval-btn {
      width: 100%;
    }
  }
</style>
