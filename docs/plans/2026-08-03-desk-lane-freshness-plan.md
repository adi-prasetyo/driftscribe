# Desk lane freshness (ds-jk9, ds-smr) — implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A failed `/decisions` or `/infra/pending-approvals` refresh must leave the desk
hero showing *what* it last saw while asserting nothing about *whether that is still true*
— no live Continue / Apply / Approve / Reject, no present-tense "is waiting for you".

**Architecture:** Extend the per-lane freshness pattern #289 introduced (`graphStale`, the
store's first per-lane flag) to the two desk lanes. The store gains `decisionsStale`;
`approvalsStale` already exists but never reaches the desk. `deskModel()` keeps selecting
exactly as it does today and stamps the result with a `stale` bit derived from the lane
that produced it. `ApprovalDesk` renders the identity of a stale card unchanged and
replaces its verdict copy plus its CTA.

**Tech Stack:** Svelte 5 runes, TypeScript, Vitest, `frontend/src/locales/*.ts` (EN+JA).

---

## The rule this implements

From #289 round 5, recorded in `five_rounds_of_review_on_one_branch`:

> **Identity survives a retained value; a verdict does not.**

A retained lane may keep showing WHICH item it last saw. It must not establish a current
CTA, and it must not restate a present-tense claim about that item's state.

## Why per-lane and not the existing aggregate `degraded`

`deskModel` already takes `degraded`, and hoisting the existing check above the selection
rules would be a two-line fix. It is the wrong fix: `degraded` is `!p.ok || !d.ok ||
p.value.degraded`, so a `/decisions` failure alone would suppress a perfectly fresh
listing-derived approval, and vice versa. Coarse gating throws away valid work from the
lane that is still good. `OverviewState.approvalsStale`'s own doc comment already argues
this exact point for the estate; this plan applies it to the hero.

## Lane → rule map

`deskModel` runs five selectors in precedence order. Each reads a specific lane, and that
determines which flag makes its result stale:

| Rule | Selector | Reads | `stale` when |
|---|---|---|---|
| 1 | `selectPendingRollback` | `decisions` | `decisionsStale` |
| 2a | `selectPendingIac` | `pendingApprovals` + `resolvedPrs` (from `decisions`) | `approvalsStale \|\| decisionsStale` |
| 2b | `selectPendingIacFromDecisions` | `decisions` | `decisionsStale` |
| 2.5 | `selectUnresolvedRollback` | `decisions` | out of scope — see below |
| 3 | `selectStamped` | `decisions` | never — terminal facts are monotonic |

**Rule 2a takes BOTH flags.** It is tempting to call it "the approvals lane", but its
actionability rests on `resolvedPrs` — an ABSENCE claim over `decisions` ("no decision
resolved this PR"). A stale decisions lane undermines it exactly as a stale listing does.
This is the same reasoning `selectPendingIacFromDecisions` needs for `supersededIds`, which
is what ds-smr names.

**Rule 3 (`stamped`) is deliberately untouched.** An applied/used decision is terminal and
monotonic: it cannot become un-applied, so retaining it asserts nothing that can go false.
The same argument covers the estate's applied+merged reconciliation, which is why #289 left
it alone too.

**Rule 2.5 (`unresolved`) is deliberately out of scope,** and this is a judgement call worth
stating rather than silently taking. `failed` and `outcome_unknown` ARE verdicts, and a
retained `failed` could in principle be a rollback that a later `/reconcile` settled. But
the card carries no CTA, both variants already read as open loops rather than resolutions,
and `outcome_unknown` is *already* the "we cannot confirm" state — so a freshness overlay on
top of it would be an admission of uncertainty about an admission of uncertainty. Noted in
the bead, not built here.

---

### Task 1: `decisionsStale` in the overview store

**Files:**
- Modify: `frontend/src/lib/overviewStore.ts`
- Test: `frontend/tests/unit/overviewStore.test.ts`

**Step 1: Write the failing test**

```ts
it('decisionsStale is set when /decisions fails and cleared when it recovers', async () => {
  // fail only /decisions; graph + pending succeed
  const store = createOverviewStore(failing(['/decisions']));
  await tick();
  expect(get(store).decisionsStale).toBe(true);
  expect(get(store).approvalsStale).toBe(false); // the OTHER lane is untouched
  // recover
  ...
  expect(get(store).decisionsStale).toBe(false);
});
```

Follow the existing `approvalsStale` tests in this file for the harness shape; do not invent
a new one.

**Step 2: Run it, confirm it fails** — `npm run test:unit -- --run overviewStore`

**Step 3: Implement**

In `OverviewState`, after `approvalsStale`:

```ts
  /**
   * The decisions lane SPECIFICALLY could not be trusted in the last completed
   * cycle. Same recompute-never-latch rule as the two flags above.
   *
   * Asymmetric with `approvalsStale` on purpose: `/infra/pending-approvals` has
   * a documented soft-fail (a 200 carrying `degraded: true`), so that flag must
   * consult the payload. `/decisions` has no such mode — a failed fetch is the
   * only way it goes stale, so this is exactly `!d.ok`. If a soft-fail is ever
   * added to that endpoint, this is the line that has to learn about it.
   */
  decisionsStale: boolean;
```

`INITIAL`: `decisionsStale: false`. `runCycle`: `decisionsStale: !d.ok,` next to
`graphStale: !g.ok`.

**Step 4: Run test to verify it passes**

**Step 5: Commit** — `feat(ui): overviewStore reports decisions-lane staleness (ds-jk9)`

---

### Task 2: `stale` on the desk model

**Files:**
- Modify: `frontend/src/lib/desk.ts`
- Test: `frontend/tests/unit/desk.test.ts`

**Step 1: Write the failing tests** — the ds-smr four-step scenario first, because it is the
bead's own acceptance criterion:

```ts
it('a retained waiting_for_rebake row is selected but NOT actionable when the decisions lane failed', () => {
  const waiting = iacDecision({ apply_status: 'waiting_for_rebake', merge_state: 'merged' });
  const model = deskModel({
    decisions: [waiting],      // retained from the last good cycle
    pendingApprovals: [],      // and pending-approvals answered empty
    now: NOW,
    settled: true,
    degraded: true,
    decisionsStale: true,
  });
  expect(model.kind).toBe('pending');        // identity survives
  if (model.kind === 'pending') expect(model.stale).toBe(true);   // the verdict does not
});
```

Plus one per lane-map row: rollback+`decisionsStale`, listing+`approvalsStale`,
listing+`decisionsStale` (the `resolvedPrs` case — this is the one a reviewer will doubt,
so assert it explicitly), and the negatives: each rule is `stale: false` when only the
*other* lane failed, and `stamped` never gains the field.

**Step 2: Run, confirm failure**

**Step 3: Implement**

`DeskModelInput` gains two optional flags, documented as lane-specific and NOT derivable
from `degraded`:

```ts
  /**
   * Did the last completed cycle fail to refresh THIS lane (ds-jk9)? Both default
   * to false. Unlike `degraded` — which is the aggregate and answers "may the
   * ABSENCE of a card be wrong" — these answer "may the CARD I am about to show
   * be wrong", and they must stay separate: a `/decisions` failure says nothing
   * about whether the open-PR listing is good, and coarse gating on `degraded`
   * would discard the lane that is still fresh.
   */
  approvalsStale?: boolean;
  decisionsStale?: boolean;
```

Both `DeskPendingRollback` and `DeskPendingIac` gain:

```ts
  /**
   * The lane this selection came from did not refresh in the last completed
   * cycle, so what the card shows is the last thing SEEN, not the current state.
   * The card still renders — identity survives a retained value — but the
   * component must withhold the live CTA and the present-tense byline.
   */
  stale: boolean;
```

Stamp it in `deskModel` at each `return`, rather than threading the flags into the
selectors: the selectors decide WHAT is showing, `deskModel` knows WHICH rule fired and
therefore which lane to blame. Keeping selection and freshness separate is what stops a
future rule from silently inheriting the wrong lane.

```ts
  const approvalsStale = input.approvalsStale === true;
  const decisionsStale = input.decisionsStale === true;

  const rollback = selectPendingRollback(decisions, now, input.origin, input.locale);
  if (rollback) return { ...rollback, stale: decisionsStale };
  ...
  const iacFromListing = selectPendingIac(pendingApprovals, decisions, resolvedPrs, input.locale);
  // BOTH lanes: the listing supplies the row, but `resolvedPrs` is an absence
  // claim over `decisions` (see the lane map in the plan header).
  if (iacFromListing) return { ...iacFromListing, stale: approvalsStale || decisionsStale };

  const iacFromDecisions = selectPendingIacFromDecisions(decisions, supersededIds, input.locale);
  if (iacFromDecisions) return { ...iacFromDecisions, stale: decisionsStale };
```

Have the three selectors keep returning their existing shapes minus `stale` (use
`Omit<DeskPendingRollback, 'stale'>` etc. as their return types) so the compiler proves
every construction site goes through `deskModel`'s stamp and none can forget it.

Leave `awaitingCount` alone: `ApprovalDesk` already renders `null` for it whenever
`degraded` is true, which covers both lanes by construction.

**Step 4: Run tests**

**Step 5: Commit** — `feat(ui): deskModel stamps the lane freshness of its selection (ds-jk9, ds-smr)`

---

### Task 3: the desk card stops asserting

**Files:**
- Modify: `frontend/src/components/ApprovalDesk.svelte`
- Modify: `frontend/src/locales/desk.ts` (EN + JA)
- Test: `frontend/tests/unit/ApprovalDesk.test.ts`

This is the task that fails if it only does half the job. **Suppressing the button while
leaving the byline is not a fix** — every one of these is present-tense and would go on
claiming under a failed refresh:

- `desk.pending.rollback.who` "Anchor is proposing a fix"
- `desk.pending.rollback.headline` "A rollback proposal **is waiting for your decision**."
- `desk.pending.iac.who` "An infrastructure change **is waiting for your review**"
- `desk.pending.iacMerged.who` "An approved infrastructure change **is waiting to be applied**"
- `desk.pending.iacView.who` "An infrastructure change **needs attention**"
- all three `*.headlineFallback` strings

**Step 1: Write the failing tests**

```ts
it('a stale pending card keeps its identity and drops its CTA', async () => {
  render(ApprovalDesk, { props: { ...base, decisions: [waitingRow], decisionsStale: true, degraded: true } });
  // identity survives
  expect(screen.getByTestId('approval-desk-pending')).toBeTruthy();
  expect(screen.getByText(/PR #42/)).toBeTruthy();
  // verdict does not
  expect(screen.queryByTestId('approval-desk-apply')).toBeNull();
  expect(screen.queryByTestId('approval-desk-approve')).toBeNull();
  expect(screen.queryByTestId('approval-desk-reject')).toBeNull();
  expect(screen.queryByTestId('approval-desk-continue')).toBeNull();
  expect(screen.getByTestId('approval-desk-view-stale')).toBeTruthy();
  expect(screen.getByTestId('approval-desk-stale-notice')).toBeTruthy();
});
```

**Assert the CLAIM, not just presence** — the #289 lesson that a presence-only test blesses
whatever copy it finds. Add the negative:

```ts
  const card = screen.getByTestId('approval-desk-pending');
  expect(card.textContent).not.toMatch(/waiting for your|needs attention|is proposing/i);
```

and the JA mirror (`待っています|確認が必要`), because a locale that keeps the claim is the
same defect in the language the pitch is delivered in.

**Step 2: Run, confirm failure**

**Step 3: Implement**

- Props: `decisionsStale = false`, `approvalsStale = false`, both optional booleans, both
  passed into the `deskModel(...)` call at line ~115.
- `pendingWhoKey(cta)` gains a stale arm returning `desk.pending.stale.who`. Simpler and
  safer than a fourth CTA kind: `pendingIacCtaState` is load-bearing for the ds-0rm
  never-lose-the-gate invariant and must not learn about freshness.
- `pendingHeadline(m, tf, cta)`: when `m.stale`, prefer the PR title if there is one
  (identity), else `desk.pending.stale.headlineFallback`; for `source === 'rollback'`, use
  `desk.pending.stale.rollbackHeadline`.
- Notice paragraph above `.approval-desk__acts`, `data-testid="approval-desk-stale-notice"`,
  reusing the existing `.approval-desk__notice` class.
- Acts block: `{#if model.stale}` renders ONLY the view-only anchor, testid
  `approval-desk-view-stale`, copy `desk.pending.viewDetailsCta`. The operator loses
  nothing — the HMAC-gated approval page is authoritative and will render the real form or
  a spent-token banner. What they stop getting is the desk *pre-judging* which.

New EN copy (no em dashes — public surface):

```
'desk.pending.stale.who': 'Last seen waiting for you',
'desk.pending.stale.headlineFallback': 'Infrastructure change PR #{pr} was waiting for you when the record was last read.',
'desk.pending.stale.rollbackHeadline': 'A rollback proposal was waiting for you when the record was last read.',
'desk.pending.staleNotice': 'The decision record could not be re-read just now, so this may already be resolved. Open the approval page to see where it actually stands.',
```

Past tense throughout, and it names the evidence ("when the record was last read") rather
than the system state. JA mirrors it: 「最後に確認した時点で…」.

**Step 4: Run tests + `npm run check`**

**Step 5: Commit** — `feat(ui): a stale desk card shows what it saw, not what is true (ds-jk9, ds-smr)`

---

### Task 4: thread the props from App

**Files:**
- Modify: `frontend/src/App.svelte` (the `<ApprovalDesk>` mount, ~2258)
- Test: `frontend/tests/unit/App.test.ts`

**Step 1: Write the failing test.** This task has its own test *because of the #289 round-3
finding*: dropping a hand-threaded prop at the parent reddened NOTHING across 1866 tests —
component tests supply the prop themselves and are structurally blind to `App` failing to,
and an optional prop passes `svelte-check` when omitted. So:

```ts
it('App hands the desk both lane-freshness flags from the store', async () => {
  // /decisions 500s, everything else fine
  ...
  await waitFor(() => expect(screen.getByTestId('approval-desk-stale-notice')).toBeTruthy());
});
```

Driven through App's real fetch mock, not by passing props.

**Step 2: Run, confirm failure**

**Step 3: Implement** — add `decisionsStale={$overview.decisionsStale}` and
`approvalsStale={$overview.approvalsStale}` to the mount.

**Step 4: Verify by injection** (mandatory, not optional): delete each of the two added
lines in turn, re-run the suite, confirm this test reddens for both. Restore from a
scratchpad copy, never `git checkout` — a checkout on a file holding uncommitted work
destroyed a round of #289.

**Step 5: Commit** — `feat(ui): App threads lane freshness to the desk (ds-jk9)`

---

### Task 5: full gates

- `cd frontend && npm run test:unit -- --run` (all green)
- `npm run check` (svelte-check, zero errors)
- `npm run build`
- `uv run ruff check .` at the repo root — no Python changed, but CI's `lint-test` runs it
  and a red PR costs a cycle
- Locale parity: `locales.test.ts` already asserts EN/JA key parity; confirm it covers the
  four new keys rather than assuming it does

### Task 6: injection sweep

For each guard added, disable it and confirm exactly the intended tests redden:
1. `decisionsStale: !d.ok` → `false` in the store
2. the `stale` stamp on rule 1, then 2a, then 2b (one at a time)
3. the `approvalsStale || decisionsStale` on rule 2a reduced to `approvalsStale` alone
4. the `{#if model.stale}` arm in the acts block
5. the stale arm of `pendingWhoKey`

(3) is the one most likely to redden nothing — if so, the `resolvedPrs` test from Task 2 is
not actually exercising the absence claim and needs rewriting, not deleting.

Back up each file to the scratchpad before editing; restore from the copy.

---

## Out of scope, filed

**ds-wer** (the ledger states current verdicts from a retained snapshot) is the same
boundary one surface down and is sequenced AFTER this: it needs `decisionsStale` from the
store, which Task 1 adds. Keeping it out holds this PR to one reviewable claim. Its extra
wrinkle — `DecisionRecord.svelte`'s `{ ...fetched, ...decision }` merge lets the retained
row override freshly fetched fields, so opening a record to check it can re-show the stale
verdict — is a genuinely separate design question, because the trace-cache copy it would
defer to is frozen at first fetch and is not reliably fresher either.
