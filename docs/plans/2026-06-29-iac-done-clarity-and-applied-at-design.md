# IaC "done" clarity + honest `applied_at` — design

Date: 2026-06-29

## Background / root cause

The Past Decisions rail sorts by Firestore `create_time` (newest first); rows for one
PR fold into one group anchored at that PR's newest doc (`frontend/src/lib/rail.ts`).

PR #32 floated above #102 because #32 got a **fresh** `iac_apply / applied` decision doc
on 2026-06-26, a month after its real apply (2026-05-30). Diagnosis (confirmed against
live `/decisions`): the Jun-26 doc shares the **same `event_key`, `approval_id`, and
`apply_attempt_id`** as the May-30 doc but has a new `decision_id` + `trace_id`. That is
the **merge-only reconcile** path:

1. 2026-05-30: #32 applied OK but the auto-merge failed → recorded `applied` +
   `merge_state="failed"` (the reconcile pointer).
2. PR later merged out-of-band on GitHub.
3. 2026-06-26: an operator re-submitted Apply. `_handle_existing_iac_decision`
   (`agent/main.py:4978`) routed `applied`+`failed` → `_iac_merge_step`
   (`agent/main.py:4808`), which **only merges** (no re-apply), saw already-merged, and
   recorded a NEW `applied`+`merged` doc reusing the prior ids — with a fresh
   `applied_at = now()` and a fresh Firestore `create_time`.

Two problems:
- **Data**: `_record_iac_decision` stamps `applied_at = now()` whenever
  `apply_status == "applied"` (`agent/main.py:4327`), so a merge-only reconcile claims the
  apply happened on the reconcile day. The open-trace "When" row prefers `applied_at`
  (`frontend/src/lib/decision.ts:104`), so it shows the wrong moment.
- **UX**: the rail shows `apply_status` (`applied`) but never `merge_state`, and `applied`
  has no help text — so a first-timer can't tell "done" (applied **and** merged) from
  "applied but merge still pending," and the row still renders an "Open approval page →"
  button that implies pending work.

## Part A — Backend: honest `applied_at` on merge-only reconcile

Carry the original apply moment forward instead of restamping; leave a genuine fresh
apply untouched.

- `_record_iac_decision(...)`: add optional `applied_at: str | None = None`. When
  `apply_status == "applied"`, set `decision["applied_at"] = applied_at or <now iso>`.
- `_iac_merge_step(...)`: add optional `applied_at: str | None = None`; forward it to
  **both** `_record_iac_decision` calls (merge-success `applied`+`merged`, and the
  merge-failure `applied`+`failed` re-record).
- `_handle_existing_iac_decision` merge-only-reconcile branch (status `applied` +
  merge_state `failed`): pass `applied_at=existing.get("applied_at")` into
  `_iac_merge_step` **only when it is a non-empty `str`** (guard the untyped persisted
  dict). If an old row has no usable `applied_at`, fall back to `now()` — a documented
  best-effort, not a correctness guarantee.
- Fresh C5 apply→merge call (`agent/main.py:4804`): leave `applied_at` unset → defaults to
  `now()` (correct — the apply just happened).

Net: a merge-only reconcile preserves the real apply time. The rail still sorts/shows by
`created_at` (so the row legitimately reflects the Jun-26 *merge* activity), but the
trace "When" row and the stored field no longer misreport the apply day.

NOT changing: the append-a-doc model, the sort key (`create_time`), and the serve-time
compute-only `reconcile_merge_state` (PR #151). #32 staying near the top is honest — its
merge reconciled most recently — and is now clearly labeled "done."

### Tests (pytest)
- Merge-only reconcile (`applied`+`failed` → re-POST) re-record carries forward the
  original `applied_at` (not now) **and asserts `call_apply` is NOT called** (extend the
  existing no-re-apply test rather than adding a parallel one).
- Merge-failure reconcile branch (`applied`+`failed` re-record on a still-failing merge)
  also preserves `applied_at`.
- Fresh apply→merge still stamps `applied_at ≈ now` (no regression).
- Reconcile with a missing / non-string `existing.applied_at` falls back safely to `now()`.

## Part B — Frontend: surface merge_state + a "done" affordance in the rail

### B1. `frontend/src/lib/format.ts` — one pure, tested helper
`iacApplyMeta(apply_status, merge_state)` → `{ label, tone, help, done }`:
- `applied` + `merged` → `{ label:'applied & merged', tone:'ok', help: DONE_HELP, done:true }`
- `applied` + `failed`  → `{ label:'applied · merge pending', tone:'warn', help: MERGE_PENDING_HELP, done:false }`
- `applied` + `pending` → `{ label:'applied · merge pending', tone:'warn', help: MERGE_PENDING_HELP, done:false }`
  (forward-compat — not emitted today, but warn beats a misleading plain "applied")
- `applied` + other/none → `{ label:'applied', tone:'', help:null, done:false }` (back-compat;
  deliberately NOT toned green — we can't claim done without a confirmed merge)
- any non-`applied` status → `{ label: iacStatusLabel(s), tone: TONE[s] ?? '', help: iacStatusHelp(s), done:false }`

`DONE_HELP` = "This change is live and merged — there's nothing more to do here."
`MERGE_PENDING_HELP` = "The apply succeeded, but its pull request hasn't merged yet. Open
the approval page to check the merge status, or retry once any branch-protection block is
resolved." (must NOT promise a plain retry fixes a permanent block — mirrors
`_iac_merge_step`'s own wording.)
Tone map (mirrors `decision.ts` `APPLY_STATUS_BADGE` for cross-surface consistency):
`failed`/`failed_state_suspect` → 'danger'; `ambiguous` → 'warn'; `waiting_for_rebake` →
'' (neutral — it carries its own label + help).

Keys on the `merge_state` already in the payload — which the serve-time reconcile
(PR #151) has already promoted to `merged` for out-of-band merges — so it covers both
natively-merged and reconciled rows.

### B2. `DecisionsRail.svelte` — render it
Face meta line + lifecycle steps use `iacApplyMeta(...)`: render `label` in a
tone-classed span; when `done`, prefix a small `check` icon (the rail already imports
`Icon`; the `check` glyph exists). HelpHint uses the returned `help`.

**Chronology cue (Codex):** the face row time is `created_at` (= last activity, e.g. the
Jun-26 merge reconcile), which can read as a wrong "applied day" for a row applied much
earlier. On an `iac_apply` row, when `applied_at` is present and differs materially from
`created_at` (> ~24h), render a faint secondary "applied {fmt(applied_at)}" note so the
real apply moment is visible alongside the last-activity time. Pure helper
`appliedAtDiffersMaterially(applied_at, created_at, thresholdMs = 86_400_000): boolean`
(tested); the component formats the date with the existing `fmtCreatedAt`.

### B3. CTA label — `frontend/src/lib/approval.ts` `iacApproveLabel(d, resolvedPrs)`
- `waiting_for_rebake` && !superseded → "Review & approve →" (unchanged)
- `applied` && `merged` (done) → "View approval history →"
- otherwise → "Go to approval page →" (was "Open approval page →")

Button is kept on done rows (per user) but worded as a record, not an action. The
`waiting_for_rebake` + `pending` CTA wrinkle (where the next POST is really a merge retry,
yet the label says "Review & approve") is **left untouched** — pre-existing behavior,
unrelated to the reported confusion, and changing it widens blast radius for no user-felt
gain here.

### CSS
Add `.iac-status--ok` (color `var(--ds-ok)`) and `.iac-status--warn` /`--danger`
(existing warn/danger tokens) for the inline status token; reuse `--ds-ok` already used on
`.past-approve-btn:hover`.

### Tests (vitest)
- `iacApplyMeta`: all branches (applied+merged/failed/none, each non-applied status,
  null/undefined inputs).
- `iacApproveLabel`: done → "View approval history →"; waiting non-superseded → "Review &
  approve →"; applied+merge-failed/other → "Go to approval page →".
- DecisionsRail render: a done row shows "applied & merged" + check + "View approval
  history →"; a merge-pending row shows "merge pending" + keeps the approval CTA.

## Out of scope (YAGNI)
- Changing the rail sort key or preventing the reconcile duplicate (the grouping already
  folds it to one row; the position is honest).
- A dedicated "Retry merge" CTA for the merge-pending case (neutral "Go to approval page →"
  + the help text suffice).
- A separate `merged_at` field.
