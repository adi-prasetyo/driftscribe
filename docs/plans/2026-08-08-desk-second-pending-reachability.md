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
  - **Shipped at 104px, and the first measurement was the wrong one.** Sizing
    off an August sample (82.8px en / 79.2px ja) would have clipped in
    December: the widest date each locale can produce is `Dec 25, 23:59` at
    89.7px and `12月25日 23:59` at 93.0px — and ja is the wider worst case
    despite being the narrower sample.
  - **Review caught a layout regression this created.** A `1fr` track is
    `minmax(auto, 1fr)`, so it floors at min-content, and the strip's narrowest
    subjects are unbreakable tokens from `rollbackSubject` (`PAYMENT_MODE`).
    Past that floor the row overflows a card that is `overflow: hidden`, so it
    clips rather than scrolls. Measured with an `applied` row in play (the
    SealStamp's 30px fourth track): 2px over at 390 and 72px at 320, against a
    58px column that was clean at 390 and already 26px over at 320.
    `min-width: 0` + `overflow-wrap: anywhere` on `.ledger-strip__title` takes
    all four widths to 0, fixing the pre-existing 320px case too.
  - Pinned in `transparency.smoke.ts` at 390px, beside the estate's own phone
    pin. Two traps, both hit and both documented in the test: the shared
    decisions fixture is all prose-with-spaces and **cannot** reach the
    min-content floor (the first version of the pin passed with the fix
    reverted), and the SealStamp is `rotate(-11deg)` so a client rect reads
    2.59px past its cell on a correct layout — the sweep neutralises transforms
    for the measurement.
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

---

## Amendment — ds-wd2.21: the clock column (shipped separately)

Filed the same day, after the operator read the shipped ds-wd2.18 column on
prod. Dating the first row of each day gave the track two shapes, and
left-aligned they shared a LEFT edge:

```
May 30, 19:52
01:09
```

so the clock itself moved ~48px sideways between consecutive rows — measured at
1280px, clock left 291 on a bare row against 339.4 on a dated one. The column a
reader scans down is the clock, and it was the one that wandered.

**Fix:** `text-align: right` on `.ledger-strip__time`. One declaration; nothing
about the track, the fixture or the markup changes.

Ragged on one side either way — the only question is which side, and the answer
is not a preference. `fmtStamp` and `fmtClock` both pin `hour`/`minute` to
`'2-digit'` with `hourCycle: 'h23'` (lib/format.ts), so the clock is ALWAYS
exactly five characters and always ENDS the string. A 1- vs 2-digit day
(`Aug 8` vs `May 30`) or a 1- vs 2-digit CJK month therefore moves the left edge
and never the right. Verified in both locales: clock left 360.4 on every row,
en and ja alike, against 291/339.4 before.

The 11px of slack in the 104px track becomes a left indent rather than trailing
space. That is the visible cost, and it is also why the track must NOT be
trimmed to fit now that it is right-aligned: right-aligned text overflows to the
LEFT, where `.ledger-strip`'s `overflow: hidden` clips it against the card edge.
Left-aligned, an overflow merely ate into the 14px gap before the glyph. The
cross-platform font margin the 104px was chosen for is load-bearing in a
stricter way than it was.

### The pin, and why it is a browser test

`the ledger clocks share one right edge, dated row or not`
(transparency.smoke.ts). jsdom runs no cascade and has no layout, so a unit test
passes with the declaration deleted.

Two traps, both checked rather than assumed — the same discipline the
phone-width pin above needed:

- **Measure a RANGE over the cell's text, never the cell's box.** The cell is a
  grid item in a fixed 104px track and `justify-self` defaults to `stretch`, so
  its rect is 291→395 with or without the fix. Confirmed by running the reverted
  build with both numbers returned: `elemRight` was 395 on all three rows while
  the range rights were 374.1 / 325.6 / 374.1. An element-rect version of this
  test is vacuous by construction.
- **Build the fixture's timestamps from the runner's wall clock.** `sameDay`
  compares calendar days in the READER's zone, so a pair two hours apart written
  as literal `Z` strings lands on two different local days somewhere in
  [-12, +14] and the fixture's premise (rows 1-2 share a day, row 3 does not)
  quietly dissolves. Anchored on yesterday's local noon: every row is in the
  past whatever time CI runs, and noon ± 2h cannot cross a local midnight even
  across a DST shift.

The test also asserts the fixture produced both shapes (one bare, two dated)
before asserting anything about alignment — a build whose day-boundary rule
stopped firing would render three identical shapes and satisfy an alignment
check trivially.

Gates: 2015 unit, 683 files 0/0, smoke 57/57, and the pin verified to FAIL on
the reverted CSS.
