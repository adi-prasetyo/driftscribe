# Phase C6 — Head-config delivery (merge-then-apply-from-main) + create-class e2e

> **For Claude:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development to
> implement this plan slice-by-slice; the two sole-mutator edits (C6a worker gate,
> C6b/C6d guard relaxation) get a multi-lens adversarial review.

**Status:** DRAFT rev-2 (Codex design consult `019e7a75` + implementation-plan Codex
review `019e7c24` folded: 5 blockers + 6 importants + 3 nits). Author: agent.
Date: 2026-05-31.
**Predecessors (all merged; C4+C5 proven live):** C2 `a689d8e`, C3 `180281c`,
C4 `fd9bc32`, C5a–g (PRs #21–#33), C5g carry-forwards P1a/P1b/P2/P3/P4/P5 (PRs #35–#40).
**Design parent:** `docs/plans/2026-05-30-infra-iac-phase-c5-coordinator-integration.md`
§2 (the C5↔C6 line) + §3.4 (the locked C6 mechanism).

**Goal:** Let the gated pipeline apply plans that **create new top-level resources**
(today fail-closed by the fidelity gate) by **merging the approved PR to `main`
first, re-baking the worker `iac/` from the new `main`, then applying the saved
plan against a baked config that now declares the resource** — with a deterministic
`iac/`-subtree content-hash gate proving the re-baked config is byte-for-byte the
approved head's config. (Provider/lockfile changes and modules are explicitly out of
scope — see §1.)

**Architecture:** No new mutator credential, no new egress, no request-time
PR-controlled HCL, no `c2.v1`/`c3.v1` wire change. A new C2 **sidecar artifact**
(`iac-tree.json`) carries the canonical `iac/`-tree hash; the worker derives the
sidecar URI from the HMAC-signed metadata path, fetches it by generation,
cross-checks its fields against the signed metadata, then compares its
`iac_tree_hash` to the worker's **own baked `IAC_DIR` hash**. The worker hash
compare is the security gate (the coordinator recompute is informational). Modules
stay forbidden. The operator-driven re-bake is unchanged `gcloud builds submit`
(the coordinator never gets `cloudbuild.builds.editor`). It is a **two-step operator
flow**: approve→merge, then operator re-bake, then resume→propose→apply.

**Tech stack:** Python 3.12, FastAPI, OpenTofu 1.12.0, GCS (pinned-generation
fetch + Object Versioning), Cloud Run, GitHub Actions WIF, pytest. `uv run pytest`.

---

## 1. The C5↔C6 line (what changes and why)

C5 applies the **saved** plan against a worker baked from `main`. The fidelity gate
(`workers/tofu_apply/tofu_runner.py:resource_set_guard`) fail-closes any plan that:

- has a `create` action (a new resource the baked `main` doesn't declare),
- touches a `module.*` address (no module-aware extraction), or
- touches a normalized address not in the baked declared set.

So C5 ships exactly: **no-ops + in-place updates of `main`-declared resources**. A
PR that *adds* a resource produces a `create` action → refused. C6 unblocks creates
by the §3.4 mechanism: **merge first → re-bake → apply**. After the merge+re-bake,
the new resource is declared in the baked `main`, so the create can pass the gate —
but only once we *prove* the re-baked config equals the approved head's config. That
proof is the new **`iac/`-tree content-hash gate** (I5).

**Why a content hash, not a commit SHA (§3.4 I5):** the repo squash-merges, so the
merged-`main` commit SHA ≠ `head_sha`. Only the `iac/` *tree content* is invariant.
The `iac.yml` diff-guard already proves the PR touches ONLY `iac/`, so the approved
head's `iac/` tree == the merged-`main` `iac/` tree (absent an interleaving `iac/`
merge — which the hash gate then *catches*, see §3.6).

**Scope (locked) — TOP-LEVEL CREATES ONLY:**

- **MODULES STAY FORBIDDEN** — `resource_set_guard` keeps refusing `module.*`
  unconditionally, and `tools/iac_static_gate.py` keeps banning non-`.tf` files /
  `module` blocks in agent mode. Re-baking does NOT unlock modules (no module-aware
  address extraction exists). The §3.4 phrase "module changes" is inaccurate and is
  corrected here.
- **PROVIDER / LOCKFILE CHANGES ARE OUT OF SCOPE (Codex blocker 1).** `.terraform.lock.hcl`
  (and `versions.tf`/`providers.tf`/`variables.tf`/`imports.tf`) are in the static
  gate's `PROTECTED_FOUNDATION` set (`tools/iac_static_gate.py:58`), so an
  **agent-mode** PR that changes them is refused at C2 with `foundation-edit-agent-mode`
  — they never produce a plan to apply. A lockfile change also wouldn't be caught by
  the create-class classifier (no `create` action), would take the C5 apply-first
  path, and the worker would refuse on the `provider_lockfile_sha256` fidelity
  mismatch *before* any merge/re-bake. Supporting provider/lockfile changes needs its
  own classifier + static-gate policy + fidelity path; it is an explicit **fast-follow,
  NOT C6**. C6's classifier keys on **create only**; lockfile changes stay refused
  (and that is correct).

---

## 2. Threat model for the new surface (why this is safe)

The danger C6 introduces: applying a `create` whose **embedded config came from a
PR** (the saved `plan.tfplan` embeds a config snapshot). We neutralize it with three
independent facts, all enforced **at the worker** (the sole mutator):

1. **A create's address must be in the baked declared set.** The baked set comes
   from `main` (CODEOWNERS-/branch-protected, CI-static-gated, re-baked by the
   operator). A create is admitted only after `main` declares the resource — i.e.
   only after the PR merged. (C5 behavior preserved: not-yet-merged ⇒ not declared
   ⇒ refused.)
2. **The baked `iac/` tree hash must equal the sidecar's `iac_tree_hash`.** This
   binds "the config I'm baked from" to "the config that was reviewed+approved in
   the PR head." Defeats applying a saved plan against a *different* `main` that
   merely happens to declare the same address (e.g. `main` advanced after merge, or
   an interleaving `iac/` PR).
3. **The sidecar is integrity-bound to the signed metadata.** The sidecar URI is
   derived from the HMAC-signed `metadata.json` path; the worker fetches it by the
   coordinator-supplied generation and **cross-checks every shared field**
   (`repo`, `pr_number`, `head_sha`, `base_sha`, `workflow_run_id`,
   `workflow_run_attempt`, `plan_sha256`, `plan_json_sha256`) against the signed
   metadata. The artifact bucket is writable only by the WIF plan-builder SA
   (coordinator + worker have `objectViewer` only), so a compromised coordinator can
   at most point at a *legitimate* C2 sidecar — and any sidecar for a different plan
   fails the field cross-check.

**Coupling rule (the crux):** whenever `resource_set_guard` *admits a create*, the
hash gate (2) MUST have passed. A create-class plan with no sidecar / a mismatched
hash is refused. Non-create (C5) plans do NOT run the hash gate — for a not-yet-
merged PR the baked `main` ≠ head by construction, so the hash *must* differ; C5's
existing fidelity gate (version + lockfile + resource-set-subset) governs there,
unchanged.

**The `generation_iac_tree` endpoint field is unsigned, and that is safe (Codex
IMPORTANT 1):** because the worker derives the sidecar *path* from the HMAC-signed
metadata URI and cross-checks every shared field (2)(3), a compromised coordinator
can at most supply a *wrong* generation (a different version of the same path object)
or *omit* it. Both outcomes are **refusal / availability** (the field cross-check or
the hash compare fails closed), never a bad apply — the unsigned field can DoS an
apply but cannot subvert one. Tests assert: wrong-generation-same-path →
field-mismatch refusal; missing generation on a create-class apply → refuse
(pre-mint at `/propose`, burned at `/apply`).

**Residual (documented, accepted):** in-worker compromise stays un-gatable by the
approval flow (design §9) — minimization only. The bearer-replay residual (C5 OD-1)
is unchanged. Main advancing between merge and re-bake is *caught* by the hash gate
(refusal, not a bad apply) but is operator friction (re-plan required).

---

## 3. Design decisions

### 3.1 The canonical `iac/`-tree hash (one function, both sides) — C6a
A single pure function `driftscribe_lib/iac_tree.py:iac_tree_hash(iac_dir: Path) -> str`
run **identically** by C2 (over the checked-out `iac/`) and the worker (over baked
`IAC_DIR`). Definition:

- Walk `iac_dir` recursively; **include EVERY regular file** whose relative POSIX
  path is not in the exclude set — not just `*.tf`. The committed `iac/` universe is
  `*.tf` + the `PROTECTED_FOUNDATION` files (`.terraform.lock.hcl`, `versions.tf`,
  `providers.tf`, `variables.tf`, `imports.tf`) + any `*.md`; hashing *all* files
  (minus the generated excludes) means a stray `.tofu`/`.tf.json`/`.tfvars` is
  hash-bound too even though the agent-mode static gate already rejects those
  (Codex IMPORTANT 4 — define the full file universe, don't enumerate suffixes).
  Record `(rel_posix_path, sha256_hex(file_bytes))`.
- **Symlinks (Codex IMPORTANT 3):** use a NON-following type check
  (`os.lstat`/`Path.is_symlink()`); the function **raises `ValueError` (fail-closed)
  on any symlink under `iac/`**. Rationale: C2 (a git checkout) and the Docker bake
  could resolve a symlink differently, so a silently-followed link is a hash-
  divergence foot-gun; agent-mode PRs only add plain `.tf`, so a symlink is already
  anomalous. (Non-regular files — fifos/sockets/devices — likewise raise.)
- **Exclude** (generated / non-committed — the byte-identical contract on both
  sides): the `.terraform/` directory (provider cache, regenerated by the bake's
  `tofu init`), `plan.tfplan`, `plan.json`, `metadata.json`, `iac-tree.json` (C2/apply
  artifacts), and `*.tfstate` / `*.tfstate.backup` (defensive — never present with the
  remote backend). `.terraform.lock.hcl` is **included** (committed + material; also
  gated by the fidelity `provider_lockfile_sha256` — belt-and-suspenders).
- File **modes are NOT hashed** (content + relative path only) — OpenTofu ignores
  `.tf` file modes, and git/Docker mode handling differs.
- Fold into a final digest: `sha256( Σ over sorted entries of
  rel_path + "\0" + file_sha256_hex + "\0" )`. Sorted by `rel_path` (byte order of
  the POSIX string) for determinism. Empty `iac/` → the digest of the empty stream.

The exclude set is a frozen module constant with a "DO NOT change without bumping
the sidecar `schema_version` AND re-baking" comment. A mismatch between the two
sides' file universe is almost always an **over-refusal** (always-mismatch ⇒ refuse,
the fail-closed direction); a false-clean would require a deliberately broken hash
definition or a malicious code change — still, the single-shared-function discipline
(below) is what keeps the two sides identical (NIT 1).

> **Why a filesystem walk (not `git ls-tree`):** the worker has no git. Both sides
> run the *same* directory-walk function, so the only requirement is that the
> exclude set drops exactly the non-committed files. Computed identically ⇒
> identical result for identical committed content.

### 3.2 The C2 sidecar artifact `iac-tree.json` (`c6.v1`) — C6a
A NEW artifact alongside the `c2.v1` triplet, in the same run dir
(`pr-<N>/<head_sha>/run-<id>-<attempt>/iac-tree.json`). Lib-owned schema
`driftscribe_lib/iac_tree.py:build_sidecar` / `serialize_sidecar` (mirrors
`iac_plan_metadata.py`), `schema_version = "c6.v1"`, fields (all required):
`repo, pr_number, head_sha, base_sha, workflow_run_id, workflow_run_attempt,
plan_sha256, plan_json_sha256, iac_tree_hash`. Every shared field MUST be derivable
from / equal to the `c2.v1` metadata (so the worker's cross-check is total).

- **NOT in `c2.v1`/`c3.v1`** — those wire formats stay frozen (no field added to
  `iac_plan_metadata.py`, no field signed in `approvals.py`). The sidecar is a
  separate artifact; the only coordinator→worker addition is the sidecar
  *generation* as an **additive, unsigned endpoint field** (§3.5). Integrity comes
  from the field cross-check against the signed metadata + bucket IAM, not signing.

### 3.3 The worker hash gate + guard relaxation (sole-mutator edit) — C6a
**`plan_has_create` lives in `driftscribe_lib` (Codex blocker 3).** The coordinator
image (`Dockerfile.agent`) copies `agent/`, `checker/`, `driftscribe_lib/` — **NOT
`workers/`** — so a predicate the coordinator (routing) AND the worker (gate) must
share CANNOT live in `workers/tofu_apply/tofu_runner.py`. New module
`driftscribe_lib/iac_plan_classify.py`:
- `plan_has_create(plan_json) -> bool` — pure, fail-closed: True if any managed
  `resource_changes` entry's `actions` contains `"create"` (so a `replace` =
  `["delete","create"]` OR `["create","delete"]` counts as create-class — Codex
  blocker/question 3). A `module.*` address with a create, or any malformed/
  unexpected entry, ⇒ True (route through the stricter create path, fail-closed).
  no-op/read are ignored.
- Unit-tested in `driftscribe_lib` so both deployables import the identical predicate.

`workers/tofu_apply/`:
- `gcs_fetch.py`: add `iac-tree.json` to `_OBJECT_RE` (so `validate_artifact_uri`
  accepts the sidecar basename); a derive-sidecar-URI-from-metadata-URI helper
  (string-replace the trailing `metadata.json` → `iac-tree.json`, then re-validate
  through `validate_artifact_uri`).
- `tofu_runner.py`:
  - `resource_set_guard(plan_json, declared, *, allow_create_of_declared: bool)` —
    NEW keyword. Refactor the body so that, per non-no-op/read change, **the
    `module.*` refusal is checked FIRST** (before any create admission — Codex
    IMPORTANT 5), then a change is admitted iff its normalized address ∈ `declared`;
    a `create` action additionally requires `allow_create_of_declared=True`.
    address-not-declared still refused. **Default `allow_create_of_declared=False`
    preserves C5 behavior exactly** (every existing caller/test). Imports
    `plan_has_create` from the lib for the worker-side classification.
  - `assert_fidelity(..., allow_create_of_declared: bool = False)` — thread the flag
    through to `resource_set_guard`.
  - **`extract_declared_addresses` stays a regex (`tofu_runner.py:188`); the hash
    gate now carries the real security weight, not the parser** (Codex IMPORTANT 5).
    Add robustness tests for the regex (commented-out `resource` blocks, heredocs
    containing `resource "..."`, duplicate declarations) so a parser miss can only
    *over-refuse* (address-not-declared), never admit an undeclared create.
- `main.py`:
  - `ProposeRequest` / `ApplyRequest` += `generation_iac_tree: str | None = None`
    (additive; `extra="forbid"` unchanged — a schema field, not a wire-format break).
  - A `_verify_iac_tree_or_raise(bucket, signed_md, *, generation_iac_tree) -> str`
    helper: derive sidecar URI from `signed_md["artifact_uri_metadata"]`, fetch
    @generation, parse, **cross-check all shared fields == `signed_md`**, compute
    `iac_tree_hash(IAC_DIR)`, compare to `sidecar["iac_tree_hash"]`. Returns the
    matched hash on success; raises a new `tofu_runner.IacTreeMismatch` (or reuses
    `FidelityError` with a distinct prefix) on any failure.
  - Wire it into BOTH `/propose` and `/apply` (Codex IMPORTANT 2 — `/propose` runs
    `_fidelity_or_raise` before minting, so without the same branch a create-class
    plan never mints after re-bake): compute `has_create =
    iac_plan_classify.plan_has_create(parsed_plan_json)`. If `has_create`: the sidecar
    generation is MANDATORY (absent ⇒ refuse), run the gate, set
    `allow_create_of_declared=True` for `_fidelity_or_raise`. If not create-class:
    skip the gate, `allow_create_of_declared=False` (C5 path untouched).
  - New terminal phase **`tree_mismatch_refused` (HTTP 409)** in `/apply` (the
    baked config ≠ approved head config — needs a re-bake or a re-plan, NOT a blind
    retry); `/propose` returns 422 (pre-mint, nothing burned). Add
    `tree_mismatch_refused` to `APPLY_AUDIT_PHASES` in `driftscribe_lib/approvals.py`.

**This edits the sole mutator → dedicated multi-lens adversarial review** (alongside
the C5g sole-mutator review discipline).

### 3.4 Coordinator: create-class routing + the merge-first state machine — C6b
`agent/main.py` POST `/iac-approvals/{pr_number}`. **The ordering must change (Codex
blocker 2):** today the POST runs PR readiness (`assert_pr_ready_at_sha`, ~line 2376)
BEFORE the idempotency-decision lookup (~2408), and the "already merged" branch
treats ANY `merge_state=="merged"` as "already applied and merged" (~2410). After
merge-first, on the **resume** re-POST the PR is merged/closed → `assert_pr_ready_at_sha`
would fail before the `waiting_for_rebake` decision is ever seen, and the
`merged`→"already applied" branch would swallow the resume. So:

- **Re-order: idempotency-decision lookup moves BEFORE PR-readiness.** Branch on the
  existing decision's `apply_status` FIRST:
  - `applied` + `merge_state=="merged"` → "Already applied and merged" (narrow the
    existing branch from `merged` to `applied && merged`).
  - **`waiting_for_rebake`** (create-class, merged, not yet applied) → **the resume
    path** (below): skip merge, **skip `assert_pr_ready_at_sha`** (the PR is merged/
    closed — readiness was already asserted pre-merge), go straight to propose→apply
    with the stored `generation_iac_tree`.
  - `applied` + `merge_state=="failed"` → merge-only reconcile (existing C5 path).
  - terminal `failed`/`failed_state_suspect`/`ambiguous` → terminal render (existing).
  - no decision yet → the FRESH path: classify create-class, then route.
- **Classify create-class** from the resolved plan view via the shared
  `iac_plan_classify.plan_has_create` predicate (the GET path already fetches
  `plan.json` for the diff; expose `IacPlanView.has_create`). Coordinator + worker use
  the identical lib predicate.
- **Fresh, non-create (C5):** unchanged apply-first→merge flow (readiness → propose →
  apply → merge), all existing ordering intact.
- **Fresh, create-class (C6) — step 1 of the two-step flow:** Origin/CSRF/dry-run/
  re-resolve/pin **+ readiness** (still asserted here, while the PR is open), then
  **instead of propose→apply, MERGE FIRST** — `github.merge_pr_at_sha(...)` at
  `view.head_sha`. Record `apply_status="waiting_for_rebake"` + `merge_state="merged"`
  carrying `{head_sha, generation_metadata, generation_iac_tree, iac_tree_hash,
  sidecar_uri, approver}`. Render the **two-step instruction page**: "Merged to
  `main`. Re-bake the worker (`gcloud builds submit
  --config=infra/cloudbuild.tofu-apply.yaml --substitutions=_TAG=$(git rev-parse
  --short HEAD)`), then re-open this approval and click Apply." Surface the expected
  `iac_tree_hash` for operator verification (+ the §3.5 poll). If the merge ITSELF
  fails here, NOTHING was applied → record a `waiting_for_rebake`-with-merge-failed
  (or just surface the merge error + release) — no mutation occurred, so this is the
  benign direction.
- **Resume (step 2):** propose→apply against the re-baked worker, **passing
  `generation_iac_tree`**; the §2 worker hash gate is the real safety (it fail-closes
  if the operator hasn't re-baked, or re-baked from the wrong `main`). On `applied` →
  record `applied`/`merged`. On a refusal → §3.6 matrix.
- `agent/worker_client.py`: `call_propose` / `call_apply` += optional
  `generation_iac_tree` param, forwarded as the additive endpoint field.
- **Idempotency / crash-recovery:** the event key is unchanged (`{repo,pr,head_sha,
  gen}`). The `waiting_for_rebake` decision makes the merge idempotent — a duplicate
  approve (or a crash after merge before the resume) re-enters via the decision lookup
  and lands on the resume path; the merge never re-runs. A crash mid-resume leaves
  `waiting_for_rebake`; re-POST resumes (the worker's claim-first single-use burn +
  the §3.6 matrix handle a partially-applied resume).

### 3.5 Surfacing the sidecar generation + hash to the coordinator + re-bake readiness — C6b/C6c
**The coordinator's `iac_tree_hash` source (Codex rev-2 watch-item):** the C2 PR
comment carries THREE new lines — `generation_iac_tree`, `artifact_uri_iac_tree`, AND
**`iac_tree_hash`** — and `C2CommentRef`/`IacPlanView` parse all three. So the
coordinator reads the expected hash from the (CI-produced) comment for the
instruction page + the CSRF pin + the §3.5 poll, WITHOUT fetching the sidecar itself
(the worker is the party that fetches + cross-checks the sidecar — the coordinator
recompute/echo is informational, §2). The hash is bound for operator-review integrity
by the CSRF pin; it is NOT a worker security input.
- `tools/iac_plan_diff_summary.py` += `--generation-iac-tree` +
  `--artifact-uri-iac-tree` + `--iac-tree-hash` → three new lines in the PR comment.
- `.github/workflows/iac.yml`: compute `IAC_TREE_HASH=$(python -m tools.iac_tree_hash
  iac)` (any point after the diff-guard — the excludes make `.terraform/`/`plan.*`/
  `metadata.json` immaterial, but running it post-diff-guard hashes the verified head
  tree); after the metadata upload, build the sidecar via `python -m
  tools.iac_tree_sidecar` (reads the same `META_*` env + `IAC_TREE_HASH`), upload
  `iac-tree.json` via `tools/iac_plan_artifact_upload.py` (a `--local-iac-tree` /
  `--mode iac-tree`), capture `GEN_IAC_TREE`, pass it + `IAC_TREE_HASH` to the diff
  summary.
- `agent/iac_artifacts.py`: `C2CommentRef` += `generation_iac_tree` + regex; thread
  through `IacPlanView` (`generation_iac_tree` property). The coordinator's POST pin
  block (lines ~2360) gains `generation_iac_tree` equality.
- **`agent/iac_csrf.py` pin (Codex IMPORTANT 6):** the CSRF form token already pins
  `head_sha`/`generation_metadata`/`plan_sha256`/`plan_json_sha256`/`comment_id`. Add
  `generation_iac_tree` + `iac_tree_hash` to the signed token so "what the operator
  saw on the page" includes the sidecar identity. Worker safety does NOT depend on
  this (the worker re-derives + cross-checks), but it completes the operator-review
  integrity contract — the operator can't approve a page whose sidecar was swapped
  under them.
- **(C6c, optional but recommended) re-bake readiness signal:** a worker
  `GET /baked-iac-hash` → `{ "iac_tree_hash": iac_tree_hash(IAC_DIR) }` (no auth
  beyond the existing `verify_caller`? — it leaks only a content hash; keep it behind
  `verify_caller` for symmetry). The coordinator's resume can **poll** this until it
  equals the approved `iac_tree_hash` before driving apply, turning "did the operator
  re-bake yet?" into a deterministic check. If omitted, the worker's apply-time hash
  gate still fail-closes a not-yet-re-baked apply (just with a less friendly message).

### 3.6 Post-merge failure handling (no auto-revert) — C6d
Locked: **treat merged `main` as desired state; never auto-revert a merge.** Per
failure class on the *resume* apply:

| Worker outcome | Mutation? | Coordinator decision | Operator action |
|---|---|---|---|
| `tree_mismatch_refused` (409) | none | `waiting_for_rebake` (kept) | re-bake from current `main`; if `main` advanced with another `iac/` change, **re-plan** (fresh C2 run) — the hash can't match a stale head |
| `lock_refused` (423) | none | `waiting_for_rebake` (kept) | `force-unlock` + resume |
| `drift_refused` (409) | none | `waiting_for_rebake` (kept) | reconcile drift + resume |
| `integrity_refused`/`fidelity_refused`/`verify_refused` (422) | none | `waiting_for_rebake` (kept) | investigate artifact/bake; re-bake/re-plan |
| `failed` (502, "clean") for a **create-class** resume | **possible live ORPHAN** | **`failed_state_suspect`-equivalent (terminal, FREEZE)** | **live reconcile** — see below |
| `failed` (502, clean) for a non-create resume | none proven | `failed` (terminal) | retry resume if saved plan still valid, else re-plan |
| `failed_state_suspect` (502) | possible | `failed_state_suspect` (terminal, freeze) | **state reconcile** (apply-failure runbook) before anything |
| `ambiguous` (504/5xx) | unknown | `ambiguous` (terminal, freeze) | manual verification |
| `applied` (200) | yes | `applied`/`merged` | done |

**Create-class `failed` is NOT retryable (Codex blocker 4).** A failed `tofu apply`
that *creates* a resource can leave a **live object that was never written to state**
(created at the provider, then the apply errored before persisting) — an ORPHAN. The
worker's post-failure diagnosis (`_diagnose_post_failure_state`, `tofu_runner.py:613`)
proves only state serial/lineage stability + refresh-only cleanliness; it CANNOT
prove "no orphan live resource was created" (a resource absent from state is absent
from the refresh too). So for a **create-class** resume, even a state-clean `failed`
must **freeze + require live reconciliation** (operator checks the provider for the
orphan, imports or deletes it), exactly like `failed_state_suspect` — never the
"retry if saved plan valid" path. The coordinator knows the resume is create-class
(it routed it), so it records the freeze decision + the orphan-check instruction.

**The reconcile-plan gap (Codex-flagged):** C2 is open-PR-oriented; after merge the
PR is closed, so a "re-plan from current `main`" needs a fresh PR (revert-the-revert
or a no-op edit) re-run through C2, OR a manual runbook. C6d ships the **manual
runbook** (extend `docs/runbooks/iac-apply-failure-recovery.md` with a "merged but
not fully applied" section, incl. the create-class orphan-check procedure); an
automated post-merge reconcile-plan is explicitly a **fast-follow, not C6**.

### 3.7 What C6 does NOT change (invariants)
`c2.v1` (`iac_plan_metadata.py`) + `c3.v1` signed payload (`approvals.py`) wire
formats; the four locked-floor primitives (denylist re-run, saved-plan-apply-no-
re-plan, claim-first single-use burn, private ingress); the freshness gate; HMAC
binding/domain separation; **modules forbidden**; **provider/lockfile changes
out of scope** (§1); no new mutator credential / egress / PR-controlled-HCL surface;
coordinator never gets `cloudbuild.builds.editor`. The C5 apply-first→merge path for
non-create plans is byte-for-byte unchanged (the new `allow_create_of_declared`
flag defaults False; the hash gate runs ONLY for create-class plans).

---

## 4. Slice plan (TDD; PR + Codex review each; adversarial review on C6a)

> House cadence (matches PRs #35–#40): branch → red test → impl → green → Codex
> `codex-reply` on the same thread → (sole-mutator) adversarial Workflow review →
> push → CI green → admin-squash-merge. `uv run pytest` is the suite (1680 green
> today). Each slice is independently mergeable + reversible.

### Slice C6a-1 — `iac_tree_hash` + sidecar schema (lib, pure, offline)
**Files:** Create `driftscribe_lib/iac_tree.py`; Create
`tests/unit/test_iac_tree_hash.py` + `tests/unit/test_iac_tree_sidecar.py`.
**Tests (red→green):** determinism (same dir → same hash; reordered file creation →
same hash); exclude-set correctness (adding `.terraform/x`, `plan.tfplan`,
`plan.json`, `metadata.json`, `iac-tree.json`, `*.tfstate` does NOT change the hash;
adding/editing a `.tf` or `.terraform.lock.hcl` DOES); empty dir; nested dirs; a file
whose content == another's but different path → different hash (path-bound);
`build_sidecar` validates every field (reuses the `iac_plan_metadata` regexes) and
raises `ValueError` on malformed; `serialize_sidecar` round-trips byte-identically
(sorted keys + trailing newline).
**Commit:** `feat(iac): C6a-1 — canonical iac/-tree hash + c6.v1 sidecar schema (lib)`.

### Slice C6a-2 — sidecar + hash CLIs + C2 workflow wiring + comment producer/consumer (tools + CI + parser)
Pick ONE CLI name per tool (NIT 2): `tools/iac_tree_hash.py`
(`python -m tools.iac_tree_hash <dir>` → prints the hash) and
`tools/iac_tree_sidecar.py` (`python -m tools.iac_tree_sidecar` → builds the sidecar
from `META_*`+`IAC_TREE_HASH` env, mirrors `tools/iac_plan_metadata.py`); both thin
re-exports of `driftscribe_lib/iac_tree.py`.
**Files:** Create `tools/iac_tree_hash.py` + `tools/iac_tree_sidecar.py`; Modify
`tools/iac_plan_artifact_upload.py` (accept the sidecar via a `--local-iac-tree` /
`--mode iac-tree`); Modify `tools/iac_plan_diff_summary.py` (+`--generation-iac-tree`
+`--artifact-uri-iac-tree` + two comment lines); **Modify `agent/iac_artifacts.py`
NOW (Codex blocker 5 — producer + consumer land together): `C2CommentRef`
+= `generation_iac_tree` + `iac_tree_hash` + regexes, so the round-trip test in THIS
slice has a parser to exercise** (the `IacPlanView.has_create` + the coordinator
wiring stay in C6b-1);
Modify `.github/workflows/iac.yml` (compute `IAC_TREE_HASH` via
`python -m tools.iac_tree_hash iac` run after the diff-guard — the excludes make the
exact step position immaterial, but run it on the verified head tree; build+upload
`iac-tree.json`; capture `GEN_IAC_TREE`; feed the diff summary). **Tests:** `tools`
unit tests for the two CLIs (golden stdout/exit); `iac_plan_diff_summary` emits the
two new lines AND `parse_c2_pr_comment` round-trips them (producer+consumer in one
slice — no cross-slice red). **No live run in CI** (the workflow is exercised live in
C6e). `generation_iac_tree` is parsed as **optional** in `C2CommentRef` for this
slice (old comments without it still parse) until C6b-1 makes the coordinator require
it on the create path.
**Commit:** `feat(iac): C6a-2 — C2 emits iac-tree.json sidecar + comment lines + ref parse`.

### Slice C6a-3 — `plan_has_create` lib + worker hash gate + guard relaxation (SOLE MUTATOR)
**Files:** Create `driftscribe_lib/iac_plan_classify.py` (`plan_has_create`, shared
by coordinator + worker — Codex blocker 3); Modify `workers/tofu_apply/gcs_fetch.py`
(allow `iac-tree.json` + derive-URI helper); Modify `workers/tofu_apply/tofu_runner.py`
(`resource_set_guard` keyword + module-before-create ordering + `IacTreeMismatch`,
importing `plan_has_create` from the lib); Modify `workers/tofu_apply/main.py`
(`generation_iac_tree` fields, `_verify_iac_tree_or_raise`, create-class branching,
`tree_mismatch_refused`); Modify `driftscribe_lib/approvals.py`
(`APPLY_AUDIT_PHASES += "tree_mismatch_refused"`); confirm `workers/tofu_apply/Dockerfile`'s
`COPY driftscribe_lib/` covers the two new lib modules (`iac_tree.py`,
`iac_plan_classify.py` — both already under `driftscribe_lib/`; no new dep). **Tests
(`tests/unit/test_tofu_*` + a new `test_iac_tree_gate.py`):**
- `resource_set_guard` default (`allow_create_of_declared=False`): create→refuse
  (C5 preserved); update-of-declared→ok; `module.*`→refuse; undeclared→refuse.
- `resource_set_guard(allow_create_of_declared=True)`: create-of-declared→ok;
  create-of-UNdeclared→refuse; `module.*`→STILL refuse (module check ordered FIRST).
- `extract_declared_addresses` regex robustness (IMPORTANT 5): commented-out
  `resource` block / heredoc containing `resource "x" "y"` / duplicate decl → the
  parser only ever *over-refuses*, never admits an undeclared create.
- `plan_has_create` (lib test, `tests/unit/test_iac_plan_classify.py`): create /
  replace (`["delete","create"]` and `["create","delete"]`) / update-only / no-op /
  malformed / `module.*`-create cases.
- `_verify_iac_tree_or_raise`: matching sidecar+hash→pass; field mismatch
  (`plan_sha256`/`head_sha`/...)→raise; hash mismatch→raise; absent sidecar on a
  create-class plan→refuse; wrong basename/bucket→`GcsFetchError`.
- `/apply` end-to-end (monkeypatched fetch + `_RUN_TOFU`): create-class +
  matching sidecar → applies; create-class + mismatched hash →
  `tree_mismatch_refused` 409 + audit phase; create-class + no sidecar gen → refuse;
  non-create + no sidecar → applies (C5 path); `/propose` mirror (422 on mismatch).
- Audit/body-token contract: `tree_mismatch_refused` appears in the audit phase + the
  refusal detail.
**Adversarial review (multi-lens Workflow):** lenses = (1) can a compromised
coordinator get a create applied without a matching baked config? (2) hash-function
divergence C2↔worker (exclude-set, encoding, symlinks, dotfiles)? (3) sidecar
substitution / generation replay? (4) does the relaxation leak into the C5 path
(default flag)? (5) fail-open on parse/recursion errors?
**Commit:** `feat(iac): C6a-3 — worker iac-tree hash gate + create-of-declared guard (sole mutator)`.

### Slice C6b-1 — coordinator create-class routing + merge-first state machine
**Files:** Modify `agent/iac_artifacts.py` (`IacPlanView.generation_iac_tree` +
`.iac_tree_hash` properties + `.has_create` via `iac_plan_classify.plan_has_create`;
the `C2CommentRef` parse of all three lines already landed in C6a-2); Modify `agent/iac_csrf.py` (pin `generation_iac_tree`
+ `iac_tree_hash` into the form token — IMPORTANT 6); Modify `agent/worker_client.py`
(`generation_iac_tree` on `call_propose`/`call_apply`); Modify `agent/main.py` (the
§3.4 re-ordering: decision-lookup BEFORE readiness; narrow `merged`→`applied&&merged`;
add the `waiting_for_rebake` resume branch that skips merge + `assert_pr_ready_at_sha`;
the fresh create-class branch: readiness → merge-first → `waiting_for_rebake` decision
→ two-step render; pass `generation_iac_tree`). **Tests (`tests/unit/test_iac_*` +
the coordinator POST tests):** create-class POST → readiness asserted → merges +
records `waiting_for_rebake` + renders the two-step page (NO propose/apply called);
resume POST (decision present) → propose/apply called WITH `generation_iac_tree`,
**NO second merge, NO `assert_pr_ready_at_sha`** (PR closed); `applied&&merged` →
"already applied" (not swallowing `waiting_for_rebake`); non-create POST → unchanged
apply-first path; idempotent double-approve after merge → resume, never double-merge;
crash-after-merge re-POST → resume; CSRF token round-trips the two new pins.
**Commit:** `feat(iac): C6b-1 — coordinator merge-first routing for create-class plans`.

### Slice C6c-1 — re-bake readiness endpoint + poll (optional, recommended)
**Files:** Modify `workers/tofu_apply/main.py` (`GET /baked-iac-hash`); Modify
`agent/main.py` (resume polls the worker hash until it matches, bounded; else surface
"re-bake not detected"). **Tests:** endpoint returns the baked hash; coordinator
poll matches→proceeds / never-matches→bounded refusal. **If descoped:** the worker
apply-time gate still fail-closes; document the manual "verify the re-bake hash"
step instead.
**Commit:** `feat(iac): C6c-1 — worker baked-iac-hash endpoint + coordinator re-bake poll`.

### Slice C6d-1 — post-merge failure handling + runbook
**Files:** Modify `agent/main.py` (map the resume-apply failure classes per §3.6 —
keep `waiting_for_rebake` on no-mutation refusals incl. `tree_mismatch_refused`;
**create-class `failed` → FREEZE like `failed_state_suspect`, NOT retry, because of
the live-orphan risk — Codex blocker 4**; freeze on `failed_state_suspect`/`ambiguous`);
Modify `docs/runbooks/iac-apply-failure-recovery.md` (+"merged but not fully applied"
section: the per-class table, the create-class orphan-check procedure, the
re-plan-from-`main` procedure, the no-auto-revert rationale); Modify
`docs/runbooks/tofu-apply.md` (+ the C6 two-step re-bake step). **Tests:** each
resume-apply failure class → the right decision state + banner + alert; create-class
`failed` → freeze (not retry); idempotent re-resume after a kept-`waiting_for_rebake`.
**Commit:** `docs(iac)+fix(iac): C6d-1 — post-merge failure handling + recovery runbook`.

### Slice C6e — live create-class e2e (OPERATOR-GATED)
**Not auto-run.** A hand-written throwaway PR adding a single **free, easily-deletable,
NON-IAM, denylist-clean** resource — the C1 denylist refuses IAM/SA/control-plane
resources, so `google_service_account` is OUT (NIT 3). Candidate: a
`google_storage_bucket` (`force_destroy=true`, empty → effectively free, trivially
deletable) — **verify the exact resource against the C1 denylist + the fidelity guard
at C6e time**; final choice operator-gated. Drive: C2 dispatch → GET page shows
create-class + the two-step notice → approve → merge → **operator re-bake** → resume
→ apply `{applied}` → verify the resource exists + state serial bumped → clean up
(delete the resource via a follow-up apply or by hand; close artifacts as evidence).
**Plus negatives:** apply BEFORE re-bake → `tree_mismatch_refused`; tamper the
sidecar generation → refuse; `module.*` PR → still refused. **This creates real
infra → explicit operator go-ahead required before I drive it.**

---

## 5. Test plan (offline, CI-green target)

New offline coverage on top of today's 1680: the hash function (determinism +
exclude set), the sidecar schema (validate/serialize), the two new CLIs + workflow
comment lines, the worker hash gate + guard relaxation full matrix (the C6a-3 list),
the coordinator create-class routing + resume + idempotency, the `C2CommentRef`
round-trip, and the post-merge failure-class mapping. Structural invariants
re-asserted: `c2.v1`/`c3.v1` unchanged; `resource_set_guard` default flag preserves
C5; modules still refused on both the static gate and the worker.

---

## 6. Risks & residuals

- **Hash-function divergence C2↔worker** — the single highest risk. Mitigation: ONE
  shared lib function, an explicit frozen exclude set with a change-control comment,
  and a C6e live assert that the coordinator-recomputed hash == the
  worker-baked-hash == the sidecar hash. (Divergence over-refuses, never false-cleans.)
- **Main advances between merge and re-bake** — caught by the hash gate (refusal),
  surfaced as "re-bake/re-plan," not a bad apply. Operator friction, not a safety gap.
- **The reconcile-plan gap** (post-merge, PR closed) — manual runbook in C6d; an
  automated post-merge re-plan is a fast-follow.
- **Two-step operator UX** — heavier than C5's one-click. The `waiting_for_rebake`
  decision + the (optional) poll keep it idempotent + legible.
- **Saved-plan embedded config is PR-authored** — neutralized by the create-of-
  declared + hash gate + bucket IAM triple (§2); in-worker compromise residual
  unchanged (design §9).
- **`cloudbuild.builds.editor` deliberately withheld** from the coordinator — the
  re-bake stays operator-run; a safe future automation (coordinator merges → polls
  for a Ready revision whose baked hash matches) is C6c's poll, not a build trigger.

---

## 7. Process

Per CLAUDE.md: this plan is Codex-reviewed (`mcp__codex__codex`) before the operator
sees it; implementation is reviewed on the same thread (`codex-reply`) against this
plan; the C6a-3 sole-mutator edit gets a multi-lens adversarial Workflow review.
**Order:** C6a-1 → C6a-2 → C6a-3 (+adversarial) → C6b-1 → C6c-1 → C6d-1 → C6e
(operator-gated). C6a/b/c/d are code+test PRs; C6e is operator-live.
