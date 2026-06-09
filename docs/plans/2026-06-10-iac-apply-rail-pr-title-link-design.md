# Design — `iac_apply` rows in the decisions rail: PR # title-link + commit SHA + PR title

**Date:** 2026-06-10
**Status:** BUILT (branch `feat/iac-apply-rail-pr-title-link`) — Codex plan-review folded;
backend 2175 + frontend 275 green, svelte-check clean. Pending: completed-work review,
deploy, backfill.
**Surface:** operator SPA decisions rail (`frontend/src/components/DecisionsRail.svelte`),
coordinator `/decisions` serve path + IaC decision recorder (`agent/main.py`).

## Problem

Every row in the operator SPA's left "Past decisions" rail shows `iac_apply` and
nothing else distinguishing one from another. The decision docs *do* carry
provenance (`pr_number`, `head_sha`, `approver`, `apply_status`, `merge_state`)
but the rail only renders `d.action` plus an internal `/iac-approvals/<n>` link.
There is no way, from the rail, to:

- reach the actual GitHub PR that was applied, or
- see *which* PR / commit a row corresponds to, or
- tell two `iac_apply` rows apart at a glance.

Live data confirms the rail is faithful, not buggy: 12 decisions total — 11
`iac_apply` (4 distinct PRs: #32/#47/#66/#68, multiple lifecycle rows each) + 1
`no_op`. All recent work has been the IaC author→approve→apply loop.

## Goal

Make each `iac_apply` row self-describing and navigable:

```
┌──────────────────────────────────────────────┐
│ PR #68 →                          Jun 5, 01:27 │  ← TITLE: link to the GitHub PR
│ infra(checkout): storefront + orders-worker …  │  ← pr_title (truncated; omitted if absent)
│ iac_apply · ⎇ 0496b30                          │  ← meta: action tag + short SHA, muted
│ open trace →     Open approval page →          │  ← existing affordances, unchanged
└──────────────────────────────────────────────┘
```

Non-`iac_apply` rows (rollback, drift_issue, …) are **unchanged**: title stays
the action text (not a link), no SHA/PR-title lines.

## Design principle: derive what's derivable, store what's not

- **PR URL is fully derivable** from `(github_repo, pr_number)`, and `github_repo`
  is trusted server config → **derive it at serve time**, never persist it. Works
  for *all* rows (including the 11 existing) with zero data migration and no
  staleness risk.
- **PR title is external, non-derivable content** that we don't want to fetch on
  the read path (the rail can show 50 rows; that would be 50 GitHub calls,
  rate-limit/latency/coupling) → **capture it once at apply (write) time** and
  persist it. A write-time snapshot is also the *more correct* semantic: it is
  the title of the PR *as applied*, not whatever it says today.

## Backend changes (`agent/main.py`)

### 1. Serve-time PR link (the title link)

New **pure** helper, applied per-row in the `/decisions` endpoint alongside the
existing `scrub_decision_rationale`:

```python
def attach_iac_pr_link(decision: dict, repo: str) -> dict:
    """For iac_apply rows, derive github.url -> the GitHub PR, from TRUSTED config repo.
    Returns a NEW dict (never mutates the input — list_decisions hands back live
    dicts, same reason scrub_decision_rationale returns a copy). No-op (returns the
    row unchanged) unless action == 'iac_apply', repo matches a simple owner/repo
    shape, pr_number passes `type(pr_number) is int and pr_number > 0` (so a bool
    True can't slip through as 1), and no 'github' field already exists (never clobber)."""
```

Returns `{**decision, "github": {"url": f"https://github.com/{repo}/pull/{pr_number}"}}`.
`repo = get_settings().github_repo`. The frontend re-validates the host via the
existing `safeGithubHref` allowlist (defense in depth). It reuses the same
`github.url` *shape* that `drift_issue`/`docs_pr` rows carry, but the rail renders
it on the **title** (not the row-actions block) — see frontend note on avoiding a
double link. (Codex review: keep this helper pure like `scrub_decision_rationale`
in `agent/renderer.py`; validate `pr_number`/`repo` narrowly.)

Applied at the `/decisions` boundary (the rail's source). NOT added to `/trace`
in this change (the trace view doesn't render a PR link today — YAGNI; the helper
is trivially reusable if that changes).

### 2. Write-time PR title capture

- `_record_iac_decision(...)` gains an optional `pr_title: str | None = None`.
  Stores `decision["pr_title"]` only when a non-empty string is supplied
  (normalization + the 200-char cap live in `_fetch_pr_title`, the single source).
- The apply handler and `_handle_existing_iac_decision` already hold a PyGithub
  `repo` handle (`agent/main.py:2659`, passed through). Compute the title **once
  per request** via a small fail-soft helper:

```python
def _fetch_pr_title(repo, pr_number) -> str | None:
    try:
        raw = (repo.get_pull(pr_number).title or "")
        # Collapse newlines/control whitespace to single spaces (anti-spoof:
        # the title is external content rendered on one ellipsised line), strip,
        # cap. Empty -> None.
        return " ".join(raw.split())[:200] or None
    except Exception as e:                      # cosmetic — never fail an apply on it
        log.warning("iac_pr_title_fetch_failed", extra={"pr_number": pr_number, "error": str(e)})
        return None
```

  Pass the resulting string into each `_record_iac_decision` call in that request.
  One extra GitHub call on a path that already does several (merge, head-SHA,
  comment fetch); zero effect on the read path. The call is placed where the
  existing GitHub reads already are — never inserted between the worker claim and
  an uncaught failure path (Codex hot-path note).

- **First-approved title wins (Codex review).** In `_handle_existing_iac_decision`
  (resume / merge-only reconcile / terminal re-POST), prefer the title already on
  the existing decision: `pr_title = existing.get("pr_title") or _fetch_pr_title(...)`.
  This preserves the *as-approved* snapshot — a PR title edited after the first
  approval can't retroactively overwrite a later lifecycle row.

## Frontend changes

### `frontend/src/lib/types.ts`

Add to `Decision`: `pr_number?: number; head_sha?: string; pr_title?: string;`
(currently reached only via the index signature).

### `frontend/src/components/DecisionsRail.svelte`

- **Title:** when `d.action === 'iac_apply'` and a dedicated `iacPrHref(d)`
  resolves, render the title as an anchor `PR #{d.pr_number} →` (host-allowlisted
  href). Otherwise render the action text as today (plain, non-link).
  - **Do NOT add `iac_apply` to `GITHUB_LINK_LABEL`** (Codex must-fix): that would
    make the existing row-actions `githubHref` block render a *second* GitHub link.
    Instead add a small dedicated helper
    `iacPrHref(d) = d.action === 'iac_apply' ? safeGithubHref(d.github?.url) : null`
    used only by the title. The row-actions GitHub block stays drift/docs-only.
    Net: exactly one GitHub affordance per iac row (the title).
- **Subtitle:** if `d.pr_title`, render it as muted text, single line, CSS
  ellipsis. Omitted entirely when absent (old rows pre-backfill / fetch failures).
- **Meta line:** for iac_apply, `iac_apply · ⎇ {head_sha.slice(0,7)}` (muted,
  smallest). SHA line omitted when `head_sha` absent.
- The existing internal approval link (`Open approval page →` /
  `Review & approve →`) and `open trace →` are **kept as-is** — the GitHub PR link
  (external) and the approval page (internal) are different destinations.
- All external text (`pr_title`) rendered via normal interpolation (Svelte
  auto-escapes) — no `{@html}`.

## One-time backfill (existing 11 docs)

Operator-approved Firestore write (per the user's standing
confirm-before-mutating-data rule — explicit go-ahead obtained, mapping shown):
set `pr_title` on the 11 existing `iac_apply` decision docs from the live PR
titles. Idempotent (only writes the field; skips if already equal). No other
field touched. Mapping (decision_id → pr_title) reviewed before the run.

| PR | apply title |
|----|-------------|
| #32 | feat(iac): C5g — repoint payment-demo to dedicated runtime SA |
| #47 | test(iac): C6e — create-class e2e probe (throwaway denylist-clean bucket) |
| #66 | infra(checkout): assets bucket + order-events topic & subscription |
| #68 | infra(checkout): storefront + orders-worker Cloud Run services |

## Testing

**Backend (pytest):**
- `attach_iac_pr_link`: iac_apply + valid repo + positive-int pr_number → correct
  `github.url`; non-iac untouched; missing/zero/non-int pr_number → no url; empty
  repo → no url; pre-existing `github` not clobbered.
- `_record_iac_decision`: stores `pr_title` (capped) when passed; omits the field
  when None/empty.
- `_fetch_pr_title`: returns stripped title; returns None on GitHub exception
  (fail-soft) and on empty/whitespace title.
- `/decisions` integration: an iac_apply doc comes back carrying `github.url`.

**Frontend (vitest + Testing Library):**
- iac_apply row: title is an anchor `PR #N →` with the safe github href; `pr_title`
  subtitle present; short SHA present.
- off-origin / non-github `github.url` → no anchor (safeGithubHref rejects).
- non-iac row: title is plain action text, no PR link, no SHA line.
- iac_apply row missing `pr_title` → subtitle omitted; missing `head_sha` → no SHA.

## Out of scope (YAGNI)

- Fetching/showing the C2 plan *comment* inline (it's machine metadata, not prose;
  the PR is one click away).
- Collapsing the multiple lifecycle rows per PR into one (changes decision-doc
  semantics; the rows are distinct reconcile pointers).
- A status pill (user chose title+SHA over a pill; PR # + title already
  differentiates rows).
- `/trace` PR link.

## Rollout

Frontend rebuild (Vite bundle) + coordinator redeploy with traffic pin to the new
revision (`coordinator_deploy_traffic_pinning`); run the backfill. Per
`deploy_autonomy`: once CI-green + Codex-SHIP, merge + redeploy autonomously.
