# DriftScribe OpenTofu layer (`iac/`)

This directory holds the [OpenTofu](https://opentofu.org/) (HCL) configuration
that describes DriftScribe's GCP infrastructure as code. Phase A adopts the one
brownfield resource — the live `payment-demo` Cloud Run service in project
`driftscribe-hack-2026` (region `asia-northeast1`) — into OpenTofu state via a
declarative `import {}` block. It is **foundation only**: there are no agents,
no apply automation, and no plan pipeline yet.

This is Phase A of the four-phase IaC plan
(`docs/plans/2026-05-27-infra-iac-agent-design.md`, §8). The trusted
plan-artifact protocol, the self-protection denylist, the
`tofu-apply`/`tofu-editor` workers, and the agent fan-out are **Phases B–D** and
are explicitly out of scope here.

Cross-references:

- Plan: `docs/plans/2026-05-27-infra-iac-phase-a.md`
- Design: `docs/plans/2026-05-27-infra-iac-agent-design.md`
- Operator bootstrap runbook: `docs/runbooks/iac-bootstrap.md`
- Phase B infra-reader (read-only whole-project inventory; reads this `iac/` tree): `docs/runbooks/infra-reader.md`
- Static gate: `tools/iac_static_gate.py`
- CI workflow: `.github/workflows/iac.yml`
- Operator bootstrap script: `infra/scripts/setup_iac_backend.sh`

---

## What's in here

| File | Role | Editable by |
|------|------|-------------|
| `versions.tf` | `required_version`, `required_providers` (google), the **gcs backend**, and the **`gcp_kms` state/plan encryption** block | FOUNDATION (operator-only) |
| `providers.tf` | `provider "google"` — project + region wiring | FOUNDATION (operator-only) |
| `variables.tf` | variable definitions/defaults (`project_id`, `region`, `tofu_state_kms_key`) | FOUNDATION (operator-only) |
| `imports.tf` | the `import {}` target adopting `payment-demo` | FOUNDATION (operator-only) |
| `.terraform.lock.hcl` | committed provider lockfile | FOUNDATION (operator-only) |
| `cloudrun.tf` | the `google_cloud_run_v2_service.payment_demo` resource | AGENT-editable |

State and plan files are encrypted at rest with Cloud KMS (`gcp_kms` key
provider, AES-256, `enforced = true` for both state and plan) and live in the
gcs backend bucket `driftscribe-hack-2026-tofu-state` (prefix `prod`). The
bucket and KMS key are provisioned out-of-band by the bootstrap script — the
gcs backend never creates its own bucket and the `gcp_kms` provider never
creates its own key.

---

## GitOps flow (today vs. later)

**Today (Phase A):** changes to `iac/` are reviewed on a `pull_request`. CI runs
a purely **static** check — the HCL gate, then `tofu init -backend=false`,
`fmt -check`, and `validate`. There is **no `tofu plan` in CI** and **no apply
automation**: a meaningful plan needs live GCP credentials and API access that
this workflow deliberately does not (and must not) have. The live
`init`/`plan`/`apply` against the real backend is an **operator** activity — see
`docs/runbooks/iac-bootstrap.md`.

**Later (Phase C):** the authoritative WIF-authenticated `tofu plan`, the
trusted plan-artifact protocol, and the **plan → PR → gated apply** loop land in
Phase C. They are NOT implemented now. The bootstrap script already provisions
the Workload Identity Federation plumbing for that future plan-builder, but
wiring CI credentials is a Phase C activation step, not a Phase A done-condition.

---

## Running a local plan (operator)

`var.tofu_state_kms_key` has **no default by design** — the operator supplies
the full Cloud KMS key resource path printed by the bootstrap script
(`infra/scripts/setup_iac_backend.sh`, "PHASE A — wire this NOW" section). The
path looks like:

```
projects/driftscribe-hack-2026/locations/asia-northeast1/keyRings/driftscribe-tofu/cryptoKeys/tofu-state
```

```bash
cd iac

# Real backend; state + plan encryption is enforced from t=0.
tofu init -var "tofu_state_kms_key=<KMS_KEY_PATH>"
tofu plan -var "tofu_state_kms_key=<KMS_KEY_PATH>"
```

The KMS key path is configuration, not a secret, but if you prefer not to keep
it in shell history you may put it in a local `tofu.tfvars`. **Do not commit a
real tfvars containing the key path** — the static gate hard-rejects any
`*.tfvars`/`*.auto.tfvars` under `iac/` in agent mode precisely because OpenTofu
auto-loads them, and committing one would also leak the path. Keep it local.

For local `fmt`/`validate` without touching the backend, use
`tofu init -backend=false` (this is also what CI runs).

---

## Foundation vs. agent-editable

**FOUNDATION** files set *authority* — the backend, the encryption key, the
provider project/region/credentials, the variable surface, and the import
targets. An agent PR that could touch any of these could redirect the whole
project (e.g. point the backend at an attacker bucket, swap the encryption key,
or add an import that adopts a foreign resource). They are therefore locked down
by two independent controls:

1. The **static gate** in **agent mode** rejects any change to a foundation file
   (`foundation-edit-agent-mode`).
2. **CODEOWNERS** (`.github/CODEOWNERS`) is the operator-mode GitHub review
   control requiring human review of the same files (the gate is the
   pre-`tofu init` policy; CODEOWNERS is the review-side control per design
   §5.1). NOTE: the CODEOWNERS file exists, but it is only **advisory** until the
   operator enables branch protection on `main` with "Require review from Code
   Owners" — see `docs/runbooks/iac-bootstrap.md`. Until then the gate's
   `foundation-edit-agent-mode` rule is the only *enforced* control.

The protected set is: `versions.tf`, `providers.tf`, `variables.tf`,
`imports.tf`, and `.terraform.lock.hcl`.

**AGENT-editable:** `cloudrun.tf` (and future per-resource `.tf` files) — the
actual managed resources. In agent mode the gate allows only plain `.tf` files
(and `.md`) under `iac/`; everything else OpenTofu loads is a bypass surface and
is rejected.

---

## Static-gate rules

`tools/iac_static_gate.py` is a pure-Python policy that runs **before**
`tofu init` in CI (`.github/workflows/iac.yml`). It parses the post-change HCL
of the PR with `python-hcl2` and is **fail-closed**: a parse failure is a
violation, not a crash.

### Two modes

| Mode | When (CI derivation) | Behavior |
|------|----------------------|----------|
| **agent** | PR has the `driftscribe-infra` label **OR** the head branch starts with `infra/` | Strict: path/file-type/foundation rules apply. Only `iac/**` may be touched; only `.tf`/`.md` files; foundation files are off-limits. |
| **operator** | otherwise (default) | Foundation edits allowed; the path/file-type rules do not fire (the gate only governs `iac/`; CODEOWNERS governs everything else). |

Content rules (providers, modules, execution constructs) run in **both** modes,
because providers legitimately live in foundation files and a clean operator PR
must still pass them.

### Rules

| Rule id | Mode | What it forbids |
|---------|------|-----------------|
| `path-outside-iac` | agent | Any changed path not under `iac/`. (Operator mode treats these as out of scope, not a violation.) |
| `disallowed-file-type` | agent | Any `iac/` file that is not a plain `.tf` or `.md`. Explicitly catches `.tofu`, `.tofu.json`, `.tf.json`, `*.tfvars`, and `*.auto.tfvars` — all loaded by OpenTofu and thus bypass surfaces. (`.tofu` even *overrides* a same-named `.tf`.) |
| `foundation-edit-agent-mode` | agent | Any edit to a foundation file (`versions.tf`, `providers.tf`, `variables.tf`, `imports.tf`, `.terraform.lock.hcl`). |
| `disallowed-provider` | both | Any provider outside the allowlist (`google`, plus the builtin `terraform`/`tofu` pseudo-providers) in `required_providers` or a top-level `provider` block. |
| `disallowed-provider-source` | both | An allowlisted provider declared with a spoofed source — e.g. `google = { source = "evil/google" }`. The canonical source must be `hashicorp/google`. (A `google` block with no source declared passes; the committed lockfile + `init -lockfile=readonly` is the guard that an unpinned/new provider cannot actually be resolved in CI.) |
| `hcl-parse-error` | both | Any HCL file that fails to parse (fail-closed). |
| `module-block-forbidden` | both | Any `module` block at all (v1 bans all modules — remote *and* local — to avoid recursively parsing local modules to enforce the same rules inside them). |
| `arbitrary-execution` | both | A `provisioner` or `connection` block (including `local-exec`/`remote-exec`), nested at any depth, **and** the `null_resource` / `terraform_data` resource types (whose purpose with provisioners is to run commands). |
| `dynamic-block-forbidden` | both | Any `dynamic` block (a `dynamic "provisioner"` would smuggle execution past a naive key check). |
| `forbidden-data-source` | both | `data "external"` (command execution) and `data "terraform_remote_state"` (cross-state read). |

> JSON-syntax HCL (`.tf.json`/`.tofu.json`) is not structurally analyzed in v1 —
> `hcl2.loads` only parses native-syntax HCL. Such files stay hard-rejected in
> agent mode via `disallowed-file-type`; in operator mode JSON config is
> governed by human review + CODEOWNERS.

The CLI form is:

```bash
python -m tools.iac_static_gate --base <sha> --head <sha> --mode <agent|operator>
```

It computes changed paths via `git diff --name-only <base>...<head>`, reads the
post-change content of changed `iac/*.tf` files at `<head>`, runs the gate, and
exits non-zero if there are any violations.

## Phase C1: plan-JSON denylist

`tools/iac_plan_denylist.py` is the **self-protection denylist** layered on top
of the static HCL gate (design doc §5.2). Where the static gate is a syntax-
level check on the PR diff, the denylist is a semantic check on a parsed
OpenTofu `tofu show -json plan.tfplan` document: it refuses any non-no-op
change targeting DriftScribe's own control plane, any IAM/WIF change, and any
state-mutating action (`delete` / `forget` / replace) — even on unrelated
resources, in v1.

The module is a pure, deterministic, fail-closed Python library plus a thin
CLI; no new third-party dependencies (stdlib `json`-only). It is designed to
be called from **three** places:

1. This CLI for local-dev validation.
2. The trusted plan-builder CI workflow (wired in **C2**, not C1).
3. The `tofu-apply` worker re-runs the denylist against the same `plan.json`
   immediately before `tofu apply` (wired in **C4**).

**14 rules** spread across structural fail-closed (`plan-json-unparseable`,
`plan-json-missing-resource-changes`, `plan-json-malformed-change`), action-
floor (`delete-action-forbidden-v1`, `forget-action-forbidden-v1`, `replace-
action-forbidden-v1`, `unknown-action-forbidden-v1`), control-plane identity
(`control-plane-service`, `control-plane-sa`, `control-plane-bucket`, `control-
plane-secret`, `control-plane-kms`), and IAM/WIF (`wif-config-change`,
`iam-change-forbidden-v1`). See the module docstring for the exact identity-
matching strategy and the full C4 worker contract.

The CLI form is:

```bash
python -m tools.iac_plan_denylist <plan.json>
```

Exit codes: `0` = pass, `1` = violations (incl. unparseable plan), `2` =
usage / I/O error. Output is ASCII-only.

**v1 floor.** The rule set is intentionally over-inclusive: hard-deny *all*
IAM changes and *all* `delete`/`forget`/replace actions, even on unrelated
resources. A positive allowlist is a later-phase decision; the false-positive
trade-off is accepted to keep the gate defensible until the C3 human-approval
flow lands.

> **C1 is library + CLI + tests only.** No CI wiring (C2 builds the trusted
> plan-builder workflow that produces the `plan.json`), no apply worker (C4),
> no HMAC schema (C3/C5).

### Phase C2 — Trusted plan-builder workflow

The `plan-builder` job in `.github/workflows/iac.yml` produces the authoritative
`tofu plan` artifact for a DriftScribe IaC PR:

- **Trigger:** `workflow_dispatch` only, AND only when the dispatched ref is
  `refs/heads/main`. A maintainer clicks **Run workflow** from the `main` branch
  and enters the PR number. `pull_request` is excluded (the WIF condition
  refuses tokens for that event — fork-PR `repository` claim cannot be
  filtered); dispatch from a non-main branch is also rejected (so a modified
  workflow file on a feature branch cannot mint creds).
- **Identity:** WIF-impersonated `tofu-plan-builder@…` SA. No long-lived keys.
  Bucket-scoped IAM: state lock + KMS encrypt/decrypt + artifact write only.
- **PR eligibility:** same-repo (no forks), base `main`, **changes only `iac/`
  paths**. After checkout of the pinned head SHA, a pure-shell
  `git diff --name-only --no-renames -z $BASE_SHA $HEAD_SHA` against the
  immutable git objects refuses if any path is outside `iac/` — no API
  call, no force-push TOCTOU. The in-checkout static-gate re-run in
  HARDCODED `MODE=agent` is the second line of defense.
- **Steps:** validate PR → resolve head/base SHAs (refuse fork/non-main-base)
  → checkout pinned head → fetch base SHA → diff-guard (`git diff --no-renames`)
  → uv/python setup → static-gate re-run (agent mode) → setup-opentofu
  (pinned 1.12.0) → WIF auth → `tofu init` (backend) → `tofu plan -out`
  → `tofu show -json` → C1 denylist (fails before upload on violation) →
  upload plan.tfplan + plan.json (capture generations) → build final
  metadata.json with real generations → upload metadata.json (no
  placeholder ever lands) → post truncated `tofu show` diff to the PR
  (with all 3 generations + 2 content hashes + 3 URIs).
- **Metadata schema:** `c2.v1` — 15 keys (`schema_version` + 14 data fields),
  validated by `tools.iac_plan_metadata`. The C3 input contract; C4 fetches by
  pinned generation. Per-run path segment means re-plans never collide.
- **Operator preconditions** (one-time after merge):
  - Re-run `infra/scripts/setup_iac_backend.sh` to apply BOTH the new
    `storage.objectCreator` IAM binding on the artifact bucket AND the
    tightened WIF condition (ref-pinned workflow_dispatch).
  - Set GitHub secrets `GCP_WIF_PROVIDER`, `GCP_TOFU_PLAN_BUILDER_SA`,
    `GCP_TOFU_STATE_KMS_KEY` (values printed by the bootstrap script).
- **What it does NOT do:** mint approvals, sign HMAC, apply state, read other
  PRs' artifacts. Those live in C3 (schema) and C4 (apply worker).
