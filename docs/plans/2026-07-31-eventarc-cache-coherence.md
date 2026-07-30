# ds-q38 — Eventarc cache poisoning: key/decision incoherence

## The bug, as proven (not hypothesised)

The Eventarc audit's idempotency key is built from a **second, independent**
Reader read taken ~12 s *after* the read the model actually analyzed:

- `agent/main.py:1854` — the ADK turn runs; the model calls `read_live_env_tool`
  and reasons about **that** snapshot.
- `agent/main.py:1889` — the coordinator makes its **own** reader call.
- `agent/main.py:1943` — `_event_key(...)` hashes **that second** read's env.

A deploy landing in the window makes the key describe a world the decision never
analyzed. Verified end-to-end on prod (trace `31461232430a4d70…`):

```
05:28:23     eventarc audit starts
05:28:28.553 model read -> rev 00011-gng, PAYMENT_MODE=mock   (pre-drift)
05:28:30     rev 00012-9xp CREATED, PAYMENT_MODE=live         <-- lands MID-AUDIT
05:28:40.505 final_response no_op "no drift present"          (correct for what it read)
05:28:40.810 coordinator post-ADK read -> rev 00012, live     -> DRIFTED key
05:28:40.924 row c76b85c8: world A's verdict under world B's key
```

All three live event keys reproduce exactly with the **container's**
`CONTRACT_PATH=/contract/demo/ops-contract.yaml` (`Dockerfile.agent`), not the
`agent/config.py` repo-relative default:

| key | env | row |
|---|---|---|
| `39b2f614e7c1c910` | `(mock,false)` clean | 05-29 `no_op` ✓ |
| `ae4170632c79c599` | **`(live,false)` DRIFTED** | 07-29 `no_op` ✗ poisoned |
| `e82da8de294c314e` | `(mock,false,DEMO_PROBE)` | 07-29 escalation ✓ |

**Severity: permanent.** `_cached_rollback_is_expired` gives a TTL to
`action == "rollback"` only; a `no_op` never expires. The canonical demo drift
is pinned to "no drift is present" forever. The existing coalescer cannot
recover it — the trailing rerun recomputes the same drifted key and hits the row
the first pass just poisoned.

**Mirror case, also reachable:** model reads drifted, world is fixed mid-audit,
so a ROLLBACK verdict lands under the CLEAN key.
`_cached_rollback_needs_ground_truth` does NOT fire there (`observed_env` is
non-None ⇒ returns False). Bounded to 15 min by the rollback TTL.

## Fix

> **Revised after Codex design review** (thread `019fb3b9-27b3-74a3-9098-088efc05624f`).
> Two findings were blockers and both were accepted; see "What the review
> changed" at the end.

### (a) Cause — coherence gate on two OBSERVATIONS

`agent.request_context.record_analyzed_env` captures the Reader Worker's
response inside `read_live_env_tool`, at the tool boundary, **before the model
sees it** — so the snapshot the agent reasoned over is a coordinator-made
observation, not a model-reported value. `analyzed_env_scope()` binds it per
turn so a reused event-loop task can't inherit turn N-1's snapshot.

```
_observation_skew(proposal, observed_env, analyzed_env) -> list[str]
```

Primary path: whole-snapshot comparison of `analyzed_env` vs `observed_env`, so
an ADDED or REMOVED variable counts as skew too. Falls back to comparing
`proposal.env_diffs` **only** when the agent never read live env this turn.

The fallback must not be primary: `env_diffs` is incomplete by contract — the
drift prompt asks for variables that *differ*, the deterministic classifier
emits `no_op` with `env_diffs=[]`, and the validator accepts that. A diff-driven
check therefore iterates zero entries and pronounces the world coherent no
matter how far it moved: the original bug surviving its own fix.

`observed_env is None` ⇒ **not** skew (the reader-failed path reconstructs
`live_env` from the proposal, so key and proposal are coherent by construction;
ds-b3m's gate owns the action that needs ground truth).

On skew, **refuse to record** — `HTTPException(409)`, count-only logging, no
env names or values in the detail.

**Placement: AFTER `validate()`.** ds-b3m's validator already refuses a skewed
ROLLBACK, and does so with a 502 shaped "the model responded and the safety
gate refused" — non-retryable on purpose. Gating first would convert that into
a retry-shaped 409 and silently retire a distinction ds-b3m's tests exist to
protect. What was missing was never the rollback case: nothing refused a skewed
`no_op`.

Why dropping is safe and self-healing: any mid-audit Cloud Run change emits its
own `ReplaceService` audit log ⇒ its own Eventarc delivery. If it arrives during
the audit, `_EventarcCoalescer` sets `dirty` and the trailing rerun re-audits a
consistent world; if it arrives after, it starts a fresh audit. Note
`_run_eventarc_audit_once` already catches `HTTPException` and logs
`eventarc_background_recheck_rejected`, then the `while` loop honours `dirty` —
so the rerun still happens after a refusal.

**Explicitly NOT the fix:** keying off the model-reported env. ds-b3m — the
idempotency cache is a second route to a rollback approval, so model-reported
values must never define the key.

### (b) Harm — never discard a fresh action for a cached `no_op`

At the cache lookup (`agent/main.py:1953`) the fresh `proposal` is **already in
hand** on both paths (ADK 1854 / classifier 1938). So:

```
_cached_decision_is_contradicted(cached, proposal) -> bool
    cached.action == "no_op" and proposal.action != NO_OP
```

A contradicted row takes the same **CAS-evict → re-propose** route as an expired
rollback (`evict_cached_decision` compare-and-delete), never a plain
fall-through — two concurrent deliveries must not both re-mint.

**The eviction is DEFERRED until after `validate()` and the coherence gate.**
At lookup time the row is only *remembered* (`contradicted`). Evicting there
would let a schema-valid but policy-invalid proposal compare-and-delete a
legitimate cached decision and only then fail the safety gate — and because
`evict_cached_decision` compare-and-deletes a pointer that no longer exists
while the decision document survives (ds-bej), nothing could ever evict it
again. Every later request would take the CAS-loser branch and 409 **forever**.

Every site that serves a cached decision asks the same
`_cached_decision_is_stale`: the initial lookup, the CAS-loser re-read,
`_do_rollback`'s claim-loser re-read, and the non-rollback claim-loser re-read.
Guarding three of four is how this class of bug ships.

This half alone neutralises the already-poisoned production row, which is why no
Firestore surgery is needed.

## Scope / non-goals

- **ds-bej is NOT fixed here.** `find_decision_for_event`'s recovery fallback
  (`decisions.where("event_key","==",…)`) means a CAS-evicted pointer still
  resolves via the field on the decision doc. After (b)'s evict → re-propose the
  new pointer wins on the primary path, so the demo unblocks; the residual
  (arbitrary `.limit(1)` with no `order_by` once two rows share a key) stays on
  ds-bej. Do not quietly widen this PR into that.
- No change to `_cached_rollback_needs_ground_truth` or the rollback TTL.

## Tests

1. **Regression, the real sequence:** model reports `PAYMENT_MODE=mock`, the
   post-ADK observed env is `live` ⇒ no row is written under the drifted key,
   409 raised.
2. Coherence gate passes when reported == observed (the 07-30 trace's shape).
3. `observed_env is None` ⇒ gate does not fire (reader-failure path unchanged).
4. Absent-var handling: `d.live is None` and the name present in observed ⇒
   contradiction; absent in both ⇒ fine.
5. **Poisoned-row recovery:** cached `no_op` under the drifted key + fresh
   rollback proposal ⇒ CAS-evict + re-propose, exactly one approval minted.
6. Cached `no_op` + fresh `no_op` ⇒ still served from cache (idempotency intact).
7. CAS-loser path returns the winner's fresh decision, not the contradicted row.
8. Mirror case: cached rollback under a clean key + fresh `no_op` ⇒ unchanged
   behaviour (documented, TTL-bounded) — pin it so a future change is deliberate.
9. Empty `env_diffs` + moved world ⇒ still refused (the finding that made the
   snapshot primary).
10. A proposal that FAILS `validate()` must leave the cached row and its pointer
    untouched.
11. A refused audit still gets its trailing coalescer rerun.
12. A Firestore-store row resurrected by the `event_key` recovery query after a
    CAS-evict is still recognised as stale (in-memory cannot exercise this).

## What the review changed

- **Blocker accepted:** the first draft compared `proposal.env_diffs`, which
  cannot see a `no_op` with an empty diff list — the bug would have survived its
  own fix. Replaced with a captured, coordinator-trusted snapshot.
- **Blocker accepted:** the first draft evicted at cache-lookup time, before
  `validate()`. Combined with ds-bej that is a permanent-409 trap. Eviction is
  now deferred until the proposal has passed the gate.
- **Narrowed:** "every Cloud Run change emits an audit log" → the two wired
  methodName variants (v1 `ReplaceService`, v2 `UpdateService`), with the
  trigger-coverage assumption stated rather than implied.

## Known residuals (not fixed here)

- **ds-bej** — `find_decision_for_event`'s recovery query can resurrect an
  evicted row; every consumer now declines it, but the row itself persists.
- The event key hashes env only. `previous_revisions` (best-effort, and the
  source of rollback candidates) and recent-PR evidence are **not** in it, so a
  transient empty candidate list can cache a `drift_issue` that a later valid
  rollback for the same env cannot displace. Same shape as this bug, different
  input; filed as follow-up rather than widened into here.
- **Deploying this does not repair the production row by itself.** `/recheck`
  uses the `manual_recheck` trigger namespace and therefore a different key;
  the poisoned key is `eventarc`-scoped. Repair needs a real Eventarc audit
  while `PAYMENT_MODE=live` — i.e. a live drift injection.
