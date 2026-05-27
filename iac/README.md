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
