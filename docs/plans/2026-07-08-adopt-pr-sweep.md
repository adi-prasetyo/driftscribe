# Demo window: auto-close stale visitor adoption PRs (adopt-pr-sweep)

2026-07-08. Status: planned.

## Problem

During the public demo window, any anonymous visitor can send an adoption
prompt to the Provision crew. That opens a REAL GitHub PR, and two correct
product behaviors then hide the "Adopt into IaC" button for that resource
from every subsequent visitor:

- `GET /infra/pending-approvals` lists open `driftscribe-infra` PRs, and the
  InfraDiagram band replaces the Adopt button with "review pending adoption"
  (PR #184).
- The crew-side dupe-guard (`find_open_adopt_pr_for_resource`) refuses a
  second adoption while an earlier PR for the same resource is OPEN.

The operator never approves visitor PRs, so each click burns one adoptable
resource's demo until someone manually closes the PR. After PR #217 the
adoptable surface is deliberately small (`adopt-probe-topic`,
`adopt-probe-sub`, `adopt-probe-svc`), which makes the burn-down fast: three
visitors can exhaust the demo for everyone after them.

Live example: PR #215 (visitor-shaped adoption of `adopt-probe-sub`,
2026-07-08) currently hides that row's button.

## Design

Add a third job, **`adopt-pr-sweep`**, to `.github/workflows/demo-reset.yml`
(the self-heal cron). It runs on every trigger the `service-reset` job runs
on (2h cron, daily 6:00 JST cron, `workflow_dispatch`) and:

1. Lists open **same-repo** PRs (`isCrossRepository == false` — a fork PR's
   `headRefName` is contributor-controlled, so without this guard a fork PR
   named `infra/adopt-…` could be swept, and its branch-delete call would
   target a same-named ref in OUR repo; Codex must-fix) whose head branch
   starts with **`infra/adopt-`** and whose `createdAt` is **≥ 2 hours**
   old, excluding **PR #168**.
2. Closes each with an explanatory comment, then deletes its branch
   (warn-and-continue per item, same as the lodash sweep).
3. Writes a step summary with closed/deleted counts.

PR lifetime for a visitor adoption is therefore 2h–4h (age threshold + 2h
cadence), and a judge mid-demo (< 2h old PR) is never swept out from under
their open approval page.

### Why filter on the branch prefix, not the `driftscribe-infra` label

- The label is applied worker-side **best-effort** — an unlabeled adoption
  PR is invisible to a label filter but still debris.
- The label is NOT adoption-specific. Live counter-example: PR #216
  (`fix(iac): recreate orders-sub with never-expire policy`, branch
  `iac/orders-sub-never-expire`) carries the label but is a real operator
  fix that must never be swept.
- The branch is server-derived and LLM-untouchable:
  `derive_iac_pr_authority` computes `infra/{slug(title)}-{ts}-{hex}`, and
  the adoption title is renderer-pinned to
  `Adopt {human_type} {name} into IaC management (zero-change import)`
  (`adopt_recipe.py`), so every adoption branch starts `infra/adopt-`.
  A freehand `open_infra_pr_tool` PR only matches if the model titled it
  "Adopt ..." — which is exactly the visitor-adoption-shaped debris this
  sweep exists to clear. (Freehand PRs with other titles don't hide any
  Adopt button and are out of scope.)

Accepted tradeoff (explicit): the sweep cannot distinguish a visitor
adoption from a legitimate operator adoption — both come from the same
renderer. The demo-window rule is therefore: no operator adoption PR other
than #168 stays open past 2h while the window (and this workflow) is
enabled. The close runbook already disables `demo-reset.yml` at window
close, so post-window operator adoptions are unaffected.

### Why exclude PR #168

The `service-reset` job's adopt-fixture verify step REQUIRES #168 to stay
open (it fails the whole run otherwise — the `/iac-approvals/168` recording
and the curated approval-page demo depend on it). Sweeping it would make
every subsequent scheduled run fail and email the owner forever. Hardcoded
exclusion with a comment pointing at that step, same spirit as the step's
own hardcoded `pulls/168` read.

### Auth

Same as the lodash-repin job: WIF → gcloud → fetch `upgrade-docs-github-pat`
from Secret Manager (the demo-reset SA already holds accessor on it —
`setup_demo_reset.sh` §6; the WIF provider is scoped to the workflow FILE,
not a job, so no infra changes). The ambient `GITHUB_TOKEN` stays read-only,
preserving the workflow's documented stance ("Neither job needs GitHub WRITE
access via the ambient GITHUB_TOKEN"). The PAT already closes PRs and
deletes branches in the lodash sweep, so no new permission is exercised.

### What downstream surfaces do on close (all zero-code)

- Pending-approvals band: `get_issues(state="open", ...)` — closed PRs drop
  off; the Adopt button re-renders (allow up to ~60s of endpoint cache/poll
  lag before the SPA reflects it).
- Crew dupe-guard: same `state="open"` filter — the next visitor's adoption
  of the same resource is accepted.
- Approval page `GET /iac-approvals/{n}` for a swept PR still renders (the
  PR exists; only nonexistent PRs get the fail-soft probe copy). Approve is
  CF-JWT-gated, so no anonymous visitor can act on a stale page; an operator
  approve of a closed PR would fail loudly at merge. Accepted.
- No Firestore cleanup needed: pending approvals are a live GitHub listing
  (nothing persisted), and a never-applied adoption has no `iac_apply`
  decision row to reconcile.

### Close comment (public, on GitHub)

> demo reset: auto-closing this public-demo-window adoption PR so the
> Adopt demo is available for the next visitor. Nothing was applied; the
> resource stays unmanaged. Adoption PRs opened during the demo window are
> swept ~2 hours after opening (`.github/workflows/demo-reset.yml`).

## Changes

- `.github/workflows/demo-reset.yml`: new `adopt-pr-sweep` job (WIF auth,
  gcloud setup, PAT fetch, sweep step, summary step). Update the header
  comment's two-job blast-domain description to three jobs.
- The sweep step honors the file's documented conventions: checked-`$()`
  capture then `mapfile` (never `mapfile < <(gh …)`), `REPO` passed via
  `env:` (never `${{ }}` interpolated into the `run:` body), all filtering
  done in `--jq` so only same-repo-derived numbers/branch names reach bash,
  the close comment held in a quoted heredoc (it contains backticks), and
  branch deletion per swept PR only — never a bulk `infra/adopt-*` ref
  delete, which could race a fresh (< 2h) adoption's branch.
- No backend, frontend, worker, or infra-script changes. No deploy.

## Testing / verification

- No unit test pins this workflow (verified: no `demo-reset` references
  under `tests/`). `actionlint` is not available locally.
- Pre-merge: dry-run the exact list+filter query read-only against the live
  repo — must select #215 (adoption, > 2h old), must NOT select #168
  (excluded) or #216 (branch `iac/…`, prefix mismatch).
- Post-merge live proof: `gh workflow run demo-reset.yml` once; confirm the
  run closes #215 + deletes its branch, #168/#216 untouched, then confirm on
  the public graph that `adopt-probe-sub`'s Adopt button is back and the
  pending band no longer lists #215.

## Not doing (deliberate)

- Sweeping non-adoption judge debris (freehand infra PRs with arbitrary
  titles): they don't hide any Adopt button; revisit only if they accumulate.
- Rejecting/annotating the in-app approval record for swept PRs: Reject is
  already a non-binding no-op in this product, and the pending listing is
  live-computed.
- Special-casing #215's previously planned plan re-dispatch (after #216
  applies): the sweep closing #215 makes that re-dispatch unnecessary — the
  button coming back IS the recovery.
- Any coordinator-side "PR was auto-closed" copy. The GitHub close comment
  carries the explanation; in-app surfaces simply revert to their
  pre-adoption state.
