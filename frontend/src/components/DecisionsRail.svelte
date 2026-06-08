<script lang="ts">
  import { safeApprovalHref, iacApprovalHref, isExpired, safeGithubHref } from '../lib/approval';
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

  // The link text reflects whether the row is still ACTIONABLE. Only a
  // `waiting_for_rebake` create-class decision still needs an operator click
  // (the second, post-rebake Apply); for applied/failed/terminal history rows
  // the page is view-only, so use neutral wording (Codex review: avoid a stale
  // "Review & approve" label on a decision that is already resolved).
  function iacApproveLabel(d: Decision): string {
    return d.apply_status === 'waiting_for_rebake'
      ? 'Review & approve →'
      : 'Open approval page →';
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

<aside id="decisions-rail" data-testid="past-decisions-pane" aria-label="Past decisions">
  <h2 class="ds-label">Past decisions</h2>

  {#if decisions.length === 0}
    <p class="empty ds-subtle">No decisions yet.</p>
  {:else}
    <ul id="decisions-list">
      {#each decisions as d (d.decision_id)}
        <li
          class="decision-row"
          data-testid="past-decision-item"
          class:active={d.trace_id && d.trace_id === activeTraceId}
        >
          <div class="row-summary">
            <span class="row-action" title={d.action}>{d.action}</span>
            {#if d.created_at}
              <time class="row-time" datetime={d.created_at}>{fmtCreatedAt(d.created_at)}</time>
            {/if}
          </div>

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
                rel="noopener">{iacApproveLabel(d)}</a>
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
        </li>
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

  .row-time {
    flex: 0 0 auto;
    font-size: var(--ds-fs-1);
    color: var(--ds-muted);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
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
</style>
