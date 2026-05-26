# DriftScribe IaC backend — operator bootstrap runbook

This runbook covers the **live-GCP** steps that stand up DriftScribe's OpenTofu
layer (`iac/`) against the real `driftscribe-hack-2026` project. Every step here
mutates live GCP infrastructure, IAM, or remote OpenTofu state — **none of it
runs in CI**, and none of it was executed when Phase A landed. The agent
deliberately stopped at the code; you (the operator) run the steps below from a
shell that holds `roles/owner` on `driftscribe-hack-2026`.

These steps are the bridge between "Phase A code is merged" and "Phase A is
actually live": the buckets + KMS key must exist before the first `tofu init`,
and `payment-demo` must be imported into encrypted state. Phase A CI only ever
runs an unauthenticated static check (`init -backend=false` + fmt + validate);
it never touches the backend.

> **Target project is `driftscribe-hack-2026`.** This is **prod**. The import in
> step 5 adopts the live `payment-demo` service into OpenTofu state. A correct
> run makes **no resource changes** (adoption only), but confirm `PROJECT` and
> the active gcloud account before running anything below.

Cross-references:

- OpenTofu layer + static gate: `iac/README.md`
- Bootstrap script: `infra/scripts/setup_iac_backend.sh`
- Shared helpers: `infra/scripts/_setup_lib.sh`
- HCL config: `iac/versions.tf`, `iac/providers.tf`, `iac/variables.tf`,
  `iac/cloudrun.tf`, `iac/imports.tf`
- CI workflow: `.github/workflows/iac.yml`
- Plan: `docs/plans/2026-05-27-infra-iac-phase-a.md`

---

## 1. Review and run the bootstrap script

`infra/scripts/setup_iac_backend.sh` is **idempotent** and **operator-run**
(top banner: `OPERATOR-RUN: creates live GCP infra/IAM ...`). Read it first — it
provisions:

1. The **state bucket** `gs://driftscribe-hack-2026-tofu-state` (Object
   Versioning ON, uniform bucket-level access, public-access-prevention).
2. The **artifact bucket** `gs://driftscribe-hack-2026-tofu-artifacts`
   (versioned) — reserved for Phase C plan artifacts; created now so the
   denylist target exists.
3. A **Cloud KMS** keyring + key for state/plan encryption; it prints the full
   key resource path for `var.tofu_state_kms_key`.
4. The **Workload Identity Federation** pool + GitHub OIDC provider (attribute
   conditions pinning repository + workflow + ref/base_ref + event_name) and a
   least-privilege CI service account. **This half is Phase C** — see step 2.

It also enables the required APIs (`cloudkms`, `iam`, `iamcredentials`, `run`,
`storage`, `sts`).

### Environment variables

All knobs default to the real prod values; override via env var only to dry-run
against a throwaway project you own.

| Env var | Default | Purpose |
|---------|---------|---------|
| `PROJECT` | `driftscribe-hack-2026` | Target GCP project. |
| `REGION` | `asia-northeast1` | Region for the buckets; also the default KMS location. |
| `GITHUB_REPO` | `adi-prasetyo/driftscribe` | Canonical repo the OIDC provider trusts (fork PRs are rejected). |
| `GITHUB_WORKFLOW` | `.github/workflows/iac.yml` | The only workflow file allowed to mint GCP creds. |
| `GITHUB_BRANCH` | `main` | Trusted branch (BARE name) — PRs targeting it (`base_ref`) and pushes to it (`ref`) may obtain creds. |
| `KMS_LOCATION` | `${REGION}` (`asia-northeast1`) | KMS keyring location. **Immutable** once the keyring exists. |

Other overridable knobs exist (`STATE_BUCKET`, `ARTIFACT_BUCKET`, `KMS_KEYRING`,
`KMS_KEY`, `WIF_POOL`, `WIF_PROVIDER`, `CI_SA_NAME`) but the defaults are the
prod values — leave them unless you are dry-running.

> **KMS location is immutable.** Do not change `KMS_LOCATION` (or `REGION`) after
> the keyring exists and state has been written. Doing so would silently create a
> *second* keyring while existing state stays encrypted under the old key, and
> `tofu init` would then fail to decrypt. Migrate deliberately (decrypt with the
> old key, re-encrypt with the new) instead.

```bash
# Review first, then run as a project owner.
infra/scripts/setup_iac_backend.sh
```

On success the script prints a summary with two sections: **PHASE A — wire this
NOW** (the `var.tofu_state_kms_key` value + bucket names) and **PHASE C — wire
this LATER** (the WIF provider resource name + CI SA email). Copy the KMS key
path — you need it in step 3.

---

## 2. Set `tofu_state_kms_key` (WIF wiring is Phase C)

From the script's "PHASE A" summary, take the printed KMS key path:

```
projects/driftscribe-hack-2026/locations/asia-northeast1/keyRings/driftscribe-tofu/cryptoKeys/tofu-state
```

This is the value for `var.tofu_state_kms_key`. It has no default by design —
supply it at `init`/`plan`/`apply` time with `-var "tofu_state_kms_key=<path>"`,
or keep it in a local `tofu.tfvars` that you do **not** commit (see
`iac/README.md`; the static gate hard-rejects any committed `*.tfvars` under
`iac/`).

> **Do NOT wire WIF in Phase A.** The script also prints a WIF provider resource
> name and CI service-account email under "PHASE C — wire this LATER". Wiring
> those into a workflow (the authenticated plan-builder) is a **Phase C**
> activation step, **not** a Phase A done-condition. Phase A CI runs no
> authenticated plan and needs no GCP credentials.

---

## 3. `tofu init` against the real backend

With the buckets and KMS key now live, initialize against the **real** gcs
backend. State and plan encryption is enforced from t=0 (`iac/versions.tf`
`encryption { ... enforced = true }`), so the KMS key must be reachable.

```bash
cd iac
tofu init -var "tofu_state_kms_key=<KMS_KEY_PATH>"
```

(This is the live backend — distinct from the CI `init -backend=false`.)

---

## 4. `tofu plan` — review the import, iterate to an EMPTY plan

```bash
tofu plan -var "tofu_state_kms_key=<KMS_KEY_PATH>"
```

The `import {}` block in `iac/imports.tf` adopts the live `payment-demo` service
into state, so the plan should show an **import** of
`google_cloud_run_v2_service.payment_demo` and ideally **no resource changes**.

`iac/cloudrun.tf` was authored from the *documented* live shape (the
`infra/cloudbuild.yaml` deploy step + `demo/ops-contract.yaml`), **not** from a
live read, so the first plan will likely show diffs. Iterate `cloudrun.tf` until
the plan is **empty** (state == live). The fields most likely to need
reconciliation are flagged with `RECONCILE` comments in the file:

- the container **image tag** (CI mutates it; pin to whatever is actually
  serving at import time — the most common source of a non-empty plan);
- server-populated defaults the v2 API returns on read — container
  `resources {}` (CPU/memory limits), `ports {}` (default 8080),
  `launch_stage`, `traffic` weights to `LATEST`, annotations/labels, and the
  default `execution_environment`;
- `ingress` (the stored value may differ from `INGRESS_TRAFFIC_ALL`).

Do not guess undocumented values — let the plan reveal them and adjust.

---

## 5. `tofu apply` the import

Once the plan is empty apart from the import itself:

```bash
tofu apply -var "tofu_state_kms_key=<KMS_KEY_PATH>"
```

This adopts `payment-demo` into OpenTofu state with **no resource changes**.
After the first successful apply the `import {}` block in `iac/imports.tf` has
done its job (state now holds the resource) and may be removed in a later
foundation PR; it is harmless to leave.

---

## 6. Confirm CI green on a no-op `iac/` PR

Open a trivial no-op PR touching `iac/` (e.g. a comment or whitespace change in
`cloudrun.tf`, or a `README.md` tweak) and confirm the `iac` workflow
(`.github/workflows/iac.yml`) is green:

- `static-gate` job passes (mode derives to `operator` unless the PR carries the
  `driftscribe-infra` label or an `infra/` head branch);
- `tofu` job passes — `init -backend=false -lockfile=readonly`, `fmt -check`,
  `validate`.

The `-lockfile=readonly` step fails if the PR would change the committed
`iac/.terraform.lock.hcl`, which is the intended provider-add guard. A green run
here confirms the gate + tofu jobs work end-to-end and that the committed
lockfile matches.

---

## What is NOT in this runbook (Phase C+)

The authenticated WIF-based `tofu plan` in CI, the trusted plan-artifact
protocol, the self-protection denylist, the plan → PR → gated apply loop, and
the `tofu-apply`/`tofu-editor` workers all land in **Phases B–D** (see
`docs/plans/2026-05-27-infra-iac-agent-design.md`). The CI SA + WIF provider are
provisioned by the bootstrap script for that future work but are not wired into
any workflow in Phase A.
