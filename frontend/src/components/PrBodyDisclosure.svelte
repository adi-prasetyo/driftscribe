<script lang="ts">
  // PrBodyDisclosure — a read-only "What this change did (from the PR)" panel for
  // the open-trace card. Shows the agent-authored PR description (fetched from
  // GET /trace/{id}/pr-body, scrubbed server-side). It answers, in the agent's
  // own words, what a past iac_apply decision actually changed — without any
  // generated prose.
  //
  // The body is agent-authored Markdown (it lands on GitHub as Markdown). We
  // render a SMALL Markdown SUBSET (see lib/markdown.ts) through Svelte's
  // auto-escaping native elements — headings, bold/italic, inline + fenced code,
  // lists, links, GFM tables. There is NO {@html} anywhere: every text leaf is a
  // `{value}` interpolation and the only parsed attribute is a link `href`,
  // which is scheme-allowlisted by `safeMarkdownLinkHref`. Rendering as prose
  // (not a monospace <pre>) also lets emoji fall back to the system emoji font.
  //
  // Renders NOTHING when there is no body (null / empty / whitespace-only), so a
  // PR with no description shows no empty box. Parsing is fail-soft: a thrown
  // parse degrades to a single plain-text paragraph.

  import { parseMarkdown, type BlockNode, type InlineNode } from '../lib/markdown';

  let { body, truncated = false }: { body: string | null; truncated?: boolean } =
    $props();

  function safeParse(src: string | null): BlockNode[] {
    if (!src) return [];
    try {
      return parseMarkdown(src);
    } catch {
      return [{ type: 'paragraph', children: [{ type: 'text', value: src }] }];
    }
  }

  let blocks = $derived(safeParse(body));
</script>

{#snippet inline(nodes: InlineNode[])}
  {#each nodes as n}
    {#if n.type === 'text'}{n.value}
    {:else if n.type === 'br'}<br />
    {:else if n.type === 'strong'}<strong>{@render inline(n.children)}</strong>
    {:else if n.type === 'em'}<em>{@render inline(n.children)}</em>
    {:else if n.type === 'code'}<code class="md-code">{n.value}</code>
    {:else if n.type === 'link'}<a
        class="md-link"
        href={n.href}
        target="_blank"
        rel="noopener noreferrer">{@render inline(n.children)}</a>
    {/if}
  {/each}
{/snippet}

{#if body && blocks.length > 0}
  <details class="ds-disclosure pr-body" data-testid="pr-body-disclosure">
    <summary class="pr-body__summary">
      What this change did <span class="pr-body__hint">(from the PR)</span>
    </summary>
    <!-- focusable so keyboard users can scroll the clipped prose (WCAG 2.1.1) -->
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <div
      class="pr-body__md"
      data-testid="pr-body-md"
      tabindex="0"
      role="group"
      aria-label="Pull request description"
    >
      {#each blocks as b}
        {#if b.type === 'heading'}
          <p class="md-heading md-heading--{b.level}">{@render inline(b.children)}</p>
        {:else if b.type === 'paragraph'}
          <p class="md-p">{@render inline(b.children)}</p>
        {:else if b.type === 'list'}
          {#if b.ordered}
            <ol class="md-list">
              {#each b.items as item}<li>{@render inline(item)}</li>{/each}
            </ol>
          {:else}
            <ul class="md-list">
              {#each b.items as item}<li>{@render inline(item)}</li>{/each}
            </ul>
          {/if}
        {:else if b.type === 'codeblock'}
          <pre class="ds-pre md-codeblock"><code>{b.value}</code></pre>
        {:else if b.type === 'table'}
          <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
          <div
            class="md-table-wrap"
            tabindex="0"
            role="group"
            aria-label="Table (scrollable)"
          >
            <table class="md-table">
              <thead>
                <tr>{#each b.header as cell}<th scope="col">{@render inline(cell)}</th>{/each}</tr>
              </thead>
              <tbody>
                {#each b.rows as row}
                  <tr>{#each row as cell}<td>{@render inline(cell)}</td>{/each}</tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      {/each}
    </div>
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

  /* Prose container — normal (NON-mono) font so the agent's description reads as
     prose and non-ASCII glyphs (e.g. 🤖) fall back to the system emoji font. */
  .pr-body__md {
    margin-top: var(--ds-sp-3);
    max-height: 24rem;
    overflow-y: auto;
    font-family: var(--ds-font);
    font-size: var(--ds-fs-1);
    line-height: var(--ds-lh-body);
    color: var(--ds-fg-soft);
  }

  .pr-body__md > :first-child {
    margin-top: 0;
  }

  .md-heading {
    margin: var(--ds-sp-4) 0 var(--ds-sp-2);
    color: var(--ds-fg);
    font-weight: var(--ds-fw-semibold);
    line-height: var(--ds-lh-tight, 1.25);
  }
  .md-heading--1,
  .md-heading--2 {
    font-size: var(--ds-fs-2);
  }
  .md-heading--3,
  .md-heading--4,
  .md-heading--5,
  .md-heading--6 {
    font-size: var(--ds-fs-1);
  }

  .md-p {
    margin: 0 0 var(--ds-sp-3);
  }

  .md-list {
    margin: 0 0 var(--ds-sp-3);
    padding-left: 1.4em;
  }
  .md-list li {
    margin: var(--ds-sp-1, 0.25rem) 0;
  }

  /* Inline code keeps the monospace face (it IS code) with a subtle chip. */
  .md-code {
    font-family: var(--ds-font-mono);
    font-size: 0.92em;
    background: var(--ds-surface-2);
    border: 1px solid var(--ds-border);
    border-radius: var(--ds-radius-sm);
    padding: 0.05em 0.35em;
    word-break: break-word;
  }

  .md-codeblock {
    margin: var(--ds-sp-3) 0;
  }
  .md-codeblock code {
    font-family: var(--ds-font-mono);
    background: none;
    border: none;
    padding: 0;
  }

  .md-link {
    color: var(--ds-accent, var(--ds-fg));
    text-decoration: underline;
    word-break: break-word;
  }

  .md-table-wrap {
    margin: var(--ds-sp-3) 0;
    overflow-x: auto;
  }
  .md-table {
    border-collapse: collapse;
    font-size: var(--ds-fs-1);
  }
  .md-table th,
  .md-table td {
    border: 1px solid var(--ds-border);
    padding: var(--ds-sp-1, 0.25rem) var(--ds-sp-2);
    text-align: left;
    vertical-align: top;
  }
  .md-table th {
    color: var(--ds-fg);
    font-weight: var(--ds-fw-semibold);
  }

  .pr-body__truncated {
    margin: var(--ds-sp-2) 0 0;
    color: var(--ds-muted);
    font-size: var(--ds-fs-1);
  }
</style>
