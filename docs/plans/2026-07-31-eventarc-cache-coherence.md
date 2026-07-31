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

### (a) Cause — compare what the agent OBSERVED with what the key names

`run_agent` reports every `read_live_env_tool` response back to `_do_recheck`
through a `reader_sink` list, populated from the ADK `function_response` events
the runner already iterates. The coordinator then compares that observation
against its own post-turn read.

```
_observation_skew(observed_env, analyzed_env) -> list[str]
```

Whole-snapshot comparison, so an ADDED or REMOVED variable counts as skew too,
plus the serving **revision** — env equality alone is weaker than it looks, as
revisions A(mock) → B(live) → C(mock) present identical env at both ends while
the world moved twice.

**One invariant, no degraded branches.** A decision is persisted only when the
agent read live state exactly once, this request could read it too, and the two
agree on env and revision. Everything else is a 409 that records nothing:

| condition | why it refuses |
|---|---|
| agent never read | nothing ties its verdict to any world; `no_op` with `env_diffs=[]` is legal |
| agent read ≥2 distinct states | it watched the world move; "which reading is it about" has no honest answer, and ADK may run calls in parallel so order is not authority |
| post-turn read failed | nothing corroborates that what the agent saw still holds |
| revision unknown or changed | a check that cannot see its subject must fail, not abstain |
| env differs | the ds-q38 case itself |

The refusals sit **after `validate()`** so ds-b3m keeps its own non-retryable
502 for an ungrounded rollback; ours is a retry-shaped 409.

**Two tolerances were deliberately removed**, because both amounted to keeping
the poisoning:

- hashing the key from `proposal.env_diffs` when a read fails (a SUBSET that can
  legally be empty, so a populated service's decision could land under the
  `{}`-env key and collide with a genuinely empty one);
- "the agent already read it" as an assumption rather than an observation —
  precisely the assumption that produced the poisoned row.

Losing an audit is recoverable: the next event re-discovers the drift. A
poisoned idempotency key is not.

**Rejected alternatives, both tried and measured:**

- *Comparing `proposal.env_diffs`* — unsound, see above.
- *Capturing the tool result in a `ContextVar`* — **does not work at all.** ADK
  dispatches each function call as its own `asyncio.Task`, and a Task starts
  from a *copy* of the context, so a write inside the tool is invisible to the
  coordinator. Measured: `child sees {...}` / `parent sees None`.
- *Bracketing the turn with the coordinator's own pre/post reads* — sound about
  the WORLD, silent about whether the agent ever looked, and it cost an extra
  Reader round-trip per audit that the sink does not.

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

### (c) A failed repair must stay retryable — `evict_cached_decision`

Added in round 5, because (b) made a new permanent failure reachable on the one
path that repairs a poisoned key.

`evict_cached_decision` compare-and-deletes the **event pointer**; the decision
document survives, carrying its `event_key`. If the repairing run then dies
before recording a replacement — the Rollback Worker's `/propose` 503s, render
raises, a GitHub side effect fails, the `record_decision` write fails — the next
audit resurrects the stale row through `find_decision_for_event`'s recovery
query (ds-bej), correctly judges it stale, and tries to evict it against a
pointer that **no longer exists**. Under the old contract that CAS could never
succeed again, so the key 409'd forever.

That is strictly worse than the bug being fixed: a wrong answer becomes *no*
answer, permanently, and only on the healing path. So an **absent pointer is now
a success** — there is nothing to clobber and no claim held:

| pointer state | result |
|---|---|
| names `decision_id` | delete, True (ordinary CAS win) |
| names something else | False (a replacement exists; don't clobber it) |
| **absent** | **True (ds-q38) — a failed repair stays retryable** |

Safety rests on the **claim**, not this CAS: minting is gated by
`record_event` (create-if-absent) on both branches — `_do_rollback` claims
before `/propose`, the non-rollback path claims before any side effect. Two runs
that both pass here still serialize one step later.

## Scope / non-goals

- **The gate refuses to PERSIST, not to SERVE.** A cache hit returns before it,
  so an already-cached decision can still be served under a key this request did
  not corroborate. That is a stale answer, not a new poisoned row; closing it
  means moving the cache lookup after the gate, which changes ds-b3m's ordering.
- **ds-bej is still NOT fully fixed here.** (c) removes the permanent *wedge*,
  which was the blocking half. The resurrection itself remains: the recovery
  query still returns a row whose pointer was deleted, and once two rows share
  an `event_key` the arbitrary `.limit(1)` with no `order_by` picks
  unpredictably. That stays on ds-bej. Do not quietly widen this PR into it.
- **The reader read still blocks the event loop.** `worker_client.call` is
  synchronous, so each attempt holds the loop. Pre-existing — the single call it
  replaced blocked for up to 30s — and the retry budget strictly *reduces* the
  worst case rather than growing it (28.5s < 30s). Moving it off-loop is a real
  improvement, filed rather than smuggled in.
- No change to `_cached_rollback_needs_ground_truth` or the rollback TTL.

## Tests

1. **Regression, the real sequence:** model reports `PAYMENT_MODE=mock`, the
   post-ADK observed env is `live` ⇒ no row is written under the drifted key,
   409 raised.
2. Coherence gate passes when reported == observed (the 07-30 trace's shape).
3. A failed post-turn read REFUSES (it no longer hashes a key from model
   diffs), with a bounded retry first so a transient blip does not drop the
   only audit.
4. Agent never read / saw two distinct states ⇒ refused.
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

Five Codex rounds. Every blocker was accepted; **four of the five were defects
in my own fix rather than in the original code** — the fix, not the bug, was
consistently the riskier thing in this change.

- **Round 5 blocker:** deferring the eviction (round 4) made a *permanent* wedge
  reachable — see (c). Verified by reverting the contract and watching
  `test_a_failed_repair_leaves_the_key_retryable` fail at its 409 assertion.
  Also from round 5, both accepted: the retry inherited the 30s default timeout,
  so three attempts could burn ~92s inside a request that may also spend 120s on
  the turn and 90s on `/propose`, against Cloud Run's 300s ceiling — now 9s per
  attempt with a guard pinning the ladder *below* the single call it replaced;
  and the capture tests all called `_emit_event_logs` directly, so dropping
  `reader_sink=` from `run_agent` would have refused every production audit with
  a green suite — now driven through real `run_agent`, verified by injecting
  exactly that regression (8 green, 1 red).

- **Round 1 blocker:** the first draft compared `proposal.env_diffs`, which
  cannot see a `no_op` with an empty diff list — the bug would have survived its
  own fix.
- **Round 2 blocker:** the replacement captured the agent's tool result in a
  `ContextVar`, which **never reaches the coordinator** because ADK runs each
  tool call in its own `asyncio.Task`. Verified by measurement. The fix was
  inert in production while the new tests passed, because the test double wrote
  from `_do_recheck`'s own task — the same "my own test pinned it" shape as
  ds-qua.
- **Round 4 blockers:** the expired-rollback eviction still ran BEFORE the gate
  — a regression I introduced, since adding refusals after that point recreated
  the very ds-bej permanent-409 trap the contradicted-row deferral existed to
  avoid. Both eviction reasons now share one deferred CAS. Also: refusing on a
  failed post-turn read had no retry driver behind it (a failed read emits no
  Eventarc delivery), so one blip could drop the only audit for a real drift —
  now a bounded read-only retry. And the capture seam itself was untested,
  which would have let a regression there refuse every production audit with a
  green suite.
- **Round 3 blockers:** the pre/post bracketing that replaced it proved the
  world was stable but never that the agent LOOKED, so an agent that skipped
  the reader sailed through; and two degraded branches still persisted rows
  under unconfirmed worlds. Replaced with the `reader_sink`, which answers both
  questions with one mechanism and costs no extra Reader call. Round 3 also
  caught that the "53 tests needed no changes" I had cited as evidence was
  worth little — those doubles never read at all. Making them faithful is now
  part of the change.
- **Blocker accepted:** the first draft evicted at cache-lookup time, before
  `validate()`. Combined with ds-bej that is a permanent-409 trap. Eviction is
  now deferred until the proposal has passed the gate.
- **Narrowed:** "every Cloud Run change emits an audit log" → the two wired
  methodName variants (v1 `ReplaceService`, v2 `UpdateService`), with the
  trigger-coverage assumption stated rather than implied.

## Known residuals (not fixed here)

- **ds-bej** — `find_decision_for_event`'s recovery query can resurrect an
  evicted row; every consumer now declines it and (c) keeps that from wedging
  the key, but the row itself persists and the `.limit(1)` tie-break between two
  rows sharing an `event_key` is still arbitrary.
- **The reader read blocks the event loop** (`worker_client.call` is sync).
  Bounded well under the pre-existing worst case, but a slow reader still stalls
  concurrent SSE chat streams on the same instance.
- **The concurrency test is sequential and cannot be otherwise.** The
  lookup→claim window is `await`-free, so no same-process test can interleave
  it; `test_the_lookup_to_claim_window_contains_no_await` pins that assumption
  so it cannot rot silently, and the losing branches are forced deterministically
  instead of raced for.
- The event key hashes env only. `previous_revisions` (best-effort, and the
  source of rollback candidates) and recent-PR evidence are **not** in it, so a
  transient empty candidate list can cache a `drift_issue` that a later valid
  rollback for the same env cannot displace. Same shape as this bug, different
  input; filed as follow-up rather than widened into here.
- **Deploying this does not repair the production row by itself.** `/recheck`
  uses the `manual_recheck` trigger namespace and therefore a different key;
  the poisoned key is `eventarc`-scoped. Repair needs a real Eventarc audit
  while `PAYMENT_MODE=live` — i.e. a live drift injection.
