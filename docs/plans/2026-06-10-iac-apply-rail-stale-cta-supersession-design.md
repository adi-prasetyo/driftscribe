# Design — `iac_apply` rail: retire stale "Review & approve →" CTAs + show per-row status

**Date:** 2026-06-10
**Status:** DRAFT (pending Codex plan review)
**Surface:** operator SPA decisions rail — `frontend/src/components/DecisionsRail.svelte`,
helpers in `frontend/src/lib/approval.ts` + `frontend/src/lib/format.ts`, type in
`frontend/src/lib/types.ts`. **Frontend-only — no backend / serve-path change.**

## Problem

A single create-class `iac_apply` legitimately produces **three** decision rows,
and the rail makes them look like confusing duplicates:

| created_at | apply_status | merge_state | trace | what it is |
|------------|-------------|-------------|-------|------------|
| Jun 4 11:53:29 | `waiting_for_rebake` | `pending` | T1 | 1st approval, recorded *before* the irreversible PR merge (crash-recovery pointer, `main.py:3243`) |
| Jun 4 11:53:36 | `waiting_for_rebake` | `merged` | T1 (same) | same click, *after* merge — instructs re-bake (`main.py:3294`) |
| Jun 5 01:27:33 | `applied` | `merged` | T2 (new) | 2nd approval — terminal success (`main.py:3436`) |

The **data is correct** — each row is a distinct, intentionally-persisted resume
pointer for a two-phase (provision → re-bake → apply-again) create-class flow.
Two genuine UX faults remain at the **presentation** layer:

1. **Stale CTA.** Both `waiting_for_rebake` rows render **"Review & approve →"**
   (`DecisionsRail.svelte:37`, `iacApproveLabel` keys only on
   `apply_status === 'waiting_for_rebake'`). Once the later `applied` row exists
   for the same PR, that work is *done* — those two rows are superseded, yet they
   present as live, actionable approvals. This mirrors the earlier Codex finding
   (PR #71) that resolved plans must not show a stale "Review & approve" affordance.
2. **Indistinguishable rows.** All three rows show the same title (`PR #68 →`),
   subtitle (PR title), and meta (`iac_apply · ⎇ 0496b30`). Nothing on the row
   says *which lifecycle state* it is, so they read as accidental duplicates.

## Goal

Make the lifecycle rows self-explanatory and stop advertising resolved work as
actionable — **without** collapsing rows (that would erase the crash-recovery
audit trail the docs deliberately keep; see the prior design's out-of-scope note).

```
┌──────────────────────────────────────────────┐
│ PR #68 →                          Jun 5, 01:27 │
│ infra(checkout): storefront + orders-worker …  │
│ iac_apply · applied · ⎇ 0496b30                │  ← status token added
│ open trace →     Open approval page →          │  (view-only — unchanged)
├──────────────────────────────────────────────┤
│ PR #68 →                          Jun 4, 11:53 │
│ infra(checkout): storefront + orders-worker …  │
│ iac_apply · awaiting re-bake · ⎇ 0496b30       │  ← status token
│ open trace →     Open approval page →          │  ← was "Review & approve →"; now
│                                                │     downgraded because PR #68 is resolved
└──────────────────────────────────────────────┘
```

A `waiting_for_rebake` row with **no** terminal row for its PR (a genuinely
in-flight apply) keeps **"Review & approve →"** — we only downgrade the
*superseded* ones.

## Approach: client-side supersession derivation (frontend-only)

The rail already fetches the full list (`/decisions?limit=50`; 12 docs today), so
"is this PR resolved?" is answerable from the list the component already holds.
No backend change, no new field, no data migration. If the list ever exceeds 50
and an `applied` row falls outside the window, the matching `waiting_for_rebake`
row simply keeps its live CTA — a **fail-safe** degradation (shows actionable, the
status quo), never a false "resolved".

### Two small pure helpers + a status formatter

**`frontend/src/lib/approval.ts`**

```ts
/** PR numbers that have a terminal `applied` iac_apply row in `decisions`.
 *  A `waiting_for_rebake` row for one of these is SUPERSEDED — its apply
 *  already succeeded on a later request, so its "Review & approve →" CTA is
 *  stale and must downgrade to the neutral view-only label. */
export function resolvedIacPrNumbers(
  decisions: ReadonlyArray<{ action?: string; apply_status?: string; pr_number?: number }>,
): Set<number> {
  const resolved = new Set<number>();
  for (const d of decisions ?? []) {
    if (
      d?.action === 'iac_apply' &&
      d?.apply_status === 'applied' &&
      typeof d.pr_number === 'number' &&
      Number.isInteger(d.pr_number) &&
      d.pr_number > 0
    ) {
      resolved.add(d.pr_number);
    }
  }
  return resolved;
}

/** Label for an iac_apply row's approval CTA. "Review & approve →" ONLY when the
 *  row is `waiting_for_rebake` AND not superseded (no `applied` row for its PR);
 *  every other state — including a superseded waiting row, applied, failed — is
 *  view-only → "Open approval page →". (Extends the prior inline `iacApproveLabel`;
 *  the link target — `/iac-approvals/<n>` — is unchanged for all states.) */
export function iacApproveLabel(
  d: { apply_status?: string; pr_number?: number },
  resolvedPrs: ReadonlySet<number>,
): string {
  const superseded =
    typeof d.pr_number === 'number' && resolvedPrs.has(d.pr_number);
  return d.apply_status === 'waiting_for_rebake' && !superseded
    ? 'Review & approve →'
    : 'Open approval page →';
}
```

**`frontend/src/lib/format.ts`**

The known set mirrors `decision.ts`'s `APPLY_STATUS_BADGE` keys
(`applied`/`failed`/`failed_state_suspect`/`ambiguous`) **plus** `waiting_for_rebake`
(which `decision.ts` lets default to a muted badge). `failed_state_suspect` is a
real backend-emitted status (`agent/main.py`, `driftscribe_lib/approvals.py`) —
Codex plan-review must-fix: do not omit it.

```ts
/** Human label for an iac_apply row's `apply_status`, for the rail meta line.
 *  Known statuses get a readable phrase; an unrecognised non-empty status passes
 *  through CLAMPED (forward-compat — our own small backend enum, but the decision
 *  doc is unredacted so we cap length, matching decision.ts's defensive style);
 *  null/empty → '' (the meta line omits the token). */
const IAC_STATUS_LABELS: Record<string, string> = {
  applied: 'applied',
  waiting_for_rebake: 'awaiting re-bake',
  failed: 'failed',
  failed_state_suspect: 'failed (state suspect)',
  ambiguous: 'ambiguous',
};
const IAC_STATUS_MAX = 40; // a status enum is tiny; cap an unexpected value hard
export function iacStatusLabel(status: string | null | undefined): string {
  if (typeof status !== 'string' || status === '') return '';
  const known = IAC_STATUS_LABELS[status];
  if (known) return known;
  return status.length > IAC_STATUS_MAX ? status.slice(0, IAC_STATUS_MAX) + '…' : status;
}
```

### Component wiring (`DecisionsRail.svelte`)

- Remove the inline `iacApproveLabel`; import the new one. Compute the resolved
  set once per render: `const resolvedPrs = $derived(resolvedIacPrNumbers(decisions));`
  and call `iacApproveLabel(d, resolvedPrs)` at the CTA.
- Meta line gains the status token between the action tag and the SHA:
  `iac_apply{#if st} · {st}{/if}{#if sha} · ⎇ {sha}{/if}` where
  `{@const st = iacStatusLabel(d.apply_status)}`.
- No change to the href, the trace button, the title/subtitle, or any non-iac row.

### Type (`frontend/src/lib/types.ts`)

Add `apply_status?: string;` to `Decision` (already read via the index-sig cast at
`App.svelte:133`; promoting it to a typed field removes the cast and lets the new
helpers type it).

## Testing (TDD — frontend, vitest)

`approval.test.ts`:
- `resolvedIacPrNumbers`: collects PRs of `applied` iac rows; ignores non-iac
  (an `applied` row with `action !== 'iac_apply'`), non-`applied`, and
  missing/zero/non-integer `pr_number`; `[]`/nullish → empty set.
- `iacApproveLabel`: `waiting_for_rebake` + PR not resolved → "Review & approve →";
  `waiting_for_rebake` + PR resolved → "Open approval page →" (superseded);
  `applied` / `failed` / undefined → "Open approval page →";
  `waiting_for_rebake` with invalid/missing `pr_number` against a non-empty
  resolved set → still "Review & approve →" (can't be superseded);
  PR A resolved, PR B waiting → only A's waiting rows downgrade, B keeps the CTA.

`format.test.ts`:
- `iacStatusLabel`: each known status (incl. `failed_state_suspect` →
  "failed (state suspect)") → its phrase; unknown non-empty → verbatim;
  an over-long unknown → clamped with ellipsis; `''`/`null`/`undefined` → `''`.

`DecisionsRail` component test (Testing Library):
- list with two `waiting_for_rebake` rows + one `applied` row, same `pr_number` →
  all three approval links read "Open approval page →" (no "Review & approve →"),
  **and each link's href stays `/iac-approvals/<n>`** (downgrade is label-only).
- a lone `waiting_for_rebake` row (no `applied` sibling) → "Review & approve →".
- meta line renders the status token (e.g. "applied", "awaiting re-bake").

## Out of scope (YAGNI / explicitly deferred)

- **Collapsing** the lifecycle rows into one per PR — changes decision-doc display
  semantics and hides the crash-recovery pointers; ruled out in the prior design.
  (A "big decision" — would wake the operator if reconsidered.)
- Backend serve-time supersession annotation — unnecessary while `limit=50` ≫ doc
  count; the client derivation is sufficient and lower-risk.
- A status *pill*/colour — the muted meta-line token is enough to disambiguate;
  no new visual weight.

## Rollout

Per `deploy_autonomy`: subagent TDD build → Codex completed-work review on the same
thread → if CI-green + Codex-SHIP, merge + coordinator redeploy with the
traffic-pin step (`coordinator_deploy_traffic_pinning`) autonomously. Frontend-only,
but the Vite bundle is baked into the coordinator image, so a redeploy is required
for it to go live. No Firestore change, no backfill.
