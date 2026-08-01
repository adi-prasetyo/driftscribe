// format.ts — small, pure display formatters for the operator UI.
//
// These mirror the strings the legacy single-file renderer produced
// (agent/templates/transparency.html): trace pills show the first 8 chars
// (`traceId.slice(0, 8)`), truncation uses the ellipsis character "…"
// (U+2026), and result_preview is capped at 2000 chars by the backend.
//
// i18n: the operator-facing labels below are looked up in the `shared.*`
// catalog (frontend/src/locales/shared.ts) via the caller-supplied
// `TranslateFn` — this module holds no English strings of its own for those,
// only the backend-enum → catalog-key maps.

import { fmtNumber, localeTag, type TranslateFn, type Locale, type MessageKey } from './i18n';

const ELLIPSIS = '…';
const DEFAULT_PREVIEW_MAX = 2000;

/**
 * Normalize a string for free-text search: lowercase, every run of
 * non-alphanumeric characters collapsed to a single space, trimmed. Applied to
 * BOTH the query and the searchable haystack so separators never block a match —
 * `iac apply` finds `iac_apply`, `docs pr` finds `docs_pr`, `applied merged`
 * finds `applied & merged`, and `PR #168` / `pr 168` / `#168` / `168` all align.
 * Unicode letters/digits are preserved (so a Japanese title stays searchable).
 */
export function normalizeForSearch(s: string | null | undefined): string {
  if (!s) return '';
  return s
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, ' ')
    .trim();
}

/**
 * Render an LLM token total as a human string, e.g. `"1,234 tok"`.
 * Returns `""` when the total is null/undefined/absent. A total of 0 is a
 * present value and renders as `"0 tok"`.
 */
export function fmtTokens(
  usage: { total_token_count?: number | null },
  t: TranslateFn,
  l: Locale,
): string {
  const total = usage?.total_token_count;
  if (total === null || total === undefined) return '';
  return t('shared.tokens', { n: fmtNumber(total, l) });
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
const IAC_STATUS_LABEL_KEYS: Record<string, MessageKey> = {
  applied: 'shared.iac.applied',
  // The label names the APPLY, not the rebuild, and not "re-bake" — plain
  // language, and the only claim true across the whole window (the coordinator
  // never sees the external build finish) and both merge_state variants. The
  // internal enum stays `waiting_for_rebake`; the help text (iacStatusHelp)
  // explains the rebuild step, where saying so is accurate.
  waiting_for_rebake: 'shared.iac.awaitingApply',
  failed: 'shared.iac.failed',
  failed_state_suspect: 'shared.iac.failedStateSuspect',
  ambiguous: 'shared.iac.ambiguous',
};
const IAC_STATUS_MAX = 40; // a status enum is tiny; cap an unexpected value hard
export function iacStatusLabel(status: string | null | undefined, t: TranslateFn): string {
  if (typeof status !== 'string' || status === '') return '';
  const key = IAC_STATUS_LABEL_KEYS[status];
  if (key) return t(key);
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
const IAC_STATUS_HELP_KEYS: Record<string, MessageKey> = {
  // Accurate for BOTH waiting_for_rebake variants — recorded with
  // merge_state="pending" (before the irreversible merge / kept on merge
  // failure) AND merge_state="merged" (after) — so it must NOT assert the merge
  // already happened (agent/main.py records the pending pointer pre-merge).
  waiting_for_rebake: 'shared.iac.help.awaitingApply',
  // Plain `failed` (NOT the state-suspect variant): the apply aborted but the
  // tofu-apply worker PROVED the live state stayed clean (TofuStepError, vs
  // ApplyStateSuspect's "may be mutated"). We deliberately do NOT point the
  // operator at the underlying OpenTofu error: the worker captures stderr
  // (capture_output) and persists only a 500-char tail to the isolated
  // apply-audit, so it is surfaced nowhere operator-facing — promising a
  // location (logs or /trace) would be false.
  failed: 'shared.iac.help.failed',
  failed_state_suspect: 'shared.iac.help.failedStateSuspect',
  ambiguous: 'shared.iac.help.ambiguous',
};
export function iacStatusHelp(status: string | null | undefined, t: TranslateFn): string | null {
  if (typeof status !== 'string') return null;
  const key = IAC_STATUS_HELP_KEYS[status];
  return key ? t(key) : null;
}

/** Visual tone for an iac_apply status token → a CSS class suffix. '' = neutral. */
export type IacTone = '' | 'ok' | 'warn' | 'danger';

export interface IacApplyMeta {
  /** Status text for the rail meta line, e.g. 'applied & merged'. */
  label: string;
  /** Tone driving the token color; '' renders the default muted meta color. */
  tone: IacTone;
  /** Plain-language HelpHint text, or null when none is warranted. */
  help: string | null;
  /**
   * True for a terminal, no-action-remaining row (applied+merged, or a
   * superseded waiting_for_rebake) — drives the ✓ "done" affordance.
   */
  done: boolean;
}

// Tone for NON-applied statuses. Mirrors decision.ts's APPLY_STATUS_BADGE so the
// rail and the open-trace decision card agree on color (ambiguous → warn, not danger).
const IAC_STATUS_TONE: Record<string, IacTone> = {
  failed: 'danger',
  failed_state_suspect: 'danger',
  ambiguous: 'warn',
};

/**
 * Merge-aware display for an iac_apply row's status. The rail historically showed
 * only `apply_status` ('applied'), which can't distinguish "done" (applied AND
 * merged) from "applied but merge still pending" — so a first-timer couldn't tell
 * a finished change from one that still needs attention. This folds `merge_state`
 * in for the `applied` case and otherwise composes the existing
 * `iacStatusLabel`/`iacStatusHelp`. A `waiting_for_rebake` row explicitly marked
 * `superseded_by_pr` is treated as resolved too — see the first branch below.
 *
 * `merge_state` arrives already promoted by the serve-time `reconcile_merge_state`
 * (agent/main.py), so an out-of-band-merged PR reads as done here too.
 */
export function iacApplyMeta(
  apply_status: string | null | undefined,
  merge_state: string | null | undefined,
  superseded_by_pr: number | null | undefined,
  t: TranslateFn,
): IacApplyMeta {
  // A parked `waiting_for_rebake` plan that was re-expressed in a NEW PR (that new
  // PR carries the real `applied` row) is terminal here: its own saved plan is
  // permanently stale, so the row must read as RESOLVED ('superseded', done),
  // not the still-pending 'awaiting apply'. Gated to `waiting_for_rebake` + a
  // positive int, mirroring the rail label (approval.ts `iacApproveLabel`) and
  // the GET/POST resume guards (agent/main.py) — see recovery runbook §7e.
  if (
    apply_status === 'waiting_for_rebake' &&
    typeof superseded_by_pr === 'number' &&
    Number.isInteger(superseded_by_pr) &&
    superseded_by_pr > 0
  ) {
    return {
      label: t('shared.iac.superseded'),
      tone: 'ok',
      help: t('shared.iac.help.superseded', { pr: superseded_by_pr }),
      done: true,
    };
  }
  if (apply_status === 'applied') {
    if (merge_state === 'merged') {
      return {
        label: t('shared.iac.appliedMerged'),
        tone: 'ok',
        help: t('shared.iac.help.done'),
        done: true,
      };
    }
    if (merge_state === 'failed' || merge_state === 'pending') {
      return {
        label: t('shared.iac.appliedMergePending'),
        tone: 'warn',
        help: t('shared.iac.help.mergePending'),
        done: false,
      };
    }
    // Applied with no/unknown merge_state: we can't claim "done" → stay neutral.
    return { label: t('shared.iac.applied'), tone: '', help: null, done: false };
  }
  return {
    label: iacStatusLabel(apply_status, t),
    tone: IAC_STATUS_TONE[apply_status ?? ''] ?? '',
    help: iacStatusHelp(apply_status, t),
    done: false,
  };
}

/**
 * True when an iac_apply row's recorded apply moment (`applied_at`) differs
 * materially from its last-activity time (`created_at`) — e.g. a row applied in
 * May whose face doc is a June merge-only reconcile. The rail sorts/labels by
 * `created_at` (last activity), so when these diverge we surface a faint "applied
 * {date}" cue alongside it. Both must parse; a sub-threshold diff (default 24h)
 * or any unparseable/missing input returns false (no cue).
 */
export function appliedAtDiffersMaterially(
  applied_at: string | null | undefined,
  created_at: string | null | undefined,
  thresholdMs = 86_400_000,
): boolean {
  if (typeof applied_at !== 'string' || typeof created_at !== 'string') return false;
  const a = Date.parse(applied_at);
  const c = Date.parse(created_at);
  if (Number.isNaN(a) || Number.isNaN(c)) return false;
  return Math.abs(c - a) >= thresholdMs;
}

/**
 * Friendly headline label for a decision's `action`, in plain language rather
 * than the bare backend enum. Shown on the rail's non-iac rows (the `{:else}`
 * branch) and, since Task 3.4, as the LedgerStrip's `noted`-row title.
 *
 * ALL THREE actions the backend actually writes are mapped: `no_op`,
 * `rollback`, and `iac_apply` (grep the writers — those are the complete set).
 * Originally only `no_op` was, on the reasoning that the other rows carry
 * their own "View PR/issue →" CTA to give them context. The desk's ledger
 * broke that assumption: it has no CTA column, so an unmapped row rendered a
 * bare `rollback` — a Latin-script code identifier sitting mid-sentence in
 * Japanese operator copy on the judge-facing front door (caught by the Task
 * 3.6 visual gate; violates the standing "no code identifiers in
 * operator-facing copy" rule).
 *
 * The verbatim pass-through below is therefore now what it always claimed to
 * be — a forward-compat path for an action this frontend has never heard of
 * (a newer coordinator writing a fourth kind), NOT the normal case. Such a
 * value is still clamped to 40 chars + '…', matching iacStatusLabel's style.
 * null/undefined/'' → '' (the caller then renders nothing).
 *
 * The labels are deliberately neutral NOUNS for the kind of action, not its
 * outcome ("Rollback", not "Rollback applied"): the same string has to serve a
 * proposed, an applied, and an expired row, and only the row's own status
 * column knows which it is.
 */
const DECISION_ACTION_MAX = 40;
const DECISION_ACTION_KEYS: Record<string, MessageKey> = {
  no_op: 'shared.decision.noOp',
  rollback: 'shared.decision.rollback',
  iac_apply: 'shared.decision.iacApply',
};
export function decisionActionLabel(action: string | null | undefined, t: TranslateFn): string {
  if (typeof action !== 'string' || action === '') return '';
  // Object.hasOwn, not a bare lookup: a decision doc is an open shape, so an
  // action literally named `toString` / `constructor` would otherwise resolve
  // to an Object.prototype member and be passed to t() as a key.
  if (Object.hasOwn(DECISION_ACTION_KEYS, action)) return t(DECISION_ACTION_KEYS[action]);
  return action.length > DECISION_ACTION_MAX
    ? action.slice(0, DECISION_ACTION_MAX) + ELLIPSIS
    : action;
}

// Mirrors agent/models.py:ContractStatus (four values). Same shape and the same
// Object.hasOwn discipline as DECISION_ACTION_KEYS above, for the same reason:
// a decision doc is an open shape, so a status literally named `toString` must
// not resolve an Object.prototype member and be handed to t() as a key.
const CONTRACT_STATUS_KEYS: Record<string, MessageKey> = {
  match: 'shared.contract.match',
  present_allow_manual: 'shared.contract.presentAllowManual',
  present_disallow_manual: 'shared.contract.presentDisallowManual',
  absent: 'shared.contract.absent',
};

/**
 * Operator-facing label for a per-variable `contract_status`. The raw enum is a
 * snake_case code identifier (`present_disallow_manual`) that was rendering
 * verbatim in the STATUS column of the judge-facing desk, in Latin script
 * directly beneath Japanese copy — the same defect class as the bare `rollback`
 * that decisionActionLabel above fixes.
 *
 * An UNRECOGNIZED status falls through to the raw string rather than to an
 * empty cell or an invented label: if the backend adds a fifth enum value, the
 * operator sees the real thing and can look it up, which is the honest failure
 * mode. Only the four values agent/models.py actually defines are translated.
 */
export function contractStatusLabel(status: string | null | undefined, t: TranslateFn): string {
  if (typeof status !== 'string' || status === '') return '';
  if (Object.hasOwn(CONTRACT_STATUS_KEYS, status)) return t(CONTRACT_STATUS_KEYS[status]);
  return status.length > DECISION_ACTION_MAX
    ? status.slice(0, DECISION_ACTION_MAX) + ELLIPSIS
    : status;
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
export function decisionActionHelp(action: string | null | undefined, t: TranslateFn): string | null {
  if (typeof action !== 'string') return null;
  return action === 'no_op' ? t('shared.decision.noOpHelp') : null;
}

/**
 * Render an ISO timestamp as a host-timezone `HH:mm` clock string, for the
 * ledger strip's per-row time column (lib/ledger.ts's `LedgerRow`, Task 3.4).
 * Deliberately NOT pinned to Asia/Tokyo: it renders directly beside
 * DecisionsRail's `fmtCreatedAt` and this module's own `fmtWhen` below,
 * neither of which pins a zone either — pinning JST here alone would print a
 * DIFFERENT clock time than the timestamp sitting right next to it for the
 * very same decision, which reads as a bug, not a feature. Same fallbacks as
 * `fmtWhen`: unparseable → the raw value, absent → `''`.
 *
 * `l` MUST be threaded from the active app locale (`$locale` from i18n.ts),
 * matching every other caller in this module — an omitted `l` falls back to
 * the HOST's default locale/hour-cycle, not the app toggle, which is exactly
 * the bug this signature guards against.
 *
 * `hourCycle: 'h23'` is pinned regardless of locale: `localeTag('en')` is
 * `'en-US'`, whose default hour cycle is 12-hour with an AM/PM suffix
 * ("09:15 AM") — eight characters into the ledger row's `58px` monospace
 * time column, sized for a 24-hour reading (the mockup's own times are all
 * `14:05` / `09:15` / `08:40` / `06:00`). This is purely a same-instant
 * formatting choice, not a second timezone pin: it does not change what
 * moment is displayed, only how many characters it takes.
 */
export function fmtClock(iso: string, l?: Locale): string {
  if (!iso) return '';
  const parsed = Date.parse(iso);
  if (Number.isNaN(parsed)) return iso;
  try {
    return new Intl.DateTimeFormat(l ? localeTag(l) : undefined, {
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
    }).format(parsed);
  } catch {
    return iso;
  }
}

/**
 * Render an ISO timestamp as a readable absolute wall-clock string with the
 * year (used by the DecisionSummary card — a historical decision can be from
 * any date, so unlike the rail's compact no-year form we include the year).
 * Falls back to the raw value when it doesn't parse, and to '' when absent.
 * `l` is OPTIONAL — a caller with no locale in scope (e.g. decision.ts) still
 * gets Intl's host-default formatting for everything except the hour cycle.
 *
 * `hourCycle: 'h23'` is pinned for the same reason `fmtClock` pins it, and the
 * desk is what forced the issue: `localeTag('en')` is `'en-US'`, whose default
 * is 12-hour, so the stamped card rendered "Applied Jul 28, 2026, 03:13 PM"
 * directly above the ledger's "15:06" row FOR THE SAME DECISION — one event,
 * two clock conventions, ~90px apart (caught by the Task 3.6 visual gate; JA
 * was always 24h and unaffected). Pinning here rather than un-pinning
 * `fmtClock` keeps the ledger's 58px time column narrow. This also settles the
 * hour cycle for locale-less callers, who previously followed the host.
 */
export function fmtWhen(iso: string, l?: Locale): string {
  if (!iso) return '';
  const parsed = Date.parse(iso);
  if (Number.isNaN(parsed)) return iso;
  try {
    return new Intl.DateTimeFormat(l ? localeTag(l) : undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
    }).format(parsed);
  } catch {
    return iso;
  }
}
