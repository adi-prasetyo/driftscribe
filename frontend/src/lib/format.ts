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
