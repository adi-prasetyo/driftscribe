# tofu-apply-sa Pub/Sub least-privilege cutover (+ eventarc orphan check)

**Date:** 2026-06-08
**Scope:** the two parked Phase-3 follow-ups —
- **(a)** replace the temporary `roles/pubsub.editor` on `tofu-apply-sa` (the sole mutator)
  with a custom, least-privilege project role `driftscribeTofuApplyPubsub`.
- **(e)** verify the eventarc trigger set has no orphan (no code change expected).

This is the planned cutover the Phase-3 execution plan flagged:
`docs/plans/2026-06-04-phase3-checkout-buildout-execution-plan.md:222` —
`roles/pubsub.editor … # temporary for Phase 3; custom create/update role is the tighter future option`.

> **STATUS: IMPLEMENTED + LIVE 2026-06-08.** §0–§3 below describe the pre-cutover
> ground truth and the design; the post-cutover live outcome is recorded in §6.
> Sections 0–3 are kept as the as-planned record — do NOT read them as current state.

---

## 0. Ground truth (verified live 2026-06-08)

- `tofu-apply-sa@driftscribe-hack-2026` project roles: `driftscribeTofuApplyStorage`
  (custom, no-IAM), `roles/datastore.user` (conditioned to `plan-approvals` DB),
  **`roles/pubsub.editor`** (target), `roles/run.developer`.
- `iac/` Pub/Sub resources: `google_pubsub_topic.order_events` (`order-events`) +
  `google_pubsub_subscription.orders_sub` (`orders-sub`) in `iac/checkout_events.tf`.
  **Zero `google_pubsub_*_iam_*` resources** → `tofu apply` never calls
  `setIamPolicy`/`getIamPolicy` on Pub/Sub.
- `roles/pubsub.editor` permission dump: it **does NOT** include `*.getIamPolicy` /
  `*.setIamPolicy` (those are in `roles/pubsub.admin`). It DOES include the data-plane
  `pubsub.topics.publish` + `pubsub.subscriptions.consume`, all `pubsub.schemas.*`,
  all `pubsub.snapshots.*`, `cloudkms.keyHandles.*` (autokey), and a `serviceusage.*`
  batch — all unused by the apply SA.
- The `pubsub.editor` grant is **codified nowhere** in `infra/scripts/*` — it was
  hand-added in Phase-3 Track B-0. (`driftscribeTofuApplyStorage` is likewise hand-made.)

### Corrected rationale
The memory's "no-IAM role" framing was imprecise: `pubsub.editor` is *already* no-IAM.
The genuine wins of the cutover are (1) **drop data-plane** `publish`/`consume` — the
sole mutator should manage Pub/Sub resources, not inject/drain order events; (2) drop
unused `schemas`/`snapshots`/autokey/serviceusage breadth; (3) give the by-hand grant a
**reproducible, auditable home** in `setup_iac_backend.sh`, exactly as the script's own
comment prescribes ("Any NEW iac/ resource type gets its own developer-style role here").

---

## 1. The custom role `driftscribeTofuApplyPubsub`

Project custom role, GA. **Permissions (minimal CRU + attach):**

```
pubsub.topics.create
pubsub.topics.get
pubsub.topics.list
pubsub.topics.update
pubsub.topics.attachSubscription          # REQUIRED: bind a subscription to its topic at create
pubsub.subscriptions.create
pubsub.subscriptions.get
pubsub.subscriptions.list
pubsub.subscriptions.update
```

**Deliberately excluded** (vs `pubsub.editor`):
- `*.delete` — mirrors `driftscribeTofuApplyStorage`'s no-delete posture; the plan
  denylist hard-denies `delete`/`replace`/`forget` at the plan gate (defense in depth).
- `pubsub.topics.publish`, `pubsub.subscriptions.consume` — data-plane.
- `pubsub.schemas.*`, `pubsub.snapshots.*`, `pubsub.topics.detachSubscription`,
  `cloudkms.keyHandles.*`, `serviceusage.*` — unused.
- `*.getIamPolicy`/`*.setIamPolicy` — never needed (no IaC-managed Pub/Sub IAM); and
  `pubsub.editor` never had them, so removing them is not even a regression to verify.

**Why no `getIamPolicy`:** the apply SA works today under `pubsub.editor` which has no
`getIamPolicy` — empirical proof tofu's topic/subscription refresh path doesn't read IAM.

---

## 2. Code changes (TDD)

### 2a. `infra/scripts/_setup_lib.sh` — new idempotent helper
```bash
# create_or_update_custom_role_idempotent PROJECT ROLE_ID TITLE PERMISSIONS_CSV
create_or_update_custom_role_idempotent() {
  local project="${1:?...}" role_id="${2:?...}" title="${3:?...}" perms="${4:?...}"
  if gcloud iam roles describe "$role_id" --project="$project" >/dev/null 2>&1; then
    gcloud iam roles update "$role_id" --project="$project" \
      --title="$title" --stage=GA --permissions="$perms" --quiet >/dev/null
  else
    gcloud iam roles create "$role_id" --project="$project" \
      --title="$title" --stage=GA --permissions="$perms" --quiet >/dev/null
  fi
}
```
Re-runs converge the live role's permission set to the declared CSV.

### 2b. `infra/scripts/setup_iac_backend.sh` §6.5 — new sub-block (hardened branch)
After the `run.developer` grant (editor mode's `roles/editor` already covers Pub/Sub):
create + grant `driftscribeTofuApplyPubsub`, then a **gated** removal of the temporary
`roles/pubsub.editor` (mirrors the c5f `SETUP_PLAN_APPROVALS_DB` cutover convention —
default re-run only ADDS the tight role; broad-role removal is the deliberate cutover,
behind `SETUP_TOFU_APPLY_PUBSUB_CUSTOM=1`). Bind-before-remove ordering.

### 2c. Tests — `tests/unit/test_setup_iac_backend_pubsub.py` (mirror `test_setup_c5f_hardening_script.py`)
- `_setup_lib.sh` defines `create_or_update_custom_role_idempotent` (describe→update/create).
- `setup_iac_backend.sh` creates `driftscribeTofuApplyPubsub` with the EXACT 9-perm set
  and grants it to the apply SA.
- the perm CSV has NO `setIamPolicy`/`getIamPolicy`, NO `topics.publish`, NO
  `subscriptions.consume`, NO `schemas`/`snapshots`, NO `.delete`.
- the perm CSV DOES include `attachSubscription` (under-scoping regression guard).
- the `roles/pubsub.editor` removal is gated (default off) — `SETUP_TOFU_APPLY_PUBSUB_CUSTOM`.
- `setup_iac_backend.sh` never `add`s `roles/pubsub.editor` (only removes it).

### 2d. Docs
- `docs/architecture/iam-matrix.md` `tofu-apply-sa` row: add the custom Pub/Sub role to
  positive grants + add `NOT roles/pubsub.editor/admin (no data-plane, no schemas, no
  Pub/Sub IAM)` to negative space.
- `docs/runbooks/tofu-apply.md`: list the new grant + the `SETUP_TOFU_APPLY_PUBSUB_CUSTOM`
  cutover flag.

---

## 3. Live execution (bind-before-remove, operator ADC)

1. Create role `driftscribeTofuApplyPubsub` (9 perms above).
2. `gcloud iam roles describe` → confirm the exact perm set.
3. Grant `projects/<P>/roles/driftscribeTofuApplyPubsub` to `tofu-apply-sa` (`--condition=None`).
4. Confirm binding present (alongside `pubsub.editor`).
5. Remove `roles/pubsub.editor` from `tofu-apply-sa`.
6. Confirm final project role set = `{driftscribeTofuApplyStorage,
   driftscribeTofuApplyPubsub, datastore.user, run.developer}`; `pubsub.editor` gone.

**Verification of sufficiency.** Static proof = role contains the documented-minimal
topic+subscription CRUD + `attachSubscription` set, and the binding is live. The
create/update paths cannot be proven without mutating real infra; the definitive proof is
the next real agent-authored Pub/Sub `tofu apply` (operator CF-Access approve, out of band).
**OPEN QUESTION for review:** is the static proof sufficient for the sole mutator, or is an
empirical impersonation read-test (`gcloud pubsub topics describe order-events` as the apply
SA, requiring a temp `serviceAccountTokenCreator` self-grant + revoke) warranted despite the
extra IAM churn? (It would prove `get` resolves under the new role but NOT create/update.)

**Reversibility.** Re-grant `roles/pubsub.editor` if the next apply 403s on a Pub/Sub op.

---

## 4. (e) eventarc orphan check — VERIFIED CLEAN (no action)

`gcloud eventarc triggers list --location=-` → exactly the expected pair:
- `driftscribe-cloudrun-changes` → `driftscribe-agent` `/eventarc`, SA `eventarc-trigger-sa@`
- `driftscribe-cloudrun-changes-v2-update` → same destination + SA

Both bound to `eventarc-trigger-sa@` (the SA is no longer unbound — the historical
`setup_secrets.sh §10` concern), both filter on `payment-demo` `ReplaceService`. No orphan
trigger; the once-absent `driftscribe-cloudrun-changes` is now present. **Observation (not
acted on):** both triggers have identical filters → the coordinator's `/eventarc` receives
each payment-demo deploy twice (handler is idempotent). Collapsing to one trigger is an
optional future tidy, out of scope for an "orphan check."

---

## 5. Out of scope (noted, not done)
- Codifying `driftscribeTofuApplyStorage` into the script (same hand-add gap; separate change).
- Collapsing the duplicate eventarc triggers.

---

## 6. Implemented — live outcome (2026-06-08)

**Code (TDD):** `create_or_update_custom_role_idempotent` added to `_setup_lib.sh`;
§6.5e + `pubsub.googleapis.com` API-enable added to `setup_iac_backend.sh` (the
`pubsub.editor` removal is presence-checked, not `|| true`-swallowed); 9 structural
tests in `tests/unit/test_setup_iac_backend_pubsub.py`; iam-matrix + runbook updated.
`ruff` clean, `bash -n` clean, 35 setup-script tests green.

**Live (operator ADC, bind-before-remove):**
1. Created `projects/driftscribe-hack-2026/roles/driftscribeTofuApplyPubsub` (GA) —
   `describe` confirms EXACTLY the 9 perms in §1.
2. Granted it to `tofu-apply-sa`; verified present alongside `pubsub.editor`.
3. Removed `roles/pubsub.editor`.
4. **Final `tofu-apply-sa` project roles** (verified via `get-iam-policy`):
   `driftscribeTofuApplyPubsub`, `driftscribeTofuApplyStorage`, `roles/datastore.user`,
   `roles/run.developer`. `roles/pubsub.editor` is **gone**.

The script's codified 9-perm CSV matches the live role exactly, so a future
`setup_iac_backend.sh` re-run (update path) converges, not drifts. Per Codex review,
the empirical impersonation read-test was skipped (static verification only); the
definitive create/update proof is the next real agent-authored Pub/Sub `tofu apply`.
Reviewed by Codex (thread `019ea2d1…`): plan GO + completed-work "no blockers".

**(e)** eventarc orphan check: VERIFIED CLEAN (§4), no action.
