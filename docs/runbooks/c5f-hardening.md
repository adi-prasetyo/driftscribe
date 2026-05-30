# Runbook — Phase C5f live cutover (IAM / secret hardening)

Phase C5f tightens three over-broad grants left by earlier phases, now that the
`driftscribe-tofu-apply` worker is network-reachable (C5c done — the coordinator
reaches it over the VPC under `--ingress=internal`). Nothing here changes worker
behaviour; it is a pure least-privilege cutover against the live
`driftscribe-hack-2026` project. Run every step from a shell holding
`roles/owner` + ADC on the project.

What C5f hardens:

1. **Dedicated payment-demo runtime SA.** `payment-demo`
   (`google_cloud_run_v2_service`) currently runs as the **default compute SA**.
   C5f introduces `payment-demo-runtime@driftscribe-hack-2026.iam.gserviceaccount.com`
   with **ZERO project roles** (a mock HTTP demo makes no GCP calls). The
   service-side repoint (`template.service_account` in `iac/cloudrun.tf`) is
   applied **through the gated tofu-apply pipeline** as an in-place UPDATE — NOT
   out-of-band — and is the **C5g positive test**. This runbook only provisions
   the SA and the `actAs` grants every mutator of `payment-demo` needs on it.
2. **`plan_approvals` named-DB isolation.** `plan_approvals` moves into a
   **separate named Firestore database `plan-approvals`** (same region
   `asia-northeast1` as `(default)`). Firestore IAM is database-level, so a
   project-wide `roles/datastore.user` reaches ALL databases. Isolation =
   **CONDITIONED `roles/datastore.user`**: `tofu-apply-sa` conditioned to the
   `plan-approvals` DB; the coordinator (`driftscribe-agent`) + `rollback-agent-sa`
   conditioned to `(default)` (the condition is
   `resource.name == "projects/driftscribe-hack-2026/databases/<DB>"`).
   REST/client libraries ENFORCE the condition — **the Cloud Console does NOT**.
   The worker env `PLAN_APPROVALS_DB=plan-approvals` selects it; empty ⇒
   `(default)` for back-compat.
3. **`github-pat` rotation.** The live `github-pat` is an over-scoped **classic**
   PAT (repo/workflow/admin/push on `adi-prasetyo/driftscribe` AND
   `driftscribe-e2e-target`). C5f rotates it to a **fine-grained** PAT scoped to
   `adi-prasetyo/driftscribe` ONLY.

> **The coordinator now MERGES approved IaC PRs (C5e).** Any doc that still
> describes the coordinator's `github-pat` as "read-only / Pull requests: Read
> only / never writes PRs" is now WRONG — the merge endpoint needs
> `Contents: write`. See step 6.

Cross-references:

- IAM backend script: `infra/scripts/setup_iac_backend.sh` (§6.5d, §6.5)
- Secret / SA / actAs script: `infra/scripts/setup_secrets.sh` (§5, §7b, §8)
- Shared helpers: `infra/scripts/_setup_lib.sh`
  (`grant_datastore_user_for_db`, `remove_unconditioned_datastore_user`,
  `create_named_firestore_db_idempotent`, `create_service_account_idempotent`)
- The sole-mutator deploy: `infra/cloudbuild.tofu-apply.yaml`
- The apply worker itself: `docs/runbooks/tofu-apply.md`

---

## 0. Prerequisites

- `gcloud` authenticated as an **owner** of `driftscribe-hack-2026`, with ADC.
- On ANY re-run of `setup_secrets.sh`, pass **`SETUP_EVENTARC=0`** — C5f does not
  touch drift triggers, and a default run would rewire them (§10 of the script).
- Pass the **(new) fine-grained** `GITHUB_TOKEN` as `$2` to `setup_secrets.sh`.
  Until you mint it (step 6) the live classic PAT is still valid; you may pass
  the existing value for steps 2 and 5 and rotate the secret last.

> **FREEZE IaC applies during the cutover.** The live worker still writes
> `plan_approvals` to `(default)` until it is redeployed with
> `PLAN_APPROVALS_DB=plan-approvals` in **step 4**. An apply that lands between
> steps 3 and 4 would write its approval doc to the wrong database. Do not run
> `/propose` + `/apply` (and do not let the coordinator merge an IaC PR that
> would trigger one) until step 4 completes. The one intentional apply — the C5g
> `service_account` repoint — runs only after step 4.

---

## 1. Coordinator artifact-read grant (C5e)

The GET `/iac-approvals/{pr_number}` page (C5e) fetches the C2 plan artifacts
from the artifact bucket by pinned generation (`agent/iac_artifacts.py`
`fetch_gcs_object`). Grant the coordinator SA read on that bucket:

```bash
PROJECT=driftscribe-hack-2026
COORD=driftscribe-agent@${PROJECT}.iam.gserviceaccount.com
gcloud storage buckets add-iam-policy-binding "gs://${PROJECT}-tofu-artifacts" \
  --project="$PROJECT" \
  --member="serviceAccount:${COORD}" \
  --role="roles/storage.objectViewer"
```

**Verify:** open `GET /iac-approvals/<pr>` for a PR with a published C2 plan and
confirm the plan renders (no 403 / "could not fetch artifact").

**Rollback:** `gcloud storage buckets remove-iam-policy-binding … --role=roles/storage.objectViewer`.

---

## 2. Dedicated payment-demo runtime SA + actAs grants

Create `payment-demo-runtime` and grant **both** mutators of `payment-demo`
`roles/iam.serviceAccountUser` (actAs) on it. Cloud Run requires the caller to
actAs the service's runtime SA for **ANY** update (including a traffic-only
update), so this is needed by:

- `tofu-apply-sa` — `tofu apply` updates the service (sets the new runtime SA);
- `rollback-agent-sa` — `/execute` traffic-shift `update_service` (the C4 plan
  named only `tofu-apply-sa`; rollback was a gap, now fixed in §7b).

`setup_secrets.sh §7b` does exactly this (idempotent, gated on the mutator SAs
existing), so run it with `SETUP_EVENTARC=0`:

```bash
SETUP_EVENTARC=0 infra/scripts/setup_secrets.sh "$PROJECT" "$GITHUB_TOKEN"
```

Expect lines:
`payment-demo-runtime` created (or "already exists"), and
`tofu-apply-sa: actAs on … payment-demo-runtime@…` +
`rollback-agent-sa: actAs on … payment-demo-runtime@…`.

> The pre-existing actAs on the **default compute SA** is left live for the
> transition window (the service still runs as it until the C5g repoint applies),
> and is removed as a documented post-cutover cleanup.

**Verify:**
```bash
gcloud iam service-accounts get-iam-policy \
  payment-demo-runtime@${PROJECT}.iam.gserviceaccount.com --project="$PROJECT" \
  --flatten=bindings --filter="bindings.role:iam.serviceAccountUser" \
  --format='value(bindings.members)'
# expect: tofu-apply-sa@… AND rollback-agent-sa@…
```

**Rollback:** delete the SA + its bindings —
`gcloud iam service-accounts delete payment-demo-runtime@${PROJECT}.iam.gserviceaccount.com`.
Harmless to leave: a zero-role SA that nothing yet runs as.

---

## 3. Create the `plan-approvals` named Firestore DB + conditioned apply grant

`setup_iac_backend.sh §6.5d` creates the named database and grants
`tofu-apply-sa` `roles/datastore.user` **conditioned to it**. The per-database
condition is enforced only at the data-plane (client libraries), NOT in the
Console, so a real impersonated read/write is the only trustworthy proof. **Order
matters: create the DB FIRST, then run the proof against it** — a denied access
to a *nonexistent* `plan-approvals` DB would surface as `NotFound`, not an IAM
denial, and would prove nothing.

### 3a. Create the DB + conditioned apply grant (do this FIRST)

```bash
infra/scripts/setup_iac_backend.sh
```

This is idempotent. §6.5d calls `create_named_firestore_db_idempotent` (named DB
`plan-approvals`, region `asia-northeast1`, native mode) then
`grant_datastore_user_for_db tofu-apply-sa plan-approvals`. A **default** run
only ADDS the conditioned grants (it does NOT remove the un-conditioned ones —
that removal is gated behind `SETUP_PLAN_APPROVALS_DB=1`, deferred to step 5).

```bash
gcloud firestore databases describe --database=plan-approvals --project="$PROJECT" \
  --format='value(name,locationId)'   # expect …/databases/plan-approvals, asia-northeast1
```

### 3b. Empirical CEL proof (against the now-existing DB)

Create a throwaway probe SA with ONLY a `(default)`-conditioned
`roles/datastore.user`, SDK-impersonate it, and assert: a read/write against
`(default)` **succeeds** and the SAME op against `plan-approvals` is **DENIED**.
Then delete the probe SA.

```bash
PROBE=cel-probe@${PROJECT}.iam.gserviceaccount.com
gcloud iam service-accounts create cel-probe --project="$PROJECT" \
  --display-name="C5f CEL proof (throwaway)"
# (default)-conditioned datastore.user ONLY — no other grant.
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${PROBE}" --role="roles/datastore.user" \
  --condition='expression=resource.name == "projects/'"$PROJECT"'/databases/(default)",title=cel-probe-default' \
  --quiet
# Let you impersonate it (then revoke after).
ME=$(gcloud config get-value account)
gcloud iam service-accounts add-iam-policy-binding "$PROBE" --project="$PROJECT" \
  --member="user:${ME}" --role="roles/iam.serviceAccountTokenCreator"
```

Then, impersonating the probe via the client library, assert
`(default)` read/write SUCCEEDS and the identical op against the **existing**
`plan-approvals` database raises `PermissionDenied` (NOT `NotFound` — the DB was
created in 3a). (Use a short Python snippet with
`google.cloud.firestore.Client(project=…, database=…)` under
`--impersonate-service-account=$PROBE` ADC, or `GOOGLE_CLOUD_*` impersonation.)
The `plan-approvals` op MUST be `PermissionDenied` — that is the isolation guarantee.

Tear down the probe:
```bash
gcloud iam service-accounts delete "$PROBE" --project="$PROJECT" --quiet
```

> **Console will mislead you here.** The Cloud Console does NOT evaluate the
> `resource.name` condition for Firestore, so a Console read against
> `plan-approvals` may appear to succeed. Only the client-library / REST proof
> above is authoritative.

**Verify:**
```bash
gcloud firestore databases describe --database=plan-approvals --project="$PROJECT" \
  --format='value(name,locationId)'   # expect …/databases/plan-approvals, asia-northeast1
```

**Rollback:** the conditioned grant is additive and harmless; if you must back
out, `remove-iam-policy-binding … --role=roles/datastore.user` with the matching
`--condition`. The named DB can be left (empty/unused).

---

## 4. Redeploy the tofu-apply worker with `PLAN_APPROVALS_DB`

Deploy from `main` so the worker writes `plan_approvals` to the named DB. The
canonical config already pins `_PLAN_APPROVALS_DB=plan-approvals`,
`--ingress=internal`, and `--memory=2Gi --cpu=2`:

```bash
gcloud builds submit \
  --config=infra/cloudbuild.tofu-apply.yaml \
  --substitutions=_TAG=$(git rev-parse --short HEAD) \
  --project="$PROJECT"
```

This is the step that ENDS the freeze: from here the worker writes
`plan_approvals` to `plan-approvals`, which `tofu-apply-sa` is conditioned to
(step 3).

**Verify (reachability) — from inside the VPC, NOT an operator laptop.** Under
`--ingress=internal` the worker is unreachable from the public internet, so probe
it via the **coordinator's** `GET /iac-apply/reachability` (the C5c probe runs
from inside the VPC and GETs each worker's canonical POST path; `405` = reached
past network→ingress→IAM→app). Expect `tofu_apply` `app_reached:true` (`405`):
```bash
COORD=$(gcloud run services describe driftscribe-agent --region=asia-northeast1 \
  --project="$PROJECT" --format='value(status.url)')
curl -s -H "X-DriftScribe-Token: $DRIFTSCRIBE_TOKEN" "$COORD/iac-apply/reachability" | jq .
# expect: go:true (or at least results[].worker=="tofu_apply" with status_code 405, app_reached true)
```
Confirm the new revision carries `PLAN_APPROVALS_DB=plan-approvals`:
```bash
gcloud run services describe driftscribe-tofu-apply --region=asia-northeast1 \
  --project="$PROJECT" \
  --format='value(spec.template.spec.containers[0].env)' | tr ',' '\n' | grep PLAN_APPROVALS_DB
```

**Rollback:** redeploy the prior image tag (or set `_PLAN_APPROVALS_DB=''` to
fall back to `(default)`); the worker code treats empty as `(default)`.

---

## 5. Re-condition coordinator + rollback to `(default)` — **highest-risk step, do LAST**

This removes the pre-isolation **un-conditioned** project-wide
`roles/datastore.user` from the coordinator and rollback, leaving only their
`(default)`-conditioned grants. `setup_secrets.sh §5` asserts the conditioned
grant **first, every run** (bind-before-remove), then — gated on
`SETUP_PLAN_APPROVALS_DB=1` — removes the un-conditioned binding.

> **This is the highest-risk step.** Removing the un-conditioned grant could
> break the coordinator's `(default)` writes (events / decisions / sessions) if
> the conditioned grant did not take. Do it LAST, and verify chat IMMEDIATELY.

Run BOTH scripts with the cutover flag — `setup_secrets.sh` strips the
coordinator + rollback un-conditioned grants, and `setup_iac_backend.sh` strips
**`tofu-apply-sa`'s** un-conditioned grant (safe now: from step 4 the worker
writes `plan_approvals` to the named DB via its conditioned grant, so it no
longer needs `(default)`):

```bash
SETUP_EVENTARC=0 SETUP_PLAN_APPROVALS_DB=1 \
  infra/scripts/setup_secrets.sh "$PROJECT" "$GITHUB_TOKEN"
SETUP_PLAN_APPROVALS_DB=1 \
  infra/scripts/setup_iac_backend.sh
```

Expect:
`C5f: removed UN-conditioned datastore.user from coordinator + rollback (isolation ACTIVE)`
and `tofu-apply-sa@…: removed UN-conditioned datastore.user (isolation ACTIVE)`.

**Verify isolation is complete** — none of the three SAs has an un-conditioned
project-wide `datastore.user` left:
```bash
for SA in driftscribe-agent rollback-agent-sa tofu-apply-sa; do
  echo "== $SA =="; gcloud projects get-iam-policy "$PROJECT" --format=json \
    | jq --arg m "serviceAccount:${SA}@${PROJECT}.iam.gserviceaccount.com" \
      '.bindings[] | select(.role=="roles/datastore.user") | select(.members[]==$m)
       | {condition: (.condition.expression // "UNCONDITIONED — FIX")}'
done
```

**Verify (do this immediately):**
- `/chat` returns a `pong`, and
- a drift recheck writes `events` + `decisions` to `(default)`
  (e.g. mutate `payment-demo` and confirm the recheck persists, or drive a
  recheck and confirm the timeline updates).

**Rollback (if chat / writes break):** re-add the un-conditioned binding —
```bash
for SA in driftscribe-agent rollback-agent-sa tofu-apply-sa; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${SA}@${PROJECT}.iam.gserviceaccount.com" \
    --role="roles/datastore.user"
done
```
This restores the pre-C5f all-databases access while you investigate.

---

## 6. Rotate `github-pat` to a fine-grained PAT

**OPERATOR mints the PAT by hand** (a PAT cannot be scripted safely). Create a
**fine-grained** PAT at
`https://github.com/settings/personal-access-tokens/new`:

- **Repository access:** select ONE repo — `adi-prasetyo/driftscribe` (NOT the
  2nd repo `driftscribe-e2e-target`).
- **Permissions:**
  - **Contents: Read and write** — PR merge (the merge endpoint needs
    `Contents: write`, verified against GitHub docs).
  - **Pull requests: Read** — get PR / head SHA / state.
  - **Checks: Read** — the readiness gate reads **check-runs** only
    (`get_check_runs`); required commit-status checks are enforced by branch
    protection at merge, so no `Commit statuses` scope is needed.
- **Nothing else** — no Issues, no Actions/workflow, no admin, no 2nd repo.

> The post-merge audit comment (`pr.create_issue_comment`) is **best-effort** and
> may **403** under this minimal scope — it does NOT lose the merge. Add
> **Pull requests: Write** ONLY if you want that comment to land.

Push the new value and disable the old classic version:

```bash
printf '%s' "$NEW_FINEGRAINED_PAT" | \
  gcloud secrets versions add github-pat --project="$PROJECT" --data-file=-
# disable the previous (classic) version — replace N with its number
gcloud secrets versions disable N --secret=github-pat --project="$PROJECT"
```

The next coordinator revision picks up `:latest`. (Re-running
`setup_secrets.sh` with the new value as `$2` also adds the version; the manual
`versions add` above is the minimal path.)

**Verify:** roll a coordinator revision (or wait for the next deploy) and confirm
it can still get a PR / read checks / merge an approved IaC PR; confirm the audit
comment either lands (if you added PR:write) or 403s harmlessly without losing
the merge.

**Rollback:** re-enable the classic version —
`gcloud secrets versions enable N --secret=github-pat --project="$PROJECT"` — the
old over-scoped token resumes serving.

---

## Verification checklist

- [ ] **Step 1:** `GET /iac-approvals/<pr>` renders the C2 plan (no 403).
- [ ] **Step 2:** `payment-demo-runtime` exists; BOTH `tofu-apply-sa` and
      `rollback-agent-sa` hold `iam.serviceAccountUser` on it.
- [ ] **Step 3:** CEL proof passed — `(default)` read/write succeeded AND the
      same op on `plan-approvals` was DENIED (probe SA deleted after).
- [ ] **Step 3:** database `plan-approvals` exists in `asia-northeast1`;
      `tofu-apply-sa` `datastore.user` conditioned to it.
- [ ] **Step 4:** new `driftscribe-tofu-apply` revision serves (405 probe),
      `PLAN_APPROVALS_DB=plan-approvals`, `--ingress=internal`, 2Gi/2cpu.
- [ ] **Freeze lifted:** no `/apply` ran between steps 3 and 4.
- [ ] **Step 5:** un-conditioned `datastore.user` removed from ALL THREE
      (coordinator + rollback + `tofu-apply-sa`) — the verify-loop shows a
      `condition` for each, none `UNCONDITIONED`; `/chat` → `pong` and a recheck
      writes `events`/`decisions` to `(default)`.
- [ ] **Step 6:** `github-pat` rotated to the fine-grained single-repo PAT;
      old classic version disabled.
- [ ] **Step 6:** `github-pat` rotated to the fine-grained single-repo token;
      classic version disabled; coordinator can still read checks + merge.
- [ ] **Post-cutover cleanup (deferred):** remove the residual actAs on the
      **default compute SA** once the C5g repoint has applied and `payment-demo`
      runs as `payment-demo-runtime`.
