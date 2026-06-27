// format.ts — small, pure display formatters for the operator UI.
//
// These mirror the strings the legacy single-file renderer produced
// (agent/templates/transparency.html): trace pills show the first 8 chars
// (`traceId.slice(0, 8)`), truncation uses the ellipsis character "…"
// (U+2026), and result_preview is capped at 2000 chars by the backend.

const ELLIPSIS = '…';
const DEFAULT_PREVIEW_MAX = 2000;

/**
 * Render an LLM token total as a human string, e.g. `"1,234 tok"`.
 * Returns `""` when the total is null/undefined/absent. A total of 0 is a
 * present value and renders as `"0 tok"`.
 */
export function fmtTokens(usage: { total_token_count?: number | null }): string {
  const total = usage?.total_token_count;
  if (total === null || total === undefined) return '';
  return `${total.toLocaleString('en-US')} tok`;
}

/**
 * First 8 characters of a trace id (for the trace pill). Safe on short,
 * empty, or null/undefined input.
 */
export function shortTrace(traceId: string): string {
  if (!traceId) return '';
  return traceId.slice(0, 8);
}

/**
 * Short commit SHA for the decision-rail meta line — first 7 chars (the
 * conventional abbreviated-SHA length). Safe on empty / null / non-string input
 * (returns ''), so a row without a `head_sha` simply renders no SHA.
 */
export function shortSha(headSha: string | null | undefined): string {
  if (typeof headSha !== 'string' || !headSha) return '';
  return headSha.slice(0, 7);
}

/**
 * Clamp a preview string to `max` chars (default 2000), appending an ellipsis
 * when the input was actually truncated. Safe on null/undefined input.
 */
export function fmtPreview(s: string, max: number = DEFAULT_PREVIEW_MAX): string {
  if (!s) return '';
  if (s.length <= max) return s;
  return s.slice(0, max) + ELLIPSIS;
}

/**
 * Human label for an iac_apply row's `apply_status`, for the rail meta line.
 * Known statuses get a readable phrase (the known set mirrors decision.ts's
 * APPLY_STATUS_BADGE keys — applied/failed/failed_state_suspect/ambiguous —
 * plus waiting_for_rebake). An unrecognised non-empty status passes through
 * CLAMPED to 40 chars + '…' if longer (forward-compat — our own small backend
 * enum, but the decision doc is unredacted so we cap length, matching
 * decision.ts's defensive style). null/undefined/'' → '' (the meta line then
 * omits the token).
 */
const IAC_STATUS_LABELS: Record<string, string> = {
  applied: 'applied',
  // "rebuild" not "re-bake": the operator-facing label uses plain language —
  // the internal enum stays `waiting_for_rebake`. The help text (iacStatusHelp)
  // explains rebuild-of-what (the apply worker, from merged code).
  waiting_for_rebake: 'awaiting rebuild',
  failed: 'failed',
  failed_state_suspect: 'failed (state suspect)',
  ambiguous: 'ambiguous',
};
const IAC_STATUS_MAX = 40; // a status enum is tiny; cap an unexpected value hard
export function iacStatusLabel(status: string | null | undefined): string {
  if (typeof status !== 'string' || status === '') return '';
  const known = IAC_STATUS_LABELS[status];
  if (known) return known;
  return status.length > IAC_STATUS_MAX ? status.slice(0, IAC_STATUS_MAX) + ELLIPSIS : status;
}

/**
 * Plain-language help for the iac_apply statuses a non-engineer operator can't
 * decode from the label alone. Surfaced as the HelpHint tooltip/accessible
 * description next to the status token (DecisionsRail face-meta + lifecycle
 * steps). The self-evident status `applied` and unknown values return null →
 * no help affordance is rendered. Keyed on the raw backend enum, the
 * same input iacStatusLabel takes.
 */
const IAC_STATUS_HELP: Record<string, string> = {
  // Accurate for BOTH waiting_for_rebake variants — recorded with
  // merge_state="pending" (before the irreversible merge / kept on merge
  // failure) AND merge_state="merged" (after) — so it must NOT assert the merge
  // already happened (agent/main.py records the pending pointer pre-merge).
  waiting_for_rebake:
    'Create/adopt changes apply in two steps: the PR is merged, then the ' +
    "agent's apply worker is rebuilt from the merged code and re-checks the " +
    "plan before applying. A later 'applied' step confirms completion.",
  // Plain `failed` (NOT the state-suspect variant): the apply aborted but the
  // tofu-apply worker PROVED the live state stayed clean (TofuStepError, vs
  // ApplyStateSuspect's "may be mutated"). We deliberately do NOT point the
  // operator at the underlying OpenTofu error: the worker captures stderr
  // (capture_output) and persists only a 500-char tail to the isolated
  // apply-audit, so it is surfaced nowhere operator-facing — promising a
  // location (logs or /trace) would be false.
  failed:
    "The apply didn't complete, but DriftScribe verified your live infrastructure " +
    'was left unchanged, so it is safe to fix the cause and retry. (Unlike "failed ' +
    '(state suspect)", the state was proven clean.)',
  failed_state_suspect:
    "The apply didn't finish cleanly and the live infrastructure state may have " +
    'changed (or a lock was held), so the result is uncertain. Re-running ' +
    're-checks the live state before retrying.',
  ambiguous:
    "DriftScribe couldn't confirm the final result of this apply (e.g. the change " +
    'merged but the apply outcome was unclear). Open the trace to see what ' +
    'happened before retrying.',
};
export function iacStatusHelp(status: string | null | undefined): string | null {
  if (typeof status !== 'string') return null;
  return IAC_STATUS_HELP[status] ?? null;
}

/**
 * Friendly headline label for a decision's `action`, shown on the rail's
 * non-iac rows (the `{:else}` branch). Today only `no_op` is remapped — from
 * the bare backend enum to plain language — because that row produces no
 * GitHub side effect and so has no "View PR/issue →" CTA to give it context;
 * the operator just sees a token. Every other action passes through verbatim
 * (those rows carry their own CTA). Defensively clamps an unexpected long value
 * to 40 chars + '…', matching iacStatusLabel's forward-compat style.
 * null/undefined/'' → '' (the caller then renders nothing).
 */
const DECISION_ACTION_LABELS: Record<string, string> = {
  no_op: 'No action needed',
};
const DECISION_ACTION_MAX = 40;
export function decisionActionLabel(action: string | null | undefined): string {
  if (typeof action !== 'string' || action === '') return '';
  const known = DECISION_ACTION_LABELS[action];
  if (known) return known;
  return action.length > DECISION_ACTION_MAX
    ? action.slice(0, DECISION_ACTION_MAX) + ELLIPSIS
    : action;
}

/**
 * Plain-language help for a decision `action` a non-engineer can't decode from
 * the label alone. Today only `no_op` — the "checked, all clear, nothing to
 * fix" receipt that surprises operators by appearing in the log when nothing
 * visibly happened: it means the live state already matched the contract, so
 * no PR / issue / rollback was created, and the row is the record that the
 * check ran. Returns null for every other action (and for null/undefined/'')
 * → no help affordance is rendered. Keyed on the raw backend enum, the same
 * input decisionActionLabel takes.
 */
const DECISION_ACTION_HELP: Record<string, string> = {
  no_op:
    'DriftScribe checked and the live state already matched what was expected, ' +
    'so there was nothing to fix — no pull request, issue, or rollback was needed. ' +
    'This entry is the record that the check ran and found everything in order.',
};
export function decisionActionHelp(action: string | null | undefined): string | null {
  if (typeof action !== 'string') return null;
  return DECISION_ACTION_HELP[action] ?? null;
}

/**
 * Render an ISO timestamp as a readable absolute wall-clock string with the
 * year (used by the DecisionSummary card — a historical decision can be from
 * any date, so unlike the rail's compact no-year form we include the year).
 * Falls back to the raw value when it doesn't parse, and to '' when absent.
 */
export function fmtWhen(iso: string): string {
  if (!iso) return '';
  const parsed = Date.parse(iso);
  if (Number.isNaN(parsed)) return iso;
  try {
    return new Intl.DateTimeFormat(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    }).format(parsed);
  } catch {
    return iso;
  }
}
