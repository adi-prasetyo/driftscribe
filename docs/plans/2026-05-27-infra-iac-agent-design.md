# Whole-Project Infra: OpenTofu Reader + Fan-Out Editor Pipeline — Design

**Status:** Approved to proceed to Phase A plan (rev 2 final, two Codex review rounds) · 2026-05-27
**Author:** Claude (autonomous session, operator asleep — trusting Claude+Codex sign-off)
**Reviewers:** Codex (thread 019e6510-39f0-7320-9b6b-e868670f3748; rev-1 + rev-2 reviews incorporated; rev-2 verdict: "good enough to proceed to a Phase A TDD implementation plan")
**Supersedes nothing.** Additive to the Phase 17 workload framework and the Phase 22 SSE timeline.

---

## 1. Motivation

DriftScribe today reads infra through a **keyhole**: the `explore` workload can
report the live env of *one* Cloud Run service (`payment-demo`) via
`read_live_env_tool`. When the operator asks "explain my whole infra," the honest
answer is two env vars on one service — accurate, but a keyhole. The limitation is
**aperture**, not correctness.

This design widens the aperture in two ways, both scoped to the **whole GCP
project** (`driftscribe-hack-2026`, region `asia-northeast1`):

1. **Full-project READER** — `explore` can enumerate and describe every managed
   resource in the project, not just one service's env.
2. **Fan-out EDITOR pipeline** — chat can *change* infra across the whole
   project by **coordinating and spawning agents that author IaC code**, funnel
   their edits into ONE pull request, run `tofu plan` + policy checks in CI, and
   apply only after a human approves the plan.

The mechanism is **OpenTofu** (MPL-2.0, Linux Foundation) — "Terraform-compatible
IaC" — chosen so the project stays as open-source as possible. CLI is `tofu`; the
config block keyword is still `terraform { }`.

### The non-negotiable trust boundary

> **Editor agents only write HCL and open PRs. They hold ZERO infra credentials
> and have ZERO live-mutation access. The `tofu-apply` worker is the single
> component that touches live infrastructure, and it runs exactly one
> `tofu apply <saved-plan>` per human-approved plan.**

The trust boundary holds **up to PR creation**. Codex's rev-1 review sharpened a
crucial nuance that this design now treats as central: *OpenTofu executes
provider/module/plugin code during `plan` and `apply`*. So PR-controlled HCL is a
latent **code-execution surface** inside CI and inside the apply worker — "the
agent only writes text" is true, but that text is executed downstream. Two new
controls below address this: a **static HCL gate** (§5) that runs before any
`tofu init`, and a **trusted plan-artifact protocol** (§6) so the bytes the apply
worker executes are exactly the bytes a human approved.

---

## 2. How this maps onto what DriftScribe already has

Every new component is a variation on a pattern the codebase already ships/tests:

| New component | Existing analog | What carries over |
|---|---|---|
| `tofu-apply` worker (broad-IAM, sole mutator) | `workers/rollback` `/execute` (HMAC-gated) + `workers/upgrade_docs` `/merge` (fail-closed CI green-gate) | separate Cloud Run service, own SA, per-secret IAM, `verify_caller` audience+caller check, fail-closed gates, **private ingress** |
| `tofu-editor` worker (opens ONE IaC PR) | `workers/upgrade_docs` `/patch` (`open_docs_pr`, label/branch/base gated) | closed pydantic schema (`extra="forbid"`), branch-prefix + base allowlist, repo pinned to env, write-scoped PAT, writes only under `iac/` |
| Plan-builder (produces authoritative saved plan) | the CI half of the upgrade flow + `infra/cloudbuild.*.yaml` deploy jobs | trusted-code execution, WIF (no long-lived keys), artifact provenance |
| "Approve THIS plan" HMAC | `compute_token_hmac(token, approval_id, target_revision)` | single-use, TTL, HMAC-stored-not-token; **rebind** third component to plan-binary digest + artifact generation + head SHA (§7) |
| Full-project reader tools | `explore` read-only subset + `test_coordinator_tool_inventory.py` | disjoint-from-mutation guarantee, read-only credential per tool, symbolic-name resolution |
| Coordinator fan-out of editor agents | coordinator → worker dispatch via `worker_client.call` | audience-bound ID token, hardcoded worker URL/endpoint, allowlist-in-code |
| Static HCL gate + self-protection denylist | `workers/upgrade_docs/validator.py` (deterministic, transport-agnostic, fail-closed) | same module shape, table-driven tests |

The workload-authority model is unchanged: the manifest names only **symbolic**
tools/workers; real URLs/secrets/repos live in `agent/workloads/registry.py`.
Flipping a YAML value can pick from the allowlist but cannot introduce a new URL,
secret, repo, or callable.

---

## 3. Architecture

### 3.1 Two halves

```
  HALF 1 — READER (read-only, explore workload)
  chat "explain my infra" ─▶ coordinator ─▶ tofu_read_project tool ─▶ infra-reader worker
                                                                       ├─ `tofu show -json` (managed)
                                                                       └─ GCP describe APIs (unmanaged)

  HALF 2 — EDITOR PIPELINE (gated mutation, provision workload)
  chat "enable FEATURE_X on payment-demo" ─▶ coordinator
      1. reader (RO)  2. DECOMPOSE → fan out editor agents  3. author HCL slices
      4. tofu-editor worker → ONE PR (branch infra/*, base main, label driftscribe-infra)
          │
          ▼  GitHub Actions (required checks on PR head SHA)
      STATIC HCL GATE (pre-init) → tofu init -lockfile=readonly → fmt -check → validate
          → PLAN-BUILDER: tofu plan -out=plan.tfplan → tofu show -json → DENYLIST(plan.json)
          → upload {plan.tfplan, plan.json, metadata} to controlled GCS artifact bucket
          → post human-readable diff to PR
          │
          ▼  human reads diff → Approve
      coordinator mints HMAC bound to (approval_id, plan_sha256, artifact_generation, head_sha)
          │
          ▼  tofu-apply worker  ← the ONLY component that mutates live infra
      fetch artifact by pinned generation → recompute digests → re-run `tofu show -json`
          → re-run DENYLIST → freshness check (refresh-only plan, refuse on drift)
          → verify HMAC (single-use, unexpired, bound) → `tofu apply plan.tfplan`
          → on success, merge the exact approved head SHA
```

### 3.2 The OpenTofu layer

A new top-level `iac/` directory holds the HCL:

```
iac/
  versions.tf        # required_version, required_providers (google), backend "gcs", encryption{}
  providers.tf       # provider "google" { project, region } — values via vars
  variables.tf       # project_id, region (no secrets, no runtime refs)
  cloudrun.tf        # google_cloud_run_v2_service resources (imported, brownfield)
  imports.tf         # import { } blocks for brownfield adoption (removable after first apply)
  README.md          # local plan/apply; the GitOps flow; what NOT to import
  .terraform.lock.hcl  # provider pins + checksums — COMMITTED (the static gate enforces it)
```

OpenTofu specifics (verified against docs 2026-05-27; skill updated):

- **Backend `gcs`** → state bucket `gs://driftscribe-hack-2026-tofu-state`, Object
  Versioning ON. The bucket **must pre-exist** (created out-of-band in the
  bootstrap script — the backend never creates it). It is part of DriftScribe's
  control plane → on the denylist (§5).
- **`gcs` locking caveat (Codex catch, verified):** the GCS backend supports state
  locking, and acquiring the lock **writes a lock object**, so the credential needs
  the `Storage Object Admin` role on the bucket **even for `plan`**. A "plan is
  read-only" assumption is therefore *false* with the GCS backend. Consequence for
  CI: see §6 (the plan-builder is a *trusted, scoped-write* identity, not a
  read-only one; alternatively `-lock=false` for a throwaway preview that is never
  used as the apply artifact).
- **State & plan encryption from t=0** (so there's never plaintext state to migrate
  — encrypting existing plaintext requires the unencrypted-fallback dance).
  Key provider `gcp_kms` (KMS key created out-of-band in bootstrap, avoiding the
  chicken-and-egg of managing the key inside the state it encrypts). Required
  fields (verified): `kms_encryption_key` (full key resource path) + `key_length`
  (bytes, 1–1024; 32 = AES-256), both resolvable at init (vars/locals only).
  `state { enforced = true }` and `plan { enforced = true }`. **Note (verified):**
  encryption does *not* defend against replay of an old plan — that's what the
  plan-identity binding (§7) and freshness check (§6) are for.
- **Brownfield via `import { }` blocks** — adopt incrementally. "Whole-project
  capability" does NOT mean importing the whole project on day one: import the
  edit-target services first (`payment-demo`), prove a **clean/empty plan**
  (state == live), grow coverage over time.
- **Provider `google`** (`hashicorp/google`) via `registry.opentofu.org`.
  **Phase A checklist:** confirm the google provider's license permits OpenTofu use
  (the major cloud providers are believed MPL-2.0/fine, but verify against
  github.com/opentofu/opentofu before relying on it).

### 3.3 The reader (Half 1)

Read-only `infra-reader` worker, `/describe` endpoint, returns a structured
inventory merged from two sources: (1) `tofu show -json` of current state
(authoritative *managed* resources), (2) GCP read-only describe APIs (to also
surface *unmanaged* resources, so the reader doesn't pretend project == state).
New symbolic tool `tofu_read_project` in `TOOL_REGISTRY`, added to `explore`'s
`enabled_tool_names`, kept in the read-only subset pinned by
`test_coordinator_tool_inventory.py`. Reader SA gets **per-API viewer roles**
(not a blanket `roles/viewer`) to honor the "no project-wide grant" invariant in
`project_structure.md`.

### 3.4 The editor pipeline (Half 2)

- **New write-capable workload `provision`** in `workloads/`. NOTE:
  `WorkloadSpec.name` is a `Literal["drift","upgrade","explore"]`
  (`agent/workloads/spec.py:73`) — adding `provision` requires extending that
  Literal + the loader's allowed set. Unlike `explore` (which narrows capability),
  `provision` lists the editor tool + reader tools. It is **chat-driven**
  (decompose → fan-out), gated end-to-end by the apply pipeline, and has **no
  autonomous `/recheck` mutation path** (route-refused like `explore`).
- **Fan-out:** coordinator decomposes a change into per-resource authoring tasks,
  runs editor sub-agents in parallel (each authors one HCL slice), funnels all
  slices into ONE branch / ONE PR via the `tofu-editor` worker. Decomposition +
  merge-into-one-PR is coordinator logic; authoring is the agents; the PR write is
  the worker.
- **`tofu-editor` worker** opens exactly one PR: branch prefix `infra/`, base
  `main`, label `driftscribe-infra`, repo pinned to env, **writes only under
  `iac/`** (path allowlist; any change outside `iac/` is rejected). Mirrors
  `workers/upgrade_docs` `/patch`.

---

## 4. End-to-end data flow (one change)

```
operator (chat): "turn on FEATURE_NEW_CHECKOUT for payment-demo, min-instances 1"
  │
  ├─[coordinator, provision workload]
  │   reader: tofu_read_project → current state of payment-demo            (READ-ONLY)
  │   decompose → 2 authoring tasks → fan out 2 editor sub-agents
  │   editors author HCL slices under iac/cloudrun.tf
  │   tofu-editor worker → ONE PR (branch infra/<slug>, base main, label driftscribe-infra)
  │
  ├─[GitHub Actions on PR head SHA — required checks]
  │   STATIC HCL GATE (pre-init): paths under iac/ only; lockfile unchanged;
  │        no new/unpinned providers; no remote modules; no provisioner/local-exec/
  │        remote-exec/data "external"/null_resource provisioners; google+builtin only
  │   tofu init -lockfile=readonly · fmt -check · validate
  │   PLAN-BUILDER (trusted code, WIF, scoped state-bucket write for lock):
  │        tofu plan -out=plan.tfplan → tofu show -json → DENYLIST(plan.json)
  │        → upload {plan.tfplan, plan.json, metadata} to artifact bucket (immutable)
  │        → post human-readable diff to PR
  │
  ├─[human] reads plan diff → Approve
  │   coordinator mints HMAC bound to (approval_id, plan_sha256, artifact_generation, head_sha)
  │
  └─[tofu-apply worker]  ← the ONLY step that mutates live infra
      fetch artifact by pinned GCS generation → recompute plan_sha256/plan_json_sha256
      re-run tofu show -json + DENYLIST + freshness check (refresh-only plan; refuse on drift)
      verify HMAC: bound ∧ single-use ∧ unexpired ; verify head SHA still == approved
      `tofu apply plan.tfplan`  → then merge the exact approved head SHA → report to chat
```

---

## 5. Static HCL gate + self-protection denylist

Two deterministic, fail-closed policy layers (both shaped like
`workers/upgrade_docs/validator.py`). The first runs on *source*, before any
OpenTofu execution; the second runs on the *plan*.

### 5.1 Static HCL gate (pre-`tofu init`) — closes the code-execution surface

Runs as the first CI step on any `driftscribe-infra` PR, and is re-asserted by the
plan-builder. Rejects the PR if any of:

- it touches paths **outside `iac/`** (esp. `.github/workflows/**`, the provider
  lockfile, the encryption/backend config) — those are operator-authored and
  separately reviewed, never agent-authored;
- `tofu init -lockfile=readonly` would need to change `.terraform.lock.hcl` (i.e.
  a new or upgraded provider) — **forbids the agent adding providers**;
- the HCL declares a provider other than the allowlisted `google` (+ builtin);
- it uses **any `module` block at all** (v1 forbids modules entirely — Codex rev-2:
  forbidding only *remote* modules would require the gate to recursively parse
  local modules and enforce the same rules inside them; simpler and safer to ban
  all modules in v1 and revisit with recursive parsing later);
- it contains `provisioner`, `local-exec`, `remote-exec`, `data "external"`,
  `null_resource`/`terraform_data` provisioner patterns, or any other
  arbitrary-execution construct.

**Two policy modes (Codex rev-2):** the gate distinguishes an **operator/bootstrap
PR** (may touch the lockfile, backend, encryption config, workflows — these are the
Phase A bootstrap edits, gated by *human-authored* review + CODEOWNERS, not by the
agent rules) from an **agent/`provision` PR** (the rules above apply in full —
identified by the `driftscribe-infra` label + the `infra/` branch prefix + author).
The strict agent rules never relax; the bootstrap mode is what lets Phase A create
the lockfile/backend/encryption in the first place without the gate rejecting its
own foundation.

This is what makes "the agent only writes text" actually safe: the text can't pull
in code that runs during plan/apply.

### 5.2 Self-protection denylist (on plan JSON)

Evaluates `tofu show -json plan.tfplan` and **fails** if any `resource_change` with
a non-no-op action targets DriftScribe's own control plane:

- coordinator service (`driftscribe-agent`); `tofu-apply`/`tofu-editor`/plan-builder
  services **and their SAs**;
- the state GCS bucket (`*-tofu-state`) and the artifact bucket;
- the approval HMAC secret + coordinator-token secret;
- **any IAM resource/binding** (v1 hard-deny all IAM changes until a positive
  allowlist exists — Codex catch: the rev-1 denylist was too narrow on IAM);
- the CI/WIF configuration.

**v1 also hard-denies `delete` and `replace` actions** (an extra floor; relax later
behind the same human gate). Runs in **two places**: as a required CI check AND
inside the `tofu-apply` worker before apply (a green PR still can't apply a
control-plane change). Fail-closed: unparseable/empty plan = deny.

Engine: **custom Python** over `plan.json` (no new runtime dep; matches the
existing validator idiom). Revisit Conftest/OPA only if policy outgrows it.

---

## 6. Trusted plan-artifact protocol (the central security boundary)

Codex rev-1's top finding: "apply the exact saved plan" is meaningless unless the
artifact's *provenance and transport* are defined. The diff in a PR comment is
**not** a trustworthy channel. Protocol:

1. **Producer:** a **trusted plan-builder** (CI job running repo-controlled
   workflow code via Workload Identity Federation — no long-lived keys). Because
   the GCS backend's lock needs object write (§3.2), the plan-builder identity has
   **scoped write to the state bucket** (for locking) and write to the artifact
   bucket — it is *not* a read-only identity, and we stop pretending it is. The
   authenticated plan-builder MUST run only on a **trusted trigger**
   (trusted-branch push / maintainer `workflow_dispatch` / `workflow_run`), **NOT**
   fork-PR OIDC: on the `pull_request` event the OIDC `repository` claim is the
   base repo even for a fork PR, so `repository ==` cannot filter fork PRs — the
   WIF condition must gate on `event_name`, not on `repository`/`base_ref` alone.
   Since a `workflow_dispatch`-triggered plan-builder runs against whatever ref it
   is told, it MUST deliberately check out the *intended reviewed* PR head SHA and
   record that exact SHA into the artifact metadata (§6.2) — so the human approves
   the plan for the SHA that was actually planned, not whatever `main` drifted to.
2. **Artifact storage:** upload `plan.tfplan` (binary) **and** `plan.json` to a
   controlled, versioned GCS **artifact bucket** (separate from state; on the
   denylist). Store immutable metadata alongside: `repo`, `pr_number`, `head_sha`,
   `base_sha`, `workflow_run_id`, `artifact_uri`, GCS object `generation`,
   `plan_sha256` (of the binary), `plan_json_sha256`, OpenTofu version, provider
   lockfile hash.
3. **Approval identity = the binary plan.** The HMAC binds **`plan_sha256` (binary
   bytes) + `artifact_generation` + `head_sha`**, not the `-json` rendering. Hash
   the saved plan bytes as the primary identity; `plan.json` is for audit/policy
   reproducibility only.
4. **Approval ownership = the apply worker, NOT the coordinator** (Codex rev-2):
   the apply worker owns the HMAC key and is the only service that creates and
   claims plan approvals — mirroring `workers/rollback` (`/propose` creates,
   `/execute` claims, the worker holds the key). Two-step:
   - **propose:** after CI uploads the artifact, the apply worker's `/propose`
     endpoint **independently verifies** the artifact (fetches by pinned
     generation, recomputes `plan_sha256`/`plan_json_sha256`, re-runs the denylist,
     checks PR head/required-checks) and only then writes the pinned plan-approval
     record + returns `approval_id` + raw token. The coordinator *requests*
     proposal and renders the approval page, but **cannot mint a valid approval
     alone** (it never holds the HMAC key). This preserves §9's "compromised
     coordinator" claim.
   - **apply:** the request carries **`approval_id` + the raw approval token**
     (the operator's), and **no artifact fields** — the worker resolves the
     artifact and its pinned digests/generation from the approval record it wrote.
     The token is what authenticates; the caller cannot substitute an artifact URI.
5. **Consumer (apply):** the worker claims the approval (single-use transactional
   flip), re-fetches the artifact by the record's pinned generation, recomputes and
   re-compares digests, re-runs `tofu show -json` + denylist, runs the freshness
   check (§6.7), then applies the binary plan.
6. **Saved-plan apply is the right primitive** (verified against OpenTofu apply
   docs): `tofu apply plan.tfplan` executes exactly the actions in that plan and
   refuses new planning options — this avoids action-level TOCTOU. Re-planning at
   apply time would *not*.
7. **Freshness check (verified caveat):** saved plans + encryption do **not**
   defend against out-of-band live drift after planning. Before applying, the apply
   worker runs a **non-mutating refresh-only plan** from the approved head and
   **refuses if it detects drift** vs the saved plan's assumptions.

---

## 7. Approval extension (HMAC bound to the plan) — new schema, not overloaded

`driftscribe_lib/approvals.py` today is **revision-shaped**: `Approval` carries
`target_revision`, and `compute_token_hmac` binds
`f"{token}|{approval_id}|{target_revision}"`. Per Codex, **do not overload
`target_revision`** for infra plans. Add a **typed plan-approval** record /
function alongside the existing one (rollback path untouched):

- new fields: `plan_sha256`, `plan_json_sha256`, `artifact_uri`,
  `artifact_generation`, `head_sha`, `pr_number`, `repo`;
- new `compute_plan_token_hmac(token, approval_id, plan_sha256, artifact_generation, head_sha, key)`;
- preserved properties: single-use (transactional `pending → used`), 15-min TTL,
  HMAC-stored-never-token, constant-time compare;
- **owned by the `tofu-apply` worker**, not the coordinator (§6.4) — the worker
  holds the HMAC key and runs both `create` (at `/propose`) and `claim` (at
  `/apply`). The coordinator can request a proposal and render the page but cannot
  forge a valid approval. Same trust split as `workers/rollback` today.

Binding `plan_sha256 + artifact_generation + head_sha` closes the "approve benign
plan, push malicious commit, apply" race **only because** the approved digest is
the digest of the exact binary plan later applied, that plan was produced by the
trusted plan-builder, the approval page shows the diff derived from that same
artifact, and the apply request can't substitute another artifact (§6.4).

---

## 8. Phasing — how this becomes tractable

Build and prove the dangerous floor with **hand-written PRs** before any LLM
stands on it. Each phase ships something independently useful.

### Phase A — OpenTofu layer + static gate (NO agents, NO apply automation)
- Bootstrap (out-of-band script): state bucket (versioned) + artifact bucket + KMS
  key + WIF pool for CI. The WIF OIDC provider pins **attribute conditions**
  (repo + workflow + ref + event); fork PRs get no GCP creds; no long-lived keys.
- `iac/` with `gcs` backend + `gcp_kms` state/plan encryption from t=0. These
  foundation files (lockfile, backend, encryption) are created via the
  **operator/bootstrap policy mode** (§5.1) — human-authored + CODEOWNERS-reviewed,
  not subject to the agent rules that would otherwise reject them.
- `import { }` `payment-demo`; `tofu plan` reports **zero changes**.
- CI on PRs touching `iac/`: **static HCL gate** + `tofu init -lockfile=readonly` +
  `fmt -check` + `validate` + `plan` (preview; `-lock=false` is acceptable here
  since Phase A has no authoritative-artifact apply yet — the artifact protocol
  lands in Phase C).
- **Ships:** a real, encrypted, version-controlled IaC layer with the
  code-execution surface already closed. **Risk:** ~none.
- **Exit:** clean empty plan in CI on a no-op PR; lockfile committed; static gate
  (agent mode) rejects a provider-adding / module / provisioner / out-of-`iac/` PR
  while bootstrap mode permits the foundation; provider license confirmed; full
  existing suite green; Codex sign-off.

### Phase B — Full-project reader (the original ask)
- `infra-reader` worker (`/describe`) + `tofu_read_project` tool + add to
  `explore`. Merge `tofu show -json` (managed) with GCP describe (unmanaged).
- **Ships:** chat answers "explain my whole infra" properly. **Risk:** none.
- **Exit:** read-only disjointness pin still green; multi-resource inventory in
  prod; Codex sign-off.

### Phase C — Gated-apply machinery, driven by HAND-WRITTEN PRs
- **First prove the artifact protocol (§6):** trusted plan-builder, immutable
  artifact storage + metadata, approval binding, artifact retrieval + digest
  verification, denylist re-run, freshness check, saved-plan apply.
- Then: denylist module + CI policy check; `tofu-apply` worker (broad IAM, private
  ingress, fail-closed gates, **minimal surface**); plan-bound HMAC schema;
  approval-page wiring; merge-the-approved-head-SHA-after-apply ordering.
- Tested by the **operator opening IaC PRs by hand** and approving them. No agent
  authoring yet.
- **Ships:** safe, human-driven, project-wide infra edits through the gate.
  **Risk:** the dangerous part — every gate proven here on human-authored PRs.
- **Exit:** hand-written benign PR applies end-to-end after approval, then merges;
  a control-plane / IAM / delete PR is rejected by the denylist in BOTH CI and the
  apply worker; a replayed / expired / wrong-plan-digest / wrong-head-SHA approval
  is rejected; a drifted target is refused by the freshness check; Codex sign-off.

### Phase D — Agent authoring + fan-out (the full vision)
- `provision` workload; coordinator decomposition + parallel editor sub-agents;
  `tofu-editor` worker opens the ONE PR. Phase C apply path reused **unchanged**.
- **Ships:** "spawn agents to edit the whole infra," gated by a proven floor.
- **Exit:** chat request → correct one-PR plan that applies after approval, then
  merges; prompt-injection attempts to author control-plane / provider-adding /
  provisioner edits are caught by the static gate + denylist; Codex sign-off.

---

## 9. Security analysis (corrected per Codex rev-1)

**What the gates DO protect against:**
- **Compromised coordinator / prompt injection / compromised editor agent:** can
  only cause PR text to be authored. The static gate (§5.1) prevents that text from
  introducing executable code (providers/modules/provisioners); the denylist
  prevents control-plane/IAM/destructive changes; the human approves the diff; the
  apply worker independently re-checks everything against the pinned artifact. The
  coordinator and editors hold no apply credentials **and no HMAC key** — the
  apply worker owns approval creation/verification (§6.4, §7), so a compromised
  coordinator cannot forge a valid plan approval; the worst it can do is request a
  proposal for a PR that still has to pass the static gate, denylist, and a human.
- **Confused-deputy / stale-or-replayed approval:** closed by single-use + TTL +
  `plan_sha256 + artifact_generation + head_sha` binding + the apply worker
  resolving the artifact from the approval record (caller can't substitute a URI).

**What the gates DO NOT protect against (honest statement):**
- **Arbitrary code execution *inside* the `tofu-apply` worker.** If that process is
  compromised, it has the broad GCP credentials by definition and can call GCP APIs
  directly, outside `tofu apply` — plan-pinning does not contain that. (Rev-1 §9
  overstated this; corrected.) Mitigations are **minimization**, not gates: tightest
  possible IAM on the apply SA, the denylist's hard-deny of self-IAM changes,
  **private ingress** (no public URL), no shell/provisioner execution in the image,
  full audit logging, and keeping the apply worker's code surface as small as
  possible. The plan-builder identity's state-bucket write scope is likewise
  minimized to lock+state only.

---

## 10. Testing strategy

- **Unit (coordinator):** registry resolution of new tool/workers; read-only
  disjointness pin extended to `tofu_read_project`; plan-bound HMAC positives +
  negatives (replay, wrong plan_sha256, wrong artifact_generation, wrong head_sha,
  expired) mirroring `test_rollback.py::test_execute_rejects_wrong_revision_token`.
- **Unit (static gate):** PRs that add a provider / use a remote module / contain a
  provisioner / touch outside `iac/` / change the lockfile are each rejected;
  a clean `iac/`-only PR passes.
- **Unit (denylist):** table-driven plan-JSON fixtures — benign passes; each
  protected class + any IAM + delete/replace are rejected; unparseable/empty = deny.
  FAKE fixtures only (add to `.gitguardian.yaml` if any look secret-shaped).
- **Unit (workers):** `tofu-apply` re-check matrix (artifact-generation,
  digest-mismatch, label, base, head, green, approval, freshness — each
  independently fail-closed); `tofu-editor` branch/base/path allowlist.
- **Integration:** CI workflow on a no-op PR yields empty plan; artifact uploaded
  with metadata; policy + static gate wired.
- **No live apply in automated tests** — apply is exercised manually in Phase C and
  via the e2e target project, never in unit/integration CI.

---

## 11. Decisions (open questions from rev-1, now resolved)

1. **Encryption key provider:** `gcp_kms` (required `kms_encryption_key` +
   `key_length`; KMS key bootstrapped out-of-band). ✅
2. **Denylist engine:** custom Python for v1. ✅
3. **Destructive actions:** hard-deny `delete`/`replace` in v1; relax later behind
   the same human gate. ✅
4. **CI plan creds:** NOT read-only with the GCS backend (lock needs object write).
   Use a **trusted plan-builder** (WIF, scoped state+artifact write) as part of the
   apply gate; its artifact is the authoritative one. ✅
5. **Reader SA scope:** per-API viewer roles, not project-wide `roles/viewer`. ✅
6. **PR merge/apply order (Codex rev-2 — do NOT say "never"):** apply the approved
   head SHA, then immediately attempt merge with `sha=head_sha`. If the merge fails,
   **block + alert and leave the PR open as the reconciliation record.** The
   workflow *minimizes* `main`↔live divergence; it cannot *eliminate* it (apply can
   succeed and the subsequent merge then fail). ✅
7. **`WorkloadSpec.name` Literal** must be extended to add `provision`
   (`agent/workloads/spec.py:73`). ✅ (noted for Phase D)
8. **CI/WIF safety (Codex rev-2, Phase A):** do **not** use `pull_request_target`
   with PR-controlled code (that runs untrusted code with secrets). The WIF OIDC
   provider must pin **attribute conditions** for repo + workflow + ref + event.
   The Phase C authenticated plan-builder MUST use a **trusted trigger**
   (trusted-branch push / maintainer `workflow_dispatch` / `workflow_run`), **NOT**
   fork-PR OIDC, because `repository ==` cannot filter fork PRs: on the
   `pull_request` event the `repository` claim is the base repo even for a fork PR,
   so the condition must gate on `event_name`. **The `pull_request` event gets NO
   GCP credentials** at all. No long-lived service-account keys anywhere. ✅

---

## 12. YAGNI / explicitly out of scope (v1)

- No multi-cloud (GCP only), no multi-project, no per-env `tofu workspace` split.
- No automated apply without human approval; no agent-initiated `destroy`.
- No importing the whole project up front — incremental, edit-targets first.
- No OPA/Conftest unless the custom checker proves insufficient.
- No self-modification of DriftScribe's own infra through this pipeline — exactly
  what the denylist forbids.
- No remote modules / external providers / provisioners — forbidden by the static
  gate.
