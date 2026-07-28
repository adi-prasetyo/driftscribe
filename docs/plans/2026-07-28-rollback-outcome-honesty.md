# Rollback outcome honesty — the seal must mean it happened

**Date:** 2026-07-28
**Bead:** `ds-2mc` (P0)
**Status:** IMPLEMENTED, after TWO Codex review rounds (both found real defects
in my drafts — see the two correction sections below).
Gates: 3544 backend · 1423 frontend unit · 21 smoke · 23 visual · 0 typecheck.
Mutation-verified: 10 mutations, each failing only its intended tests.
**NOT yet deployed** — see deploy order at the end.

## Second review correction (Codex, round 2 — post-implementation)

The first implementation was committed as `fc28184` and reviewed again. It held
up on the seal itself but had six real gaps, all now fixed:

1. **Blocker, and the same defect a third time.** `/execute` treated every
   non-timeout exception from `op.result()` as proof of failure. It isn't:
   google.api_core fails the whole future when a POLLING RPC errors and its
   retries are exhausted, while the operation itself may still be running and
   may still apply. Failure is now recorded only when the raw operation is
   `done` with a nonzero error code (`_is_established_failure`); anything else
   is `outcome_unknown`. My test could not have caught this — it raised a bare
   `RuntimeError`, which represents both cases equally.
2. **`/reconcile` had no production caller.** The endpoint and
   `call_reconcile()` both existed with zero invocations — which is precisely
   the "handle written for a poller that does not exist" defect this whole
   change was filed about, reintroduced one level up. Now wired into
   `_memoized_approval_reader` via `_maybe_reconcile`, bounded at 3 round-trips
   per request, fail-soft. And the plan had claimed `/reconcile` tests existed
   when they did not.
3. **A late reconcile fabricated an apply time.** Stamping `resolved_at=now` on
   an operation that completed hours earlier would pop a fresh 判子 reading the
   wrong time — right outcome, invented story. Reconcile now promotes the phase
   WITHOUT a timestamp, so a rollback confirmed only by reconcile never seals.
   The IaC lane already refuses the same move.
4. **Terminal immutability was TOCTOU.** `record_phase` did read-check-write
   non-transactionally. With reconcile as a second post-claim writer, the
   docstring's "the claim already settled all concurrency" was false. Now a
   Firestore transaction.
5. **`concurrency=1` was worse than latency.** `/deny` and `/propose` kept 30s
   budgets while queueing behind a 60s `/execute`; a client timeout does not
   cancel server work, so a timed-out `/propose` could still mint an approval
   whose once-returned token was lost. Both now get a 90s budget.
6. **Rule 2.5 was permanently sticky, and `phase_at` was never plumbed.** An old
   failure outranked every later success forever. Now a later CONFIRMED rollback
   supersedes it, and a stuck `applying` (past `STUCK_APPLYING_MS`) surfaces as
   unknown. `phase_at` is now actually projected and typed — the previous
   ordering comment described behavior the code did not have.

Also corrected: the approval page called `applying`/`outcome_unknown`
"resolved", asserting a terminality the backend does not have.

## Third review correction (Codex, round 3)

Round 2 was committed as `2f989e3` and reviewed again. Five more, all real:

1. **`claimed` was a permanent invisible state.** A crash between the claim and
   the handle write — including the window where `update_service` had already
   accepted the traffic change — leaves `phase=claimed` with no handle. Nothing
   could reconcile it (nothing to look up), rule 2.5 only aged `applying`, and
   once it fell off the four-row ledger the desk rendered resting. A burned
   approval with an unknown outcome, silently. Exactly the "silent `used`" this
   change exists to abolish, hiding one phase to the left. Rule 2.5 now ages
   `claimed` too.
2. **Supersession confused observation order with attempt order.** A rollback
   that succeeded at 10:00 but was only reconciled at 10:07 carried
   `phase_at=10:07`, which "post-dated" and silently buried a DIFFERENT rollback
   that genuinely failed at 10:06. Supersession now compares `created_at`
   (attempt chronology); `phase_at` still picks among unresolved rows (what we
   most recently learned). The round-2 tests could not catch this — in all of
   them the two clocks agreed.
3. **Reconcile was a wall-clock foot-gun.** `call_reconcile` inherited the 30s
   default, and a reconcile for a FRESH `applying` queues behind the very
   `/execute` that owns it on the single-concurrency worker. Three eligible rows
   could burn ~90s of the ~100s edge budget, and `overviewStore` awaits
   `/decisions` alongside the graph and pending fetches, so the whole desk
   stalls. Now a 15s client timeout plus a `_RECONCILE_MIN_AGE_S` gate so a
   live rollback is never chased.
4. **The approval page still lied for `claimed`** ("still running" when nothing
   started) **and for an absent phase** ("resolved" when the outcome is
   unknown). An existing integration test positively pinned the second one; it
   now asserts the honest wording.
5. **Four more false comments**, including one introduced in round 2
   ("nothing is coming back for it" — untrue; a long LRO can exceed the
   threshold and reconcile can still settle it) and two stale claims in this
   very plan.
**Related:** `docs/plans/2026-07-28-composite-redesign-implementation.md` (Tasks 3.0b/3.1/3.4 introduced the defect)

## Defect

`workers/rollback/main.py` `/execute` flips the approval `pending → used` at
`:494` (`store.claim_pending`) and only then calls `_apply_traffic` at `:508`.
Task 3.0b stamps `resolved_at` inside that same transaction
(`driftscribe_lib/approvals.py` `_claim`), so the resolution timestamp records
the *claim*, not the outcome. `_apply_traffic` then explicitly does not block:

> We deliberately don't block on `.result()` — Cloud Run's traffic-shift LROs
> take 10–30s and the coordinator already polls.

The frontend seals on exactly that pair. `desk.ts` `selectStamped` treats
`approval.status === 'used'` + `resolved_at` as a stamped candidate, and
`STAMP_WINDOW_MS` is 10 minutes, so:

- **Every** rollback renders "The proposed rollback was applied." /
  「提案されたロールバックを適用しました。」 for the full 10–30s the LRO is still
  running. The claim is premature 100% of the time.
- If `_apply_traffic` raises (tagged-target race, IAM, quota, API error), the
  approval is **permanently** `used` with `resolved_at` set. `agent/main.py:5951`
  maps the `WorkerClientError` to an HTTP error and never compensates. The desk
  shows the applied seal for ten minutes for a rollback that definitively did not
  happen, then silently decays to resting.

### Two false comments

1. `driftscribe_lib/approvals.py` (`Approval.status`) — "the rollback worker's
   /execute is what actually flips it, and only after actually executing the
   traffic shift." The flip precedes the shift. Introduced by Task 3.0b's commit,
   which corrected a *different* inaccuracy in the same comment.
2. `_apply_traffic` — "the coordinator already polls." Nothing consumes
   `operation_name`; `grep` finds only the return site. No LRO reconcile exists.

### Encoded in tests

`ledger.test.ts:28` is literally named
`'classifies approval.status==="used" as applied (rollback lane)'`, and
`desk.test.ts:417` asserts a `used` approval stamps the desk. Both pin the false
assumption rather than catching it.

## Principle

Separate the **anti-replay claim** from the **outcome record**.

The claim *must* precede the action — that transaction is the single-use HMAC
gate's whole point, and reordering it would reintroduce a replay window. What
must move is the assertion that the action *succeeded*.

This is not a new pattern here. `PlanApprovalStore` (the IaC lane, same file)
already does exactly this and documents why:

> `apply_audit` is an optional nested map written **atomically with the status
> flip** so a worker crash after the claim but before the terminal audit leaves a
> `used` doc whose `apply_audit.phase` records the outcome-unknown state (never a
> silent `used`).

The rollback lane is a silent `used`. The fix is to give it the treatment the
IaC lane already has, reusing the existing `APPLY_AUDIT_PHASES` vocabulary —
which already contains `claimed`, `applied`, `failed`, and
`failed_state_suspect`.

## Review correction (Codex, 2026-07-28)

The first draft of this plan would have replaced a false success with a **false
failure** — the same defect class inverted. Four corrections, all accepted:

1. **`except LroTimeout` does not exist.** The pinned SDK converts polling expiry
   to a bare `concurrent.futures.TimeoutError`
   (`google/api_core/future/polling.py:136`). A hand-invented exception type
   would have made the tests pass while production fell through to the broad
   `except Exception` → `failed` branch. Verified in the installed package.
2. **A polling timeout does not establish failure.** The operation is
   uncancelled and may succeed a second later. Likewise a transport error around
   `update_service()` may be lost-response on a mutation the server accepted.
   Neither may render as "failed."
3. **`failed_state_suspect` means something else.** In the IaC lane it means the
   apply itself failed with possible partial mutation. Reusing it for
   "we couldn't confirm" overloads an existing vocabulary with a second meaning.
4. **120s does not fit the public edge budget.** `worker_client.py:149` states it
   directly: against Cloudflare's ~100s proxied-response budget, "90s read +
   overhead fits, 120 would not." Judges reach the approval POST through the
   proxy.

## Backend

### Phase vocabulary

A rollback-specific `ROLLBACK_PHASES`, **not** a reuse of `APPLY_AUDIT_PHASES`
(see correction 3):

| phase | meaning |
|---|---|
| `claimed` | credential burned, nothing attempted yet |
| `applying` | `update_service` accepted; operation handle persisted; LRO in flight |
| `applied` | LRO confirmed success — the only phase that may seal |
| `failed` | definite refusal, or an LRO that returned a terminal error |
| `outcome_unknown` | mutation may have been accepted and we cannot confirm |

`outcome_unknown` is the honest landing spot for polling expiry and for any
transport error around the mutation call. It must never be rendered as "failed."

### `driftscribe_lib/approvals.py`

- `Approval` gains `apply_audit: dict[str, Any] | None = None` (default for
  backward compat, same convention as `resolved_at`).
- `_claim` gains an explicit `stamp_resolved: bool`:
  - `claim_denied` → `True`. Deny is genuinely terminal at flip time — no Cloud
    Run call, no outcome to await. Correct today; unchanged.
  - `claim_pending` → `False`, writing `apply_audit={"phase": "claimed"}`
    atomically with the flip instead.
- New `record_phase(approval_id, *, phase, detail=None, resolved_at=None)` —
  **transactional. Terminal phases are immutable**: once `applied`/`failed`, a
  later write cannot downgrade it. Stamps `phase_at` on every transition.
  (This bullet originally said "non-transactional (the claim already settled
  concurrency)". That stopped being true the moment `/reconcile` became a second
  post-claim writer — review round 2, finding 4.)

### Timestamps (correction 4 of the review)

The first draft claimed `resolved_at` meant "reached a terminal outcome" while
also withholding it from failures — which cannot both hold. Split:

- `phase_at` — every phase transition. This is what orders a failure in the UI.
- `resolved_at` — **only** a confirmed terminal outcome: `applied`, or `denied`.
  Never written for `claimed`/`applying`/`outcome_unknown`.

### `workers/rollback/main.py`

`_apply_traffic` splits so the operation handle is durable **before** the wait —
otherwise a container death mid-wait leaves neither a handle nor a result, and
the approval is stuck "in flight" forever with nothing to reconcile:

```python
store.claim_pending(id)                       # phase=claimed
try:
    op = _start_traffic_update(rev)           # returns the LRO, does NOT wait
except HTTPException:                         # our own pre-mutation tag re-check
    store.record_phase(id, phase="failed", ...); raise
except Exception:                             # may be lost-response on an accepted mutation
    store.record_phase(id, phase="outcome_unknown", ...); raise
store.record_phase(id, phase="applying", detail={"operation_name": op.operation.name})
try:
    op.result(timeout=_LRO_TIMEOUT_S)         # 60s — see budget below
except concurrent.futures.TimeoutError:
    store.record_phase(id, phase="outcome_unknown", ...); raise HTTPException(504, ...)
except Exception:
    # NOT automatically a failure — result() also raises when a POLLING RPC
    # errors out while the operation itself is still running (review round 2,
    # blocker). Failure is established only by the raw operation being `done`
    # with a nonzero error code.
    phase = "failed" if _is_established_failure(op) else "outcome_unknown"
    store.record_phase(id, phase=phase, ...); raise HTTPException(502, ...)
store.record_phase(id, phase="applied", resolved_at=now)
```

The approval stays `used` on every path — the credential is burned and must not
become reusable. What changes is that the doc records *which* state it reached.

**Timeout budget.** `_LRO_TIMEOUT_S = 60`, not 120. Typical traffic-shift LROs
are 10–30s, so 60s covers the overwhelming majority, and 60s + overhead sits
comfortably under the ~100s Cloudflare budget. The tail beyond 60s lands in
`outcome_unknown` with a durable operation handle — honest, and reconcilable.

**Acknowledged cost.** `--concurrency=1 --max-instances=1` means a blocking
`/execute` serializes the worker: `/propose` and `/deny` queue behind it for up
to 60s. Denying the *same* approval is already moot (its credential is burned),
but an unrelated deny waits. Acceptable for an operator-driven action at demo
scale; stated rather than discovered.

### Reconciliation

New `POST /reconcile {approval_id}` on the rollback worker: for a doc in
`applying`/`outcome_unknown` carrying an `operation_name`, poll that operation
and finalize the phase. This is what makes `outcome_unknown` a temporary state
rather than a permanent shrug.

The coordinator calls it from `attach_approval_status` **only** for rows in a
non-terminal phase, bounded (≤3 per request), fail-soft, memoized per request
like the existing `_memoized_approval_reader`. Because the normal path
terminalizes synchronously inside `/execute`, non-terminal rows are rare and the
common case makes no call at all.

This narrows but cannot fully close the cross-system gap — a successful rollback
followed by a Firestore outage still leaves `applying` until a later reconcile.
That residue is inherent to two systems without a distributed transaction, and
is recorded here rather than claimed away.

### `agent/worker_client.py`

`call_execute` inherits `_HTTPX_TIMEOUT = 30.0`, which a blocking LRO would sit
on top of. Add `_EXECUTE_HTTPX_TIMEOUT` (read ≈75s) above `_LRO_TIMEOUT_S`. This
does **not** reproduce the tofu "timeout-then-skip-merge" divergence its sibling
comment warns about — rollback has no downstream merge step — but a coordinator
transport timeout still cannot establish failure, which is exactly why the
worker-side phase, not the coordinator's exception, is the source of truth.

### `agent/main.py`

`attach_approval_status` additionally projects `apply_audit.phase` as
`approval.phase` — the phase string only, allowlisted, never the raw detail dict
(it can carry API error text) — plus `phase_at`. Same fail-soft, never-synthesize
discipline as the existing `status`/`resolved_at` projection.

## Frontend

- `types.ts`: `DecisionApproval` gains
  `phase?: 'claimed' | 'applying' | 'applied' | 'failed' | 'outcome_unknown' | null`
  and `phase_at?: string | null`.
- `desk.ts` `selectStamped`, rollback lane: require
  `status === 'used' && phase === 'applied'` in addition to a parseable
  `resolved_at`. An absent phase is **not** a stamped candidate — the same stance
  the existing comment already takes for an absent `resolved_at` ("must NEVER be
  treated as 'just now'").
- `ledger.ts` `classify`: `used` + `applied` → applied; `used` + `claimed`/
  `applying` → in-flight; `used` + `failed` → failed; `used` + `outcome_unknown`
  → its own state, worded as unconfirmed, never as failed.
- The two tests named above are rewritten to pin the honest semantics.

### A guaranteed surface for a bad outcome (review finding 5)

The first draft argued a failed rollback "surfaces honestly in the ledger strip."
That is not guaranteed, and Codex was right to reject it: `ledgerRows` sorts by
the proposal's `created_at` (`ledger.ts:28`) and caps at 4 (`DEFAULT_MAX`). A
rollback proposed twenty minutes ago that fails *now* can be pushed out of the
strip entirely by four newer decisions — no seal, no row, hero renders resting.

So `deskModel` gains **rule 2.5**, selected after the pending rules and before
`stamped`:

```
pending rollback > pending iac (2a, 2b) > unresolved outcome (2.5) > stamped > resting
```

Pending still outranks it — something awaiting a decision is more urgent than
something already decided. But a bad outcome outranks a *success seal*, because
the seal is the thing most likely to be mistaken for "all good."

Copy must distinguish the two phases precisely:

- `failed` → "The rollback did not apply."
- `outcome_unknown` → "The rollback's outcome is unconfirmed — verify in Cloud
  Run." Never "failed." This is the whole point of the phase.

I no longer claim this overlaps `ds-eh6`. That bead is about *unloaded* data
being presented as empty; this is *known* terminal state being omitted. Codex's
distinction is correct and my original scope cut was wrong.

### Backward compatibility (review finding 6)

The first draft claimed no old-shape docs exist. That was too strong: the
rollback approval schema predates today, so resolved `used`/`denied` docs may
well exist in Firestore, and repo history cannot prove otherwise. The narrower
claim is the one that actually matters and does hold:

> Old-shape docs may exist, but they lack `resolved_at`, the current selector
> already refuses to seal them, and no released UI ever sealed one. Requiring
> `phase === 'applied'` therefore retracts no seal a user has seen.

No migration is needed for honesty.

### Third false comment (review finding 7)

`ApprovalStore.claim_denied` states the coordinator owns the deny transition
"because the rollback worker only knows the `pending → used` flip." Phase 11.9
moved deny authority to the worker's `/deny` (`workers/rollback/main.py:517`) —
the docstring is stale and now asserts the opposite of the code. Corrected here
with the other two, since this change touches that method's `_claim` signature
anyway.

## Tests

- `test_approval_store.py`: `claim_pending` does not stamp `resolved_at` and does
  write `phase="claimed"`; `record_phase` stamps `resolved_at` only on `applied`;
  terminal phases are immutable (an `applied` doc cannot be downgraded);
  `claim_denied` still stamps.
- Rollback worker, one test per path: success → `applied` + `resolved_at`;
  pre-mutation tag refusal → `failed`, no `resolved_at`; **polling expiry raised
  as a real `concurrent.futures.TimeoutError`** → `outcome_unknown`; LRO terminal
  error → `failed`. Assert the approval is `used` on all four (anti-replay
  preserved) and that `operation_name` is persisted *before* the wait.
  - The timeout test must raise the SDK's actual exception type. Asserting
    against an invented `LroTimeout` is precisely the trap this review caught:
    green tests, broken production.
- `/reconcile`: finalizes an `applying` doc whose operation has since completed;
  is a no-op on a terminal doc.
- `test_decision_approval_status.py`: `phase`/`phase_at` projected; the detail
  dict never projected.
- `desk.test.ts` / `ledger.test.ts`: `used` alone no longer seals or classifies
  as applied; `used` + `applied` does; `outcome_unknown` renders as unconfirmed
  and never as failed; rule 2.5 outranks `stamped` and is outranked by pending.
- A ledger-overflow test for the exact scenario Codex constructed: a failed
  rollback older than four newer decisions still reaches the operator via rule
  2.5.

## Deploy order

`driftscribe_lib/` is shared, so the two services run skewed library versions
for the length of the deploy. Order:

**infra-reader worker → coordinator → `update-traffic` → rollback worker LAST.**

An earlier version of this section had it backwards — rollback worker before
coordinator, reasoning "the projection never reads a field no writer produces."
That protects the harmless direction and breaks the fatal one, because the skew
runs both ways and only one way crashes:

| Skew | What happens |
|---|---|
| New reader, old writer (coordinator first) | `apply_audit`/`resolved_at` absent → dataclass defaults absorb it. `_maybe_reconcile` gates on `apply_audit.detail.operation_name`, which the old worker never persists (it only returns it in the HTTP body), so reconcile no-ops. On the desk, a phase-less doc neither seals (`selectStamped` requires `phase === 'applied'`) nor alarms (`selectUnresolvedRollback` requires an explicit phase) — it falls through to resting. **Degrades to nothing.** |
| Old reader, new writer (worker first) | The worker's `claim_pending` writes `apply_audit` *atomically with the `pending → used` flip*, so the doc gains the key the instant a judge clicks Approve. The old coordinator's `Approval(approval_id=…, **data)` raises `TypeError: unexpected keyword argument 'apply_audit'`. **The approve POST 500s, and so does every later GET of that approval page** — including the always-200 anti-enumeration property of `approval_get` — until the coordinator ships. |

The rollback worker is the writer, so it goes last. (ds-wjw.)

**The order is no longer load-bearing**, and that is the actual fix: all four
read-side construction sites — `ApprovalStore.get`/`_claim`,
`PlanApprovalStore.get`/`_claim` — now project the raw Firestore dict onto the
fields the dataclass declares (`_drop_unknown_fields`). Dropping is the right
reader behavior: an unknown key is one this build has no code to act on.
Read-side only, so the stored doc keeps the newer writer's field and the newer
reader still finds it. Pinned by four tests that were confirmed to fail with the
exact `TypeError` before the projection landed. Deploy in the order above
anyway; it now costs nothing if someone gets it wrong at 2am.

## Live PROD probe (2026-07-28) — found a shipping blocker

Three review rounds could not have caught this one: it is not in the code.

Everything the reviews flagged as "verified against the pinned library but not
against real GCP" checked out on a live call (owner credentials, read-only,
against operation `bc2961ab-…` in `asia-northeast1`):

| Assumption | Live result |
|---|---|
| `_get_services_client().transport.operations_client` | ✅ `google.api_core.operations_v1.operations_client.OperationsClient` (transport is gRPC) |
| `get_operation(name=…, timeout=…)` kwargs | ✅ both are real parameters |
| return shape | ✅ `google.longrunning.operations_pb2.Operation`; `.done`, `.error.code`, `.HasField("response")` all present |
| `_operation_name()` / `_is_established_failure()` on a real LRO | ✅ correct; the `/reconcile` predicate walk promotes to `applied` |

**What was actually broken: IAM.** `rollback-agent-sa` could not call
`operations.get` *anywhere in the project*. Its only Run grant is
`roles/run.developer` bound to the **`payment-demo` service**. That role does
contain `run.operations.get`, but an operation is
`projects/{p}/locations/{l}/operations/{id}` — not a child of the service — so
the binding does not reach it.

Confirmed with Cloud Asset Policy Analyzer, plus three controls to prove the
analyzer was not simply blind: the SA's project-level `datastore.entities.get`
resolved ✅, `run.services.get` via that same service binding resolved ✅, and
`run.operations.get` for the owner account resolved ✅ — only the SA's
`run.operations.get` came back NOT GRANTED.

### Why production could not vouch for it

The tempting shortcut — "`op.result()` polls through the same client, and
rollbacks work live, so the permission must be fine" — is false. The pre-change
`_apply_traffic` fired `update_service` and returned the LRO name **without ever
polling**; that is precisely the ds-2mc defect. `main.py:631` is the only LRO
poll in the entire codebase and this change introduced it. Production had never
exercised `run.operations.get`.

### Blast radius had this shipped

Every rollback: poll raises `PermissionDenied` → `_is_established_failure()` is
correctly `False` (nothing established) → `outcome_unknown` + HTTP 502, while
the traffic shift itself lands fine. `/reconcile` then 403s, so the phase never
settles. Every approval would have reported "we could not confirm your
rollback" — strictly worse than the bug being fixed, and aimed straight at the
demo's headline flow.

`test_execute_broken_poll_is_unknown_not_failed` already pins that exact
behavior. Its docstring reads *"Concretely: `operations.get` starts returning
PermissionDenied after an IAM change."* That was written as a hypothetical. It
was the shipping state.

### Fix

Project-level custom role `driftscribeRunOperationsReader`, one permission,
bound to the rollback SA (matching the existing `driftscribeTofuApply*` pattern;
`roles/run.viewer` was rejected as project-wide read for one needed permission):

```bash
gcloud iam roles create driftscribeRunOperationsReader \
  --project=driftscribe-hack-2026 --permissions=run.operations.get --stage=GA
gcloud projects add-iam-policy-binding driftscribe-hack-2026 \
  --member=serviceAccount:rollback-agent-sa@driftscribe-hack-2026.iam.gserviceaccount.com \
  --role=projects/driftscribe-hack-2026/roles/driftscribeRunOperationsReader
```

Applied and re-verified GRANTED (~80s analyzer propagation). `iac/` declares no
IAM at all, so this creates no OpenTofu drift — and equally, nothing in the repo
protects it. The dependency is recorded in `_get_operations_client()`'s
docstring because it is invisible from the code and would break silently.

**Still unproven:** a real `get_operation` call made *by the worker's own
credentials*. Owner cannot impersonate the SA here (no
`iam.serviceAccounts.getAccessToken`), so the last mile is the post-deploy
rollback on PROD.
