<script lang="ts">
  // CapabilityCard — "What this agent can — and cannot — do", the agent's safety
  // cage in plain language for ClickOps operators.
  //
  // BODY ONLY. It used to be a collapsed <details> squatting above the chat
  // transcript, lazily fetching on first open. ds-jns moved it behind a link on
  // the empty chat's front door, and deleting that inline mount left the modal
  // as its only consumer — so the disclosure went too. A modal opened BY a link
  // that names what it will show must not contain a second thing to click.
  //
  // Design:
  //  - Fetches GET /capabilities on MOUNT. The host is expected to mount this
  //    only while it is showing (Modal renders children under {#if open}), so a
  //    reopen refetches rather than reusing a previous instance's cache. One
  //    small GET on an explicit operator action, and it buys what a lifetime
  //    cache would cost: a first open that failed comes back clean on the
  //    second, without the operator having to find the Retry button.
  //  - Render order is anxiety-first: gates → denylist → workloads.
  //  - Headings are h3/h4, not h2/h3: the host spends an h2 on its own title
  //    (the Modal does), so h2 sections would be its SIBLINGS rather than its
  //    contents. Page h1 → modal h2 → these. No skipped levels.
  //  - The `call` prop is the same token-aware fetch wrapper as InfraDiagram.

  import { onMount } from 'svelte';
  import {
    groupRules,
    categoryHeading,
    gateTitle,
    gateDescription,
    ruleDescription,
    toolDescription,
    workerDescription,
    actionDisplayName,
    adoptableTypeLabel,
    type Capabilities,
  } from '../lib/capabilities';
  import { parseWorkloadPrompts } from '../lib/prompts';
  import type { WorkloadPrompts } from '../lib/prompts';
  import { crewLifecycle, crewDescriptor, type Workload } from '../lib/workloads';
  import { t, type TranslateFn } from '../lib/i18n';
  import Icon from './Icon.svelte';
  import CrewGlyph from './CrewGlyph.svelte';

  /** The crew's place in the stewardship loop, keyed by the frozen symbolic
   *  workload name. Pure copy, not a safety claim — the gates section above is
   *  the authority on what waits for approval. An unrecognized future workload
   *  has no lifecycle key, so its row is hidden ('' → the {#if} guard) instead
   *  of rendering a raw catalog key. */
  function loopRole(name: string, t: TranslateFn): string {
    return KNOWN_WORKLOADS.has(name as Workload) ? crewLifecycle(name as Workload, t) : '';
  }

  // Frozen symbolic workload values (agent/workloads/spec.py's name Literal).
  // wl.descriptor (the DTO field) localizes via the shared crew-descriptor
  // catalog for these four; an unrecognized future workload name falls back
  // to the DTO's own English descriptor rather than calling crewDescriptor
  // with an id it has no key for.
  const KNOWN_WORKLOADS: ReadonlySet<Workload> = new Set(['drift', 'upgrade', 'explore', 'provision']);
  function workloadDescriptor(wl: { name: string; descriptor: string }, tf: TranslateFn): string {
    return KNOWN_WORKLOADS.has(wl.name as Workload)
      ? crewDescriptor(wl.name as Workload, tf)
      : wl.descriptor;
  }

  let {
    call,
    autonomyNote = null,
  }: {
    /** App's token-aware fetch wrapper. */
    call: (path: string, init?: RequestInit) => Promise<Response>;
    /** Autonomy-mode note, derived by App from the shared autonomyStore via
     *  autonomyNoteFor(); null = render nothing (loading/unknown/propose_apply).
     *  The card is a dumb renderer — best-effort silence lives in the selector. */
    autonomyNote?: string | null;
  } = $props();

  let data = $state<Capabilities | null>(null);
  let loading = $state(false);
  let fetchError = $state(false);
  let fetched = $state(false);

  /** Structural check on the four load-bearing DTO keys. A 200 with valid
   *  JSON but missing structure must route to the error/retry path: Svelte 5
   *  has no error boundary, so letting the template iterate a missing array
   *  would blank the panel with no error row and no way to re-attempt. */
  function isValidCapabilities(body: unknown): body is Capabilities {
    if (typeof body !== 'object' || body === null) return false;
    const b = body as Record<string, unknown>;
    return (
      typeof b.version === 'number' &&
      Array.isArray(b.workloads) &&
      Array.isArray(b.human_gates) &&
      typeof b.denylist === 'object' &&
      b.denylist !== null &&
      Array.isArray((b.denylist as Record<string, unknown>).rules)
    );
  }

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
      let body: unknown;
      try {
        body = await resp.json();
      } catch {
        fetchError = true;
        return;
      }
      if (!isValidCapabilities(body)) {
        // fetched stays false → the Retry button (and a future toggle) can
        // re-attempt; data stays null so no half-rendered sections.
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

  // Mount IS the open — see the header note. onMount rather than an $effect:
  // this must run exactly once per instance, and an effect that writes the same
  // state it would have to read to guard itself is a loop waiting to happen.
  onMount(() => {
    void fetchCapabilities();
  });

  // Per-crew lazy prompt state — keyed by workload name.
  // A fetch in flight (promptLoading[name]) blocks duplicate calls; a prior
  // error does NOT block — closing and reopening the disclosure retries
  // (transient failures shouldn't require a page reload).
  let promptsByName = $state<Record<string, WorkloadPrompts>>({});
  let promptLoading = $state<Record<string, boolean>>({});
  let promptError = $state<Record<string, boolean>>({});

  async function onPromptsToggle(name: string, el: HTMLDetailsElement): Promise<void> {
    if (!el.open) return;
    if (promptsByName[name] || promptLoading[name]) return;
    promptLoading = { ...promptLoading, [name]: true };
    promptError = { ...promptError, [name]: false };
    try {
      const resp = await call('/workloads/' + encodeURIComponent(name) + '/prompts');
      if (!resp.ok) { promptError = { ...promptError, [name]: true }; return; }
      const parsed = parseWorkloadPrompts(await resp.json());
      if (!parsed) { promptError = { ...promptError, [name]: true }; return; }
      promptsByName = { ...promptsByName, [name]: parsed };
    } catch {
      promptError = { ...promptError, [name]: true };
    } finally {
      promptLoading = { ...promptLoading, [name]: false };
    }
  }

  // Defensive ?? []: isValidCapabilities already guarantees rules is an
  // array, but a throw inside a $derived has no error boundary to catch it.
  const ruleGroups = $derived(groupRules(data?.denylist?.rules ?? []));
</script>

<!-- Not a .ds-card since ds-7ag.5: this explains what the agent can and cannot
     do, which is reference material, and as a boxed card it competed with the
     composer and the reasoning timeline. The host now supplies the frame (the
     Modal's surface, border, radius and padding), so there is no box here at
     all — a well drawn a few pixels inside the dialog's own would be exactly
     the boxed-in-a-box look .ds-card was demoted for.
     `capability-card` stays as the root hook: consumers asking "is the panel
     up?" have always asked it that way. -->
<div class="cap-card" data-testid="capability-card">
  {@render capBody()}
</div>

{#snippet capBody()}
  <div class="cap-body">
    {#if loading && !data}
      <p class="ds-subtle cap-loading">{$t('common.loading')}</p>
    {:else if fetchError}
      <div class="cap-error-row" data-testid="cap-error">
        <span class="ds-note">{$t('capability.error.load')}</span>
        <button
          class="ds-btn ds-btn--ghost cap-retry"
          type="button"
          data-testid="cap-retry"
          onclick={() => void retry()}
        >{$t('common.retry')}</button>
      </div>
    {:else if data}
      <!-- 1. Gates — anxiety-first: operator wants to know what requires their approval -->
      <section class="cap-section" data-testid="cap-gates" aria-labelledby="cap-gates-heading">
        <h3 class="cap-section__heading" id="cap-gates-heading">{$t('capability.gates.heading')}</h3>
        {#each data.human_gates as gate (gate.id)}
          <div class="cap-gate">
            <p class="cap-gate__title"><strong>{gateTitle(gate, $t)}</strong></p>
            <p class="cap-gate__desc ds-subtle">{gateDescription(gate, $t)}</p>
          </div>
        {/each}
      </section>

      <!-- 2. Denylist — blocked outright, approval cannot override -->
      <section class="cap-section" data-testid="cap-denylist" aria-labelledby="cap-denylist-heading">
        <h3 class="cap-section__heading" id="cap-denylist-heading">{$t('capability.denylist.heading')}</h3>
        <p class="ds-subtle cap-denylist__summary">{data.denylist.summary}</p>
        {#each ruleGroups as group (group.category)}
          <div class="cap-rule-group">
            <h4 class="cap-rule-group__heading">{categoryHeading(group.category, $t)}</h4>
            <ul class="cap-rule-list">
              {#each group.rules as rule (rule.id)}
                <li class="cap-rule">
                  <span class="cap-rule__desc">{ruleDescription(rule, $t)}</span>
                  {' '}<code class="cap-rule__id">{rule.id}</code>
                </li>
              {/each}
            </ul>
          </div>
        {/each}
        {#if data.denylist.adoptable_resource_types?.length}
          <p class="ds-subtle cap-denylist__adoptable">
            {$t('capability.denylist.adoptableTypes', {
              list: data.denylist.adoptable_resource_types.map((entry) => adoptableTypeLabel(entry, $t)).join(', '),
            })}
          </p>
        {/if}
        <p class="ds-subtle cap-denylist__enforced">
          {$t('capability.denylist.enforcedAt', { list: data.denylist.enforced_at.join(' → ') })}
        </p>
      </section>

      <!-- Autonomy mode note (Task 10) — shown when the dial is below
           propose_apply; absent for propose_apply and on fetch failure. -->
      {#if autonomyNote}
        <p class="cap-autonomy-note ds-subtle" data-testid="capability-autonomy-note"
          >{autonomyNote}</p>
      {/if}

      <!-- 3. Workloads — what each workload can use -->
      <section class="cap-section" data-testid="cap-workloads" aria-labelledby="cap-workloads-heading">
        <h3 class="cap-section__heading" id="cap-workloads-heading">{$t('capability.workloads.heading')}</h3>
        {#each data.workloads as wl (wl.name)}
          <details class="cap-workload">
            <summary
              class="cap-workload__summary"
              data-testid="cap-workload-{wl.name}-summary"
            >
              <!-- The crew agent's verb, as a small looping glyph (decorative;
                   aria-hidden). `verb` is the frozen symbolic value (wl.name),
                   not the display name. It slots in as the first flex item; the
                   text seams below are untouched. -->
              <CrewGlyph verb={wl.name} />
              <!-- Crew identity + domain descriptor + autonomy pill. Each seam
                   is glued with an explicit {' '} (the Svelte-5 whitespace
                   gotcha, PR #83 lesson): the rendered text is exactly
                   "<display_name> — <descriptor> <pill>", pinned by the
                   glued-exact-string test. The pill vocabulary is the honest
                   one — only a wired trigger reads "Autonomous". The descriptor
                   itself localizes via the shared crew-descriptor catalog
                   (workloads.ts::crewDescriptor), not the raw DTO field. -->
              <span class="cap-workload__name">{wl.display_name}</span>{#if wl.descriptor}<span
                class="cap-workload__descriptor">{' '}— {workloadDescriptor(wl, $t)}</span>{/if}{' '}<span
                class="ds-pill {wl.autonomous ? 'ds-pill--ok' : 'ds-pill--muted'} cap-workload__pill"
                >{wl.autonomous ? $t('capability.pill.autonomous') : $t('capability.pill.onDemand')}</span>
            </summary>
            <div class="cap-workload__body">
              <p class="ds-subtle cap-workload__desc">{wl.description}</p>

              {#if loopRole(wl.name, $t)}
                <p
                  class="cap-workload__loop ds-subtle"
                  data-testid="cap-workload-{wl.name}-loop"
                ><span class="cap-workload__loop-label">{$t('capability.workload.loopLabel')}</span> {loopRole(wl.name, $t)}</p>
              {/if}

              {#if wl.tools.length > 0}
                <p class="cap-workload__sub-heading">{$t('capability.workload.tools')}</p>
                <ul class="cap-item-list">
                  {#each wl.tools as tool (tool.name)}
                    <li
                      class="cap-tool"
                      data-testid="cap-tool-{tool.name}"
                    >
                      <code class="cap-item__name">{tool.name}</code>
                      <span class="cap-item__desc ds-subtle">{toolDescription(tool, $t)}</span>
                      {' '}<span
                        class="ds-pill cap-badge {tool.write_capable ? 'ds-pill--warn' : 'ds-pill--muted'}"
                      >{tool.write_capable ? $t('capability.badge.writeCapable') : $t('capability.badge.read')}</span>
                    </li>
                  {/each}
                </ul>
              {/if}

              {#if wl.workers.length > 0}
                <p class="cap-workload__sub-heading">{$t('capability.workload.workers')}</p>
                <ul class="cap-item-list">
                  {#each wl.workers as worker (worker.name)}
                    <li class="cap-worker">
                      <code class="cap-item__name">{worker.name}</code>
                      <span class="cap-item__desc ds-subtle">{workerDescription(worker, $t)}</span>
                    </li>
                  {/each}
                </ul>
              {/if}

              {#if wl.actions.length > 0}
                <p class="cap-workload__sub-heading">{$t('capability.workload.actions')}</p>
                <ul class="cap-item-list">
                  {#each wl.actions as action (action.name)}
                    <li class="cap-action">
                      <span class="cap-item__name">{actionDisplayName(action, $t)}</span>
                      {#if action.requires_approval}
                        {' '}<span class="ds-pill ds-pill--warn cap-badge">{$t('capability.badge.needsApproval')}</span>
                      {/if}
                    </li>
                  {/each}
                </ul>
              {/if}

              <details
                class="ds-disclosure cap-workload__prompts"
                data-testid="cap-workload-{wl.name}-prompts"
                ontoggle={(e) => onPromptsToggle(wl.name, e.currentTarget as HTMLDetailsElement)}
              >
                <summary class="cap-workload__prompts-summary">
                  <Icon name="file-text" size={14} /> {wl.name === 'drift' || wl.name === 'upgrade' ? $t('capability.workload.viewPrompts') : $t('capability.workload.viewPrompt')}
                </summary>
                {#if promptError[wl.name]}
                  <p class="ds-subtle">{$t('capability.prompts.unavailable')}</p>
                {:else if promptsByName[wl.name]}
                  {@const p = promptsByName[wl.name]}
                  <p class="ds-subtle" data-testid="cap-workload-{wl.name}-prompts-note">{p.demo_note}</p>
                  <p class="ds-subtle">{$t('capability.prompts.runningArtifact')} <code class="ds-code">{p.source_dir}</code> @ <code class="ds-code">{p.revision}</code></p>
                  <div class="ds-field"><span class="ds-label">{$t('capability.prompts.recheckLabel')}</span></div>
                  <pre class="ds-pre cap-prompt-pre">{p.recheck_prompt}</pre>
                  {#if p.chat_prompt_distinct && p.chat_prompt}
                    <div class="ds-field"><span class="ds-label">{$t('capability.prompts.chatLabel')}</span></div>
                    <pre class="ds-pre cap-prompt-pre">{p.chat_prompt}</pre>
                  {:else}
                    <p class="ds-subtle">{$t('capability.prompts.noSeparateChat')}</p>
                  {/if}
                {:else}
                  <p class="ds-subtle">{$t('common.loading')}</p>
                {/if}
              </details>
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
{/snippet}

<style>
  .cap-card {
    padding: 0;
    margin: 0;
  }

  /* No padding, no well, no radius: the host's frame is the only frame. The
     inner section layout below is untouched — it is the surface around it that
     went away, not the arrangement inside. */
  .cap-body {
    padding: 0;
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

  /* Autonomy note — calm informational line above the workloads section */
  .cap-autonomy-note {
    margin: 0 0 var(--ds-sp-4);
    font-size: var(--ds-fs-1);
    padding: var(--ds-sp-2) var(--ds-sp-3);
    background: var(--ds-neutral-surface);
    border-radius: var(--ds-radius-sm);
    border: 1px solid var(--ds-border-strong);
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
  /* ds-2fp: the worst instance found. This summary fills its <details> exactly,
     and `.cap-workload` is overflow:hidden (it rounds this filled row against
     the container's border), so the global outward ring was cut on ALL FOUR
     sides — a keyboard user tabbing the workload list saw nothing at all.
     Same remedy as the autonomy dial: put the ring inside. --ds-neutral-surface
     (#efeeea) reads 4.854:1 against --ds-stream-ink, so the on-light precondition
     holds; contrast.test.ts proves it for every consumer of the token, not just
     the first one. */
  .cap-workload__summary:focus-visible {
    outline: var(--ds-ring-inset-on-light);
    outline-offset: -3px;
    box-shadow: none;
  }
  .cap-workload__summary::-webkit-details-marker {
    display: none;
  }
  .cap-workload__name {
    font-weight: 600;
    font-size: var(--ds-fs-2);
  }
  .cap-workload__descriptor {
    font-weight: 400;
    font-size: var(--ds-fs-2);
    color: var(--ds-muted);
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
  /* Stewardship-loop role — a calm one-liner placing the crew in the loop,
     sitting just under its description. */
  .cap-workload__loop {
    margin: 0 0 var(--ds-sp-3);
    font-size: var(--ds-fs-1);
  }
  .cap-workload__loop-label {
    color: var(--ds-muted);
    font-weight: 600;
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

  /* Prompt disclosure — cap height so a long prompt scrolls rather than
     dominating the workload card. */
  .cap-prompt-pre { max-height: 24rem; overflow-y: auto; }
</style>
