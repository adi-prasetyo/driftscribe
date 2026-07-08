# Demo self-heal reset + homepage notice — plan (2026-07-07)

Status: **Codex-reviewed 2026-07-07 (thread `019f3965-7977-7623-a1c9-da584dc1df9b`),
amendments folded, approved by operator, implementation in progress.**
Original plan parked 2026-07-07 (earlier session); reviewed + amended same day.

## Context / decisions made

- **Public judging window is OPEN** at autonomy `propose_apply` (opened
  2026-07-07): Worker deployed `DEMO_MODE="1"` (CHAT_RATE_LIMIT 5/60s binding
  confirmed), CF Access `driftscribe-demo-bypass` policy created, edge verified
  OPEN. `infra/cloudflare/worker/wrangler.toml` is left at `DEMO_MODE="1"`
  (uncommitted window state; canonical repo value is "0").
- Operator chose: reset scope = **Everything**; posture = **propose_apply, open now**.
- Why propose_apply (not the design-doc default `propose`): judges get a real
  self-serve **apply** experience (live rollback of `payment-demo`; the video
  alone "doesn't prove anything"). IaC `tofu apply` stays operator-only (CF-JWT
  gated) — that human gate is the production-readiness story, keep it.

## Live-state snapshot (verified 2026-07-07, review session)

- `payment-demo` is currently CLEAN: traffic `latestRevision: True` at
  `payment-demo-00011-gng`, env exactly at contract baseline. First reset run
  should be a silent no-op for the service.
- Only open PR is **#168** (adopt fixture — must stay open). No stray upgrade
  PRs, but the stale branch **`upgrade/lodash-4-17-21` EXISTS** (left from
  merged PR #2; `delete_branch_on_merge=false`). Deterministic branch name
  (`agent/adk_tools.py:423`) means a judge's future propose fails until swept.
- `demo/upgrade-target/package.json` is at lodash `4.17.21` — the upgrade demo
  is a dud TODAY. Re-pin is urgent (first workflow_dispatch run fixes it).
- Branch protection on `main`: required checks `lint-test` +
  `GitGuardian Security Checks`, strict=false, enforce_admins=false.
- Repo `allow_auto_merge` is **false** — must be enabled (one-time repo
  setting) for the auto-merged re-pin PR mechanism.
- Prod WIF: single pool `github-actions`, single provider `github-oidc` whose
  attribute condition is **scoped to `iac.yml` only** (repo == canonical &&
  workflow_ref startsWith iac.yml && ref == refs/heads/main && event in
  {push, workflow_dispatch}). `e2e.yml`'s WIF secrets point at the separate
  `payment-demo-e2e` project — its SA **cannot** touch prod. → the reset
  workflow needs its own provider + narrow SA in `driftscribe-hack-2026`.
- Secret Manager (prod): `coordinator-shared-token`, `upgrade-docs-github-pat`
  (the PAT that already patches/merges `demo/upgrade-target/package.json` —
  same authority domain as the re-pin), no `GITHUB_PAT_COORDINATOR_IAC_MERGE`
  (old plan draft guessed that name; wrong).
- Coordinator has `GET /autonomy`, `GET /pause` (plus POSTs) — asserts can
  read-then-restore instead of blind-POSTing.

## Latent bugs being fixed by this work

1. **payment-demo traffic is never restored after a rollback.** Rollback worker
   pins a specific old revision (`workers/rollback/main.py:244-255`) and never
   restores LATEST. `scripts/demo.sh` cleanup only resets env vars, not traffic.
   → first judge who rolls back leaves the service broken for everyone.
   Fix template: `tests/e2e/conftest.py:76-96` (traffic→LATEST + env, LRO wait).
2. **lodash upgrade demo already rotted.** `demo/upgrade-target/package.json:7`
   is `4.17.21` (patched by PR #2, merged 2026-05-25); documented baseline is
   `4.17.20`. Patch crew currently finds nothing to upgrade. Stale
   `upgrade/lodash-4-17-21` branch compounds it (see snapshot above).
3. **(Review finding) The invited judge rollback flow is a dead end.** The
   banner invites "roll back the payment-demo service", but the drift crew's
   only rollback tool is `propose_rollback_tool(target_revision, reason)`
   (`agent/adk_tools.py:83`), the Reader worker returns only the ACTIVE
   revision (`workers/reader/main.py:85-96`), and no revision-enumeration tool
   exists (`scripts/demo.sh:415` documents this for beat-e). Anonymous judges
   don't know revision names → Anchor has nothing concrete to act on. Since
   the live rollback IS the reason we opened at propose_apply, fix the product
   gap (Task A below) rather than deleting the invite from the banner.
   Coupling: shipping Task A makes bug #1 reachable by judges, so the traffic
   reset (Task B) must land with or before it.

## Task A — revision discovery for the rollback flow (NEW, from review)

Give Anchor a way to discover rollback candidates:

- **Reader worker** (`workers/reader/main.py`): also return a short list of
  previous READY revisions for the target service (newest first, exclude the
  active one, cap ~5, names only or name+create-time). Reader's SA already has
  `run.viewer` (includes `run.revisions.list`). Keep the response shape
  backward-compatible: add a field (e.g. `previous_revisions`), don't rename
  existing ones.
- **Coordinator** (`agent/worker_client.py` / `agent/models.py` /
  `agent/adk_tools.py` `read_live_env_tool`): pass the new field through to
  the tool's return dict so the LLM sees it.
- **Drift chat prompt** (`workloads/drift/chat_system_prompt.md`): one short
  addition — when asked to roll back without a concrete revision, pick the
  most recent previous revision from the live-state read (or confirm with the
  operator), never invent revision names. NOTE: drift prompts are
  byte-golden-pinned (`tests/unit/test_drift_workload_loads.py`, PR #174) —
  update the golden.
- Tests: reader unit tests for the new field (incl. empty/no-previous case);
  coordinator tool passthrough test; prompt golden update.

## Reset scope = "Everything" (restore demo-ready WITH intentional drift intact)

Judges at propose_apply can disturb only #1 and #2; #3/#4 are safety-net asserts.
"Intentional drift intact" = the adopt fixtures (infra-map drift), item #3.

1. **payment-demo** (`asia-northeast1`): restore traffic→LATEST and env to the
   `demo/ops-contract.yaml` baseline (`PAYMENT_MODE=mock`,
   `FEATURE_NEW_CHECKOUT=false`, remove `NEW_THING`).
   **Idempotency is pre-read-and-diff, MANDATORY:** `get_service`, compare
   traffic type + env against baseline, and exit silently when clean. Never
   call update on a no-op: (a) `gcloud run services update` creates a new
   revision even for identical values; (b) payment-demo mutations feed
   Eventarc back into the coordinator's `/eventarc` and autonomously trigger
   an Anchor run (LLM cost + rail noise every 2h forever). Template:
   `tests/e2e/conftest.py:76-96`.
2. **lodash**: re-pin `demo/upgrade-target/package.json` → `"lodash":"4.17.20"`
   via an **auto-merged PR** (decision — see Mechanism), and sweep stray
   upgrade PRs + `upgrade/lodash-*` branches (pattern:
   `tests/e2e/_github_helpers.py:sweep_upgrade_prs`; sweep must also delete
   the branch — `delete_branch_on_merge` is off). Skip cleanly when already
   at baseline AND no strays (idempotent; no commit, no PR).
   NOTE: `upgrade_merge_pr` merges into `main` of the coordinator's own repo
   (in-repo `demo/upgrade-target/`); no push-to-main build trigger exists
   (checked: 0 triggers) so no auto-deploy. Merge surface is provenance-gated
   (`workers/upgrade_docs/main.py:515` — `driftscribe` label + `upgrade/` head
   + `main` base + deploy-pinned green checks; none request-controlled).
3. **adopt fixtures**: verify `adopt-probe-topic` / `adopt-probe-sub` /
   `adopt-probe-svc` exist and **PR #168 stays OPEN** (the `/iac-approvals/168`
   recording depends on it; `docs/plans/2026-06-30-live-segments-shoot.md:54`).
   **Notify-only: never auto-repair** (Codex + review agree — this is a safety
   check, not a reset primitive). Anonymous judges CANNOT merge #168 (IaC
   merge is CF-JWT gated) — low risk.
4. **autonomy/pause**: `GET /autonomy` / `GET /pause` (operator token, run.app
   URL); if drifted from `propose_apply` / unpaused, POST to restore and say
   so loudly in the step summary. Anonymous can't change these (excluded from
   injection) — safety net.

Failure notification: any assert failure or unexpected state FAILS the job →
GitHub emails the owner; details in `$GITHUB_STEP_SUMMARY`. No webhook needed.

## Mechanism: GitHub Actions scheduled workflow

Only place that can touch GCP (payment-demo) + GitHub (lodash) + coordinator
(`/autonomy`,`/pause`) in one reviewable file; no new GCP infra footprint
beyond IAM (Cloud Scheduler API is disabled on the project).

**One workflow file `.github/workflows/demo-reset.yml`** (WIF condition is
per-workflow-file, so one file keeps IAM simple):

- Triggers: two crons + `workflow_dispatch` (with a `force-lodash` input for
  on-demand re-pin). Jobs branch on `github.event.schedule`:
  - `0 */2 * * *` UTC → service-reset job (payment-demo diff/restore,
    autonomy/pause assert, adopt-fixture verify). 2h not hourly: top-of-hour
    GH-Actions cron is the most-delayed slot, and 2h halves the odds of
    resetting under an active judge mid-demo.
  - `0 21 * * *` UTC (= 6:00 JST) → lodash job (re-pin PR + sweep). Daily so
    main history doesn't fill with reset commits; every run idempotent.
- `schedule`/`workflow_dispatch` on `main` only; never `pull_request_target`
  (same stance as `iac.yml`).
- **Auth (from review — e2e SA unusable, iac provider file-scoped):**
  - New narrow SA `demo-reset-sa@driftscribe-hack-2026`: `run.viewer` (project),
    `run.developer` on `payment-demo` only, `iam.serviceAccountUser` on
    payment-demo's runtime SA (needed for Cloud Run update), per-secret
    accessor on `coordinator-shared-token` + `upgrade-docs-github-pat`.
  - New WIF provider `github-oidc-demo-reset` on the existing
    `github-actions` pool, same condition shape as `github-oidc` but
    `workflow_ref.startsWith(...demo-reset.yml@)`.
  - Setup lives in a reviewable script `infra/scripts/setup_demo_reset.sh`
    (same conventions as `setup_iac_backend.sh` / `setup_e2e_project.sh`);
    operator session runs it once, then sets repo secrets
    `GCP_WIF_PROVIDER_DEMO_RESET` + `GCP_DEMO_RESET_SA`.
- **Lodash re-pin mechanism (decision): auto-merged PR, not PAT direct push.**
  Branch protection deliberately keeps required checks (`lint-test`,
  GitGuardian) on a public judged repo; a scheduled direct push would bypass
  CI + audit trail. Flow: fetch `upgrade-docs-github-pat` from Secret Manager,
  create `demo-reset/lodash-repin-<date>` branch + PR **with the PAT** (PAT-
  authored PRs trigger CI; default `GITHUB_TOKEN`-authored ones do NOT — the
  required checks would never run and auto-merge would never fire), enable
  auto-merge, done. Prereq: flip repo `allow_auto_merge` to true (one-time,
  operator session does it alongside secrets).
- Keep GH-Actions log output small; step-summary the reset actions taken.

## Task C — homepage notice

New `frontend/src/components/DemoNoticeBanner.svelte`, reuse `TourBanner.svelte`
`.ds-card` shell, slot at `frontend/src/App.svelte:880-885` (between TourBanner
`{/if}` and `<PauseBanner/>`). Dismissible (localStorage), since DEMO_MODE is
an edge-worker var the SPA can't see; banner is honest even for the operator
post-window, and gets removed at window close (runbook below). House voice: no
em dashes, honest, plain (memory `de_ai_home_copy`; models: `App.svelte:804`,
`TourBanner.svelte:17`).

Draft copy (**amended — old draft said "every day at 6:00 JST" which
contradicted the 2h service cadence; split the truth instead**):

> **This is a live sandbox.** Ask a crew to investigate drift, propose a fix,
> or roll back the payment-demo service and watch it happen. You can't break
> it for the next visitor: the service heals itself every couple of hours, and
> the upgrade demo resets every morning.

Frontend gates before push: `npm run test:unit -- --run`, `npm run check`,
`npm run build`. Coordinator serves the SPA → needs a coordinator-update deploy
(see `driftscribe-deploy` skill; traffic is pinned, shift after build).

## Deploy checklist (this initiative)

1. PR with Tasks A+B+C; CI green; Codex review (reply on thread above).
2. Merge → deploy **reader worker** AND **coordinator** (Task A spans both) +
   traffic shift. NOTE (final review): the field is additive and fail-soft in
   BOTH skew directions, so a one-sided deploy degrades gracefully (Anchor
   just asks for a revision name) rather than 5xx-ing — deploy both because
   that's what makes the prompt useful, not because skew breaks.
3. Operator session: run `infra/scripts/setup_demo_reset.sh`, set repo secrets
   `GCP_WIF_PROVIDER_DEMO_RESET`/`GCP_DEMO_RESET_SA`, enable repo
   `allow_auto_merge`.
4. `workflow_dispatch` demo-reset with `force-lodash=true` → verifies auth
   path end-to-end AND fixes today's rot (re-pin + stale branch sweep).
5. **(Codex) Verify the bootstrap actually converged:** the workflow enables
   auto-merge but does not wait — after CI on the re-pin PR completes, check
   the PR merged and `main:demo/upgrade-target/package.json` is back at
   `4.17.20` before calling the window healthy.
6. Live probe: anonymous-style rollback ask on /chat → Anchor proposes a
   concrete previous revision; approve; verify traffic restored by next
   2h reset run.

Deferred hardening (Codex, non-blocking): the `workloadIdentityUser` binding
is repository-scoped across the whole `github-actions` pool (matches existing
repo precedent), so any trusted-workflow provider on the pool could mint for
`demo-reset-sa`. Providers only admit main-branch schedule/dispatch, so not a
public-PR exposure; a per-provider attribute binding would tighten it later.

## Close-window runbook (when judging ends ~7/30)

1. `infra/cloudflare/demo-window.sh off` (edge FIRST; self-verifies gated).
2. `wrangler.toml` DEMO_MODE="0" + `wrangler deploy`.
3. Restore autonomy dial as desired.
4. **Disable the `demo-health.yml` and `demo-reset.yml` workflows**
   (`gh workflow disable demo-health.yml` — else it emails a failure
   every 30 min forever — then `gh workflow disable demo-reset.yml`).
5. **Remove `DemoNoticeBanner` from the SPA** (revert commit) + redeploy
   coordinator.

## Resolved items (were open in the parked draft)

- ~~Confirm GH-Actions GCP auth reuse~~ → NOT reusable (e2e SA lives in
  `payment-demo-e2e`; prod provider is iac.yml-scoped). New SA + provider,
  see Mechanism.
- ~~Decide lodash re-pin push mechanism~~ → auto-merged PR with
  `upgrade-docs-github-pat`; enable repo auto-merge.
- ~~Codex plan-review~~ → done 2026-07-07; findings folded in (Task A, copy
  fix, pre-read idempotency + Eventarc rationale, notify-only asserts,
  branch sweep, per-file WIF scoping).
