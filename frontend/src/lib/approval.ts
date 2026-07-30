import type { TranslateFn } from './i18n';
import type { DecisionApproval, DecisionNotify } from './types';

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
 * `locale === 'ja'` appends `lang=ja` (unless the link already carries a
 * `lang` param) so the server-rendered approval page opens in the operator's
 * language — the backend allowlists the value and defaults to English, so
 * omitting it (every pre-i18n caller) is always safe.
 *
 * Rejects: off-origin absolute URLs, non-http(s) schemes (`javascript:`,
 * `data:`, `file:`, …), non-`/approvals/` paths, empty/malformed input, and
 * links whose `?t=` token is a scrub's literal `<redacted>` placeholder — after
 * the 2026-07-09 operator-seat reversal the anonymous /decisions, /trace, /chat
 * and /conversations serve scrubs are gone (visitors get the live link), but the
 * surviving scrubs (`/runs`, the model-facing decisions-history and
 * read_conversations reads) still emit that literal, and both Approve and Reject
 * need the real token, so a CTA for such a link is a dead button (the literal
 * can never be a real token: the redactor's value class excludes `<`).
 */
export function safeApprovalHref(
  raw: string,
  origin?: string,
  locale?: string,
): string | null {
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
    // searchParams decodes, so both `?t=<redacted>` and `?t=%3Credacted%3E`
    // (the URL-encoded form a browser produces) are caught here.
    if (u.searchParams.get('t') === '<redacted>') return null;
    let href = u.pathname + u.search;
    if (locale === 'ja' && !u.searchParams.has('lang')) {
      href += (u.search ? '&' : '?') + 'lang=ja';
    }
    return href;
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
 *
 * `locale === 'ja'` appends `?lang=ja` (backend-allowlisted, EN default) so
 * the approval page opens in the operator's language.
 */
export function iacApprovalHref(
  prNumber: unknown,
  locale?: string,
): string | null {
  if (
    typeof prNumber !== 'number' ||
    !Number.isInteger(prNumber) ||
    prNumber <= 0
  ) {
    return null;
  }
  return `/iac-approvals/${prNumber}${locale === 'ja' ? '?lang=ja' : ''}`;
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

/**
 * True when a rollback decision's `approval` is a genuine, safe, live gate
 * the operator can act on right now — the SINGLE "is this decision awaiting
 * the operator" predicate for the rollback lane. Shared by desk.ts's
 * `selectPendingRollback` (rule 1: what to show as the desk's pending CTA)
 * and ledger.ts's `classify` (the ledger's `open` state) — both are
 * answering the exact same question about the exact same field, so they
 * share one implementation rather than two hand-copied condition lists that
 * could quietly drift apart.
 *
 * Requires ALL of: `approval.approval_url` present, `safeApprovalHref`
 * resolves it to a non-null href (rejects off-origin URLs AND the
 * `<redacted>` scrub-placeholder token — a dead button is not "awaiting the
 * operator", it has no working Approve/Reject path), `status` is `'pending'`
 * or absent (pre-enrichment rows never had the field at all), and
 * `expires_at` is not in the past.
 *
 * Callers that also need the href itself (desk.ts, to build its `href`
 * field) call `safeApprovalHref` again directly — the two calls can never
 * disagree (this function's own href check already proved a non-null result
 * for this exact URL/origin), and `locale` (only ever added by the second
 * call) never flips null-vs-non-null, only whether `?lang=ja` is appended.
 */
/**
 * ds-hdt: TRUE only when the server positively recorded that the operator
 * notification for this row FAILED.
 *
 * Deliberately not `state !== 'delivered'`. Four states have to stay distinct,
 * and three of them are not "we failed to reach you":
 *
 *   - `'failed'`    → say so. Nobody was paged; the operator is looking at
 *                     this card because they happened to open the desk.
 *   - `'delivered'` → a notification went out.
 *   - `'pending'`   → in flight, or the outcome patch was lost. NOT KNOWN.
 *   - absent        → every row written before ds-hdt. Never recorded.
 *
 * Warning on an unknown would cry wolf on every historical row; treating
 * unknown as delivered would quietly promise a page that never happened. The
 * same unknown-≠-empty rule the rest of the desk already follows.
 */
export function notifyFailed(
  decision: { notify?: DecisionNotify | null } | null | undefined,
): boolean {
  return decision?.notify?.state === 'failed';
}

export function isRollbackAwaitingOperator(
  decision: { approval?: DecisionApproval | null } | null | undefined,
  opts: { now?: number; origin?: string } = {},
): boolean {
  const approval = decision?.approval;
  if (!approval?.approval_url) return false;
  if (safeApprovalHref(approval.approval_url, opts.origin) === null) return false;
  if (!isRollbackApprovalUnresolved(approval)) return false;
  if (isExpired(approval.expires_at, opts.now)) return false;
  return true;
}

/**
 * The STATUS half of `isRollbackAwaitingOperator`, without the clock: `true`
 * when the approval's credential is still unspent as far as we can tell.
 *
 * Extracted (ds-d4z) because "spent" and "timed out" are different facts and a
 * surface that distinguishes them needs to ask about status ALONE. The rail
 * renders a struck-through Approve plus an "expired" badge for a timed-out gate
 * — correct for a pending approval nobody got to in 15 minutes, and a WRONG
 * REPORT for one the operator actually used, which merely also sat past its TTL.
 * Ordering the two checks does not fix that: a `used` approval with a past
 * `expires_at` fails the awaiting-predicate for the RIGHT reason and then
 * matches the expired branch anyway. The expired branch has to gate on status
 * itself, and this is that gate — shared, not re-derived, so the rail cannot
 * drift from the desk and the ledger again.
 *
 * `false` for a `used` or `denied` status, and for ds-mml's
 * `status_unavailable` (the backend could not READ the doc — a different thing
 * from the doc not carrying a status; when the server says it doesn't know,
 * don't guess). `true` when `status` is `'pending'` or absent entirely
 * (pre-enrichment rows never had the field).
 */
export function isRollbackApprovalUnresolved(
  approval: DecisionApproval | null | undefined,
): boolean {
  if (approval == null) return false;
  if (approval.status_unavailable) return false;
  if (approval.status !== undefined && approval.status !== 'pending') return false;
  return true;
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
 * The apply_status values that mean an iac_apply REQUEST ended in failure and
 * no operator action remains. Mirrors `_IAC_RUN_ENDED_STATUSES` in
 * agent/main.py, minus `applied` — that one is handled as its own branch
 * everywhere here because success and failure read differently to an operator
 * even though both are terminal.
 *
 * `agent/main.py`'s `iac_approval_get` renders these as a no-action failure
 * banner and SUPPRESSES the Approve form, so a surface that still offers to
 * approve one is describing a page that has no button (see `iacApproveLabel`).
 */
const TERMINAL_FAILED_APPLY_STATUSES: ReadonlySet<string> = new Set([
  'failed',
  'failed_state_suspect',
  'ambiguous',
]);

/**
 * PR numbers that have a terminal `apply_status === 'applied'` iac_apply row in
 * `decisions`, for suppressing a stale LISTING entry: ds-0rm's 60-second cache
 * window, where a merged-and-applied PR was still being served by the open-PR
 * listing and inflated the count.
 *
 * SCOPE, learned twice the hard way (ds-dzd). Two constraints:
 *
 * 1. `applied`-ONLY. A terminal FAILURE must not land here. `estate.ts`'s
 *    `reconcileApprovals` DROPS a listing entry on the strength of this set and
 *    states the invariant outright — "an in-progress or failed apply leaves the
 *    entry standing" — because a failed apply is not counter-evidence that the
 *    entry is stale. Widening it to failures was tried and reverted.
 *
 * 2. LISTING LANE ONLY. Do not read this to decide whether a DECISION row is
 *    awaiting the operator. PR-wide is the wrong grain there: an `applied` row on
 *    generation A says nothing about a `waiting_for_rebake` row on generation B,
 *    and the backend permits that pairing. `isIacAwaitingOperator` and
 *    `iacApproveLabel` therefore consult `supersededWaitingIds` instead, and this
 *    set is not passed to either.
 *
 * It stays PR-wide because the listing has no finer identity available:
 * `PendingApproval` carries only pr_number/title/url/asset_type/resource_name,
 * with no head_sha or event_key. Failing OPEN there instead would re-admit
 * ds-0rm, an observed over-report, to close a narrower theoretical one — so the
 * honest fix is to give the listing payload a generation identity, which is a
 * backend change (bead ds-qib).
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

/** The shape `supersededWaitingIds` needs off each decision. */
type IacGenerationRow = {
  action?: string;
  apply_status?: string;
  decision_id?: string;
  created_at?: string;
  /** Generation identity — see the note on `Decision.event_key`. */
  event_key?: string;
};

function parsedTime(iso: string | undefined): number | null {
  if (typeof iso !== 'string' || iso === '') return null;
  const t = Date.parse(iso);
  return Number.isFinite(t) ? t : null;
}

/**
 * `decision_id`s of `waiting_for_rebake` rows that a STRICTLY NEWER terminal row
 * FOR THE SAME GENERATION has already overtaken — i.e. the operator was asked to
 * approve a specific plan artifact, and that artifact's own run has since
 * finished (applied, failed, ambiguous). Such a row is no longer the thing to
 * approve.
 *
 * ds-dzd. This is the question `resolvedIacPrNumbers` cannot answer, because
 * approval state is not PR-wide. `agent/main.py`'s `_iac_event_key` keys an
 * apply on `{repo, pr_number, head_sha, generation_metadata}`, and
 * `docs/runbooks/iac-apply-failure-recovery.md` tells the operator to rebuild the
 * C2 plan on the SAME PR after a failure and approve the newest one. So one PR
 * holds several generations — prod already does: PR #32 carries one `applied`
 * event_key and a separate `failed` one.
 *
 * MATCHED BY `event_key`, NOT `pr_number`. A per-PR "newest terminal" rule is
 * unsound, because generations are NOT serialized against each other: the claim
 * is made per event key, so two can be in flight at once and complete out of
 * order. That admits
 *
 *     waiting(A) -> waiting(B) -> terminal(A)
 *
 * where terminal(A) is newer than waiting(B) but says nothing whatsoever about
 * generation B. A per-PR rule retires the live B row; scoping to the event_key
 * cannot, because a terminal row only ever speaks for its own artifact.
 *
 * What it DOES still catch is the case that started this: PR #95's
 * `failed_state_suspect` row and both of its `waiting_for_rebake` rows share one
 * event_key, so the failure is genuinely those rows' own outcome. `awaitingCount`
 * had been reporting them for two months as an item no surface could render.
 *
 * FAILS SAFE toward showing work. A row is retired only when both rows carry the
 * same non-empty `event_key`, both timestamps parse, and the terminal one is
 * strictly greater. A missing event_key or `created_at`, a missing
 * `decision_id`, or a tie all leave the row actionable — the same degradation
 * `resolvedIacPrNumbers` chooses when its window truncates. Hiding real work is
 * the expensive direction: an operator who cannot see a proposal cannot approve
 * it, whereas a stale-looking row costs one click.
 *
 * On `created_at`: each `_record_iac_decision` call persists a NEW doc with a
 * fresh `uuid4()` decision_id, and state_store stamps `created_at` at record
 * time — so it is per-row birth time, not a field a later reconcile mutates. (A
 * merge-only reconcile APPENDS an `applied` row, and an `applied` row already
 * retires its PR's waiting rows through `resolvedIacPrNumbers`.) Within one
 * event_key the waiting row is always written before the apply runs, so the
 * strict comparison is belt-and-braces rather than the load-bearing part.
 */
export function supersededWaitingIds(
  decisions: ReadonlyArray<IacGenerationRow | null | undefined> | null | undefined,
): Set<string> {
  const list = decisions ?? [];
  // Newest terminal outcome per GENERATION — success or failure, since either
  // means that artifact's run has completed.
  const newestTerminal = new Map<string, number>();
  for (const d of list) {
    if (d?.action !== 'iac_apply') continue;
    const status = d.apply_status;
    if (status === undefined) continue;
    if (status !== 'applied' && !TERMINAL_FAILED_APPLY_STATUSES.has(status)) continue;
    const key = d.event_key;
    if (typeof key !== 'string' || key === '') continue;
    const at = parsedTime(d.created_at);
    if (at === null) continue;
    const prev = newestTerminal.get(key);
    if (prev === undefined || at > prev) newestTerminal.set(key, at);
  }

  const stale = new Set<string>();
  for (const d of list) {
    if (d?.action !== 'iac_apply' || d.apply_status !== 'waiting_for_rebake') continue;
    const id = d.decision_id;
    const key = d.event_key;
    if (typeof id !== 'string' || id === '') continue;
    if (typeof key !== 'string' || key === '') continue;
    const mine = parsedTime(d.created_at);
    const terminal = newestTerminal.get(key);
    if (mine === null || terminal === undefined) continue;
    if (terminal > mine) stale.add(id);
  }
  return stale;
}

/**
 * True when an `iac_apply` decision genuinely still needs the operator's
 * post-rebake Apply — the SINGLE "is this decision awaiting the operator"
 * predicate for the iac lane, mirroring `isRollbackAwaitingOperator` above
 * for the rollback lane. Shared by desk.ts's `selectPendingIacFromDecisions`
 * (the decisions-derived fallback rule 2b — see its header comment for why
 * that fallback exists) and ledger.ts's `classify` (the ledger's `open`
 * state, iac lane).
 *
 * MUST stay in lockstep with `iacApproveLabel` below's "Review & approve →"
 * branch (the ONLY actionable iac label) — that function is the rail's
 * ground truth for what counts as actionable, and this predicate exists so
 * every OTHER surface (desk, ledger) agrees with it instead of re-deriving
 * its own answer. A future edit to one branch that misses the other
 * silently re-opens the exact bug this predicate was extracted to close
 * (desk.ts previously had NO rule that could ever surface a merged,
 * `waiting_for_rebake` decision, because its only iac source was the
 * open-PR-only GitHub issue listing).
 *
 * Requires ALL of: `action === 'iac_apply'`, `apply_status ===
 * 'waiting_for_rebake'`, no positive-integer `superseded_by_pr` annotation, and
 * this row's own `decision_id` not in `supersededIds` (`supersededWaitingIds` — a
 * strictly newer TERMINAL row, success or failure, for THIS ROW'S OWN
 * GENERATION).
 *
 * Deliberately does NOT consult `resolvedIacPrNumbers` (ds-dzd). That set is
 * PR-wide, and PR-wide is the wrong grain for a decision row: an `applied` row
 * from generation A says nothing about a `waiting_for_rebake` row from generation
 * B, and the backend permits exactly that pairing — apply A, merge A fails, the
 * head advances, B is built and records its own waiting row (main.py's claim and
 * lookup are both per event key). Reading the PR-wide set here deleted B from the
 * desk, the count, the ledger and the rail at once. Same-generation supersession
 * already covers the case the PR-wide check was reaching for, because
 * `supersededWaitingIds` counts `applied` as terminal too.
 *
 * The PR-wide set keeps its own, narrower job: suppressing a stale LISTING entry
 * for a PR whose apply already succeeded (ds-0rm's 60s cache window). That lane
 * has no generation identity to work with — `PendingApproval` carries only
 * pr_number/title/url/asset_type/resource_name — so it stays PR-wide by
 * necessity. See `desk.ts`'s two listing filters and `estate.ts`'s
 * `reconcileApprovals`.
 *
 * `supersededIds` is REQUIRED, not optional. Every caller must compute it,
 * because an optional parameter is precisely how a shared predicate drifts: the
 * surface that forgets it silently keeps the old, wrong answer, which is the
 * failure this whole change exists to close.
 */
export function isIacAwaitingOperator(
  decision:
    | {
        action?: string;
        apply_status?: string;
        pr_number?: number;
        superseded_by_pr?: number;
        decision_id?: string;
      }
    | null
    | undefined,
  supersededIds: ReadonlySet<string>,
): boolean {
  if (decision == null) return false;
  if (decision.action !== 'iac_apply') return false;
  if (decision.apply_status !== 'waiting_for_rebake') return false;
  const supersededByPr = decision.superseded_by_pr;
  const explicitlySuperseded =
    typeof supersededByPr === 'number' && Number.isInteger(supersededByPr) && supersededByPr > 0;
  if (explicitlySuperseded) return false;
  if (typeof decision.decision_id === 'string' && supersededIds.has(decision.decision_id))
    return false;
  return true;
}

/**
 * Label for an iac_apply row's approval CTA. The link target — `/iac-approvals/<n>`
 * — is unchanged for every state; only the wording reflects how the row reads to
 * an operator.
 *
 * Five wordings:
 * - "superseded by #N →" — a `waiting_for_rebake` row explicitly annotated
 *   `superseded_by_pr` (recovery-runbook marker: its plan was re-expressed in a
 *   NEW PR that already carries the real `applied` row — see
 *   docs/runbooks/iac-apply-failure-recovery.md §7e). Checked FIRST, but gated to
 *   the `waiting_for_rebake` shape so a mis-annotated `failed` row still reads
 *   "View failure details →" instead of being masked.
 * - "Review & approve →" — the ONLY actionable label: a `waiting_for_rebake` row
 *   that is NOT superseded (no strictly-newer terminal row for its own
 *   GENERATION, success OR failure — see `supersededWaitingIds`) still needs the
 *   operator's second, post-rebake Apply. MUST stay in lockstep with
 *   `isIacAwaitingOperator` above — see its comment.
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
 *   generation-superseded waiting row (`supersededWaitingIds`),
 *   applied-but-merge-pending
 *   (still actionable via the merge-only reconcile), or an unmatchable
 *   `pr_number`. Neutral wording so a parked row doesn't imply pending approval
 *   work (Codex review, PR #71: no stale "Review & approve" affordance).
 */
export function iacApproveLabel(
  d: {
    apply_status?: string;
    merge_state?: string;
    pr_number?: number;
    superseded_by_pr?: number;
    decision_id?: string;
  },
  supersededIds: ReadonlySet<string>,
  t: TranslateFn,
): string {
  if (
    d.apply_status === 'waiting_for_rebake' &&
    typeof d.superseded_by_pr === 'number' &&
    Number.isInteger(d.superseded_by_pr) &&
    d.superseded_by_pr > 0
  )
    return t('shared.approve.supersededBy', { pr: d.superseded_by_pr });
  // Superseded per GENERATION: a strictly newer terminal row for this row's own
  // event_key overtook it (ds-dzd). NOT PR-wide — see isIacAwaitingOperator for
  // why an `applied` row on a different generation must not silence this one.
  const superseded = typeof d.decision_id === 'string' && supersededIds.has(d.decision_id);
  if (d.apply_status === 'waiting_for_rebake' && !superseded) return t('shared.approve.reviewApprove');
  if (d.apply_status === 'applied' && d.merge_state === 'merged')
    return t('shared.approve.viewHistory');
  if (d.apply_status !== undefined && TERMINAL_FAILED_APPLY_STATUSES.has(d.apply_status))
    return t('shared.approve.viewFailure');
  return t('shared.approve.goToPage');
}
