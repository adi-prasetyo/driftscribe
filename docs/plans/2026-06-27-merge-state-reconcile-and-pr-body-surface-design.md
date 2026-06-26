# Design: merge_state reconcile-on-load + surface the agent's PR body in the open-trace card

Date: 2026-06-27
Status: DRAFT (for Codex review, then operator approval)
Author: Claude (Opus 4.8)

## Why

Two gaps surfaced while verifying PR #32 (the C5g "repoint payment-demo to a dedicated runtime SA" apply):

1. **Stale `merge_state` bookkeeping.** A decision can persist `apply_status="applied"` +
   `merge_state="failed"` even though the PR was actually merged on GitHub out-of-band
   (operator merged it by hand 7 min after the agent's auto-merge recorded a failure).
   The ONLY thing that corrects this today is the operator re-opening `/iac-approvals/{pr}`
   and re-clicking Approve (the C5 merge-only reconcile). Until then the rail silently shows
   `merge=failed` for a PR that is, in fact, merged. PR #32 carried this stale state for ~27 days.

2. **No plain-language explanation of what a past decision did.** The completed past-decision
   view (open-trace → `DecisionSummary`) shows `Action / Pull request / Apply / Merge / Head SHA /
   Approver / When`. There is no human-readable "what & why". The agent already *wrote* that —
   it's the PR body it authored when opening the IaC PR (e.g. #32's body has `Resource:` / `Why:` /
   `Gate path:` sections). We just don't surface it.

Operator decision (2026-06-27): do the reconcile bug-fix, and surface the agent's **existing**
PR body (no agent-generated prose). Lean, honest, no new inference.

## Non-goals

- No agent-generated/LLM summaries of decisions (explicitly rejected — overkill, hallucination
  surface, latency/cost per open).
- No change to the apply/approve security pipeline (`_handle_existing_iac_decision`, the C2 plan
  gate, `merge_pr_at_sha`, readiness checks). Both features are read-only/serve-time.
- No mutation of stored decision documents (see Feature 1 design choice).

---

## Feature 1 — merge_state reconcile-on-load (serve-time-only, no persistence)

### Design choice: compute, don't persist

The StateStore has **no update/patch method** (`state_store.py:21-41`); the only write path is
`record_decision`, which writes a brand-new doc with a new UUID. So "persist the fix" would mean
calling `_record_iac_decision` from a **GET** handler (a side-effecting read), which:
- makes a read endpoint mutate prod data (your standing rule: confirm before modifying data
  directly — a serve-time-only design needs no such confirmation because it writes nothing);
- writes a new `apply_status="applied"` doc that the SPA's `noteApplied`/`appliedEpoch` watermark
  (`App.svelte:203`) would read as a *newly applied* decision → spurious `/infra/graph` refresh;
- races across coordinator instances (benign but wasteful).

**Chosen design:** a pure serve-time transform that mirrors the existing `attach_iac_pr_link`
precedent (`renderer.py:252`) — compute the corrected `merge_state` on the fly, return a shallow
copy, never persist. The stored doc stays faithful to what actually happened at the time (it
*did* fail to auto-merge); the UI shows current truth.

### The reconcile helper

New helper (in `agent/main.py`, or a small module) — pure except for the cache + one GitHub read:

```
def reconcile_merge_state(decision, *, repo, cache, settings) -> dict:
    # Only the exact stale case; everything else returns the input unchanged (by identity).
    if decision.get("action") != "iac_apply": return decision
    if decision.get("apply_status") != "applied": return decision
    if decision.get("merge_state") != "failed": return decision
    pr = decision.get("pr_number"); sha = decision.get("head_sha")
    if not isinstance(pr, int) or pr <= 0: return decision
    merged = _merge_status(pr, sha, repo=repo, cache=cache, settings=settings)  # True | False | None
    if merged is True:
        return {**decision, "merge_state": "merged", "merge_reconciled": True}
    return decision  # False/None → leave 'failed' (truthful); re-check next load
```

`merge_reconciled: true` is a **cosmetic-only** marker for optional SPA copy ("confirmed on
GitHub"). It does NOT need to interact with the `appliedEpoch` watermark: that watermark keys on
`decision_id` (`decision.ts:143`) and reconcile writes **no new doc** (it edits `merge_state` on
the existing `decision_id` at serve time), so no fresh-apply bump occurs — confirmed in Codex
review. (This corrects an earlier worry in this doc that the marker was needed to suppress a
spurious infra-graph refresh; there is none.)

**Codex must-fix (head match):** `_merge_status` MUST require `pr.head.sha == head_sha` AND
`pr.merged is True` before returning/caching `True`. `merge_pr_at_sha` enforces exactly this
invariant today (`github.py:1010`): a force-push-then-merge at a *different* head means the thing
that was applied (old `head_sha`) is not what merged, so promoting it would be a wrong-positive.
Verified this does NOT break the normal case — a squash/merge-commit leaves `pr.head.sha` at the
original branch tip, which equals the decision's stored `head_sha` (e.g. PR #32), so normal
applies still reconcile; only the force-push edge is guarded.

### `_merge_status` — terminal-state cache

- Read-through a Firestore cache, keyed `(pr_number, head_sha)`, collection `iac_pr_merge_status`,
  mirroring `iac_pr_source_cache.py` (Protocol + Firestore + InMemory doubles).
- Cache backend gated on **`gcp_project` alone, NOT `dry_run`** (the canonical read-only-cache
  gate — `get_iac_pr_source_cache_store` at `main.py:2026`). Prod runs `DRY_RUN=true`; gating on
  dry_run would silently make it in-memory and defeat cold-start survival.
- "merged" is **terminal** → cache `merged=True` with a long/effectively-permanent TTL.
  Never cache `False`/`None` for more than a few minutes (a not-yet-merged PR is transient).
- Entry gate: `if not (settings.github_token and settings.github_repo): return None` →
  tests/demo with no token never reach GitHub.
- One GitHub call on miss: `repo.get_pull(pr_number).merged` (a bool). **Reuse a single `repo`**
  captured once per request (`get_repo` is uncached — `github.py:97` — never call it per row).
- **Fail-soft:** any GitHub/cache error → `None` (leave state unchanged). A read endpoint must
  never 5xx because GitHub hiccuped.

### Where it runs (must be consistent across surfaces)

The rail reads `/decisions`; the open-trace card reads `/trace/{id}`. If we reconcile only one,
the rail and the card disagree. So apply the **same helper** at both serve boundaries:

- `list_decisions_endpoint` (`main.py:1903`) — add as a transform after `attach_iac_pr_link`.
  Capture `repo` once, pass to each row's reconcile.
- `get_trace` (`main.py:2752`) — reconcile the single `decision` before returning (after
  `scrub_decision_rationale`).
- **REQUIRED (Codex must-fix, was optional):** the server-rendered `GET /iac-approvals/{pr}`
  decision read (`main.py:3491`/`:3524`) — apply the same helper AND suppress the Approve form
  when it reconciles to merged. Rationale: if this page still presents the row as actionable, the
  operator clicks, the POST routes to `_iac_merge_step` (`main.py:4469`) and **writes a new
  applied+merged doc** — exactly the mutation the serve-time design exists to avoid. Reconciling
  here closes that loop.

### Cost

In the steady state: **0 GitHub calls** (no stale rows, or cache warm). In the rare unreconciled
case: 1 `get_pull` per stale `applied+failed` row, then cached permanently. The endpoint is
token-gated, single-operator, and the SPA does not poll `/decisions`. Far inside the 5000/hr PAT.

### Tests (TDD — write first, watch fail)

Unit (`tests/unit/`):
- reconcile leaves non-iac / non-applied / non-failed / merged decisions unchanged (by identity).
- reconcile promotes `applied+failed` → `merged` when `pr.merged` is True (mocked repo).
- reconcile leaves `failed` when `pr.merged` is False / when token unset / on GitHub error (fail-soft).
- cache: `merged=True` is served from cache without a second GitHub call; `False` is not cached long.
- repo captured once: N stale rows ⇒ 1 `get_repo`, N `get_pull` (assert via mock call counts).
Integration:
- `GET /decisions` reconciles a seeded `applied+failed` row to `merged` with a mocked repo; a
  second call hits cache (no GitHub). `GET /trace/{id}` for the same decision agrees.
- `GET /decisions` with no `github_token` returns the row untouched (no GitHub), still 200.

---

## Feature 2 — surface the agent's PR body in the open-trace card

### Surface & data flow

The open-trace card is `DecisionSummary.svelte`, shown when
`historicalActive && finalReply == null && historicalDecision` (`App.svelte:487`). The decision
object there already carries `pr_number` and `head_sha`. We add a **new sibling disclosure**
`PrBodyDisclosure.svelte` (NOT a row inside the allowlisted `<dl>` — the body is multi-KB prose;
`decision.ts` MAX_VALUE is 256 and its security note forbids arbitrary-key rendering).

The SPA fetches the body from a **new token-gated JSON endpoint** (the existing `.tf` source view
is Jinja-rendered HTML on `/iac-approvals`, not consumable by the SPA). **Codex must-fix:** the
endpoint binds to a **persisted decision via `trace_id`** (which the SPA already has from
`openTrace(tid)`), NOT to a bare `pr_number` — the StateStore has no lookup-by-PR and a PR has
multiple lifecycle docs, so a bare `pr_number` couldn't safely resolve *which* decision / which
`head_sha`. Server-side it resolves the decision (`find_decision_by_trace_id`), validates
`action === "iac_apply"` and a positive `pr_number`, and derives `head_sha` from the decision
(never trusts a client-supplied SHA):

```
GET /trace/{trace_id}/pr-body     (Depends(verify_token), trace_id is HEX32-validated like /trace)
-> { pr_number, head_sha, body: str|null, body_truncated: bool, cached: bool }
   (404 if no decision / not iac_apply; 400 on bad trace_id; always-200 fail-soft to body:null
    on a GitHub/cache miss for a valid iac_apply decision)
```

Why a dedicated endpoint rather than storing `pr_body` on the decision doc at write time:
the operator specifically wants the **7 historical** PRs explained; write-time capture only
helps future decisions. Lazy fetch + cache works for historical PRs immediately.

### Backend

- New helper `get_pr_body(repo, pr_number) -> (body: str|None, truncated: bool)` in
  `driftscribe_lib/github.py`, mirroring `list_pr_iac_tf_files`: one `repo.get_pull(pr_number)`,
  read `.body` (already `str|None`), cap at **16 KiB** (truncate + flag), strict behavior on
  oversize. Unit-tested like `test_github_pr_iac_files.py`.
- New read-through cache collection `iac_pr_body` (mirror `IacPrSourceCacheStore`: Protocol +
  Firestore + InMemory + `_..._for_tests` override seam), keyed `(pr_number, head_sha)`,
  `format_version=1`, TTL backstop (default 24h; merged-PR bodies are stable). Backend gated on
  `gcp_project` alone. **Decoupled from the existing `iac_pr_source` cache on purpose** — do not
  extend that doc/format_version, to avoid any risk to the security-critical server-rendered
  approval page.
- `head_sha` is **resolved server-side from the persisted decision** (looked up by `trace_id`),
  never trusted from the client. It's the as-applied SHA — trusted because it went through the
  gated pipeline. The cache key is `(pr_number, head_sha)` (a head move ⇒ cache miss ⇒ refetch),
  matching the `.tf` source cache freshness model.
- **Provenance honesty (Codex should-fix):** GitHub PR bodies are *mutable*; `get_pull().body`
  returns the *current* description, not a commit-pinned snapshot. So this is a **convenience
  cache of the current PR body**, not an "as-applied" artifact. UI copy says "from the PR"
  (accurate). `head_sha` is a cache-invalidation heuristic, not a provenance guarantee. (Skip
  `pr.updated_at` for v1; merged historical bodies are stable.)
- **Scrub before cache (Codex should-fix — order matters):** scrub BEFORE writing the cache, not
  after read. Body is agent-authored markdown (low secret risk), but belt-and-braces run
  `redact_text` (credentialed-URL userinfo scrub, `secret_guard.py:41`) + `redact_approval_tokens_deep`
  (strip any `?t=` rollback token). New `scrub_pr_body()` in `renderer.py`. **Honest scope:** this
  is NOT robust arbitrary-secret redaction (`redact_text` only strips URL credentials; there's no
  key-name gate on free prose). The real mitigations are: the body is agent-authored from a
  template (not user free-text), the endpoint is token-gated (operator-only), and the render is
  escaped `<pre>` (no XSS). Acceptable given that surface; documented, not overclaimed.
- **Render as escaped plain text** — `<pre>{body}</pre>`. Never `{@html}`. (Markdown stays raw
  text; the strict IaC CSP has no script-src anyway, and the SPA must not introduce an HTML-inject
  surface.)
- Entry gate `if not (settings.github_token and settings.github_repo)` → `{ body: null }`,
  endpoint still 200. Fail-soft on every error.
- Refresh: **skip an operator-gated refresh POST for v1.** Merged historical bodies are stable; the
  TTL backstop + `head_sha` key handle the rare edit. (Add later if needed, mirroring
  `refresh-source`.)

### Frontend

> AS-BUILT NOTE: the endpoint referenced as `/iac-approvals/{pr}/explain` in the as-planned
> prose below shipped as **`GET /trace/{trace_id}/pr-body`** (Codex MF4 — bind to the decision via
> `trace_id`, derive `head_sha` server-side). The disposition section records this.

- `openTrace` (`App.svelte:357`): after `historicalDecision` is set, if
  `action === "iac_apply" && pr_number`, fetch `/iac-approvals/{pr}/explain` via the existing
  token-authed `call()`; guard with the `runSeq` stale-run pattern (same as the rest of openTrace).
- New `PrBodyDisclosure.svelte`: a `<details>` "What this change did (from the PR)" →
  `<pre>{body}</pre>`. Render nothing when body is null/empty/errored (fail-soft, no empty box).
  Place as a sibling right after `<DecisionSummary>` under the same `{#if}` guard.
- `Decision` type: add explicit `pr_body?: never` is NOT used — body comes from the endpoint, not
  the decision doc. Add a small `PrExplain` type for the endpoint response.

### `.tf` source in the open-trace card — RECOMMEND DEFER (decision point D2)

The selected preview showed a `[Source (.tf) ▸]` disclosure too. Surfacing it in the SPA needs a
JSON endpoint over the `.tf` content (the existing one is HTML). Options:
- **(Recommended for v1)** Skip inline `.tf`; the card already links to the PR, and the full `.tf`
  is one click away on `/iac-approvals/{pr}`. Keeps scope to "explanation".
- **(Optional)** Extend `/explain` to also return `files[]` at the decision's `head_sha` (reuse
  `list_pr_iac_tf_files`), render a second disclosure. More code + a second GitHub call class.

### Tests (TDD)

Unit: `get_pr_body` (body str, None, oversize→truncate); `scrub_pr_body` (strips credentialed URL +
`?t=` token); cache validation rejects a tampered/non-str body. Frontend: `PrBodyDisclosure`
renders/escapes body, renders nothing on null; `App` openTrace fetches `/explain` for iac_apply and
not for chat traces; stale-run guard drops a superseded fetch.
Integration: `/explain` returns body on cache miss (mock repo) then cache hit (no 2nd call);
no `github_token` → `{ body: null }`, 200; fail-soft on GitHub error → `{ body: null }`, 200.

---

## Security review checklist

- Both serve paths stay **fail-soft** and never 5xx on GitHub/cache failure.
- New `/explain` endpoint is **token-gated** (`verify_token`), like `/trace` and `/decisions`.
- PR body **scrubbed** (`redact_text` + approval-token redaction) before cache + serve; rendered
  **escaped** (`<pre>{…}</pre>`, no `{@html}`).
- Cache backends gated on **`gcp_project`** (not `dry_run`) — read-only-cache convention.
- `head_sha` is **server-resolved from the persisted decision**, not trusted from the client.
- No change to the apply/approve/merge security pipeline. No stored-data mutation (Feature 1).
- `repo` captured once per request (no `get_repo` per row).

## Rollout

- **Coordinator-only** deploy (all new endpoints + SPA bundle live in `driftscribe-agent`).
  No worker images. Build via `infra/cloudbuild.coordinator-update.yaml`, then the mandatory
  `update-traffic --to-revisions=<new>=100` (traffic is pinned).
- Frontend bundle rebuilt in Docker (gitignored `agent/static/`); source-only commit.
- Live-verify on prod: open-trace #32 shows the PR body; the rail/card both show `merged`
  consistently; a no-`github_token` path (n/a in prod) covered by tests.

## Codex review (thread 019f04c7) — disposition

Reviewed before coding. Verdict folded in:
- **MF1 head-match in `_merge_status`** → ADOPTED (require `pr.head.sha == head_sha && pr.merged`).
- **MF2 cache key `(pr, head_sha)`** → already planned; CONFIRMED.
- **MF3 reconcile `/iac-approvals/{pr}` GET + suppress form** → ADOPTED (promoted to required, was D3).
- **MF4 `/explain` binds to `trace_id`/decision, server-resolves head_sha** → ADOPTED
  (endpoint is now `GET /trace/{trace_id}/pr-body`).
- **SF body provenance / mutable** → ADOPTED (convenience-cache framing, honest copy).
- **SF scrub-before-cache + "not arbitrary-secret-proof"** → ADOPTED (documented honestly).
- **SF `merge_reconciled` is cosmetic-only, watermark untouched** → ADOPTED (no doc written → no bump).
- Codex test list → ADOPTED into the TDD plans above.

No material disagreement with Codex on this plan.

## Open decision for the operator

- **D1 (settled):** serve-time-only reconcile (no data mutation) — Codex concurs.
- **D2 (settled 2026-06-27):** PR body ONLY in the open-trace card. No inline `.tf` in the SPA;
  the full `.tf` stays one click away on `/iac-approvals/{pr}`.
- **D3 (settled):** reconcile the server-rendered `/iac-approvals/{pr}` GET too — now required
  (Codex MF3).

## NOT in this PR — reassigned

A `failed:` `IAC_STATUS_HELP` entry (a tooltip for the plain `failed` apply badge) was briefly
considered for this branch but **REASSIGNED to another agent on a separate worktree** (operator,
2026-06-27). `frontend/src/lib/format.ts` is intentionally **untouched** here, so there is no
overlap with that agent's branch.
