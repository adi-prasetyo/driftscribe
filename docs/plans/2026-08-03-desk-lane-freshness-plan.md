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
rules would be a two-line fix. It is the wrong fix — but **the asymmetry runs one way, and
the first draft of this plan had it backwards** (Codex review, finding 2).

A `/decisions` failure invalidates **every** pending selection the hero can make, the
listing-derived one included. So per-lane gating is NOT here to spare rule 2a from a
decisions outage. It is here so the converse holds: a **pending-approvals** failure says
nothing about a rollback (rule 1) or a decisions-derived IaC card (rule 2b), and gating
those on `degraded` — which fires for either lane — would withdraw two perfectly sound
cards every time the GitHub listing blinked.

`OverviewState.approvalsStale`'s own doc comment argues the same shape for the estate; this
plan applies it to the hero, in the one direction that is actually true here.

## Lane → rule map

`deskModel` runs five selectors in precedence order. Each reads a specific lane, and that
determines which flag makes its result stale:

| Rule | Selector | Reads | `stale` when |
|---|---|---|---|
| 1 | `selectPendingRollback` | `decisions` | `decisionsStale` |
| 2a | `selectPendingIac` | `pendingApprovals` + `resolvedPrs` (from `decisions`) | `approvalsStale \|\| decisionsStale` |
| 2b | `selectPendingIacFromDecisions` | `decisions` | `decisionsStale` |
| 2.5 | `selectUnresolvedRollback` | `decisions` | `decisionsStale` |
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

**Rule 2.5 (`unresolved`) is IN scope** — the first draft excluded it, and Codex was right
that the exclusion does not hold. The argument I gave for excluding it was also factually
wrong, which is worth recording: I claimed a retained `failed` might be a rollback a later
`/reconcile` settled, but terminal phases are immutable
(`workers/rollback/main.py:1043`). The real defect is elsewhere and is worse:

`selectUnresolvedRollback` suppresses an old failure via `newestAppliedAttempt`
(`desk.ts:531`, `:595`) — *"a later success supersedes an earlier failure"*. That is an
**absence claim over `decisions`**, exactly like rule 2a's `resolvedPrs`. A stale list can
omit the later successful rollback, so the old failure keeps rendering as the CURRENT open
loop after the estate has actually recovered. Separately, `outcome_unknown` genuinely is
nonterminal and `/reconcile` can promote it (`workers/rollback/main.py:1025`).

`DeskUnresolved` therefore gains `stale` too, and its copy gets the same last-seen
treatment. Having no CTA is not a defence: the card's whole content is a verdict.

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

  // Rule 2.5. No CTA, but its whole content is a verdict, and its suppression
  // rule (`newestAppliedAttempt`) is an absence claim over the same lane.
  const unresolved = selectUnresolvedRollback(decisions, now);
  if (unresolved) return { ...unresolved, stale: decisionsStale };
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
- `desk.pending.notifyFailed` "...it **has been waiting here** unannounced." (Codex finding
  3 — missed in the first draft, and it renders on its own path, independent of the CTA)

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
  expect(card.textContent).not.toMatch(
    /waiting for your|waiting to be applied|needs attention|is proposing|is approved/i,
  );
```

and the JA mirror (`待っています|確認が必要|承認済み|提案しています`), because a locale that
keeps the claim is the same defect in the language the pitch is delivered in. Codex finding
5: the first draft's regexes covered only three of the six asserting variants, which would
have let `iacMerged` ("is approved and waiting to be applied") through the negative
assertion entirely.

**Step 2: Run, confirm failure**

**Step 3: Implement**

- Props: `decisionsStale = false`, `approvalsStale = false`, both optional booleans, both
  passed into the `deskModel(...)` call at line ~115.
- **The byline stale-check must sit ABOVE the source split, not inside `pendingWhoKey`.**
  Codex finding 3: the rollback arm never calls that helper — the template has a direct
  ternary (`model.source === 'rollback' ? ... : $t(pendingWhoKey(cta))`), so a stale arm
  added inside the helper would silently miss every rollback card. One `{#if model.stale}`
  around the whole byline. `pendingIacCtaState` still must not learn about freshness: it is
  load-bearing for the ds-0rm never-lose-the-gate invariant.
- `notifyFailed`: suppress on a stale card. Its claim is "this has been sitting here
  unannounced", which is precisely a current-waiting assertion, and the stale notice below
  it already tells the operator the card may be resolved.
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
'desk.pending.stale.headlineFallback': 'Infrastructure change PR #{pr} was waiting for you when this was last checked.',
'desk.pending.stale.rollbackHeadline': 'A rollback proposal was waiting for you when this was last checked.',
'desk.pending.staleNotice': "This card's current status could not be refreshed just now, so it may already be resolved. Open the approval page to see where it actually stands.",
'desk.unresolved.stale.detail': 'Last checked',
'desk.unresolved.staleNotice': "This outcome could not be re-checked just now, so a later rollback may already have settled it.",
```

Past tense throughout, naming the evidence rather than the system state.

**The notice must be lane-NEUTRAL** (Codex finding 3): the first draft said "the decision
record could not be re-read", which is simply false when only `/infra/pending-approvals`
failed. One card can be stale from either lane, and the copy cannot name a lane it does not
know. JA mirrors it: 「最後に確認した時点で…」.

**Step 4: Run tests + `npm run check`**

**Step 5: Commit** — `feat(ui): a stale desk card shows what it saw, not what is true (ds-jk9, ds-smr)`

---

### Task 4: thread the props from App

**Files:**
- Modify: `frontend/src/App.svelte` (the `<ApprovalDesk>` mount, ~2258)
- Test: `frontend/tests/unit/App.test.ts`

**Step 1: Write the failing tests.** This task has its own tests *because of the #289
round-3 finding*: dropping a hand-threaded prop at the parent reddened NOTHING across 1866
tests — component tests supply the prop themselves and are structurally blind to `App`
failing to, and an optional prop passes `svelte-check` when omitted.

**TWO scenarios, not one** (Codex finding 4). One decisions-failure test cannot
mutation-prove both props: deleting `approvalsStale` at the mount would not redden it. And
each scenario must be **good → then failed**, because a first-cycle failure leaves the
initial empty sentinel with no retained card to show, so no stale notice could appear and
the test would pass for the wrong reason:

```ts
it('App hands the desk decisionsStale from the store', async () => {
  // cycle 1: a decisions-derived pending card lands
  // cycle 2: /decisions 500s, pending + graph fine
  await waitFor(() => expect(screen.getByTestId('approval-desk-stale-notice')).toBeTruthy());
});

it('App hands the desk approvalsStale from the store', async () => {
  // cycle 1: a LISTING-derived pending card lands (decisions empty)
  // cycle 2: /infra/pending-approvals 500s, decisions + graph fine
  await waitFor(() => expect(screen.getByTestId('approval-desk-stale-notice')).toBeTruthy());
});
```

Driven through App's real fetch mock, not by passing props.

**Step 2: Run, confirm failure**

**Step 3: Implement** — add `decisionsStale={$overview.decisionsStale}` and
`approvalsStale={$overview.approvalsStale}` to the mount.

**Step 4: Verify by injection** (mandatory, not optional): delete each of the two added
lines in turn, re-run the suite, confirm the MATCHING test reddens and — the point of
having two — that each prop has a test that fails only for it. Restore from a scratchpad
copy, never `git checkout`; a checkout on a file holding uncommitted work destroyed a round
of #289.

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
