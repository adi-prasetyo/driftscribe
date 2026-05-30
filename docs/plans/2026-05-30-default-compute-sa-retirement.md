# Plan — Retire the GCP default compute service account

**Status:** ✅ COMPLETE — reviewed by Codex (thread `019e78c2`). **All 7 phases EXECUTED live (Phases 1–4 + spikes 2026-05-30; Phases 5–7 on 2026-05-31)** (see EXECUTION LOG below), 100% operator-bootstrap; open-decisions #1 = **`run.admin`**, #2 = **disable-not-delete**, #3 = **dedicated build SA**, #4 = **repo-scoped AR**, #5 = **retire the spikes** all RESOLVED. The build SA `cloudbuild-deploy-sa@` is PROVEN GREEN across every permission class — including a **post-editor-strip smoke** (`tofu-apply.yaml` build `1dfea533`, all 4 steps SUCCESS AS the build SA *after* the compute SA's `editor` was gone). **The compute SA is now roleless AND disabled** — Phase 6 stripped every project role (`editor` LAST); Phase 7 pruned all dead build-identity actAs (compute@ ×7 SAs, `@cloudbuild` ×6 SAs) + the legacy `@cloudbuild` dead project grants, then `disable`d the SA (NOT deleted; reversible via `enable`+re-grant). **Retirement COMPLETE.** Codex reviewed Phase 6 (`019e78c2`): "looks clean … no gap I'd call a blocker."
**Date:** 2026-05-30 (Phases 5–7 executed 2026-05-31)

---

## EXECUTION LOG

### 2026-05-30 — Phase 1 EXECUTED + Phase 2 CONFIRMED (operator gcloud, owner `theghostsquad00@gmail.com`)
Gated by a 6-angle read-only verification sweep (all green, zero anomalies): compute-SA resource policy = exactly the two expected `iam.serviceAccountUser` members; compute SA still holds `editor` (backstop intact); **0 of 12** Cloud Run services run as (or default to) the compute SA; both workers independently hold actAs on `payment-demo-runtime@`; Eventarc/Pub-Sub fully on `eventarc-trigger-sa@` (compute SA absent); Cloud Build = sole consumer (10/10 recent builds SUCCESS on the compute SA).

- **Phase 1 DONE:** removed both vestigial `roles/iam.serviceAccountUser` bindings (`rollback-agent-sa@`, `tofu-apply-sa@`) from the compute SA's **resource** policy. Pre-state etag `BwZS9A9ncBg=`; post-state = **empty policy** (no bindings, etag `BwZTCChyehY=`). Pre-mutation backup: `docs/handoff/2026-05-30-compute-sa-resource-policy.pre-phase1.json`.
- **Post-verify:** `driftscribe-rollback` + `driftscribe-tofu-apply` both `Ready=True`; both still hold actAs on `payment-demo-runtime@` (the grant they actually use — unchanged); `payment-demo` serving 100% on `payment-demo-00011-gng`. A post-mutation Cloud Logging scan found **no** permission failures caused by the removal (the only `PERMISSION_DENIED` in the window — `tofu-apply-sa` AR `downloadArtifacts` on payment-demo at `10:52:27Z` — predates the removal at `12:32:41Z` by ~100 min and is an unrelated image-pull/AR-reader event, not an actAs failure). No Cloud Build triggers exist (global or regional); `driftscribe-rollback` `TARGET_SERVICE=payment-demo`.
  - **Why this was a safe no-op (Codex correction `019e78c2`):** `roles/editor` on the compute SA is **irrelevant** to this removal — editor grants powers *to* the compute SA, not other identities' ability to actAs it. The removal was inert because **nothing needs to actAs the compute SA anymore**: these two grants were leftovers from when `payment-demo` ran as the compute SA; now it runs as `payment-demo-runtime@`, the rollback worker only does a traffic-mask `update_service` (no actAs), tofu-apply runs under its own identity (no compute-SA impersonation path), and both workers retain actAs on `payment-demo-runtime@`. (Editor still matters as the **Cloud-Build deploy backstop** for Phases 4–6, just not for Phase 1.)
- **Rollback (if ever needed):** `gcloud iam service-accounts add-iam-policy-binding 1079423440495-compute@developer.gserviceaccount.com --member="serviceAccount:<sa>@driftscribe-hack-2026.iam.gserviceaccount.com" --role="roles/iam.serviceAccountUser"` for each of the two members.
- **Phase 2 CONFIRMED (no mutation):** two drift triggers (`driftscribe-cloudrun-changes`, `driftscribe-cloudrun-changes-v2-update`) live in `asia-northeast1`, both on `eventarc-trigger-sa@`; both backing Pub/Sub push subs auth as `eventarc-trigger-sa@`; compute SA appears nowhere. **The MEMORY carry-forward note that the drift trigger was "absent" and `eventarc-trigger-sa@` "orphaned" is STALE/incorrect** — both are present and actively bound (trigger created 2026-05-29T15:59Z).

### 2026-05-30 — Phase 3 EXECUTED (open-decision #1 resolved: `run.admin`)
User chose `run.admin` for the build SA. Created `cloudbuild-deploy-sa@driftscribe-hack-2026.iam.gserviceaccount.com` (inert — no cloudbuild file references it yet) and granted the full least-privilege build set; all verified (project roles, AR repo roles, bucket role, 10/10 actAs). The actAs list was **derived from live configs** (every `--service-account=` across `infra/cloudbuild*.yaml` + `payment-demo-runtime@`), not hand-maintained.

- **Created:** `cloudbuild-deploy-sa@` (display "Cloud Build deploy SA (default-compute retirement)").
- **Granted:** project `roles/run.admin` + `roles/logging.logWriter`; AR repo `driftscribe` (asia-northeast1) `roles/artifactregistry.writer` + `roles/artifactregistry.reader`; **`roles/storage.objectViewer` scoped to `gs://driftscribe-hack-2026_cloudbuild`** (build source-fetch — an addition beyond the plan's enumerated role set, since `gcloud builds submit` stages source there; flagged to Codex); `roles/iam.serviceAccountUser` on each of **10** runtime SAs (`driftscribe-agent, reader-agent-sa, docs-agent-sa, rollback-agent-sa, notifier-agent-sa, upgrade-reader-sa, upgrade-docs-sa, infra-reader-sa, tofu-apply-sa, payment-demo-runtime`).
- **NOT granted:** `secretmanager.secretAccessor` — config audit found only deploy-time `--set-secrets` (runtime-SA-scoped), no build-step secret reads. (Watch in Phase 4: confirm `--set-secrets` deploy-time validation doesn't need a deployer-side secret read; if it does, add narrow `secretmanager.viewer`.)
- **Held (least-privilege):** actAs on `spike-caller-sa@`/`spike-callee-sa@` — pending open-decision #5 (retire the spikes?). Trivially added if spikes are kept.
- **Codified:** new idempotent block `# 4c.` in `infra/scripts/setup_secrets.sh` (additive; does NOT remove the legacy `${PROJECT_NUMBER}@cloudbuild` §3/§4 grants — that's Phase 4's cutover). `bash -n` + shellcheck-error clean.
- **Rollback:** delete the SA (`gcloud iam service-accounts delete cloudbuild-deploy-sa@…`) — fully additive, nothing references it.

### 2026-05-30 — SPIKES RETIRED + Phase 4 EXECUTED (deploy-path cutover PROVEN GREEN)
User authorized "retire the spikes, then proceed to Phase 4." Gated by a 4-agent read-only recon fan-out (spike-deletion safety + build-SA re-verify + cloudbuild edit-scope map) and a Codex review of the execution micro-plan on this thread.

- **Spikes retired (decision #5 = retire):** deleted Cloud Run services `spike-caller` + `spike-callee`, SAs `spike-caller-sa@` + `spike-callee-sa@` (held ZERO grants; recon proved zero live/repo references), AR image packages `spike-caller` + `spike-callee`; removed `spikes/cloud_run_auth/` from the repo; updated 3 architecture doc links (`iam-matrix.md`, `multi-agent-design.md` ×2) to note the retirement (learnings preserved in git history). The build SA was never granted spike-SA actAs (Phase 3 held it), so this cleanly removes the spike cloudbuild from the pin scope.
- **Phase 4 cloudbuild cutover (5 remaining files):**
  - **4 hack-2026-only files** (`coordinator-update`, `infra-reader`, `tofu-apply`, `upgrade-docs-update`): pinned `serviceAccount: projects/driftscribe-hack-2026/serviceAccounts/cloudbuild-deploy-sa@…` + `options: { logging: CLOUD_LOGGING_ONLY }` (hardcoded literal — the top-level `serviceAccount` field is **not** a documented substitution site; Codex-confirmed).
  - **`infra/cloudbuild.yaml` left UNPINNED — new e2e discovery:** this file is DUAL-PURPOSE (DO-NOT-RUN on hack-2026 but the canonical build for project `driftscribe-e2e`). On e2e, `payment-demo-e2e` runs as e2e's **default compute SA** (`486336957620-compute@`) and there is **no** `cloudbuild-deploy-sa@`. A hardcoded hack-2026 SA would break e2e (cross-project). So the file is deliberately not pinned; on hack-2026 post-Phase-6 it runs as the editor-stripped compute SA and **fails-closed at the first AR push** (no churn). The `payment-demo` no-`--service-account` regression was fixed **e2e-safely**: new `_TARGET_SERVICE_SA` substitution (empty default ⇒ e2e unchanged; a hack-2026 run can pin `payment-demo-runtime@`), appended via the proven `$$`-escaped conditional-arg bash pattern (same as `coordinator-update.yaml`). Stale "Option C / driftscribe-agent doesn't exist on hack-2026" header rewritten (hack-2026 is now full-stack).
  - **`setup_secrets.sh`:** removed the dead legacy `@cloudbuild` §3 grant block + §4 actAs loop (that SA was never the identity builds ran as); §4c (the Phase-3 `cloudbuild-deploy-sa@` block) is the source of truth. Live legacy `@cloudbuild` bindings left inert (prune in a later hygiene pass). `bash -n` + shellcheck-error clean.
- **Green-build proof (both ran AS `cloudbuild-deploy-sa@`, verified via `builds describe`):**
  - `tofu-apply.yaml` — build `7d542d57`, 3m43s, SUCCESS; `SERVICE_ACCOUNT=cloudbuild-deploy-sa@`, `options.logging=CLOUD_LOGGING_ONLY`, 4/4 steps SUCCESS → proved **class 1** (AR push), **2** (run update / OWN_URL), **3** (run deploy + actAs `tofu-apply-sa`), **4** (`--set-secrets plan-hmac-key`). New rev `driftscribe-tofu-apply-00007-8bx` Ready, runtime SA `tofu-apply-sa@` preserved; NO tofu apply executed by the deploy.
  - `infra-reader.yaml` — build `c7538147`, 3m4s, SUCCESS; same build SA, 5/5 steps SUCCESS → 2nd independent actAs target (`infra-reader-sa`, rev 00004-8lb Ready) + actAs on the **user-facing coordinator** `driftscribe-agent@` via the URL-wiring `run services update` step (rev 00025-sg8 Ready).
  - **class 5** (`--allow-unauth` → `setIamPolicy`): asserted from `roles/run.admin` membership — `gcloud iam roles describe roles/run.admin` confirms `run.services.setIamPolicy` (+ `create`/`update`/`getIamPolicy`) is included (project-wide; nothing per-target to miss, unlike actAs). The only `--allow-unauth` deploys live in `infra/cloudbuild.yaml` (payment-demo + agent), both unsafe to run as a proof.
  - The *"gcloud builds submit only displays logs from Cloud Storage"* message on both builds is itself confirmation the user-specified SA + `CLOUD_LOGGING_ONLY` took effect (the compute SA writes streamable GCS logs).
- **Rollback:** `git revert` the cloudbuild edits → builds fall back to the compute SA (still holds `editor` through Phase 6).

### 2026-05-31 — Phase 5 EXECUTED (dropped the no-consumer narrow roles + a discovered per-secret binding)
Operator-authorized ("continue to the next phase"). Gated by a read-only recon workflow (`wf_297c31a9-653`): 4 parallel surface sweeps (Secret Manager, Firestore/Datastore, run.viewer+Eventarc/PubSub/Scheduler, repo/setup-script grep) + 3 per-role **adversarial skeptics** (default-to-blocker), **all** returning `consumerFound=false`/`blocker=false`. Codex reviewed the completed work (`019e78c2`): *"Phase 5 looks correct. I don't see a reason to roll anything back."*

- **Removed from the compute SA (4 bindings, all reversible via re-grant; pre-state backed up):**
  1. project `roles/datastore.user` (etag `BwZTCmPpYmI=`)
  2. project `roles/run.viewer` (etag `BwZTCmQIJoM=`)
  3. project `roles/secretmanager.secretAccessor` (etag `BwZTCmQgfR4=`)
  4. **per-secret** `roles/secretmanager.secretAccessor` on secret **`gemini-api-key`** (etag `BwZTCmE6dHE=`) — an **extension beyond the planned 3** discovered by recon: the compute SA was the *sole* binding on that secret, which is mounted by no service and unexercisable; folded in because it's the same "no-consumer secret access on the compute SA" intent and the Phase-6 gate sweeps secret IAM. `gemini-api-key` now has empty bindings. Backups: `docs/handoff/2026-05-31-compute-sa-project-roles.pre-phase5.json` + `2026-05-31-gemini-api-key-iam.pre-phase5.json`.
- **`--condition=None` wrinkle:** the first removal attempt failed (`Removing a binding without specifying a condition from a policy containing conditions is prohibited`) because the project policy *does* contain conditional bindings — the dedicated SAs' **database-scoped** `datastore.user` grants (`driftscribe-agent`+`rollback-agent-sa` on the default DB; `tofu-apply-sa` on `plan-approvals`). Confirmed the compute SA's own 3 target bindings are **unconditional**, re-ran with `--condition=None`; post-verify confirms the 2 conditional grants are **intact** (untouched).
- **Why safe (consumer-less, not editor-masked for secrets):** `datastore.user`/`run.viewer` are editor-masked (and `run.viewer` is also a strict subset of the retained `run.admin` — 47⊂78 perms). `secretmanager.secretAccessor` is **NOT** editor-masked — per Google's Secret Manager IAM docs only `roles/owner` includes `secretmanager.versions.access`, not `editor`/`viewer` — so its removal is a *real* reduction, justified solely by zero consumer. Structural proof of zero consumer: compute SA has 0 user-managed keys + empty resource IAM policy (no actAs/tokenCreator principals) → not impersonable; 0/10 Cloud Run services run as it; no GCE/Functions/Jobs/Scheduler; every live secret read via each consumer's own per-secret binding.
- **Codex precision (record for Phase 6):** "not impersonable" applies to keys/resource-policy paths — but **Cloud Build's project default still authenticates *as* the compute SA for any UNPINNED build config** (i.e. `infra/cloudbuild.yaml` on hack-2026). Phase 4 neutralizes this for the 4 pinned files; it is the one residual "runs as compute SA" path and is exactly why Phase 6 must verify `infra/cloudbuild.yaml` stays fail-closed.
- **Post-verify:** compute SA now holds exactly **`artifactregistry.writer`, `editor`, `iam.serviceAccountUser`, `run.admin`** (keystone set); `gemini-api-key` bindings empty; all 10 Cloud Run services `Ready=True` on dedicated SAs; coordinator `https://driftscribe.adp-app.com` → HTTP 302 (up). Zero impact (blast radius was already zero).
- **Rollback:** re-grant any subset (additive, instant) from the backup files.

### 2026-05-31 — Phase 6 EXECUTED (KEYSTONE — `editor` + build-overlapping roles stripped; compute SA now ROLELESS)
Operator-authorized ("go to the next phase"). Gated by a read-only **adversarial** GATE-CHECK workflow (`wf_7670efb6-65a`, 9 agents): 6 surface sweeps (compute-SA self-state, Secret-Manager IAM, build-SA completeness, Cloud-Build surface+repo pin-state, cross-surface runs-as, plan-approvals Firestore) + 3 default-to-blocker skeptics (live-workload breakage, residual runs-as-compute-SA path, build-SA grant completeness) — **gateClear=true, 0 blockers**, all high-confidence. Codex reviewed both the micro-plan and the completed work (`019e78c2`): *"looks clean … no gap I'd call a blocker."* Codex added two checks (re-run the `--ongoing` race guard immediately pre-mutation; post-strip Cloud-Logging `PERMISSION_DENIED` scan) — both folded in.

- **GATE-CHECK results (all 8 items green):** compute SA held exactly the 4 expected **unconditional** roles + empty resource policy + 0 user keys; **0/10** secrets bind the compute SA (incl. empty `gemini-api-key`); build SA independently complete (project `run.admin`+`logging.logWriter`, repo AR `reader`+`writer`, **10/10** actAs, bucket `objectViewer`; `run.admin` includes `setIamPolicy` and contains **zero** `actAs` → actAs sourced solely from the per-SA grants, independent of the compute SA); **0** Cloud Build triggers (global+regional); the 4 pinned files still carry `cloudbuild-deploy-sa@`+`CLOUD_LOGGING_ONLY`; `infra/cloudbuild.yaml` is the only unpinned config and is **doubly fail-closed** on hack-2026 (leading bash guard `exit 1` + first AR push dies before the payment-demo deploy; nothing triggers it; CI `iac.yml` authenticates via WIF as the plan-builder SA, never `builds submit`); org-policy `automaticIamGrantsForDefaultServiceAccounts` has no enforcement spec set + governs grant-at-creation only (won't re-add `editor`); no deny policies; `plan_approvals` has 3 docs all `status=used` (none pending, all post-C5g + expired); no in-flight builds.
- **Removals (project scope, each `--condition=None`, decrement verified after each):** `roles/run.admin` (etag `BwZTCx8xAxo=`) → `roles/artifactregistry.writer` (`BwZTCx9nZks=`) → `roles/iam.serviceAccountUser` (`BwZTCx-HNCs=`) → **`roles/editor` LAST** (`BwZTCyAsmbk=`). Compute SA now holds **zero project-level role bindings**. The 3 dedicated SAs' conditional DB-scoped `datastore.user` grants survived untouched (proof `--condition=None` hit only the compute SA's unconditional bindings). Backup/rollback spec: `docs/handoff/2026-05-31-compute-sa-project-roles.pre-phase6.json`.
- **Post-strip smoke (gate item 8):** `infra/cloudbuild.tofu-apply.yaml` build `1dfea533-6d0f-42a6-9ad3-f22b8a9c565d` — STATUS=SUCCESS, 3m8s, `serviceAccount=cloudbuild-deploy-sa@`, **all 4 steps SUCCESS** → AR push + `run deploy` + actAs(`tofu-apply-sa`) + `--set-secrets` all green with `editor` gone. New rev `driftscribe-tofu-apply-00009-ttz` Ready, runs as `tofu-apply-sa@`, `ingress=internal` (unchanged).
- **Post-verify:** all 10 Cloud Run services `Ready=True` on dedicated SAs (none on the compute SA, revision-level); coordinator `https://driftscribe.adp-app.com` → HTTP 302; compute SA resource policy still empty + not disabled. Cloud-Logging scans (`status.code=7` for either SA; `severity>=WARNING` naming the compute SA; `--freshness=2h`) returned **zero** rows.
- **Rollback (break-glass):** re-grant any subset of the 4 roles from the backup (additive, instant). Still holds while the SA is enabled — Phase 7's disable adds an `enable` step to the rollback.

### 2026-05-31 — Phase 7 EXECUTED (DISABLE + close regression sources — RETIREMENT COMPLETE)
Operator-authorized ("commit and push everything first. then continue to phase 7"). Codex-recommended ordering followed (disable LAST). Gated by a read-only **adversarial** workflow (`wf_ad38fe98-17a`, 8 agents) + Codex pre-execution review + Codex tie-break (`019e78c2`).
- **Step 1 — reintroduction sources patched (code, committed `2ed8478`):** `setup_prod_project.sh:303` rollback-SA actAs fallback default `compute@` → `payment-demo-runtime@` (post-C5g reality). `setup_e2e_project.sh:339` + `docs/runbooks/e2e-environment.md` + `e2e-ci.md` — e2e's compute SA is NOT being retired (separate project), so the hardcoded compute-SA default became a live-resolve of `payment-demo-e2e`'s runtime SA with e2e-compute fallback. `bash -n` clean.
- **Step 2 — dead build-identity debt pruned (Codex Q3 correction folded in):** removed `roles/iam.serviceAccountUser` for the **compute SA** from **7** runtime SAs (driftscribe-agent, reader-agent-sa, docs-agent-sa, rollback-agent-sa, notifier-agent-sa, upgrade-reader-sa, upgrade-docs-sa) + for the legacy **`@cloudbuild` SA** from **6** runtime SAs (the 5 agents + tofu-apply-sa), each `--condition=None`. Also removed `@cloudbuild`'s 3 dead **project** roles (`artifactregistry.writer`, `run.admin`, `iam.serviceAccountUser`), leaving only the Google-managed `cloudbuild.builds.builder` — because pruning per-SA actAs alone is false cleanup while the project-level `iam.serviceAccountUser` still grants actAs over all SAs (Codex). Pre-mutation backup: `docs/handoff/2026-05-31-build-identity-actas.pre-phase7.json`.
- **Negative-reference check (post-prune):** compute@ actAs = **0/10**, `@cloudbuild` actAs = **0/10**, `cloudbuild-deploy-sa@` coverage = **10/10** (deploy path intact), compute@ project roles = **0**, `@cloudbuild` project roles = only `cloudbuild.builds.builder`. Compute SA referenced in zero IAM bindings.
- **Step 3 — DISABLE:** `gcloud iam service-accounts disable 1079423440495-compute@developer.gserviceaccount.com` → `disabled: True`. NOT deleted (reversible via `enable` + re-grant from backup).
- **GATE NOTE — one blocker, disproven:** the adversarial gate returned `gateClear=FALSE` on a single high-confidence skeptic (S1) claiming compute@ is the live deploy path (28/31 historical builds ran as compute@; build `80c19220` deployed driftscribe-agent via compute@ actAs on 2026-05-30). **Confirmed a timeline false positive:** those builds predate the Phase-6 role strip; build `80c19220` re-run today fails at the `run.services.update` permission check (compute@ has zero project roles) BEFORE actAs is consulted. The 3 post-strip builds + the most-recent build (`1dfea533`) all ran as `cloudbuild-deploy-sa@`/SUCCESS — the deploy path was already cut over. Skeptics S2 + S3 (both high-confidence, no blocker) + surface checks P1–P5 all cleared; **Codex tie-break = GO** (condition: fresh backups before prune — done).
- **Post-disable smoke:** `infra/cloudbuild.tofu-apply.yaml` build `d244f7fb-89f5-4ab7-982a-9eef5a7ed12d` — STATUS=SUCCESS, 3m29s, AS `cloudbuild-deploy-sa@`, **with compute@ disabled** → source-fetch + AR push + `run.services.update` + actAs(`tofu-apply-sa`) + `--set-secrets` all green. New rev `driftscribe-tofu-apply-00011-jzq` Ready on `tofu-apply-sa@`.
- **Post-verify:** all 10 Cloud Run services `Ready=True` on dedicated SAs; coordinator `https://driftscribe.adp-app.com` → HTTP 302; **zero** `PERMISSION_DENIED` (code=7) + **zero** cloud_run/build errors in last 2h.
- **Eventarc note:** gate confirmed **2 ACTIVE** drift triggers (`driftscribe-cloudrun-changes` + `…-v2-update`) on `eventarc-trigger-sa@` — the earlier "trigger absent" carry-forward is stale/corrected.
- **Rollback (break-glass):** `enable` the SA + re-grant any pruned binding from `docs/handoff/2026-05-31-build-identity-actas.pre-phase7.json` (additive, instant).

**RETIREMENT COMPLETE** — the default compute SA `1079423440495-compute@` is roleless, referenced in zero IAM bindings, and disabled. Nothing held.
**Project:** `driftscribe-hack-2026`
**Target SA:** `1079423440495-compute@developer.gserviceaccount.com` (the GCP **default compute** service account)
**Goal:** Move every workload onto dedicated least-privilege SAs and strip the compute SA's `roles/editor` (plus prune redundant narrow roles), ending at a disabled-but-present, role-less SA.
**Mechanism:** 100% **operator-bootstrap** (owner `gcloud`). The gated C2→C4 tofu pipeline is structurally barred from SA/IAM changes (`driftscribe_lib/iac_plan_denylist.py` hard-denies all `google_service_account` + `*_iam_*` + delete/forget/replace; static gate makes imports/providers/variables operator-only). Only the already-done `payment-demo` runtime-SA flip (C5g) rode the pipeline.

---

## TL;DR — the crux

**Live runtime blast radius is ZERO.** Verified live (2026-05-30):

| Surface | Runs as compute SA? | Evidence |
|---|---|---|
| 12 Cloud Run services (incl. `payment-demo`) | **No** — all on dedicated SAs | `run services list` → each has its own `*-sa@…iam`; `payment-demo`→`payment-demo-runtime@` (C5g) |
| Eventarc drift triggers + Pub/Sub push subs | **No** — `eventarc-trigger-sa@` | `eventarc triggers list`; both triggers live (the "absent/orphaned" carry-forward notes were stale) |
| GCE / GKE / Cloud Functions / Scheduler | **No consumers** | those APIs are disabled |
| **Cloud Build** | **YES — sole consumer** | `gcloud builds get-default-service-account` → compute SA; every recent build ran as it |

So this is **not** a broad blast-radius project — it is a **single-consumer migration**: move **Cloud Build** onto a dedicated build SA, then strip `editor`.

The compute SA's current project roles: `editor, datastore.user, artifactregistry.writer, iam.serviceAccountUser, run.admin, run.viewer, secretmanager.secretAccessor`. Resource-level (ON the SA's own policy): `rollback-agent-sa` + `tofu-apply-sa` hold `iam.serviceAccountUser` (a dead transition leftover — both already hold the equivalent on `payment-demo-runtime@`). No member holds `serviceAccountTokenCreator`. Key is `SYSTEM_MANAGED` only.

---

## Two Codex corrections that reshape the risk story

1. **`roles/editor` *does* include `iam.serviceAccounts.actAs`.** Earlier framing ("project-level `iam.serviceAccountUser` is the one narrow role load-bearing now and NOT editor-implied") was **wrong**. While `editor` is present, the compute SA can actAs **every** runtime SA via `editor` alone — that's *why* Cloud Build deploys all ~12 services today. The explicit `iam.serviceAccountUser` is redundant until `editor` is removed. The keystone risk is the **`editor` removal**, and the build SA must explicitly replicate editor's blanket actAs (per-SA) before that.

2. **`roles/run.developer` is NOT enough for the build SA.** Three deploy steps use `--allow-unauthenticated`, which calls `run.services.setIamPolicy` (in `run.admin`, **not** `run.developer`):
   - `infra/cloudbuild.yaml:174` (`payment-demo`) and `:198` (`driftscribe-agent`)
   - `spikes/cloud_run_auth/cloudbuild.yaml:109` (`spike-caller`)

   The eight workers deploy `--no-allow-unauthenticated`. **Trap:** the natural cutover test (`coordinator-update.yaml`) uses `gcloud run services update` (line 111 — preserves the SA, no `setIamPolicy`), so it would pass on `run.developer` while the public-service deploys fail later. → Build SA needs **`run.admin`**, OR refactor public-invoker out-of-band (see open decisions).

**Why builds work today despite `setup_secrets.sh` looking wrong:** `setup_secrets.sh:117` grants the build/actAs roles to the **legacy `${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com`** SA — *not* the compute SA that builds actually run as — and its actAs loop (line 158) misses `upgrade-reader`, `upgrade-docs`, `infra-reader`, `payment-demo-runtime`. That loop is **dead config**. Every deploy works purely because `editor` on the compute SA silently covers actAs + setIamPolicy + AR write + secret access. Removing `editor` removes ALL of that at once — which is exactly what the build SA must replace.

---

## The plan (7 phases — lowest-risk first, strip `editor` LAST)

### Phase 1 — Remove the vestigial actAs grants ON the compute SA — ✅ DONE 2026-05-30
Strip the two dead `roles/iam.serviceAccountUser` bindings (`rollback-agent-sa`, `tofu-apply-sa`) from the compute SA's **resource** policy.
- **Pre-check:** confirm no Cloud Run service runs as the compute SA; confirm both workers hold the equivalent actAs on `payment-demo-runtime@`.
- **Verify:** `get-iam-policy` on the compute SA shows no `iam.serviceAccountUser` binding; a C4 no-op `/propose`+`/apply` smoke and a rollback traffic-describe both still succeed.
- **Rollback:** re-add the two bindings (additive, instant).
- **Risk:** Low.

### Phase 2 — Reconcile/confirm the Eventarc surface (decouple, confirm-only) — ✅ CONFIRMED 2026-05-30
Confirm both drift triggers + `eventarc-trigger-sa@` are healthy and **out of scope**; correct the stale "drift trigger absent / eventarc-trigger-sa orphaned" carry-forward notes.
- **No mutation.** Pure confirmation so a later phase never conflates `eventarc-trigger-sa@` with the compute SA.
- **Risk:** Very low.

### Phase 3 — Create `cloudbuild-deploy-sa@` and grant the full least-privilege build set (additive, inert) — ✅ DONE 2026-05-30
Stand up the dedicated build identity; nothing uses it until Phase 4.
- Repo-scoped `roles/artifactregistry.writer` **+ `roles/artifactregistry.reader`** on the `driftscribe` AR repo (the C5g image-pull-admission prereq — a missing reader yields an opaque `PERMISSION_DENIED` at deploy, not plan).
- **`roles/run.admin`** at project level (NOT `run.developer` — `--allow-unauthenticated` needs `setIamPolicy`), unless the public-invoker-refactor decision is taken.
- `roles/logging.logWriter` (mandatory once a user-specified build SA runs with `options.logging`).
- `roles/iam.serviceAccountUser` (actAs) bound on **each** runtime SA individually — **generated from the live deploy configs**, not hand-maintained. Source of truth = every `--service-account=` in `infra/cloudbuild*.yaml` + `spikes/.../cloudbuild.yaml`: `driftscribe-agent`, `reader-agent-sa`, `docs-agent-sa`, `rollback-agent-sa`, `notifier-agent-sa`, `upgrade-reader-sa`, `upgrade-docs-sa`, `infra-reader-sa`, `tofu-apply-sa`, `payment-demo-runtime`, and (if kept) `spike-caller-sa`/`spike-callee-sa`. (Known gaps in old config: `tofu-apply-sa`'s actAs is only on the legacy `@cloudbuild` SA; `infra-reader-sa` has none.)
- Add `secretmanager.secretAccessor` **only if** a build step reads a secret while running as the build SA (audit the configs; likely none — secrets are runtime-SA-scoped).
- **Codify** in `setup_secrets.sh` (mirror the correct `get-default-service-account` pattern), with the actAs list **derived** from the deploy configs.
- **Verify:** each runtime SA's policy shows the build SA as `iam.serviceAccountUser`; AR repo shows writer+reader; project shows run.admin + logging.logWriter. No build run yet.
- **Rollback:** delete the new SA or leave it unused (fully additive).
- **Risk:** Low (additive on an unused identity). Foot-gun = forgetting an SA → caught by Phase 4.

### Phase 4 — Pin the build SA + logging into every `cloudbuild*.yaml` and prove green builds — ✅ EXECUTED 2026-05-30 (see EXECUTION LOG)
Pinned the 4 hack-2026 files to `cloudbuild-deploy-sa@` + `CLOUD_LOGGING_ONLY`; left dual-project `infra/cloudbuild.yaml` unpinned (e2e-safe) with an e2e-safe `payment-demo` SA fix; proved green across all 5 permission classes via real `tofu-apply.yaml` + `infra-reader.yaml` builds that ran AS the build SA. Original prerequisites + scope below, for reference.

**Prerequisites (from Codex Phase-3 review `019e78c2`):**
- **Submitting principal needs actAs on the build SA.** Whatever identity runs `gcloud builds submit` with the pinned `serviceAccount:` must hold `iam.serviceAccounts.actAs` (i.e. `roles/iam.serviceAccountUser`) on `cloudbuild-deploy-sa@`. Owner has it implicitly for manual Phase-4 tests; a CI/WIF submitter SA would need an explicit grant. (Same-project builds need no cross-project `serviceAccountTokenCreator`.)
- **Spike SAs are NOT yet granted.** The build SA has actAs on the 10 production runtime SAs but NOT on `spike-caller-sa@`/`spike-callee-sa@` (held pending decision #5). If Phase 4 pins `spikes/cloud_run_auth/cloudbuild.yaml`, that build will fail without those two actAs grants → either grant them first, or exclude/retire the spike config (decision #5). So "all 6 cloudbuild files" green is contingent on resolving #5.
- **`--set-secrets` needs no build-SA secret grant** (confirmed by Codex): the secret-access check is against the *runtime* identity (which already holds `secretmanager.secretAccessor`), not the deploying build SA. Only add a narrow `secretmanager.viewer` if a live smoke unexpectedly disproves this.

- Add `serviceAccount: 'projects/…/serviceAccounts/cloudbuild-deploy-sa@…'` **and** `options: { logging: CLOUD_LOGGING_ONLY }` to **all 6** files. A user-specified build SA with **no** logging config fails immediately — both keys, every file. (Per-file pin is the only supported mechanism: there is **no** project setting to set the Cloud Build default to an arbitrary custom SA; `get-default-service-account` only selects between compute-default and legacy-cloudbuild. Triggers would override a config-level SA, but this repo uses `gcloud builds submit` only — no triggers.)
- **Prove the cutover across BOTH deploy shapes** so the `run.admin`/`setIamPolicy` need is actually exercised: (a) a `--no-allow-unauthenticated` worker (`coordinator-update.yaml` — but note it only `update`s, so also test a full `deploy`), and (b) at least one `--allow-unauthenticated` public deploy. Confirm `gcloud builds describe` shows `SERVICE_ACCOUNT=…cloudbuild-deploy-sa@` and `STATUS=SUCCESS`, no image-pull 403, no actAs-denied, no logging-config error.
- **Exercise EACH permission class the build SA must cover before Phase 6 (Codex `019e78c2`)** — a green `coordinator-update.yaml` alone is necessary but insufficient: (1) AR **push** (`docker push` to the repo), (2) `run services update` (no-SA-change path), (3) full `run deploy` with explicit `--service-account=` (exercises actAs on a runtime SA), (4) deploy/update with `--set-secrets` (exercises the runtime SA's secret access *and* the build SA's ability to set it), (5) the retained `--allow-unauthenticated` path (exercises `setIamPolicy` → `run.admin`) **if** decision #1 keeps public-invoker in Cloud Build rather than refactoring it out.
- **Do NOT** use `infra/cloudbuild.yaml` as the test — it rebuilds+redeploys `payment-demo` at a fresh tag (churns prod, breaks the tofu zero-diff). Also fix its `payment-demo` deploy (`:174`, `--allow-unauthenticated`, **no** `--service-account`) to pin `payment-demo-runtime@`, or it's a latent regression that reverts payment-demo onto the compute SA.
- Reconcile `setup_secrets.sh:117` to target the build SA (drop the dead legacy-`@cloudbuild` grants). Clean the stale "Option C / default compute SA" comments (`cloudbuild.yaml:5`, `coordinator-update.yaml:8`).
- **Rollback:** `git revert` the per-file edits → builds fall back to the compute SA (which still has every role at this phase). This safe fallback is the whole reason `editor` is stripped only in Phase 6.
- **Risk:** HIGH. Failure modes: missing logging → instant fail; missing actAs on any one runtime SA → that deploy fails at admission; missing repo `artifactregistry.reader` → opaque image-pull 403; missing `run.admin` → public deploys fail `setIamPolicy`; accidentally running `infra/cloudbuild.yaml` churns payment-demo.

### Phase 5 — Drop the no-consumer narrow roles — ✅ EXECUTED 2026-05-31 (see EXECUTION LOG)
Removed `datastore.user`, `run.viewer`, `secretmanager.secretAccessor` (project-level) **+ a discovered per-secret `secretAccessor` on `gemini-api-key`** from the compute SA — all consumer-less. Note `editor` masks `datastore.user`/`run.viewer` (and `run.admin` masks `run.viewer`), so those two are cosmetic; but `editor` does **NOT** grant `secretmanager.versions.access` (only `roles/owner` does), so the secretAccessor removals were a *real* reduction, justified by zero consumer.
- **Rollback:** re-grant (backups in `docs/handoff/2026-05-31-compute-sa-*.pre-phase5.json`). **Risk:** Low (executed clean, zero impact).

### Phase 6 — Remove `editor` + the build-overlapping roles (KEYSTONE) — ✅ EXECUTED 2026-05-31 (see EXECUTION LOG)
- **GATE CHECK (expanded per Codex 2026-05-30 + 2026-05-31):** run an **exhaustive** negative reference query for the compute SA across **all** surfaces — `builds get-default-service-account` + build history, triggers (global+regional), every Cloud Run service template, Scheduler/Eventarc/Pub/Sub auth SAs, secret IAM, and the setup scripts — and confirm `cloudbuild-deploy-sa@` independently holds `artifactregistry.writer`+repo-`reader`, `run.admin`, `iam.serviceAccountUser` on all runtime SAs, `logging.logWriter`. Proceed only if every load-bearing capability is verified on the build SA and no surface still references the compute SA. **Codex Phase-5-review additions (2026-05-31):**
  - Confirm the compute SA holds **exactly** `editor`, `run.admin`, `artifactregistry.writer`, `iam.serviceAccountUser` and **no** resource-policy bindings, **no** user-managed keys (Phase-5 post-state — re-verify at Phase-6 start, it can drift).
  - Confirm **no secret IAM binding anywhere** includes the compute SA — *including empty/legacy secrets like `gemini-api-key`* (Phase 5 cleared both the project grant and the `gemini-api-key` per-secret binding; re-verify nothing re-added them).
  - Confirm the **4 pinned hack-only** Cloud Build files still carry `serviceAccount: cloudbuild-deploy-sa@` **+** `CLOUD_LOGGING_ONLY`.
  - Confirm `infra/cloudbuild.yaml` is the **only** unpinned config and is intentionally **fail-closed** post-editor-strip on hack-2026 — this is the **one residual path that still authenticates as the compute SA** (Cloud Build's project default for unpinned configs; see Codex precision in the Phase-5 log). It must fail at the first AR push, not silently re-deploy.
  - Confirm **no Cloud Build triggers** exist (global+regional); if any appear, none may use the compute SA.
  - **After** stripping, re-run **one pinned build smoke** (`tofu-apply.yaml` or `infra-reader.yaml`) to confirm the build SA still covers every class with `editor` gone.
- **Also gate on no stale pre-C5g plan approvals (Codex `019e78c2`):** before stripping `editor`, confirm there is no pending/unapplied `tofu-apply` approval in the `plan-approvals` Firestore DB that was signed *before* the C5g runtime-SA repoint. Not a live-serving risk (a stale apply should freshness-refuse), but clears the ambiguity so a delayed apply can't surprise during/after the keystone removal.
- Remove `run.admin`, then `artifactregistry.writer`, then `iam.serviceAccountUser`, then **`editor` LAST**. Full smoke after: incremental build/deploy (both auth shapes) + C4 no-op `/propose`+`/apply` + rollback traffic-describe.
- **Rollback:** re-grant any subset (additive, instant) — break-glass only.
- **Risk:** Medium (feels irreversible) but fully retired by the Phase 3–5 gates + instant re-grant.

### Phase 7 — DISABLE (not delete) + close the regression sources — ✅ EXECUTED 2026-05-31 (see EXECUTION LOG)
**Codex-recommended ordering (`019e78c2`, 2026-05-31): disable the SA LAST** — keep the fastest rollback (re-grant only, no `enable`) available while removing the reintroduction sources.
1. **Patch the code/script reintroduction sources first** so a bootstrap re-run can't resurrect the dependency: `setup_prod_project.sh:303` (`LIVE_RUNTIME_SA`) + `setup_e2e_project.sh:338-339` (`RUNTIME_SA`) default a runtime SA to `PROJECT_NUMBER-compute@`; any docs/runbooks that still default a runtime SA to the compute SA. (NB: the `setup_secrets.sh` legacy `@cloudbuild` grant blocks were already removed in Phase 4; §4c `cloudbuild-deploy-sa@` is the source of truth.)
2. **Prune the live legacy `${PROJECT_NUMBER}@cloudbuild` actAs bindings** — recon (Phase-6 gate, surface G3) found them still present on 8/10 runtime SAs (all except `infra-reader-sa`, `payment-demo-runtime`); harmless (that SA is not the build identity) but should be cleaned once confirmed unused + no triggers. **Codex caveat:** don't claim "all build-identity debt gone" until this prune is done.
3. **`gcloud iam service-accounts disable` the compute SA LAST** (reversible with `enable`; **do not delete** — a default compute SA can auto-recreate and orphaning a missed reference is unrecoverable; disabling does **not** require the Compute API enabled), then a negative-reference check + one pinned-build smoke / config validation.
- (Spike retirement is already DONE — Phase 4 deleted `spike-caller`/`spike-callee` + their SAs + AR images + `spikes/cloud_run_auth/`.)
- Update MEMORY + runbooks.
- **Risk:** Low (disable is instantly reversible; SA already role-less + consumer-free, and the build SA is proven self-sufficient post-editor-strip).

---

## Latent regression sources — ✅ ALL ADDRESSED
- `infra/cloudbuild.yaml` `payment-demo` deploy — now takes an optional `_TARGET_SERVICE_SA` pin (Phase 4) so an accidental run keeps payment-demo on `payment-demo-runtime@`; the full-stack file is also DO-NOT-RUN-guarded on hack-2026 (`_ALLOW_HACK2026`). ✅
- `infra/cloudbuild.yaml` + `infra/cloudbuild.coordinator-update.yaml` — stale "Option C / default compute SA" comments rewritten (Phase 4). ✅
- `setup_secrets.sh` — legacy `@cloudbuild` §3/§4 grant blocks removed + `cloudbuild-deploy-sa@` provisioned in §4c (Phase 4); the live `@cloudbuild` dead project grants pruned (Phase 7). ✅
- `setup_prod_project.sh:303` (→ `payment-demo-runtime@`) + `setup_e2e_project.sh` + `docs/runbooks/e2e-{environment,ci}.md` (→ live-resolve with e2e-compute fallback) patched (Phase 7, commit `2ed8478`). ✅

## Do-NOT list
- **DO NOT** remove `editor`/`run.admin`/`artifactregistry.writer`/`iam.serviceAccountUser` from the compute SA before the build SA is created, granted, pinned into all 6 cloudbuild files, and a **green build across both auth shapes** is verified. `editor` is the LAST role stripped.
- **DO NOT** delete the compute SA — disable it.
- **DO NOT** pin a custom `serviceAccount:` into any cloudbuild file without ALSO adding `options.logging` in the same file.
- **DO NOT** route any SA/IAM change through the gated C2→C4 tofu pipeline (denylist hard-denies it by design).
- **DO NOT** grant the C2 plan-builder SA state-write/KMS/apply authority.
- **DO NOT** delete/deprivilege `eventarc-trigger-sa@` (active drift-trigger + Pub/Sub identity).
- **DO NOT** create any new runtime SA / repoint the deploy identity without `artifactregistry.reader` on the AR repo for BOTH the new SA and the deployer (C5g lesson).
- **DO NOT** run `infra/cloudbuild.yaml` to test the migration (churns payment-demo).
- **DO NOT** grant the build SA only `run.developer` while `--allow-unauthenticated` deploys exist — they need `run.admin`'s `setIamPolicy`.

## Open decisions (for the user)
1. **Build-SA auth privilege:** grant `run.admin` (keeps all 6 configs runnable as-is) **vs** refactor public-invoker out-of-band (operator grants `run.invoker` to `allUsers` once) + drop `--allow-unauthenticated` from the build configs → then `run.developer` suffices (tighter, more work). *Recommend: `run.admin` now, refactor later if desired.*
2. **End state:** disable-but-present (recommended, reversible) **vs** delete (30-day undelete-only, orphan risk).
3. **Build identity:** new `cloudbuild-deploy-sa@` (recommended, explicit/least-priv) vs reuse legacy `@cloudbuild` SA (already half-wired but dead/less clean).
4. **AR scope:** repo-scoped (recommended) vs project-level.
5. **Spikes:** also retire `spike-caller`/`spike-callee` + SAs + their cloudbuild file? — ✅ RESOLVED 2026-05-30 = **retire** (services + SAs + AR images deleted; `spikes/cloud_run_auth/` removed; doc links updated).
6. **Future gated IAM path:** keep all project/SA-IAM operator-only (current, fail-closed) vs build a new reviewable pipeline for IAM changes (larger initiative, out of scope).
7. **Pre-flight:** confirm org policy `constraints/iam.automaticIamGrantsForDefaultServiceAccounts` / `iam.managed.preventPrivilegedBasicRolesForDefaultServiceAccounts` state and whether a GCS `logsBucket` is wanted vs `CLOUD_LOGGING_ONLY`, before Phase 4. (Org policy will **not** re-add `editor` to an existing disabled SA — the auto-grant only fires at default-SA creation.)

## Rollback summary
Every phase has an additive inverse; the only irreversible-feeling step (`editor` removal) is fully gated behind verified independent grants on the build SA + a proven green build. At no phase boundary does a live workload lose an identity it still needs — runtime blast radius is already zero, and the build identity is repointed only after its replacement is proven green (with `git revert` as an instant fallback while the compute SA still holds every role).
