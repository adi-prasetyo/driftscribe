# Estate Truthfulness + Ledger Subject Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stop three surfaces from making claims they cannot support — a ledger row that says "applied" without naming what, an estate that calls a resource "IaC-managed" when it has only proved "declared", and a snapshot that goes stale in silence.

**Architecture:** Task 1 is a self-contained frontend fix. Tasks 2–3 change what `/infra/graph` may assert. The central decision, arrived at after review: **do not try to prove apply-completion. Weaken the claim to what the evidence supports.** That is cheaper, safer, and it dissolves the dependency the first draft of this plan was built around.

**Tech Stack:** Svelte 5 runes + vitest/jsdom, Python 3.12 + pytest, Playwright, OpenTofu, Cloud Run.

> **Revision history.** v1 of this plan proposed deriving "not applied" from decision docs and reclassifying resources per-resource via a GitHub source join. Codex review (thread `019fc377`) refuted both, with citations that verified. v2 is a materially different plan. The rejected approaches are recorded in "Approaches rejected" at the bottom — they look reasonable, and the next person will otherwise re-propose them.

---

## Implementation record (2026-08-03, branch `fix/estate-truthfulness-ledger-subject`)

Tasks 1, 3a and 2 are **built and green**. Tasks 4 and 5 remain as filed beads.

| Commit | Task |
|---|---|
| `203c329` | Task 1 — `ds-bch`, the ledger row names its subject |
| `fc85094` | Task 3a backend — `ds-1vn`, `build_graph` reports snapshot freshness |
| `1354f7b` | Task 3a frontend — `ds-1vn`, the estate discloses it |
| `3a500bf` | Task 2 — `ds-403`, "declared in IaC" |

### Four deviations from v2, and why

**1. The freshness comparison is worker-tree vs COORDINATOR-tree, not vs `main`.**
v2 said "compare against main's current `iac/` tree hash" without saying how the
serve path would obtain it. It cannot cheaply: `Dockerfile.agent` does not bundle
`iac/`, `.gcloudignore` excludes `.git`, and reading `main` at request time puts a
GitHub call (and its rate limits, latency and outage mode) on the landing page.

So both sides hash their own baked `iac/` with the canonical `iac_tree_hash()`, and
`Dockerfile.agent` gains `COPY iac/`. What that proves is narrower than "current with
main", and the copy says so: *"than the running deployment"*. Since the coordinator
ships from `main` HEAD on every merge and the worker ships rarely, a mismatch means
the worker is behind in practice — but two equally-behind sides report a match, and
overstating that would be the same class of error this whole plan is about.

Hashing a tree the process physically holds also beats a build-time stamp, which can
drift from the content it claims to describe.

**2. No global `provisional` flag.** v2's Task 3 test asserted `g["provisional"] is
True` whenever freshness was unverifiable. Unverifiable is prod's *normal* state until
infra-reader is redeployed, so that flag would hedge every number on the page
indefinitely — and a permanent hedge stops being read. The substance of the
`rollback_deploy_config_pr259` lesson is that "could not check" must not render as
"checked and fine"; that is satisfied by a distinct, visible, tested `unverified`
notice. Shipped that instead.

**3. No L2 cache format bump.** The L2 doc caches the WORKER's inventory and
`build_graph` re-runs on read, so a pre-change cached doc simply lacks
`iac_tree_hash` and reports unverified — correct, and without invalidating every
cached inventory and forcing the fleet onto the ~30s CAI path (#244's v3→v4 lesson).

**4. Task 2 changed no logic at all.** The diff is locale catalogs plus one comment.
`estate.test.ts:148` already pins that `managed` rows are never adoptable, so the
`infra_graph.ts:772-797` hazard v2 flagged is structurally out of reach.

### Codex review round 2 (thread `019fc3a1`) — six findings, all verified, all acted on

Two were **High**, and both were the same mistake: I fixed the *sentence* and left
the *action*.

1. **A stale snapshot still armed the Adopt button.** The notice rendered while
   `model.drift` stayed fully actionable — so the estate disclosed that its declared
   set came from a different tree, then invited the operator to act on it anyway.
   That is the ds-1vn incident verbatim. Adoption is now suppressed with its own
   reason chip when `stale`. Deliberately **not** on `unverified`: that is absence of
   evidence, and it is the state every build before this one shipped in — disabling a
   working control there would let a check that cannot see its subject remove the
   thing it cannot evaluate. The tour's adopt spotlight is nulled alongside it, or it
   would point at a control that is no longer there.

2. **A failed refresh kept vouching for freshness.** `runCycle` retains the previous
   graph when `/infra/graph` fails — right for the numbers, wrong for
   `iac_snapshot_stale`, because that field is an *assurance*. A retained `false`
   reports "checked, current" about a check that did not run. Added `graphStale` to
   the store (sibling of the existing `approvalsStale`) and degraded a retained
   `fresh` to `unverified`. Ordered AFTER the `true` test: a mismatch already observed
   does not stop being true because a later fetch failed.

3. **`contract_status` is model-authored and the rollback gate does not trust it**
   (`validator.py:324` derives the real verdict and never rewrites the proposal;
   `main.py:1988` persists the LLM's value). So a genuine violation mislabelled
   `match` is filterable by the ledger. Survivable, and now documented at the call
   site: the subtitle is identity, not a verdict, and `target_revision` is enforced
   for every rollback (`validator.py:219`), so the worst case is a row naming the
   revision instead of the variable — never the blank subject that caused the misread.

4. **"Older" was not derivable from the evidence.** Hash inequality is symmetric, the
   prescribed deploy order (worker first) makes worker-newer routine, and
   `iac/.terraform.lock.hcl` is inside the hashed tree, so a provider bump alone flips
   it. Copy now says "a different iac/ snapshot", which is what the check proves.

5. **The approval page still carried the retired claim.** `iac_approval.html` promised
   the map and meter "count it as managed once the change merges" — wrong twice over,
   since those surfaces count declarations and the worker's snapshot does not move on
   merge. Fixed EN+JA, along with the test that had **pinned the obsolete wording**.

6. **The cache tests over-claimed.** The header said both layers; only L1 was
   exercised. Added real L2 coverage, including a v4 doc written before the field
   existed. Verified by injection: dropping the local hash from the L2 read alone now
   reddens four tests, where before it reddened none.

### What the work found that the plan did not anticipate

- **A `$derived` guard that caught nothing.** The `graph.degraded` arm of
  `snapshotFreshness` reddened no test when deleted — the template already gates it.
  Removed, following the convention `EstateView`'s `unmatched` derived already
  records for the identical situation. The degraded *behaviour* stays pinned by a
  test, where it is real.
- **The bug appeared in the fixture helper first.** The visual rig's `graphBody`
  wrote `opts.snapshotStale ?? false`, and `??` treats `null` as nullish — collapsing
  a deliberate "could not verify" into "verified fresh". Exactly the conflation
  `ds-1vn` exists to stop, one layer below the code it was written to test.
- **Live data confirmed both of Task 1's arms.** Prod `/decisions` holds seven
  rollbacks: the applied one carries `FEATURE_NEW_CHECKOUT` at `match` beside
  `PAYMENT_MODE` (so the filter is exercised), and a 2026-07-29 doc has a
  `target_revision` and no diffs at all (so the fallback is too).

---

## Background: the incident

On 2026-08-02 an operator read the live desk ledger and concluded PR #168 had been applied. It had not.

```
00:31  ◍  Approved · awaiting apply
          Adopt Pub/Sub topic adopt-probe-topic into IaC management (zero-change import)
11:18  ✓  Approved · applied
          (no subject)
```

The 11:18 row is a **rollback** of `PAYMENT_MODE` — a different decision. It renders with no subject, so the eye carries the subject down from the row above.

| Fact | Value | Source |
|---|---|---|
| PR #168 merged | `2026-07-31T15:31:05Z` | `gh pr view 168` |
| `.tf` on main | `3de3123`, `iac/adopt_topic_adopt_probe_topic.tf` | `git log` |
| Decision status | `waiting_for_rebake` + `merged` | `GET /decisions` |
| **Tofu state last written** | **`2026-07-08T08:58:01Z`** | `gsutil ls -l gs://…-tofu-state/prod/` |
| infra-reader snapshot | `IAC_SNAPSHOT_SHA=f72ef298…` (07-29) | `gcloud run services describe` |

Beads: `ds-bch`, `ds-403`, `ds-k46`.

---

## The decision that shapes this plan

The estate labels resources **"IaC 管理下" / "IaC-managed"**. It computes that from HCL declarations only (`driftscribe_lib/infra_inventory.py:1-6`, via `iac_hcl.DeclaredIdentity`); it never reads tofu state.

**Proving actual state membership is not available cheaply, and decision docs cannot substitute for it.** A resource can be in state with no `applied` decision:

- The apply worker finishes `tofu apply` (`workers/tofu_apply/main.py:691-694`), writes its audit afterward non-transactionally (`:744-757`), and the coordinator writes the `applied` decision later still (`agent/main.py:7586-7591`). A failed Firestore write leaves state changed and the newest decision reading `waiting_for_rebake`.
- The recovery runbook documents this outright. `docs/runbooks/iac-apply-failure-recovery.md` line ~317: *"exists | exists | likely a **complete-but-unrecorded apply**"*. It also documents state-only applies (`:145-152`) and manual `tofu import` (`:313-318`) as supported operator procedures.
- `failed_state_suspect` and `ambiguous` explicitly mean membership is *uncertain* (`workers/tofu_apply/main.py:713-737`).

So decision docs support **"last recorded as awaiting apply."** They cannot support **"definitely not in state,"** and — this is the part the first draft missed — they cannot make the *remaining* resources "definitely managed" either.

**Therefore: fix the label, not the inference.** The estate says "declared in IaC"; that is exactly what it proves. State-derived membership stays available as a later, larger piece of work (see Task 5).

This dissolves the `ds-403` → `ds-k46` block. Once the label is honest, refreshing the snapshot makes a *true* claim more current instead of making a *false* claim more confident.

---

## Task 1: `ds-bch` — a rollback row must name its subject

**Files:**
- Modify: `frontend/src/components/LedgerStrip.svelte:173-178`
- Test: `frontend/tests/unit/LedgerStrip.test.ts` (fixture factory at `:19`)

`subtitleFor()` only reads `pr_title`/`pr_number`. A rollback carries neither — but it does carry `diffs`, each with `name` + `contract_status`, plus `target_revision`.

**⚠️ Naming every diff would be a new lie.** The live payload carries `FEATURE_NEW_CHECKOUT` at `contract_status: "match"` — context, not drift. Only `PAYMENT_MODE` (`present_disallow_manual`) is the subject.

**Rule:** exclude the two known non-violations (`match`, `present_allow_manual`); name everything else *including an unrecognised status*. Positive exclusion set, never `!isViolation` — unknown must fail toward naming (rule (i), `desk_awaiting_rebake_and_ledger_dedup.md`).

Review corrections folded in:
- **Gate to rollback rows.** Do not derive a subject from `diffs` on arbitrary actions.
- **Deduplicate names.** Identical duplicate diffs are permitted by `agent/validator.py:275-286`.
- **Clamp for overflow.** A long variable name must not break the strip.
- **The subtitle means "this decision concerns X", not "X was proven to violate policy."** That is what makes naming an unknown status safe.

**Step 1: Write the failing tests**

```ts
it('names the drifted variable on an applied rollback row', () => { /* expect 'PAYMENT_MODE' */ });
it('does NOT name a diff whose contract_status is match', () => { /* expect not 'FEATURE_NEW_CHECKOUT' */ });
it('names a diff with an unrecognised contract_status', () => { /* rule (i) */ });
it('deduplicates repeated diff names', () => { /* two identical diffs -> one name */ });
it('falls back to target_revision when a rollback has no violating diffs', () => {});
it('does not derive a diff subject for a non-rollback action', () => {});

// The honest invariant. NOT "never undefined" — a decision with no identity
// at all must still render no <small>, because a placeholder would be worse.
it('renders a subject whenever the decision carries identity', () => {
  for (const d of [rollbackWithDiffs(), rollbackNoDiffsWithRevision(), iacApplied()]) {
    expect(subtitleOf(d)).toBeTruthy();
  }
});
it('renders no <small> at all when no identity exists', () => {
  expect(rowFor(bareDecision()).querySelector('.ledger-strip__title small')).toBeNull();
});
```

> v1 of this plan asserted "never renders a terminal applied row with no subject" while the implementation could still return `undefined`. The test and the code disagreed. The pair above is what is actually achievable and true.

**Step 2:** `cd frontend && npx vitest run tests/unit/LedgerStrip.test.ts` → FAIL.

**Step 3: Implement**

```ts
/** contract_status values that are NOT a violation. A positive exclusion set,
 *  not `!isViolation`: an unrecognised status must fall through to being NAMED.
 *  The subtitle asserts "this decision concerns X", never "X violated policy",
 *  which is what makes naming an unknown safe. */
const NON_VIOLATION_STATUS: ReadonlySet<string> = new Set(['match', 'present_allow_manual']);

function rollbackSubject(d: Decision): string | undefined {
  if (d.action !== 'rollback' || !Array.isArray(d.diffs)) return undefined;
  const seen = new Set<string>();
  for (const raw of d.diffs as unknown[]) {
    if (!raw || typeof raw !== 'object') continue;
    const o = raw as Partial<EnvDiff>;
    if (typeof o.name !== 'string' || o.name === '') continue;
    const status = typeof o.contract_status === 'string' ? o.contract_status : '';
    if (NON_VIOLATION_STATUS.has(status)) continue;
    seen.add(o.name);
  }
  return seen.size > 0 ? clamp([...seen].join(', ')) : undefined;
}
```

Then in `subtitleFor`, after the PR fields: `rollbackSubject(d)`, then `d.target_revision`, then `undefined`.

**Do NOT reuse `diffRows()`** (`lib/diff.ts`) — it clamps names for a table and formats values through `displayDiffValue`. Different filter, different truncation contract.

**Step 4:** tests green, then **delete the `NON_VIOLATION_STATUS.has(status)` line** — the `match` test must redden, and only it. Restore from a scratchpad copy (never `git checkout` a file holding uncommitted work).

**Step 5: Commit**

```bash
git add frontend/src/components/LedgerStrip.svelte frontend/tests/unit/LedgerStrip.test.ts
git commit -m "fix(ui): an applied ledger row now names what it applied (ds-bch)"
```

*(No locale change: the subtitle is data, not copy. v1 listed `locales/desk.ts` and added no strings.)*

---

## Task 2: `ds-403` — say "declared", because that is what we proved

**Files:**
- Modify: `frontend/src/locales/infra.ts` + `desk.ts` (EN **and** JA — parity enforced by `locales.test.ts`)
- Modify: `frontend/src/components/EstateView.svelte`, `InfraDiagram.svelte` (group headings, legend)
- Test: `frontend/tests/unit/`, `frontend/tests/smoke/`

Rename the operator-facing claim from "IaC-managed" to "declared in IaC" across the instrument band, estate group headings, legend, and card copy. The internal `managed` boolean can keep its name; this is a copy change, not a data-model change.

**⚠️ Do NOT flip `managed` to `false` for anything.** `frontend/src/lib/infra_graph.ts:772-797` routes any non-`managed`, non-`control_plane` node into an **adoptable** row with an Adopt button. Demoting a declared resource would invite the operator to open a *second* adopt PR for something already declared. This is the single most dangerous thing in the vicinity of this work.

**Grep the vocabulary, not the identifier** (rule (iii)). "managed", "管理下", "under IaC management" appear across locales, the legend, the tour, and the card.

**Step 5: Commit**

```bash
git commit -m "fix(infra): the estate says 'declared in IaC', which is what it proves (ds-403)"
```

---

## Task 3: `ds-k46` — disclose a stale snapshot, using the right identity

**⚠️ Two corrections from review, both load-bearing.**

**3a — Compare the `iac/` TREE HASH, not the commit SHA.** A docs-only commit moves main HEAD without touching `iac/`; comparing commit SHAs would report "stale" forever, and an `iac/`-path deploy trigger could never clear it. The canonical machinery already exists: `driftscribe_lib/iac_tree.py:94 iac_tree_hash()`, and the apply worker uses the same concept at `workers/tofu_apply/main.py:442-451`. Stamp an infra-reader IaC tree hash; compare against main's current `iac/` tree hash.

**3b — The caveat channel will not show this.** `InfraDiagram.svelte:887-889` renders `graph.caveat` only `{#if graph && !degraded}`, and `EstateView.svelte:138-140` replaces the entire estate with a generic degraded message. So reusing `degraded`/`caveat` either hides the disclosure or hides the whole estate. **Staleness needs its own visible state** — the estate still renders, with a specific "this is a snapshot from X, main has moved" line. Add a tested staleness reason, not just a JSON field.

**Step 1: Failing tests**

```python
def test_graph_discloses_a_stale_iac_snapshot():
    g = build_graph({**INV, "iac_tree_hash": "a" * 64}, main_tree_hash="b" * 64)
    assert g["iac_snapshot_stale"] is True
    assert g["iac_snapshot_reason"]           # a shown reason, not a silent flag

def test_not_stale_when_tree_hashes_match():
    g = build_graph({**INV, "iac_tree_hash": "a" * 64}, main_tree_hash="a" * 64)
    assert g["iac_snapshot_stale"] is False

def test_docs_only_commit_does_not_read_as_stale():
    """main HEAD moved; iac/ did not. The whole reason for a tree hash."""

def test_unknown_main_hash_does_not_claim_freshness():
    g = build_graph({**INV, "iac_tree_hash": "a" * 64}, main_tree_hash=None)
    assert g["iac_snapshot_stale"] is None
    assert g["provisional"] is True           # cannot verify => must not render confident numbers
```

That last assertion is the point: a check that cannot see its subject must fail, not abstain into a green claim (`rollback_deploy_config_pr259`). v1 asserted only `is None`, which would still have let the UI paint confident totals.

*(Note the widths: a tree hash is 64 hex chars. v1's fixtures used `"old" * 13` — 39 characters, not even a valid commit SHA.)*

**Step 5: Commit**

```bash
git commit -m "fix(infra): the estate says so when its IaC snapshot is behind main (ds-k46)"
```

---

## Task 4 (follow-up bead): decision-derived pending overlay — ADVISORY ONLY

Once the label is honest, a *provisional* advisory is still worth having: "1 declaration merged, apply not confirmed." Estate-level, never a per-resource reclassification.

If built, it **must** treat `failed_state_suspect` and `ambiguous` as unconfirmed too, not only `waiting_for_rebake` — those states mean membership is uncertain, and folding them into "fine" is a confident false positive in the dangerous direction. Scope by newest-per-`event_key` (`event_key` hashes `head_sha`, so it names the generation — `agent/main.py:6406`); decision docs accumulate and a stale witness sits there forever (`ds-0rm`, four separate times).

## Task 5 (separate design bead): real state membership + automated refresh

Two pieces, each needing its own design:

1. **State-derived membership.** Expose a narrow, redacted manifest from `tofu-apply`, which already holds the KMS/state permissions — only `(asset_type, canonical_identity)` plus state lineage, serial, and read time; degrade on failure. Do **not** grant `infra-reader-sa@` decrypt access.
2. **Automated snapshot refresh.** `infra/cloudbuild.infra-reader.yaml:1-29` is operator-run with no trigger, and the GitHub WIF plan-builder identity is write-only to the artifact bucket and explicitly *not* a deploy identity (`infra/scripts/setup_iac_backend.sh:300-308,:413-417`). This needs a narrowly scoped automation identity, IAM, bootstrap changes, concurrency behaviour, failure visibility, and a deployment test. "Add a workflow trigger" is not a design.

---

## Order

1. **Task 1** — independent, ship anytime.
2. **Task 3a** — detection + disclosure. Suppresses a known-bad surface; do this before anything makes the snapshot fresher.
3. **Task 2** — the rename. After this, the estate's claim is true.
4. **Task 4** — advisory overlay, optional.
5. **Task 5** — the real fix, separate design.

`ds-k46` is currently blocked by `ds-403` in beads. That block was correct for v1's plan; under v2 the two are independent once the rename lands, and `ds-k46` should be split into 3a (detect) and 3b (refresh), with only 3b sequenced after the rename.

## Gates before PR

```bash
cd frontend && npm run check && npx vitest run && npm run build
npm run test:smoke      # ⚠️ requires `npm run build` FIRST — serves gitignored agent/static/
npx playwright test tests/visual
uv run ruff check .     # local pre-commit SKIPS ruff; CI's lint-test does not
uv run pytest
```

`ui-smoke` is a **required** CI job.

## After merge

Deploy is operator-facing. `driftscribe_lib` is baked into both images, so a change there ships worker **and** coordinator (`unmatched_iac_declarations_pr244`). Follow the `driftscribe-deploy` skill.

**Verify on prod** that `adopt-probe-topic` reads as *declared*, carries no second Adopt button, and that the estate discloses its snapshot age.

## Approaches rejected (do not re-propose without reading this)

- **Teach `infra-reader` to read tofu state.** State is KMS-encrypted; this means permanent decrypt access for a read-only worker rendering a landing page. Use a manifest from `tofu-apply` instead (Task 5).
- **Derive "not applied" from decision docs and reclassify per resource.** Decision docs cannot prove state membership — see "The decision that shapes this plan". Complete-but-unrecorded applies are documented and supported.
- **Join an unapplied PR to its declared identities via the GitHub source view.** `driftscribe_lib/github.py:940-964` returns only added/modified files (not removed/renamed), caps at 25 files / 768 KiB, and may return `content=None`; parsing the whole post-change file over-attributes every unchanged block in it; and cross-file variable defaults (`iac_hcl.py:132-142`) mean changed files alone are not sufficient to resolve identities. If precise attribution is ever needed, persist the verified plan's affected identities on the decision at `waiting_for_rebake` write time — the coordinator already holds the verified view at `agent/main.py:7351-7367`.
- **`build_graph(inventory, unapplied: set[str])`.** Exact matching happens before aggregation (`infra_inventory.py:153-190`), only ten samples per type retain names (`:202-235`), and full unmatched identities are stripped before L1/L2 persistence (`agent/main.py:4240-4251`) — the DTO cannot repair counts afterward. A bare string set is also the wrong key; identity is at least `(asset_type, canonical_identity)`.

## Not in scope

Resuming the #168 apply — an operator action against live infrastructure.
