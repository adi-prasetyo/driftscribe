# Runbook — IaC apply-failure recovery

What to do after an `/iac-approvals` **Approve** that did **not** cleanly apply +
merge. The gated `tofu-apply` worker is fail-closed: every non-success outcome is
a *deliberate refusal or a recorded failure*, never a silent partial. This
runbook maps each terminal outcome to its recovery, with special care for the one
that needs a **state reconcile** (`failed_state_suspect`).

Companion: `docs/runbooks/tofu-apply.md` (deploy + model). Worker phases are the
single source of truth in `driftscribe_lib/approvals.py` (`APPLY_AUDIT_PHASES`).

---

## 0. First — read the `apply_audit`

The authoritative record of what happened lives on the approval doc: Firestore
**named** database `plan-approvals`, collection `plan_approvals`, document id =
the `approval_id`. Read it (operator, owner + ADC):

```bash
/home/adi/driftscribe/.venv/bin/python - <<'PY'
from google.cloud import firestore
db = firestore.Client(project="driftscribe-hack-2026", database="plan-approvals")
for d in db.collection("plan_approvals").stream():
    a = d.to_dict().get("apply_audit") or {}
    print(d.id, "| pr", d.to_dict().get("pr_number"),
          "| status", d.to_dict().get("status"),
          "| phase", a.get("phase"), "| serial", a.get("state_serial"),
          "| freshness_exit", a.get("freshness_exit_code"))
PY
```

`status=used` means the single-use approval was **burned** (claim-first): any
retry needs a **fresh** approval (re-Approve), never the same token.

---

## 1. Decide by `apply_audit.phase`

| phase | meaning | recovery |
|---|---|---|
| `applied` | `tofu apply` succeeded. If the chat said "merge pending" the apply is done and only the merge is outstanding | §6 (merge) |
| `claimed` | **outcome UNKNOWN** — the approval was burned but the worker crashed (OOM/restart) before writing a terminal phase. The apply may have NOT run, partly run, or fully run | **treat as potentially-applied + state-suspect: §2**, but FIRST check the worker logs + live resource to see if a revision was created. Never retry blindly |
| `failed` | a tofu step failed and the worker **PROVED state stayed clean** (serial readable + unchanged + refresh-only exit 0) | no reconcile — fix the root cause (see `stderr_tail`), re-Approve |
| `failed_state_suspect` | `tofu apply` failed and the worker could **NOT** prove state clean (serial bumped / refresh-only drift / serial unreadable) — the failed apply may have **persisted partial state** | **§2 — state reconcile required before any retry** |
| `drift_refused` | the semantic freshness gate found **material** desired-state drift (an out-of-band change to a managed resource) | §3 |
| `lock_refused` | a tofu step could not acquire the GCS state lock (held or orphaned) | §4 |
| `integrity_refused` / `fidelity_refused` / `verify_refused` | the C2 artifact failed re-verification (hash / version-lockfile / resource-set / payload) | §5 — rebuild C2 |

A **permanent merge block** (apply succeeded, but branch protection blocks the
merge) is **not** an `apply_audit` failure — see §6.

---

## 2. State reconcile (`failed_state_suspect` / partial apply)

**The hazard.** A `tofu apply plan.tfplan` can write the planned attribute into
the backend state **even when the live update is rejected**. This bit production
during C5g: a `service_account` repoint that Cloud Run rejected at admission (403)
still persisted `service_account=…` into state and bumped the serial, so the next
run's refresh-only gate 409'd on a *phantom* drift (`state` said `runtime@`, live
said the old SA). The worker now flags exactly this case `failed_state_suspect`
and attaches a read-only diagnostic to the audit (`serial_before` / `serial_after`
/ `serial_bumped` / `refresh_drift` / `post_failure_refresh_tail`). It does **not**
auto-reconcile — reconciling state is an operator decision.

> **Trust boundary:** the reconcile is run by the **operator** directly
> (owner + ADC), NEVER by the coordinator, the C2 plan-builder, or the apply
> worker. C2/C4 are deliberately denied state-write + KMS-encrypt; widening that
> would break the sole-mutator invariant.

**Procedure** (deliberate — step 7 is the only step that writes the canonical
Terraform state object; steps 2-3 write the bucket's versioning config + a backup):

```bash
set -euo pipefail
PROJECT=driftscribe-hack-2026
REGION=asia-northeast1
export TF_VAR_tofu_state_kms_key="projects/$PROJECT/locations/$REGION/keyRings/driftscribe-tofu/cryptoKeys/tofu-state"

# 0. Reconcile from the SAME iac/ the worker baked — its deployed image tag is the
#    git short SHA (the deploy uses _TAG=$(git rev-parse --short HEAD)). Check out
#    THAT commit, NOT bare main (which may have advanced). If the tag is not a
#    commit-ish (e.g. a 'manual' deploy), STOP and identify the baked commit by hand.
TAG=$(gcloud run services describe driftscribe-tofu-apply --region=$REGION --project=$PROJECT \
       --format='value(spec.template.spec.containers[0].image)' | sed 's/.*://')
cd "$(mktemp -d)" && git clone https://github.com/adi-prasetyo/driftscribe .
git rev-parse --verify "$TAG^{commit}" >/dev/null 2>&1 || { echo "image tag '$TAG' is not a commit — identify the baked iac/ commit manually" >&2; exit 1; }
git checkout "$TAG" && cd iac

# 1. Fail closed if an apply is in flight / a live lock exists (gcloud cp does NOT
#    lock; a concurrent apply + your backup/reconcile would race → §4 first).
if gcloud storage ls gs://$PROJECT-tofu-state/prod/default.tflock >/dev/null 2>&1; then
  echo "LOCK PRESENT — resolve via §4 first" >&2; exit 1
fi

# 2. Object versioning on the state bucket (recovery safety net). Idempotent.
gcloud storage buckets update gs://$PROJECT-tofu-state --versioning

# 3. Back up the live state object (timestamp the reason).
gcloud storage cp gs://$PROJECT-tofu-state/prod/default.tfstate \
  gs://$PROJECT-tofu-state/prod/_backup/default.tfstate.pre-reconcile.$(date -u +%Y%m%dT%H%M%SZ)

# 4. Init against the real gcs backend (KMS-encrypted state). Match worker flags.
tofu init -lockfile=readonly

# 5-6. Refresh-only plan, then INSPECT it (JSON for exact fields). The refresh-only
#      diff lives under `resource_drift[]` (state→live), with `resource_changes`
#      no-op (refresh-only proposes no config action).
tofu plan -refresh-only -out=refresh.tfplan -input=false -no-color -lock-timeout=120s
tofu show -json refresh.tfplan | python3 -m json.tool | less   # inspect resource_drift
```

**The inspection gate (read carefully — this is the safety stop).** For EVERY
attribute under `resource_drift[].change` that differs, it must be one of:

- **computed/cosmetic churn** — `generation`, `etag`, `observed_generation`,
  `conditions`/`terminal_condition` timestamps, `update_time`, `last_modifier`,
  `client_version`, revisions; or
- **this failed apply's own poison** — state holds the value the *approved plan*
  intended (`after`) while live still holds the *prior* value (because the live
  update was rejected). E.g. the C5g signature: `~ service_account =
  "payment-demo-runtime@" -> "compute@"` (state=the planned SA, live=the old SA).
  That IS the partial-state to undo — the refresh rewrites state back to live.

**Confirm "poison" against the approved plan — do NOT eyeball it.** The refresh
JSON only shows the current `state → live` delta; it does NOT prove the state
value equals the *failed* apply's intended `after`. Before classifying a material
field as this-apply's-poison, open the approved C2 plan for the burned approval
(the PR's `tofu show` comment, or fetch the `plan.json` artifact named in the
approval's `metadata` — bucket `gs://$PROJECT-tofu-artifacts/pr-<N>/<head_sha>/…`)
and verify the drifted field's **state** value equals that plan's `after` for the
same address. If it doesn't match the approved `after`, it is NOT this apply's
poison → treat as independent drift.

If you see a difference you **cannot** attribute to computed churn OR to this
exact failed apply — i.e. an **independent** live change nobody approved (a value
that is neither the prior state nor the approved plan's `after`) — **STOP** and go
to **§3** (real out-of-band drift). Reconciling it away would silently erase a
real change.

```bash
# 7. Persist the refresh (STATE-ONLY: expect 0 added / 0 changed / 0 destroyed).
#    This rewrites state to match LIVE, undoing the failed apply's poison.
tofu apply -input=false -no-color -lock-timeout=120s refresh.tfplan

# 8. Confirm fresh.
tofu plan -refresh-only -detailed-exitcode -input=false -lock-timeout=120s   # exit 0 = state==live
```

Then **rebuild the C2 plan and re-Approve** — the new plan is built against the
reconciled state, so the freshness gate passes:

```bash
gh workflow run iac.yml -f pr_number=<N>     # re-run the plan-builder
```

Re-load the `/iac-approvals/<N>` page (so the coordinator binds the *newest* C2
comment) and Approve again.

---

## 3. Material drift (`drift_refused`)

The semantic freshness gate refused because a **managed resource's desired-state**
attribute changed out of band (someone edited the live service directly). This is
**not** a self-inflicted partial — do not blindly reconcile it away.

1. Identify the change: the offending `address:path` set is in
   `apply_audit.stderr_tail` (the `refusing apply: material refresh drift: …`
   message — `detail` is the generic refusal string). Inspect the live resource
   (`gcloud run services describe …`) and the Cloud audit logs (who/what changed it).
2. Decide intent:
   - the live change is **wanted** → fold it into `iac/` (a new PR), re-plan,
     re-Approve. Now state⟷config⟷live agree.
   - the live change is **unwanted** → revert it on the live resource (or let the
     approved plan overwrite it once you confirm the approved plan's `after`
     matches intended), then reconcile state (§2 steps 1-8) and re-Approve.
3. Never bypass the gate by force-applying — the refusal is the safety property.

---

## 4. Lock (`lock_refused`, HTTP 423)

A held or **orphaned** GCS state lock (e.g. an OOM-killed apply that never
released it — see `tofu-apply.md` §5). The worker **never** auto-unlocks.

```bash
# Confirm no apply is in flight, then read the lock's Who/Created to match the
# dead attempt before clearing it:
gcloud storage cat gs://driftscribe-hack-2026-tofu-state/prod/default.tflock
export TF_VAR_tofu_state_kms_key="projects/driftscribe-hack-2026/locations/asia-northeast1/keyRings/driftscribe-tofu/cryptoKeys/tofu-state"
tofu -chdir=iac init
tofu -chdir=iac force-unlock <LOCK_ID>       # operator, deliberate
```

Then re-Approve (a fresh approval — the burned one is `used`).

---

## 5. Artifact refusals (`integrity_/fidelity_/verify_refused`)

The C2 artifact failed the worker's independent re-verification (content hash,
`opentofu_version` / lockfile, baked-config resource-set, or signed-payload
round-trip). Do **not** edit the artifact — **rebuild** it: re-run the C2
plan-builder (`gh workflow run iac.yml -f pr_number=<N>`), confirm the new PR
comment's hashes/generations, reload the approval page, Approve. A persistent
fidelity refusal usually means the baked worker image and the plan-builder pins
have drifted (bump `tofu`/lockfile in lockstep — `tofu-apply.md` §5).

---

## 6. Permanent merge block (apply succeeded, merge blocked)

If the outcome was **"Applied; the merge is blocked by branch protection"**, the
infra change **already applied** — only the GitHub merge is outstanding because a
required review/status is not yet satisfied (e.g. the sole-owner repo can't
self-approve the required code-owner review). A bare re-submit will **not** merge
it. Resolve out-of-band, then re-submit the approval — the coordinator does a
**merge-only reconcile** (it does NOT re-run the apply):

```bash
# satisfy the requirement out-of-band, e.g. an admin merge of the exact head:
gh pr merge <N> --squash --admin
# (or approve the required review, or wait for the required status to pass)
```

The decision doc is parked `apply_status=applied`, `merge_state=failed`; once the
PR is merged (by you or a later re-submit) the reconcile records `merged`.

---

## Appendix — constants

- State bucket: `gs://driftscribe-hack-2026-tofu-state`, prefix `prod`, object `prod/default.tfstate`, lock `prod/default.tflock`, backups under `prod/_backup/`.
- KMS key: `projects/driftscribe-hack-2026/locations/asia-northeast1/keyRings/driftscribe-tofu/cryptoKeys/tofu-state` (keyring `driftscribe-tofu` — **not** `…-tofu-state`).
- Artifact bucket: `gs://driftscribe-hack-2026-tofu-artifacts` (C2 plan triplet under `pr-<N>/<head_sha>/run-<id>-<attempt>/`).
- Firestore: named DB `plan-approvals`, collection `plan_approvals` (apply audit); the coordinator's decision docs are in the `(default)` DB.
- Every `tofu` invocation needs `TF_VAR_tofu_state_kms_key` exported (the `iac/` encryption block is enforced — `init`/`plan`/`show`/`apply` all must decrypt).
