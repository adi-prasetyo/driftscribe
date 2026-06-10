<script lang="ts">
  // CapabilityCard — a collapsed "What this agent can — and cannot — do" panel
  // that renders the agent's safety cage in plain language for ClickOps operators.
  //
  // Design:
  //  - Lazy fetch: nothing on mount; fetches GET /capabilities ONCE on first
  //    open and caches for the component's lifetime (data is static per deploy).
  //  - Render order is anxiety-first: gates → denylist → workloads.
  //  - The `call` prop is the same token-aware fetch wrapper as InfraDiagram.

  import { groupRules, CATEGORY_HEADINGS, type Capabilities } from '../lib/capabilities';

  let {
    call,
  }: {
    /** App's token-aware fetch wrapper. */
    call: (path: string, init?: RequestInit) => Promise<Response>;
  } = $props();

  let data = $state<Capabilities | null>(null);
  let loading = $state(false);
  let fetchError = $state(false);
  let fetched = $state(false);

  async function fetchCapabilities(): Promise<void> {
    loading = true;
    fetchError = false;
    try {
      let resp: Response;
      try {
        resp = await call('/capabilities');
      } catch {
        fetchError = true;
        return;
      }
      if (!resp.ok) {
        fetchError = true;
        return;
      }
      let body: Capabilities;
      try {
        body = (await resp.json()) as Capabilities;
      } catch {
        fetchError = true;
        return;
      }
      data = body;
      fetched = true;
    } finally {
      loading = false;
    }
  }

  async function retry(): Promise<void> {
    // Reset fetched so we can re-run without the "already fetched" guard.
    // The cache lives in `data` — on retry success it will be repopulated.
    await fetchCapabilities();
    if (!fetchError) fetched = true;
  }

  function onToggle(e: Event): void {
    const d = e.currentTarget as HTMLDetailsElement;
    if (d.open && !fetched && !loading) {
      void fetchCapabilities().then(() => {
        if (!fetchError) fetched = true;
      });
    }
  }

  const ruleGroups = $derived(data ? groupRules(data.denylist.rules) : []);
</script>

<details class="ds-card cap-card" data-testid="capability-card" ontoggle={onToggle}>
  <summary class="cap-summary" data-testid="cap-summary">
    <span class="cap-summary__title ds-label">What this agent can — and cannot — do</span>
    <span class="cap-summary__hint">safety cage, generated from enforcement code</span>
  </summary>

  <div class="cap-body">
    {#if loading && !data}
      <p class="ds-subtle cap-loading">Loading…</p>
    {:else if fetchError}
      <div class="cap-error-row" data-testid="cap-error">
        <span class="ds-note">Could not load capability data.</span>
        <button
          class="ds-btn ds-btn--ghost cap-retry"
          type="button"
          data-testid="cap-retry"
          onclick={() => void retry()}
        >Retry</button>
      </div>
    {:else if data}
      <!-- 1. Gates — anxiety-first: operator wants to know what requires their approval -->
      <section class="cap-section" data-testid="cap-gates" aria-labelledby="cap-gates-heading">
        <h3 class="cap-section__heading" id="cap-gates-heading">Always needs your approval</h3>
        {#each data.human_gates as gate (gate.id)}
          <div class="cap-gate">
            <p class="cap-gate__title"><strong>{gate.title}</strong></p>
            <p class="cap-gate__desc ds-subtle">{gate.description}</p>
          </div>
        {/each}
      </section>

      <!-- 2. Denylist — blocked outright, approval cannot override -->
      <section class="cap-section" data-testid="cap-denylist" aria-labelledby="cap-denylist-heading">
        <h3 class="cap-section__heading" id="cap-denylist-heading">Blocked outright — approval cannot override these</h3>
        <p class="ds-subtle cap-denylist__summary">{data.denylist.summary}</p>
        {#each ruleGroups as group (group.category)}
          <div class="cap-rule-group">
            <h4 class="cap-rule-group__heading">{group.heading}</h4>
            <ul class="cap-rule-list">
              {#each group.rules as rule (rule.id)}
                <li class="cap-rule">
                  <span class="cap-rule__desc">{rule.description}</span>
                  {' '}<code class="cap-rule__id">{rule.id}</code>
                </li>
              {/each}
            </ul>
          </div>
        {/each}
        <p class="ds-subtle cap-denylist__enforced">
          checked at: {data.denylist.enforced_at.join(' → ')}
        </p>
      </section>

      <!-- 3. Workloads — what each workload can use -->
      <section class="cap-section" data-testid="cap-workloads" aria-labelledby="cap-workloads-heading">
        <h3 class="cap-section__heading" id="cap-workloads-heading">What each workload can use</h3>
        {#each data.workloads as wl (wl.name)}
          <details class="cap-workload">
            <summary
              class="cap-workload__summary"
              data-testid="cap-workload-{wl.name}-summary"
            >
              <!-- {' '} is the ONLY whitespace at this seam (spans glued): the
                   rendered text is exactly "<display_name> <pill>", which the
                   glued-exact-string test pins. -->
              <span class="cap-workload__name">{wl.display_name}</span>{' '}<span
                class="ds-pill {wl.autonomous ? 'ds-pill--ok' : 'ds-pill--muted'} cap-workload__pill"
                >{wl.autonomous ? 'autonomous + chat' : 'chat-only'}</span>
            </summary>
            <div class="cap-workload__body">
              <p class="ds-subtle cap-workload__desc">{wl.description}</p>

              {#if wl.tools.length > 0}
                <p class="cap-workload__sub-heading">Tools</p>
                <ul class="cap-item-list">
                  {#each wl.tools as tool (tool.name)}
                    <li
                      class="cap-tool"
                      data-testid="cap-tool-{tool.name}"
                    >
                      <code class="cap-item__name">{tool.name}</code>
                      <span class="cap-item__desc ds-subtle">{tool.description}</span>
                      {' '}<span
                        class="ds-pill cap-badge {tool.write_capable ? 'ds-pill--warn' : 'ds-pill--muted'}"
                      >{tool.write_capable ? 'write-capable' : 'read'}</span>
                    </li>
                  {/each}
                </ul>
              {/if}

              {#if wl.workers.length > 0}
                <p class="cap-workload__sub-heading">Workers</p>
                <ul class="cap-item-list">
                  {#each wl.workers as worker (worker.name)}
                    <li class="cap-worker">
                      <code class="cap-item__name">{worker.name}</code>
                      <span class="cap-item__desc ds-subtle">{worker.description}</span>
                    </li>
                  {/each}
                </ul>
              {/if}

              {#if wl.actions.length > 0}
                <p class="cap-workload__sub-heading">Actions</p>
                <ul class="cap-item-list">
                  {#each wl.actions as action (action.name)}
                    <li class="cap-action">
                      <span class="cap-item__name">{action.display_name}</span>
                      {#if action.requires_approval}
                        {' '}<span class="ds-pill ds-pill--warn cap-badge">needs approval</span>
                      {/if}
                    </li>
                  {/each}
                </ul>
              {/if}
            </div>
          </details>
        {/each}
      </section>

      <!-- Footer: IAM note + provenance -->
      <footer class="cap-footer ds-subtle">
        <p class="cap-footer__iam">{data.iam_note}</p>
        <p class="cap-footer__provenance">{data.provenance}</p>
      </footer>
    {/if}
  </div>
</details>

<style>
  .cap-card {
    padding: 0; /* summary + body own their padding */
  }

  .cap-summary {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--ds-sp-3);
    padding: var(--ds-sp-4) var(--ds-sp-5);
    cursor: pointer;
    list-style: none;
  }
  .cap-summary::-webkit-details-marker {
    display: none;
  }
  .cap-summary__title::before {
    content: '▸';
    display: inline-block;
    margin-right: var(--ds-sp-2);
    color: var(--ds-faint);
    transition: transform var(--ds-dur-fast) var(--ds-ease);
  }
  .cap-card[open] .cap-summary__title::before {
    transform: rotate(90deg);
  }
  .cap-summary__hint {
    font-size: var(--ds-fs-1);
    color: var(--ds-muted);
    font-style: italic;
  }

  .cap-body {
    padding: var(--ds-sp-4) var(--ds-sp-5) var(--ds-sp-5);
    border-top: 1px solid var(--ds-border);
  }

  .cap-loading {
    margin: var(--ds-sp-2) 0;
  }

  .cap-error-row {
    display: flex;
    align-items: center;
    gap: var(--ds-sp-3);
    padding: var(--ds-sp-3) 0;
  }
  .cap-retry {
    padding: 0.3em 0.85em;
    font-size: var(--ds-fs-1);
  }

  .cap-section {
    margin-bottom: var(--ds-sp-5);
  }
  .cap-section__heading {
    margin: 0 0 var(--ds-sp-3);
    font-size: var(--ds-fs-2);
    color: var(--ds-fg);
  }

  .cap-gate {
    margin-bottom: var(--ds-sp-3);
    padding: var(--ds-sp-3) var(--ds-sp-4);
    background: var(--ds-neutral-surface);
    border-radius: var(--ds-radius-sm);
    border: 1px solid var(--ds-border-strong);
  }
  .cap-gate__title {
    margin: 0 0 var(--ds-sp-1);
    font-size: var(--ds-fs-2);
  }
  .cap-gate__desc {
    margin: 0;
    font-size: var(--ds-fs-1);
  }

  .cap-denylist__summary {
    margin: 0 0 var(--ds-sp-3);
    font-size: var(--ds-fs-1);
  }
  .cap-rule-group {
    margin-bottom: var(--ds-sp-3);
  }
  .cap-rule-group__heading {
    margin: 0 0 var(--ds-sp-2);
    font-size: var(--ds-fs-1);
    font-weight: 600;
    color: var(--ds-muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .cap-rule-list {
    margin: 0 0 var(--ds-sp-2);
    padding-left: var(--ds-sp-4);
  }
  .cap-rule {
    margin-bottom: var(--ds-sp-2);
    font-size: var(--ds-fs-1);
  }
  .cap-rule__desc {
    color: var(--ds-fg);
  }
  .cap-rule__id {
    font-size: 0.75em;
    color: var(--ds-muted);
    background: var(--ds-neutral-surface);
    padding: 0.1em 0.35em;
    border-radius: var(--ds-radius-sm);
    border: 1px solid var(--ds-border-strong);
  }
  .cap-denylist__enforced {
    font-size: var(--ds-fs-1);
    margin-top: var(--ds-sp-2);
  }

  .cap-workload {
    margin-bottom: var(--ds-sp-3);
    border: 1px solid var(--ds-border-strong);
    border-radius: var(--ds-radius-sm);
    overflow: hidden;
  }
  .cap-workload__summary {
    display: flex;
    align-items: center;
    gap: var(--ds-sp-2);
    padding: var(--ds-sp-3) var(--ds-sp-4);
    cursor: pointer;
    list-style: none;
    background: var(--ds-neutral-surface);
  }
  .cap-workload__summary::-webkit-details-marker {
    display: none;
  }
  .cap-workload__name {
    font-weight: 600;
    font-size: var(--ds-fs-2);
  }
  .cap-workload__pill {
    font-size: var(--ds-fs-1);
  }
  .cap-workload__body {
    padding: var(--ds-sp-3) var(--ds-sp-4);
  }
  .cap-workload__desc {
    margin: 0 0 var(--ds-sp-3);
    font-size: var(--ds-fs-1);
  }
  .cap-workload__sub-heading {
    margin: var(--ds-sp-3) 0 var(--ds-sp-1);
    font-size: var(--ds-fs-1);
    font-weight: 600;
    color: var(--ds-muted);
  }

  .cap-item-list {
    margin: 0 0 var(--ds-sp-2);
    padding-left: var(--ds-sp-4);
  }
  .cap-tool,
  .cap-worker,
  .cap-action {
    margin-bottom: var(--ds-sp-2);
    font-size: var(--ds-fs-1);
  }
  .cap-item__name {
    font-weight: 600;
    margin-right: var(--ds-sp-1);
  }
  .cap-item__desc {
    margin-right: var(--ds-sp-1);
  }
  .cap-badge {
    font-size: var(--ds-fs-1);
    vertical-align: middle;
  }

  .cap-footer {
    margin-top: var(--ds-sp-4);
    padding-top: var(--ds-sp-3);
    border-top: 1px solid var(--ds-border);
    font-size: var(--ds-fs-1);
  }
  .cap-footer p {
    margin: 0 0 var(--ds-sp-2);
  }
  .cap-footer p:last-child {
    margin-bottom: 0;
  }
</style>
