// SECURITY-CRITICAL. Same-origin guard for HITL approval links, ported
// verbatim-in-spirit from the legacy `_safeApprovalHref` renderer guard in
// `agent/templates/transparency.html` (~lines 1000-1055). These functions
// re-home the security assertions from
// `tests/integration/test_ui_transparency.py:148-166`.
//
// Rationale (see the legacy block comment): the rollback worker emits ABSOLUTE
// approval URLs (`https://<coordinator>/approvals/<id>`), while operators may
// also receive RELATIVE ones (`/approvals/<id>?t=<token>`). Both must be
// accepted, but ONLY when they resolve to the current page's origin and target
// the `/approvals/` path — so an attacker-shaped result cannot open-redirect the
// operator or smuggle a `javascript:` / `data:` URL into an anchor `href`. We
// return only the RELATIVE href (pathname + search), never the absolute URL, so
// the DOM never carries an off-origin attacker-controlled string even as text.

/**
 * Resolve `raw` against `origin ?? window.location.origin` and accept it ONLY
 * if (a) the resolved origin equals the base origin, (b) the protocol is
 * http/https, and (c) the pathname starts with `/approvals/`. Returns the
 * RELATIVE href (`pathname + search`) on success, or `null` if rejected.
 *
 * Rejects: off-origin absolute URLs, non-http(s) schemes (`javascript:`,
 * `data:`, `file:`, …), non-`/approvals/` paths, and empty/malformed input.
 */
export function safeApprovalHref(raw: string, origin?: string): string | null {
  const base = origin ?? window.location.origin;
  let baseOrigin: string;
  try {
    // Normalise the base so the origin comparison is apples-to-apples even if
    // a full base URL (rather than a bare origin) is passed in.
    baseOrigin = new URL(base).origin;
  } catch {
    return null;
  }
  try {
    const u = new URL(raw, base);
    if (u.origin !== baseOrigin) return null;
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
    if (!u.pathname.startsWith('/approvals/')) return null;
    return u.pathname + u.search;
  } catch {
    return null;
  }
}

/**
 * Build the same-origin relative approval href for an infra (IaC) decision from
 * its numeric PR number: `/iac-approvals/<n>` for a positive integer, else null.
 *
 * Unlike `safeApprovalHref` (which validates an arbitrary URL string), this
 * takes ONLY a number and constructs the path itself — so there is no host, no
 * scheme, and no attacker-controlled URL to parse. It is inherently same-origin
 * and immune to open-redirect / `javascript:` smuggling. This is the deliberate
 * data path for IaC approvals: callers derive it from an allowlisted
 * `action === 'iac_apply'` decision's `pr_number`, never by reading a raw URL
 * field off an unredacted decision doc.
 */
export function iacApprovalHref(prNumber: unknown): string | null {
  if (
    typeof prNumber !== 'number' ||
    !Number.isInteger(prNumber) ||
    prNumber <= 0
  ) {
    return null;
  }
  return `/iac-approvals/${prNumber}`;
}

/**
 * Returns `true` if `expiresAtIso` parses to a time at or before `now`
 * (defaults to the current epoch-ms clock). Fail-safe: if `expiresAtIso` is
 * absent or unparseable, returns `false` (NOT expired) — matching the legacy
 * renderer, which strikes through an approval only when `expires_at` is in the
 * PAST.
 */
export function isExpired(
  expiresAtIso: string | null | undefined,
  now?: number,
): boolean {
  if (expiresAtIso == null || expiresAtIso === '') return false;
  const parsed = Date.parse(expiresAtIso);
  if (Number.isNaN(parsed)) return false;
  const ref = now ?? Date.now();
  return parsed <= ref;
}

// Canonical PyGithub artifact path: /<owner>/<repo>/(issues|pull)/<number>.
// PyGithub's html_url only ever emits this shape, so we pin to it (defence in
// depth — the /trace + /decisions decision docs are UNREDACTED).
const GITHUB_ARTIFACT_PATH = /^\/[^/]+\/[^/]+\/(?:issues|pull)\/\d+$/;

/**
 * External-link guard for a decision's `github.url` (the PR/issue the agent
 * opened). Unlike `safeApprovalHref` (relative, same-origin), this is a
 * DELIBERATE off-origin link, so it returns the ABSOLUTE url — but only after a
 * strict allowlist: https, host EXACTLY `github.com` (no port, no userinfo), and
 * a canonical issue/PR pathname. Rejects every other host, non-TLS schemes,
 * `javascript:` / `data:` smuggling, look-alike hosts (`github.com.evil`,
 * `user@github.com`), and any raw string carrying whitespace / control chars /
 * backslashes (which a real html_url never does). Callers still gate on an
 * allowlisted `action`, and the anchor uses `rel="noopener noreferrer"`.
 */
export function safeGithubHref(raw: unknown): string | null {
  if (typeof raw !== 'string' || raw === '') return null;
  // Reject up front so no URL-parser normalization trick slips a control char,
  // newline, tab, space, or backslash through (\s covers ASCII whitespace).
  if (/[\u0000-\u001f\s\\]/.test(raw)) return null;
  let u: URL;
  try {
    u = new URL(raw);
  } catch {
    return null;
  }
  if (u.protocol !== 'https:') return null;
  if (u.hostname !== 'github.com') return null;
  if (u.port !== '') return null;
  if (u.username !== '' || u.password !== '') return null;
  if (!GITHUB_ARTIFACT_PATH.test(u.pathname)) return null;
  return u.href;
}

/**
 * The PR link for an `iac_apply` decision row's title. The coordinator derives
 * `github.url` (`https://github.com/<repo>/pull/<n>`) at serve time from the
 * trusted config repo; this gates on the allowlisted `action === 'iac_apply'`
 * (so we never read `github.url` off an unrelated row) and routes it through
 * `safeGithubHref` (host-allowlisted) before it becomes an anchor href. Returns
 * the absolute github.com URL on success, or `null`.
 */
export function iacPrHref(decision: {
  action?: string;
  github?: { url?: string | null } | null;
}): string | null {
  if (decision?.action !== 'iac_apply') return null;
  return safeGithubHref(decision.github?.url);
}

/**
 * PR numbers that have a terminal `apply_status === 'applied'` iac_apply row in
 * `decisions`. A `waiting_for_rebake` row whose PR is in this set is SUPERSEDED
 * — its apply already succeeded on a later request, so its "Review & approve →"
 * CTA is stale and must downgrade to the neutral view-only label.
 *
 * The rail already holds the full list (`/decisions?limit=50`), so supersession
 * is answerable client-side with no backend change. If a list ever exceeds the
 * window and an `applied` row falls outside it, the matching waiting row simply
 * keeps its live CTA — a fail-safe degradation (shows actionable, the status
 * quo), never a false "resolved".
 *
 * Tolerates a null/undefined list and null/undefined entries. A pr_number is
 * only counted when it is a positive integer (mirrors `iacApprovalHref`'s
 * guard), so a missing/zero/non-integer number can never resolve a PR.
 */
export function resolvedIacPrNumbers(
  decisions:
    | ReadonlyArray<
        { action?: string; apply_status?: string; pr_number?: number } | null | undefined
      >
    | null
    | undefined,
): Set<number> {
  const resolved = new Set<number>();
  for (const d of decisions ?? []) {
    if (
      d?.action === 'iac_apply' &&
      d?.apply_status === 'applied' &&
      typeof d.pr_number === 'number' &&
      Number.isInteger(d.pr_number) &&
      d.pr_number > 0
    ) {
      resolved.add(d.pr_number);
    }
  }
  return resolved;
}

/**
 * Label for an iac_apply row's approval CTA. The link target — `/iac-approvals/<n>`
 * — is unchanged for every state; only the wording reflects how the row reads to
 * an operator.
 *
 * Four wordings:
 * - "Review & approve →" — the ONLY actionable label: a `waiting_for_rebake` row
 *   that is NOT superseded (no `applied` row for its PR — see
 *   `resolvedIacPrNumbers`) still needs the operator's second, post-rebake Apply.
 * - "View approval history →" — a DONE row (`applied` + `merge_state==='merged'`):
 *   the gate is closed, so the link is a record to look back at, not an action.
 * - "View failure details →" — a TERMINAL-FAILED row (`failed`,
 *   `failed_state_suspect`, or `ambiguous`, regardless of merge_state): the
 *   approval page renders these as a no-action failure banner and suppresses the
 *   Approve form (agent/main.py iac_approval_get), so the rail must not imply an
 *   approval is pending. The honest "Go to approval page →" catch-all read as an
 *   invitation to approve when the page had nothing to approve (PR #95: a
 *   `failed_state_suspect` + merged row whose page had no button).
 * - "Go to approval page →" — every other (non-actionable, not-yet-done) state: a
 *   superseded waiting row, applied-but-merge-pending (still actionable via the
 *   merge-only reconcile), or an unmatchable `pr_number`. Neutral wording so a
 *   parked row doesn't imply pending approval work (Codex review, PR #71: no
 *   stale "Review & approve" affordance).
 */
const TERMINAL_FAILED_APPLY_STATUSES: ReadonlySet<string> = new Set([
  'failed',
  'failed_state_suspect',
  'ambiguous',
]);

export function iacApproveLabel(
  d: { apply_status?: string; merge_state?: string; pr_number?: number },
  resolvedPrs: ReadonlySet<number>,
): string {
  const superseded = typeof d.pr_number === 'number' && resolvedPrs.has(d.pr_number);
  if (d.apply_status === 'waiting_for_rebake' && !superseded) return 'Review & approve →';
  if (d.apply_status === 'applied' && d.merge_state === 'merged') return 'View approval history →';
  if (d.apply_status !== undefined && TERMINAL_FAILED_APPLY_STATUSES.has(d.apply_status))
    return 'View failure details →';
  return 'Go to approval page →';
}
