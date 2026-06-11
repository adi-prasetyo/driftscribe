<script lang="ts">
  import {
    safeApprovalHref,
    iacApprovalHref,
    isExpired,
    safeGithubHref,
    iacPrHref,
    resolvedIacPrNumbers,
    iacApproveLabel,
  } from '../lib/approval';
  import { shortSha, iacStatusLabel } from '../lib/format';
  import { groupRailDecisions, hasAnomalousStep, lifecycleSummaryLabel } from '../lib/rail';
  import type { Decision } from '../lib/types';

  let {
    decisions,
    activeTraceId,
    onOpenTrace,
  }: {
    decisions: Decision[];
    activeTraceId: string | null;
    onOpenTrace: (traceId: string) => void;
  } = $props();

  // PRs whose iac_apply has terminally `applied` — a `waiting_for_rebake` row
  // for one of these is superseded, so its CTA downgrades to view-only
  // (iacApproveLabel). Derived once per render from the list the rail holds.
  const resolvedPrs = $derived(resolvedIacPrNumbers(decisions));

  // Fold same-PR iac_apply docs into one group per PR. resolvedPrs stays derived
  // from the raw list (App.svelte noteApplied also reads the raw list) — only
  // the render loop changes.
  const railItems = $derived(groupRailDecisions(decisions));

  // Resolve the rollback approval link for a row, same-origin-guarded. Returns
  // the safe RELATIVE href, or null when there is no approval / it fails the
  // origin guard (off-origin, non-http(s), non-/approvals/ path).
  function approveHref(d: Decision): string | null {
    const raw = d.approval?.approval_url;
    return raw ? safeApprovalHref(raw) : null;
  }

  // Resolve the infra-apply approval link for a row. An iac_apply decision
  // carries a numeric `pr_number` (not an `approval` object, unlike rollback),
  // so we build the same-origin `/iac-approvals/<n>` path from it — gated on the
  // allowlisted action so we never construct a link from an unrelated decision.
  function iacApproveHref(d: Decision): string | null {
    return d.action === 'iac_apply' ? iacApprovalHref(d.pr_number) : null;
  }

  // Resolve the GitHub PR/issue link for a drift/docs decision. Gated on an
  // allowlisted `action` (so we never read github.url off an unrelated/iac
  // decision) AND host-allowlisted via safeGithubHref. Returns null otherwise.
  //
  // IMPORTANT: use Object.hasOwn, NOT the `in` operator — `'toString' in obj`
  // (and other prototype keys) is true, so `in` would let an unexpected action
  // string slip the gate (Codex review). Object.hasOwn is own-key-only.
  const GITHUB_LINK_LABEL: Record<string, string> = {
    drift_issue: 'View issue →',
    escalation: 'View issue →',
    docs_pr: 'View PR →',
    // `upgrade_pr` is NOT emitted by /recheck in this build (the upgrade
    // workload is unimplemented — agent/main.py:1139), so no such decision
    // currently persists a github.url. Listed for forward-compat only: it
    // renders nothing today and lights up automatically if a future build
    // starts persisting upgrade_pr decisions with a github.url.
    upgrade_pr: 'View PR →',
  };
  function githubHref(d: Decision): string | null {
    if (!Object.hasOwn(GITHUB_LINK_LABEL, d.action)) return null;
    return safeGithubHref(d.github?.url);
  }
  function githubLabel(d: Decision): string {
    return Object.hasOwn(GITHUB_LINK_LABEL, d.action)
      ? GITHUB_LINK_LABEL[d.action]
      : 'View on GitHub →';
  }

  // Render `created_at` as a compact, readable wall-clock string. Falls back to
  // the raw value when it doesn't parse, and to '' when absent.
  function fmtCreatedAt(iso: string | undefined): string {
    if (!iso) return '';
    const parsed = Date.parse(iso);
    if (Number.isNaN(parsed)) return iso;
    try {
      return new Intl.DateTimeFormat(undefined, {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      }).format(parsed);
    } catch {
      return iso;
    }
  }
</script>

{#snippet decisionCard(d: Decision, subtitle: string | undefined, isActive: boolean, lifecycle: Decision[] | null)}
  {@const prHref = iacPrHref(d)}
  <li
    class="decision-row"
    data-testid="past-decision-item"
    class:active={isActive}
  >
    <div class="row-summary">
      {#if prHref}
        <!-- iac_apply: the PR # IS the title and links to the GitHub PR
             (host-allowlisted via iacPrHref/safeGithubHref). -->
        <a
          class="row-action row-action-link"
          data-testid="decision-pr-link"
          href={prHref}
          target="_blank"
          rel="noopener noreferrer">PR #{d.pr_number} →</a>
      {:else}
        <span class="row-action" title={d.action}>{d.action}</span>
      {/if}
      {#if d.created_at}
        <time class="row-time" datetime={d.created_at}>{fmtCreatedAt(d.created_at)}</time>
      {/if}
    </div>

    {#if d.action === 'iac_apply' && subtitle}
      <!-- As-applied PR title (write-time snapshot, iac_apply only). Plain
           text — Svelte auto-escapes; CSS keeps it to one ellipsised line. -->
      <p class="row-subtitle" title={subtitle}>{subtitle}</p>
    {/if}

    {#if d.action === 'iac_apply'}
      {@const sha = shortSha(d.head_sha)}
      {@const st = iacStatusLabel(d.apply_status)}
      <p class="row-meta">iac_apply{#if st} · {st}{/if}{#if sha} · <span class="row-sha">⎇ {sha}</span>{/if}</p>
    {/if}

    {#if d.suppressed_by_autonomy === true}
      <span class="rail-status rail-status--muted" data-testid="autonomy-suppressed"
        >not executed — {d.autonomy_mode === 'observe' ? 'Observe' : d.autonomy_mode} mode</span>
    {/if}

    <div class="row-actions">
      {#if d.trace_id}
        <button
          class="open-trace-btn"
          data-testid="open-trace-button"
          type="button"
          onclick={() => onOpenTrace(d.trace_id as string)}
        >open trace →</button>
      {/if}

      {#if approveHref(d)}
        {@const href = approveHref(d)}
        {#if isExpired(d.approval?.expires_at)}
          <a class="past-approve-btn expired" aria-disabled="true">Approve →</a>
          <span class="expired-badge">expired</span>
        {:else}
          <a class="past-approve-btn" href={href} target="_blank" rel="noopener">Approve →</a>
        {/if}
      {/if}

      {#if iacApproveHref(d)}
        {@const iacHref = iacApproveHref(d)}
        <a
          class="past-approve-btn"
          data-testid="iac-approve-link"
          href={iacHref}
          target="_blank"
          rel="noopener">{iacApproveLabel(d, resolvedPrs)}</a>
      {/if}

      {#if githubHref(d)}
        {@const ghHref = githubHref(d)}
        <a
          class="past-approve-btn"
          data-testid="decision-github-link"
          href={ghHref}
          target="_blank"
          rel="noopener noreferrer">{githubLabel(d)}</a>
      {/if}
    </div>

    {#if lifecycle && lifecycle.length > 0}
      <!-- `open` is the initial state, fail-open for anomalous histories. On a
           /decisions refresh Svelte re-applies it only if the computed value
           changes, so an operator's manual collapse survives refreshes while
           the history stays anomalous (pinned by the rerender test). -->
      <details class="lifecycle" open={hasAnomalousStep(lifecycle)}>
        <!-- ONE expression — lifecycleSummaryLabel returns the complete string,
             so this seam has no whitespace to collapse and the exact-string
             test is safe by construction. -->
        <summary data-testid="iac-lifecycle-summary">{lifecycleSummaryLabel(lifecycle)}</summary>
        <ol class="lifecycle-steps">
          {#each [...lifecycle].reverse() as step (step.decision_id)}
            {@const stepStatus = iacStatusLabel(step.apply_status)}
            <li class="lifecycle-step" data-testid="iac-lifecycle-step">
              <!-- Three sibling ELEMENTS spaced by flex gap — no text-node
                   separators, hence no seam-gluing needed (grounding fact 10
                   applies only where text nodes meet). -->
              <span class="step-status">{stepStatus || 'status not recorded'}</span>
              {#if step.created_at}<time class="row-time" datetime={step.created_at}>{fmtCreatedAt(step.created_at)}</time>{/if}
              {#if step.trace_id}
                <button class="open-trace-btn" data-testid="lifecycle-open-trace" type="button"
                  onclick={() => onOpenTrace(step.trace_id as string)}>open trace →</button>
              {/if}
            </li>
          {/each}
        </ol>
      </details>
    {/if}
  </li>
{/snippet}

<aside id="decisions-rail" data-testid="past-decisions-pane" aria-label="Past decisions">
  <h2 class="ds-label">Past decisions</h2>

  {#if decisions.length === 0}
    <p class="empty ds-subtle">No decisions yet.</p>
  {:else}
    <ul id="decisions-list">
      {#each railItems as item (item.kind === 'group' ? 'g:' + item.pr : 's:' + item.d.decision_id)}
        {#if item.kind === 'single'}
          {@render decisionCard(item.d, item.d.pr_title, !!(item.d.trace_id && item.d.trace_id === activeTraceId), null)}
        {:else}
          {@render decisionCard(
            item.docs[0],
            item.docs.find((x) => x.pr_title)?.pr_title,
            item.docs.some((x) => x.trace_id && x.trace_id === activeTraceId),
            item.docs.slice(1),
          )}
        {/if}
      {/each}
    </ul>
  {/if}
</aside>

<style>
  #decisions-rail {
    display: flex;
    flex-direction: column;
    gap: var(--ds-sp-3);
    min-height: 0;
  }

  #decisions-rail > .ds-label {
    padding: 0 var(--ds-sp-1);
  }

  .empty {
    margin: var(--ds-sp-1) 0 0;
    padding: 0 var(--ds-sp-1);
    font-style: italic;
    color: var(--ds-faint);
  }

  #decisions-list {
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: var(--ds-sp-2);
    margin: 0;
    padding: 0;
    overflow-y: auto;
    min-height: 0;
  }

  /* --- A row: a calm card with a hairline + left-accent on .active --------- */
  .decision-row {
    position: relative;
    display: flex;
    flex-direction: column;
    gap: var(--ds-sp-2);
    padding: var(--ds-sp-3) var(--ds-sp-4);
    background: var(--ds-surface);
    border: 1px solid var(--ds-border);
    border-radius: var(--ds-radius);
    /* room for the accent stripe that the .active state paints */
    border-left: 3px solid transparent;
    transition:
      border-color var(--ds-dur) var(--ds-ease),
      background-color var(--ds-dur) var(--ds-ease),
      box-shadow var(--ds-dur) var(--ds-ease);
  }

  .decision-row:hover {
    background: var(--ds-surface-2);
    border-color: var(--ds-border-strong);
    box-shadow: var(--ds-shadow-sm);
  }

  .decision-row.active {
    border-left-color: var(--ds-stream);
    border-color: var(--ds-stream-border);
    background: var(--ds-stream-surface);
  }

  /* --- Summary line: action prominent, timestamp small/muted -------------- */
  .row-summary {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--ds-sp-3);
    min-width: 0;
  }

  .row-action {
    font-size: var(--ds-fs-2);
    font-weight: var(--ds-fw-semibold);
    color: var(--ds-fg);
    line-height: var(--ds-lh-snug);
    /* keep long action strings on one tidy line */
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    min-width: 0;
  }

  /* iac_apply title rendered as an external PR link — inherits the prominent
     .row-action type, adds the stream-ink link affordance. */
  .row-action-link {
    color: var(--ds-stream-ink);
    text-decoration: none;
    transition: color var(--ds-dur-fast) var(--ds-ease);
  }

  .row-action-link:hover {
    color: var(--ds-stream);
    text-decoration: underline;
  }

  .row-time {
    flex: 0 0 auto;
    font-size: var(--ds-fs-1);
    color: var(--ds-muted);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  /* As-applied PR title — the human-readable subtitle. One tidy ellipsised line. */
  .row-subtitle {
    margin: 0;
    font-size: var(--ds-fs-1);
    color: var(--ds-fg);
    line-height: var(--ds-lh-snug);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* Meta line: the action tag + short commit SHA, muted and small. */
  .row-meta {
    margin: 0;
    font-size: var(--ds-fs-1);
    color: var(--ds-muted);
  }

  .row-sha {
    font-family: var(--ds-font-mono, ui-monospace, monospace);
    font-variant-numeric: tabular-nums;
  }

  /* --- The action affordances -------------------------------------------- */
  .row-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--ds-sp-2) var(--ds-sp-3);
  }

  .open-trace-btn {
    appearance: none;
    border: none;
    background: none;
    padding: 0;
    margin: 0;
    cursor: pointer;
    font-size: var(--ds-fs-1);
    font-weight: var(--ds-fw-semibold);
    color: var(--ds-stream-ink);
    line-height: 1.4;
    transition: color var(--ds-dur-fast) var(--ds-ease);
  }

  .open-trace-btn:hover {
    color: var(--ds-stream);
    text-decoration: underline;
  }

  .past-approve-btn {
    display: inline-flex;
    align-items: center;
    font-size: var(--ds-fs-1);
    font-weight: var(--ds-fw-semibold);
    color: var(--ds-ok-ink);
    line-height: 1.4;
    text-decoration: none;
    transition: color var(--ds-dur-fast) var(--ds-ease);
  }

  .past-approve-btn:hover {
    color: var(--ds-ok);
    text-decoration: underline;
  }

  /* expired approval: inert, struck through, no pointer affordance */
  .past-approve-btn.expired {
    color: var(--ds-faint);
    text-decoration: line-through;
    pointer-events: none;
    cursor: not-allowed;
  }

  .expired-badge {
    display: inline-flex;
    align-items: center;
    padding: 0.1em 0.55em;
    border-radius: var(--ds-radius-pill);
    background: var(--ds-neutral-surface);
    border: 1px solid var(--ds-border);
    color: var(--ds-muted);
    font-size: 0.6875rem; /* 11px — finer than the meta scale */
    font-weight: var(--ds-fw-semibold);
    text-transform: uppercase;
    letter-spacing: var(--ds-tracking-caps);
    line-height: 1.3;
  }

  /* --- Lifecycle expander: earlier iac_apply steps on demand -------------- */

  /* Hairline separator above the expander so it reads as subordinate to the
     card body — same ds-border token the card itself uses. */
  .lifecycle {
    border-top: 1px solid var(--ds-border);
    padding-top: var(--ds-sp-2);
  }

  /* Summary styled as a muted, small affordance — the operator knows they can
     click it without a large prominent CTA competing with the card. */
  .lifecycle > summary {
    font-size: var(--ds-fs-1);
    color: var(--ds-muted);
    cursor: pointer;
    list-style: none;
    user-select: none;
  }
  .lifecycle > summary::-webkit-details-marker {
    display: none;
  }
  /* the same custom disclosure caret InfraDiagram/CapabilityCard render —
     rotates when open */
  .lifecycle > summary::before {
    content: '▸';
    display: inline-block;
    margin-right: var(--ds-sp-2);
    color: var(--ds-faint);
    transition: transform var(--ds-dur-fast) var(--ds-ease);
  }
  .lifecycle[open] > summary::before {
    transform: rotate(90deg);
  }

  .lifecycle-steps {
    list-style: none;
    margin: var(--ds-sp-2) 0 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--ds-sp-1);
  }

  /* Step rows: status + time + trace button as flex siblings — the gap IS the
     separator, no text nodes between elements (grounding fact 10). */
  .lifecycle-step {
    display: flex;
    align-items: baseline;
    gap: var(--ds-sp-2);
    font-size: var(--ds-fs-1);
    color: var(--ds-muted);
  }

  .step-status {
    flex: 0 0 auto;
  }

  /* Autonomy-suppressed status token — muted/pending style (not alarm).
     "Would have" rows are the Observe mode working as designed. */
  .rail-status {
    display: inline-flex;
    align-items: center;
    padding: 0.1em 0.55em;
    border-radius: var(--ds-radius-pill);
    font-size: 0.6875rem; /* 11px — same as expired-badge */
    font-weight: var(--ds-fw-semibold);
    text-transform: uppercase;
    letter-spacing: var(--ds-tracking-caps);
    line-height: 1.3;
  }

  .rail-status--muted {
    background: var(--ds-neutral-surface);
    border: 1px solid var(--ds-border);
    color: var(--ds-muted);
  }
</style>
