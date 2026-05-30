# Execution record — default-compute-SA retirement, Phases 1–3

**Date:** 2026-05-30
**Operator:** `theghostsquad00@gmail.com` (owner + ADC), project `driftscribe-hack-2026`
**Plan:** `docs/plans/2026-05-30-default-compute-sa-retirement.md` (Codex thread `019e78c2`)
**Target SA:** `1079423440495-compute@developer.gserviceaccount.com` (default compute SA)

## What ran

1. **Read-only verification sweep** (6 independent gcloud angles, all green / zero anomalies):
   - compute-SA resource policy = exactly `{rollback-agent-sa@, tofu-apply-sa@}` on `roles/iam.serviceAccountUser`; no tokenCreator/keyAdmin.
   - compute SA still holds `roles/editor` + the 6 other expected project roles (editor is the **Cloud-Build deploy backstop** for Phases 4–6 — *not* relevant to Phase 1, see correction below).
   - **0 of 12** Cloud Run services run as (or blank-default to) the compute SA.
   - both `rollback-agent-sa@` and `tofu-apply-sa@` independently hold actAs on `payment-demo-runtime@` → removal won't strand them.
   - Eventarc (2 triggers) + both Pub/Sub push subs all auth as `eventarc-trigger-sa@`; compute SA absent.
   - Cloud Build default SA = compute SA; 10/10 recent builds SUCCESS on it → **sole remaining consumer**.

2. **Phase 1 (mutation):** removed both `roles/iam.serviceAccountUser` members from the compute SA's **resource** policy.
   - Pre-state etag `BwZS9A9ncBg=` → post-state **empty policy** (no bindings), etag `BwZTCChyehY=`.
   - Backup of pre-state: `docs/handoff/2026-05-30-compute-sa-resource-policy.pre-phase1.json`.

3. **Post-verify:** `driftscribe-rollback` + `driftscribe-tofu-apply` `Ready=True`; both still hold actAs on `payment-demo-runtime@` (unchanged); `payment-demo` serving 100% on `payment-demo-00011-gng`. Post-mutation Cloud Logging scan: **no** permission failures caused by the removal. No Cloud Build triggers exist (global/regional); `driftscribe-rollback` `TARGET_SERVICE=payment-demo`.
   - **Why safe (Codex correction `019e78c2`):** `editor` on the compute SA is irrelevant to this removal — it grants the compute SA's *own* powers, not other identities' ability to actAs it. Safe because nothing needs to actAs the compute SA anymore (leftover from when payment-demo ran as it): rollback only does a traffic-mask `update_service` (no actAs), tofu-apply runs under its own identity, and both retain actAs on `payment-demo-runtime@`. Codex verified `workers/rollback/main.py:195` + `workers/tofu_apply/main.py:430` + `iac/cloudrun.tf:22`.
   - **Incidental (unrelated to this work):** one pre-existing `PERMISSION_DENIED` at `10:52:27Z` — `tofu-apply-sa` `artifactregistry.repositories.downloadArtifacts` on `payment-demo` (image-pull/AR-reader class, the C5g admission lesson) — predates the Phase-1 removal (`12:32:41Z`) by ~100 min. Flag for the task #19 carry-forward review; not caused by and not part of this retirement.

4. **Phase 2 (confirm-only, no mutation):** Eventarc/Pub-Sub surface fully decoupled from the compute SA. **Corrected a stale MEMORY note** that claimed the drift trigger `driftscribe-cloudrun-changes` was absent and `eventarc-trigger-sa@` orphaned — both are present and actively bound.

## Phase 3 (mutation, additive/inert) — open-decision #1 resolved: `run.admin`

Created `cloudbuild-deploy-sa@driftscribe-hack-2026.iam.gserviceaccount.com` and granted the full least-privilege build set (the identity Cloud Build will run as, replacing the compute SA). **Inert** — no cloudbuild file pins it yet. actAs list **derived from live configs**.

- **Project:** `roles/run.admin` + `roles/logging.logWriter`.
- **AR repo `driftscribe` (asia-northeast1):** `roles/artifactregistry.writer` + `roles/artifactregistry.reader`.
- **Staging bucket `gs://driftscribe-hack-2026_cloudbuild`:** `roles/storage.objectViewer` (build source-fetch — addition beyond the plan's role list; flagged to Codex).
- **actAs (`roles/iam.serviceAccountUser`) on 10 runtime SAs:** `driftscribe-agent, reader-agent-sa, docs-agent-sa, rollback-agent-sa, notifier-agent-sa, upgrade-reader-sa, upgrade-docs-sa, infra-reader-sa, tofu-apply-sa, payment-demo-runtime`. Verified **10/10**.
- **NOT granted:** `secretmanager.secretAccessor` (config audit: only deploy-time `--set-secrets`, runtime-SA-scoped). **Held:** spike SA actAs (decision #5 pending).
- **Codified:** idempotent block `# 4c.` in `infra/scripts/setup_secrets.sh` (additive; legacy `@cloudbuild` grants left for Phase 4). `bash -n` + shellcheck clean.
- **Rollback:** `gcloud iam service-accounts delete cloudbuild-deploy-sa@driftscribe-hack-2026.iam.gserviceaccount.com` (fully additive; nothing references it).

## Rollback (break-glass)

```bash
for SA in rollback-agent-sa tofu-apply-sa; do
  gcloud iam service-accounts add-iam-policy-binding \
    1079423440495-compute@developer.gserviceaccount.com \
    --project=driftscribe-hack-2026 \
    --member="serviceAccount:${SA}@driftscribe-hack-2026.iam.gserviceaccount.com" \
    --role="roles/iam.serviceAccountUser"
done
```

## 2026-05-30 — SPIKES RETIRED + Phase 4 EXECUTED (deploy-path cutover PROVEN GREEN)

Operator-authorized ("retire the spikes, then proceed to Phase 4"). Gated by a read-only recon fan-out + a Codex review of the execution micro-plan (thread `019e78c2`).

**Spikes retired** (open-decision #5 = retire): deleted services `spike-caller`/`spike-callee`, SAs `spike-caller-sa@`/`spike-callee-sa@` (zero grants, zero references — recon-proven), AR packages `spike-caller`/`spike-callee`; removed `spikes/cloud_run_auth/`; updated 3 architecture doc links. Build SA never held spike-SA actAs, so the spike cloudbuild left the pin scope cleanly.

**Phase 4 cutover:**
- 4 hack-2026-only files pinned `serviceAccount: cloudbuild-deploy-sa@` + `options.logging: CLOUD_LOGGING_ONLY` (hardcoded literal path).
- `infra/cloudbuild.yaml` left UNPINNED — it is DUAL-PURPOSE (canonical build for `driftscribe-e2e`, where `payment-demo-e2e` runs as e2e's default compute SA `486336957620-compute@` and there is no `cloudbuild-deploy-sa@`). The `payment-demo` no-SA regression fixed e2e-safely via empty-default `_TARGET_SERVICE_SA` + `$$`-escaped conditional bash arg. Stale header rewritten.
- `setup_secrets.sh`: removed dead legacy `@cloudbuild` §3/§4 grant blocks (§4c `cloudbuild-deploy-sa@` is the source of truth). bash -n + shellcheck clean.

**Green-build proof — both ran AS `cloudbuild-deploy-sa@` (verified via `builds describe`):**
- `tofu-apply.yaml`: build `7d542d57`, SUCCESS, 4/4 steps → classes 1 (AR push), 2 (run update), 3 (deploy + actAs `tofu-apply-sa`), 4 (`--set-secrets`). Rev `driftscribe-tofu-apply-00007-8bx` Ready.
- `infra-reader.yaml`: build `c7538147`, SUCCESS, 5/5 steps → 2nd actAs target `infra-reader-sa` + actAs on user-facing coordinator `driftscribe-agent@` (rev 00025-sg8 Ready).
- class 5 (`setIamPolicy`): asserted from `roles/run.admin` (includes `run.services.setIamPolicy`).

**Rollback:** `git revert` the cloudbuild edits → builds fall back to the compute SA (still holds `editor` through Phase 6).

## 2026-05-31 — Phase 5 EXECUTED (dropped the no-consumer narrow roles)

Operator-authorized ("continue to the next phase"). Gated by a read-only recon workflow (`wf_297c31a9-653`): 4 parallel surface sweeps + 3 per-role **adversarial skeptics** (default-to-blocker), all `consumerFound=false`/`blocker=false`. Codex (`019e78c2`): *"Phase 5 looks correct. I don't see a reason to roll anything back."*

- **Removed 4 bindings from the compute SA (all reversible; pre-state backed up):** project `roles/datastore.user` (`BwZTCmPpYmI=`), project `roles/run.viewer` (`BwZTCmQIJoM=`), project `roles/secretmanager.secretAccessor` (`BwZTCmQgfR4=`), and the **per-secret** `secretAccessor` on **`gemini-api-key`** (`BwZTCmE6dHE=` — a recon-discovered extension; sole binding on that secret, mounted by nothing). Backups: `docs/handoff/2026-05-31-compute-sa-project-roles.pre-phase5.json` + `2026-05-31-gemini-api-key-iam.pre-phase5.json`.
- **`--condition=None` needed:** the project policy contains conditional bindings (the dedicated SAs' DB-scoped `datastore.user` grants). Confirmed the compute SA's targets are unconditional; the conditional grants are intact post-removal.
- **Safety basis:** `datastore.user`/`run.viewer` are editor-masked (and `run.viewer` ⊂ retained `run.admin`); `secretmanager.secretAccessor` is NOT editor-masked (only `roles/owner` grants `secretmanager.versions.access`) so its removal is a real but consumer-less reduction. Structural proof of zero consumer: compute SA has 0 user keys + empty resource policy (not impersonable), 0/10 services run as it, all live secrets read via each consumer's own per-secret binding.
- **Codex precision (Phase-6 note):** the one residual "runs as compute SA" path is Cloud Build's **project default for UNPINNED configs** (`infra/cloudbuild.yaml` on hack-2026) — neutralized by Phase 4's pins; Phase 6 must verify it stays fail-closed.
- **Post-verify:** compute SA now holds exactly `artifactregistry.writer`, `editor`, `iam.serviceAccountUser`, `run.admin`; `gemini-api-key` bindings empty; 10/10 services `Ready=True`; coordinator HTTP 302. Zero impact.

## 2026-05-31 — Phase 6 EXECUTED (KEYSTONE — `editor` stripped; compute SA now ROLELESS)

Operator-authorized ("go to the next phase"). Gated by a read-only **adversarial** GATE-CHECK workflow (`wf_7670efb6-65a`, 9 agents: 6 surface sweeps + 3 default-to-blocker skeptics) → **gateClear=true, 0 blockers**, all high-confidence. Codex reviewed both the micro-plan and the completed work (`019e78c2`): *"looks clean … no gap I'd call a blocker."*

- **GATE CHECK (8/8 green):** compute SA = exactly 4 unconditional roles + empty resource policy + 0 user keys; **0/10** secrets bind the compute SA (incl. empty `gemini-api-key`); build SA independently complete (project `run.admin`+`logging.logWriter`, repo AR `reader`+`writer`, **10/10** actAs, bucket `objectViewer`; `run.admin` carries `setIamPolicy` and **zero** `actAs`); **0** Cloud Build triggers; 4 pinned files intact; `infra/cloudbuild.yaml` only unpinned + **doubly fail-closed** on hack-2026 (bash guard `exit 1` + AR push dies before the payment-demo deploy; CI `iac.yml` uses WIF as the plan-builder SA, never `builds submit`); org-policy won't re-add `editor`; no deny policies; `plan_approvals` = 3 docs all `status=used` (post-C5g, expired); no in-flight builds.
- **Removals (project scope, each `--condition=None`, decrement verified after each):** `run.admin` (`BwZTCx8xAxo=`) → `artifactregistry.writer` (`BwZTCx9nZks=`) → `iam.serviceAccountUser` (`BwZTCx-HNCs=`) → **`editor` LAST** (`BwZTCyAsmbk=`). Compute SA now holds **zero project-level role bindings**. The 3 dedicated SAs' conditional DB-scoped `datastore.user` grants survived untouched. Backup: `docs/handoff/2026-05-31-compute-sa-project-roles.pre-phase6.json`.
- **Codex-added checks (both folded in):** pre-mutation `gcloud builds list --ongoing` (empty); post-strip Cloud-Logging scans — `status.code=7` for either SA, and `severity>=WARNING` naming the compute SA, both `--freshness=2h` → **zero rows**.
- **Post-strip smoke (gate item 8):** `infra/cloudbuild.tofu-apply.yaml` build `1dfea533-6d0f-42a6-9ad3-f22b8a9c565d` — STATUS=SUCCESS, 3m8s, `serviceAccount=cloudbuild-deploy-sa@`, **all 4 steps SUCCESS** (AR push + `run deploy` + actAs(`tofu-apply-sa`) + `--set-secrets`) → build SA self-sufficient with `editor` gone. New rev `driftscribe-tofu-apply-00009-ttz` Ready on `tofu-apply-sa@`, `ingress=internal`.
- **Post-verify:** all 10 services `Ready=True` on dedicated SAs (none on compute SA); coordinator HTTP 302; compute SA resource policy empty + not disabled.
- **Rollback (break-glass):** re-grant any subset of the 4 roles from the backup (additive, instant) while the SA is still enabled.

## 2026-05-31 — Phase 7 EXECUTED (DISABLE + close regression sources — RETIREMENT COMPLETE)

Operator-authorized ("commit and push everything first. then continue to phase 7"). Gated by a read-only **adversarial** workflow (`wf_ad38fe98-17a`, 8 agents) + Codex pre-execution review + tie-break (`019e78c2`). Disable LAST.

- **Step 1 — reintroduction sources patched (committed `2ed8478`):** `setup_prod_project.sh:303` rollback-SA actAs fallback `compute@` → `payment-demo-runtime@`; `setup_e2e_project.sh` + `docs/runbooks/e2e-{environment,ci}.md` → live-resolve of `payment-demo-e2e`'s runtime SA with e2e-compute fallback (e2e's compute SA is NOT retired — separate project). `bash -n` clean.
- **Step 2 — dead build-identity debt pruned:** removed `roles/iam.serviceAccountUser` for **compute@** from 7 runtime SAs + for legacy **`@cloudbuild`** from 6 runtime SAs (`--condition=None`); plus `@cloudbuild`'s 3 dead **project** roles (`artifactregistry.writer`, `run.admin`, `iam.serviceAccountUser`), keeping only Google-managed `cloudbuild.builds.builder`. (Codex Q3: per-SA prune alone is *false cleanup* while the project-level `iam.serviceAccountUser` still grants actAs over all SAs.) Backup: `docs/handoff/2026-05-31-build-identity-actas.pre-phase7.json`.
- **Negative-reference check:** compute@ actAs **0/10**, `@cloudbuild` actAs **0/10**, `cloudbuild-deploy-sa@` **10/10**, compute@ project roles **0**, `@cloudbuild` only `cloudbuild.builds.builder`.
- **Step 3 — DISABLE:** `gcloud iam service-accounts disable 1079423440495-compute@developer.gserviceaccount.com` → `disabled: True`. NOT deleted (reversible: `enable` + re-grant).
- **Gate blocker disproven:** the gate returned `gateClear=FALSE` on one skeptic (S1) citing 28/31 historical compute@ builds. **Timeline false positive** — those builds predate the Phase-6 strip; a compute@ build today fails at `run.services.update` (zero project roles) before actAs is consulted. The 3 post-strip + most-recent builds all ran as `cloudbuild-deploy-sa@`/SUCCESS. S2 + S3 + surface checks P1–P5 cleared; **Codex tie-break = GO** (fresh backups taken first).
- **Post-disable smoke:** `tofu-apply.yaml` build `d244f7fb-89f5-4ab7-982a-9eef5a7ed12d` SUCCESS (3m29s) AS `cloudbuild-deploy-sa@` **with compute@ disabled** → rev `driftscribe-tofu-apply-00011-jzq` Ready on `tofu-apply-sa@`.
- **Post-verify:** 10/10 services `Ready=True` on dedicated SAs; coordinator HTTP 302; **zero** `code=7` + **zero** cloud_run/build errors (last 2h).
- **Eventarc:** 2 ACTIVE drift triggers on `eventarc-trigger-sa@` (the "trigger absent" carry-forward is stale/corrected).
- **Rollback (break-glass):** `enable` + re-grant from `docs/handoff/2026-05-31-build-identity-actas.pre-phase7.json`.

**RETIREMENT COMPLETE** — the default compute SA `1079423440495-compute@` is roleless, referenced in zero IAM bindings, and disabled.

## Commit status

All compute-SA-retirement work is on branch `chore/compute-sa-retirement` / **PR #34** (Phases 1–7): spike retirement, the Cloud Build deploy-SA cutover + fail-closed guard, the Phase-7 reintroduction-source patches, and the plan + handoff + IAM backups. The live Phase 5/6/7 IAM mutations touched **no repo code** — only live gcloud + untracked backup JSONs (`*.pre-phase1`, `*.pre-phase5` ×2, `*.pre-phase6`, `build-identity-actas.pre-phase7`).

**Rollback now (post-disable)** = `gcloud iam service-accounts enable 1079423440495-compute@developer.gserviceaccount.com` **+** targeted role/actAs re-grants from the backups (the `enable` step is new vs the pre-Phase-7 re-grant-only rollback).

**Post-merge hygiene (Codex `019e78c2`, operator action after PR #34 merges):** one `main`-branch grep for `1079423440495-compute@` / `compute@developer` to confirm no active config/script re-introduces the dependency — remaining hits should only be intentional point-in-time records under `docs/plans/*` (left as history) and e2e's *own* compute SA (not retired). Leave the Google-managed `cloudbuild.builds.builder` on `@cloudbuild` alone; keep the unpinned `infra/cloudbuild.yaml` guard documented (the project default build SA still points at the now-disabled compute SA by design → unpinned builds fail-closed).
