# Rollback outcome honesty — the seal must mean it happened

**Date:** 2026-07-28
**Bead:** `ds-2mc` (P0)
**Status:** IMPLEMENTED. Codex-reviewed (findings folded in — see the review
correction section below; the first draft would have shipped a false *failure*).
Gates: 3522 backend · 1419 frontend unit · 21 smoke · 23 visual · 0 typecheck
errors. Mutation-verified: 6 mutations, each fails only its intended tests.
**NOT yet deployed** — see deploy order at the end.
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
  non-transactional (the claim already settled concurrency). **Idempotent, and
  terminal phases are immutable**: once `applied`/`failed`, a later write cannot
  downgrade it. Stamps `phase_at` on every transition.

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
    store.record_phase(id, phase="failed", ...); raise HTTPException(502, ...)
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

`driftscribe_lib/` is shared. Per the existing convention and the Task 3.0b
deploy note: **infra-reader worker → rollback worker → coordinator →
`update-traffic`**. The rollback worker must land before the coordinator so the
projection never reads a field no writer produces.
