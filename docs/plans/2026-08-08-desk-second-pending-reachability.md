# Reaching the second pending decision, and reading the record that lists it

**Beads:** ds-wd2.17 (P1), ds-wd2.18 (P2) — both under epic ds-wd2.
**Branch:** `fix/desk-second-pending-reachability`
**Date:** 2026-08-08

Two defects found together while answering an operator question: "there are two
things that need my decision — how do I choose the other one?" They are
independent fixes on two adjacent surfaces of the same card stack, so they ship
together.

## What is actually broken

### ds-wd2.17 — the second pending item has no in-app path

The desk queue surfaces ONE item (`deskModel`, `lib/desk.ts:658`) while the band
numeral reports the honest system-wide total (`awaitingCount`,
`lib/desk.ts:754`). That split is deliberate and documented. What is not
deliberate is that the item it defers has nowhere to be clicked.

Live case, 2026-08-08 — two open `driftscribe-infra` PRs:

| PR | Subject | Opened | Where it appears |
|---|---|---|---|
| #308 | Adopt Pub/Sub topic `shipping-events` | 08-07 | desk card |
| #297 | Adopt GCS bucket `driftscribe-hack-2026-receipts` | 08-05 | estate chip only |

The obvious escape hatch — click the item's row in Recent record, expand its
decision record, use the link that card carries (`DecisionRecord.svelte:228`) —
**does not exist for this class of PR.** `propose_adoption_tool`
(`agent/adk_tools.py:1539-1657`) never calls `record_decision`; an `iac_apply`
decision doc is written only at APPROVE time (`agent/main.py:6635`). So a
not-yet-approved adopt PR has no decision row, and `LedgerStrip` — built from
`decisions` alone — cannot show it by construction.

Confirmed against a live operator dump: every `Infrastructure change PR #N` row
in that strip (#168, #216, #95, #32) is a PR the operator already approved whose
apply then stalled. The strip has never shown a proposal awaiting first review.

That leaves `EstateView.svelte:233` as the only pixel in the application naming
PR #297, rendered inert (`estate-view__chip--q`, `cursor: default`).

### ds-wd2.18 — Recent record prints a clock with no date

`LedgerStrip.svelte:312` renders `fmtClock` alone. That follows the mockup,
whose rows were all same-day, and the `58px` monospace time column follows from
that.

The strip provably spans days — `ledger.ts` documents its own live cases: one
`event_key` carrying a no_op on 07-29 and a rollback on 07-31, and an
`iac-apply-32` key carrying two applied+merged records **27 days apart**. The
same live dump reads `21:47 → 00:31 → 11:18 → 14:13 → 14:09 → 00:17 → 23:19 →
23:09` across ~25 rows, so a correctly sorted newest-first list reads as
shuffled.

## Design decisions

**The chip becomes a link, and the comment that said it must not changes with
it.** The existing comment argues the chip survives a stale approvals lane
*because* "it drives no action" — a retained `pendingApprovals` list can name a
PR closed or merged elsewhere, which is why the wording flips to "status not
refreshed". A link does not break that argument: navigation is not an action,
and `/iac-approvals/{pr}` deliberately re-reads live state on GET and may serve
a spent-token, paused or autonomy-blocked form. That is precisely the reasoning
#307 used to collapse Approve+Reject into a single Review anchor. So the chip
links in BOTH freshness states and the wording rule is untouched — but the
comment must stop asserting a premise the code no longer satisfies, because
name-vs-reality skew is a repeat offender in this component.

**Never a dead link.** `findPendingPr` (`infra_graph.ts:424`) returns
`a.pr_number` without validating it, so `iacApprovalHref` returning `null` is
reachable from a malformed backend row — not merely defensive. Null falls back
to today's inert `<span>`, mirroring `InfraDiagram.svelte:769`, which already
gates its own per-row link exactly this way.

**`sameDay` gets lifted, not copied.** `fmtStamp`'s doc comment records
absorbing four byte-identical formatter copies that had drifted into two clock
conventions on one screen. Copying `ConversationThread`'s private `sameDay`
(`ConversationThread.svelte:48`) into `LedgerStrip` would start that over.

**The day rule is relative between rendered rows**, matching
`ConversationThread.turnTime` — not relative to the current clock. It therefore
does not change under the reader at midnight, needs no injected `now`, and holds
under both the 3-row cap and "Show all" because `rows` is already the capped,
collapsed list.

**Date inline in the existing cell, not a separate separator row.** A separator
row would be a new row type in a grid whose four columns are load-bearing
(`58px 18px 1fr auto`, with the fourth column reserved for the applied
`SealStamp`). Inline keeps the grid and matches the thread.

## Tasks

### 1. `sameDay` → `lib/format.ts` (ds-wd2.18, prerequisite)

- Move the function verbatim, export it, keep its doc comment (the "unparseable
  ⇒ false, because over-labelling is recoverable and silently implying two rows
  share a day is not" rationale is the load-bearing part).
- `ConversationThread.svelte` imports it; delete the local copy.
- Unit tests in `format.test.ts`: same day, different day, year boundary,
  missing, unparseable.

### 2. Ledger row time (ds-wd2.18)

- `LedgerStrip.svelte`: derive the cell text from `(row, rows[i-1])` —
  `sameDay(prev, cur) ? fmtClock(...) : fmtStamp(...)`. The `{#each}` already
  binds `i`; thread the computed string into the `cells` snippet as a second
  parameter so both shells (button and div) cannot diverge.
- Widen `.ledger-strip__time` from `58px`. Must fit `Aug 5, 10:28` and
  `8月5日 10:28` at 11.5px mono. Measure, don't guess.
- A row with absent/unparseable `created_at` still renders `''` — `fmtStamp`
  returns `''` for absent and the raw string for unparseable, same as today's
  `fmtClock`. A null-ts row resets the run, so the row after it may carry a
  redundant date; accepted, per the thread's own documented stance.

### 3. Estate PR chip becomes a link (ds-wd2.17)

- `EstateView.svelte`: compute `iacApprovalHref(row.pendingPr, $locale)`. Render
  an `<a … target="_blank" rel="noopener">` when non-null (matching the desk's
  Review anchor and the infra band's per-PR links), else keep the inert
  `<span>`.
- `data-testid="estate-pr-chip"` stays on whichever element renders, so existing
  assertions keep pointing at the same subject.
- New class `estate-view__chip--pr` replacing `--q` on the anchor: reads as a
  control (navy ink/border, `cursor: pointer`, no underline) rather than the
  muted `cursor: default` of `--q`. `--q` stays for the null-href fallback.
- Focus ring comes from `base.css:136`'s zero-specificity
  `:where(a, button, …):focus-visible`. Verify it is not clipped —
  `.estate-view` is `overflow: hidden` and has clipped a control before.
- The chip already shares `.estate-view__chip`, which the `max-width: 460px`
  restack rule targets, so the phone layout follows without a new rule.

## Verification

- `npm run build` FIRST, then the frontend smoke suite (`ui-smoke` is a separate
  REQUIRED CI job that reads the built bundle, not the source).
- Unit: vitest — `format.test.ts`, `LedgerStrip.test.ts`, `EstateView.test.ts`,
  `ConversationThread` regressions from the `sameDay` move.
- `svelte-check` 0/0, `uv run ruff check .` (CI lints the whole repo even for a
  frontend-only change).
- Visual: re-render the estate and ledger rigs in EN and JA, and at 390px for
  the restack. Confirm the widened time column has not pushed the title column
  or reopened the `--composer-control-h` style seam.
- Mutation check for the review agent: deleting the anchor, or leaving the href
  ungated, must fail a test.

## Explicitly out of scope

- Surfacing InfraDiagram's existing "Open infra changes (N)" band on the desk —
  it already lists every open PR with working links but is mounted only under
  `?preview_pr=N`. Real, larger, and needs its own layout decision. File
  separately.
- Making `propose_adoption_tool` write a proposal-time decision doc. That would
  give adopt PRs ledger rows and is arguably the deeper fix, but it changes what
  `/decisions` means and what every `awaitingCount`/`deskModel` rule reasons
  over. Not on a pitch-week branch.
