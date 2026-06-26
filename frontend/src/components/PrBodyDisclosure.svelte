<script lang="ts">
  // PrBodyDisclosure — a read-only "What this change did (from the PR)" panel for
  // the open-trace card. Shows the agent-authored PR description (fetched from
  // GET /trace/{id}/pr-body, scrubbed server-side). It answers, in the agent's
  // own words, what a past iac_apply decision actually changed — without any
  // generated prose.
  //
  // Renders NOTHING when there is no body (null / empty), so a PR with no
  // description shows no empty box (fail-soft). The body is plain text in a
  // <pre> — Svelte auto-escapes {body}, so there is NO {@html} / injection
  // surface (the body is agent-authored markdown but rendered as literal text).

  let { body, truncated = false }: { body: string | null; truncated?: boolean } =
    $props();
</script>

{#if body}
  <details class="ds-disclosure pr-body" data-testid="pr-body-disclosure">
    <summary class="pr-body__summary">
      What this change did <span class="pr-body__hint">(from the PR)</span>
    </summary>
    <pre class="ds-pre pr-body__pre">{body}</pre>
    {#if truncated}
      <p class="pr-body__truncated" data-testid="pr-body-truncated">
        Description truncated — open the PR on GitHub for the full text.
      </p>
    {/if}
  </details>
{/if}

<style>
  .pr-body {
    margin-top: var(--ds-sp-4);
  }

  .pr-body__summary {
    cursor: pointer;
    color: var(--ds-fg);
    font-size: var(--ds-fs-2);
    font-weight: var(--ds-fw-semibold);
  }

  .pr-body__hint {
    color: var(--ds-muted);
    font-weight: var(--ds-fw-normal);
  }

  .pr-body__pre {
    margin-top: var(--ds-sp-3);
    max-height: 24rem;
    overflow-y: auto;
  }

  .pr-body__truncated {
    margin: var(--ds-sp-2) 0 0;
    color: var(--ds-muted);
    font-size: var(--ds-fs-1);
  }
</style>
