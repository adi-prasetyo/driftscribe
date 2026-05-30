# Phase C5f + C5g — IAM/secret hardening + live §8 smoke (implementation plan)

Status: rev-2 (Codex round-1 folded — thread 019e7739). Parent: `2026-05-30-infra-iac-phase-c5-coordinator-integration.md`
§3.7 (C5f) + §6/§7 (C5g). Author: agent. Date: 2026-05-30.

C5c resolved the live network blocker (coordinator → internal worker over the VPC).
This phase closes the remaining C5 hardening (C5f) and proves the gate end-to-end
live with the worker now reachable (C5g). The two are filed together because the
**C5g positive apply-test IS the C5f dedicated-runtime-SA change** flowing through
the real gated pipeline.

---

## 0. Current live state (verified 2026-05-30 via operator gcloud/ADC)

| Fact | Value | Implication |
|------|-------|-------------|
| payment-demo runtime SA | `1079423440495-compute@developer.gserviceaccount.com` (default compute SA) | the over-privileged identity C5f replaces |
| payment-demo resource | `google_cloud_run_v2_service.payment_demo`, field `template.service_account` **unset**, **NOT** in `ignore_changes=[client,client_version,scaling]` | declaring it = drift today; apply-through-pipeline restores zero-diff |
| Firestore | only `(default)` (FIRESTORE_NATIVE, asia-northeast1); all collections (`events`,`decisions`,`sessions`,`approvals`,`plan_approvals`) live there | no named DB yet |
| `datastore.user` | **project-wide, UN-conditioned**, on all 3: `driftscribe-agent`, `rollback-agent-sa`, `tofu-apply-sa` | a new named DB is still reachable by the coordinator → isolation needs **conditioning**, not just a new DB |
| actAs on runtime SA | `tofu-apply-sa` **and** `rollback-agent-sa` both hold `iam.serviceAccountUser` on the default compute SA | a dedicated SA must re-grant BOTH (§3.7 names only tofu-apply-sa) |
| tofu-apply worker (LIVE) | rev `driftscribe-tofu-apply-00003-fqr`, image **`fd9bc32` (C4 code)**, env has **no** `CF_ACCESS_*`/`IAC_OPERATOR_AUTH_MODE` | worker is **pre-C5**; C5b-2 operator-JWT re-verify + C5d lock_refused **not deployed** → must redeploy before C5g |
| coordinator | rev `00025-car` (VPC), `TOFU_APPLY_URL` set ✓ | C5e POST target wired |
| coordinator SA on tofu-artifacts bucket | **NOT bound** (only `tofu-plan-builder`=objectCreator, `tofu-apply-sa`=objectViewer) | C5e GET/POST artifact fetch needs `objectViewer` → add |
| `GITHUB_REPO` (live) | `adi-prasetyo/driftscribe` | PAT scope target |
| `github-pat` | classic PAT, over-scoped (`repo`/`workflow`/`admin`/`push`) on driftscribe + driftscribe-e2e-target | rotate to fine-grained, single-repo |

**Knowledge facts verified:**
- Firestore named-DB IAM isolation: a project-level `roles/datastore.user` reaches **all** databases; to deny one, scope the grant with an IAM **condition** `resource.name == "projects/<P>/databases/<DB>"` (documented per-database isolation pattern).
- GitHub fine-grained PAT permissions (authoritative `permissions-required-for-fine-grained-personal-access-tokens`): merge `PUT …/pulls/{n}/merge` = **Contents: write**; get PR = **Pull requests: read**; list check-runs = **Checks: read**; combined status = **Commit statuses: read**. (Live fallback: if a merge 403s, the merge perm may also want Pull requests:write — bump then.)

---

## 1. Scope

**C5f (this phase):**
1. **Dedicated minimal `payment-demo` runtime SA** replacing the default compute SA — created out-of-band; the service-side repoint is applied **through the C5 gated pipeline** (= the C5g positive test).
2. **`plan_approvals` named-Firestore-DB isolation** — code (`database=` kwarg + worker env), provisioning script (named DB + conditioned `datastore.user`), live cutover (bind-before-remove on the coordinator grant).
3. **`github-pat` rotation** to a fine-grained single-repo PAT — operator-minted; code unchanged; **doc corrections** to iam-matrix/deploy/tofu-apply/setup_secrets.

**Uncovered prerequisites folded into the live tail (parent §7 step 4, never completed):**
4. **Redeploy the tofu-apply worker** from main (C5b-2/C5d code + CF env + `PLAN_APPROVALS_DB`), staying `--ingress=internal`.
5. **Grant coordinator SA `storage.objectViewer`** on `gs://…-tofu-artifacts` (C5e artifact fetch).

**C5g (next):** live §8 negatives + a positive in-place apply→merge, worker now reachable.

**Out of scope (deferred, parent OD-1/OD-3):** bearer-replay PoP/WebAuthn (OD-1); head-config delivery / create-class applies (C6).

---

## 2. C5f deliverables — detail

### 2.1 Dedicated payment-demo runtime SA (applied via the pipeline)

- **New SA:** `payment-demo-runtime@driftscribe-hack-2026.iam.gserviceaccount.com`,
  display "DriftScribe payment-demo runtime (minimal)". **Zero project roles** —
  payment-demo is a mock HTTP demo that makes no authenticated GCP calls; the
  default compute SA's broad access is exactly the over-privilege we drop. (If a
  live boot reveals a needed permission, grant it narrowly and document — do not
  fall back to compute-SA breadth.)
- **actAs grants on the NEW SA — ALL mutators of payment-demo (the §3.7 fix):**
  | Identity | Why | Script home |
  |----------|-----|-------------|
  | `tofu-apply-sa` | `tofu apply` updates the service | `setup_secrets.sh §7b` (auto-run) |
  | `rollback-agent-sa` | `/execute` traffic-shift `update_service` → else `actAs denied` 5xx | `setup_prod_project.sh` heredoc → **also script in §7b** so it is auto-applied in prod |
  | Cloud Build deploy SA | only if a future payment-demo `gcloud run deploy` pins `--service-account`; **N/A here** — the SA is set by tofu, not by a redeploy | n/a (note only) |
- **Service-side repoint = HCL change applied through the gate (NOT out-of-band):**
  add to `iac/cloudrun.tf` inside `template { … }`:
  ```hcl
      # Dedicated minimal runtime identity (replaces the default compute SA).
      service_account = "payment-demo-runtime@driftscribe-hack-2026.iam.gserviceaccount.com"
  ```
  This is a **separate PR** (the C5g positive-test vehicle), not part of the C5f
  code PR — opening it, approving, applying (in-place UPDATE), and merging is C5g.
  It preserves zero-diff (config==live post-apply) and is fully governed/audited.
  Denylist: `update` of a non-control-plane `google_cloud_run_v2_service`, no IAM
  type, no delete/replace → **passes**. Fidelity: in-place UPDATE of a baked-declared
  resource → **allowed** (the C4-live-proven class).
- **Ordering invariant:** the SA must EXIST and BOTH actAs grants must be in place
  **before** the apply (apply sets `service_account` → needs actAs on the target SA).

### 2.2 plan_approvals named-Firestore-DB isolation

- **Code (back-compat, ships in the C5f PR, no behavior change until env set):**
  - `driftscribe_lib/approvals.py` `PlanApprovalStore.__init__` (line ~648):
    ```python
    def __init__(self, project: str, client: Any = None, *, database: str | None = None) -> None:
        self._client = client or firestore.Client(project=project, database=database)
    ```
    `database=None` ≡ omit ≡ `(default)` → every existing caller/test (which inject a
    fake `client`) is unaffected; `ApprovalStore` (rollback) untouched.
  - `workers/tofu_apply/main.py`: read `PLAN_APPROVALS_DB = os.environ.get("PLAN_APPROVALS_DB") or None`
    (empty/absent → `(default)`); pass it in `_get_plan_approval_store` (line 138-139):
    `return PlanApprovalStore(project=GCP_PROJECT, database=PLAN_APPROVALS_DB)`.
  - `infra/cloudbuild.tofu-apply.yaml` line 92: append `PLAN_APPROVALS_DB=${_PLAN_APPROVALS_DB}`
    to `--set-env-vars`; add substitution `_PLAN_APPROVALS_DB` default `plan-approvals`.
  - **[Codex IMPORTANT-3]** the same deploy step (`cloudbuild.tofu-apply.yaml:69-93`) does
    **not** set `--ingress=internal`; `gcloud run deploy` is not guaranteed to preserve the
    live ingress. The worker is the sole mutator and MUST stay internal — add an explicit
    `--ingress=internal` to the deploy args.
  - `google-cloud-firestore>=2.19` (worker pin) already supports named DBs — no bump.
- **Named DB:** `plan-approvals` (FIRESTORE_NATIVE, asia-northeast1 — same region as
  `(default)`). Created out-of-band (script). No data migration: all existing
  `plan_approvals` docs are already `used` (consumed in C2/C4 smokes); a fresh DB is
  clean. **Verify no `pending`/un-consumed approval exists before cutover.**
- **Conditioned IAM (the actual isolation):**
  - `tofu-apply-sa`: `datastore.user` **conditioned to the named DB**
    `resource.name == "projects/driftscribe-hack-2026/databases/plan-approvals"`.
  - `driftscribe-agent` (coordinator) + `rollback-agent-sa`: replace the
    **un-conditioned** project-wide `datastore.user` with one **conditioned to
    `(default)`** `resource.name == "projects/driftscribe-hack-2026/databases/(default)"`.
    **Bind-before-remove** (add the conditioned binding, verify, THEN delete the
    un-conditioned one).
  - **[Codex BLOCKER-1] The scripts must STOP re-adding the un-conditioned grant.**
    The un-conditioned project-wide `datastore.user` lives at 5 script sites:
    `setup_secrets.sh:176` (coordinator), `setup_secrets.sh:200` (rollback),
    `setup_iac_backend.sh:460` (tofu-apply), `setup_prod_project.sh:175` (coordinator),
    `setup_prod_project.sh:187` (rollback). Each must be **replaced** by the conditioned
    form — otherwise the first "safe" re-run silently re-grants all-DB access and undoes
    isolation. Design: each site **always** asserts the conditioned binding (additive,
    idempotent — `add-iam-policy-binding … --condition='expression=…,title=…'`); a
    **guarded removal** of the lingering un-conditioned binding
    (`remove-iam-policy-binding … --condition=None || true`) runs **only** under the
    explicit cutover flag `SETUP_PLAN_APPROVALS_DB=1`. Net: fresh bootstraps are
    isolated-by-default (no un-conditioned grant ever added); existing prod keeps its
    working un-conditioned grant on default re-runs (the conditioned add is harmless
    union-of-allows) until the deliberate flagged cutover removes it.
  - **[Codex IMPORTANT-1] Empirical proof is data-plane (client-library), not Console.**
    GCP docs: REST/client libraries enforce the `resource.name` condition; the Console
    does NOT. The `(default)` spelling (with parens) is the documented literal. Proof:
    create a throwaway probe SA with ONLY the `(default)`-conditioned `datastore.user`,
    **impersonate it via the SDK** and assert a read/write to `(default)` succeeds and
    the same op against `plan-approvals` is denied; delete the probe SA. Only after that
    proof do the coordinator bind-before-remove.
  - **[Codex IMPORTANT-2] IAM Deny is NOT a cleaner substitute** — deny conditions
    recognize only resource-tag functions (not `resource.name`), and `datastore.entities.*`
    is not a useful deny target. Conditioned-allow is the correct mechanism.
- **Coordinator code does NOT touch plan_approvals** (asserted invariant, 3 sites) —
  so conditioning its grant to `(default)` cannot break any coordinator path; it
  only removes the *latent* write-capability the B3 threat needs.

### 2.3 github-pat fine-grained rotation (operator + docs)

- **Operator action (cannot be done by the agent):** mint a fine-grained PAT,
  Repository access = **only `adi-prasetyo/driftscribe`**, permissions:
  - **Contents: Read and write** (merge)
  - **Pull requests: Read** (get PR / head sha / state)
  - **Checks: Read** (check-runs gate)
  - nothing else (no Issues, no Actions/workflow, no admin, no 2nd repo)
  - NOT `Commit statuses` — the coordinator reads check-runs only (`get_check_runs`);
    required commit-status checks are enforced server-side by branch protection at merge
    (Codex completed-work NIT)
  **[Codex IMPORTANT-4]** the minimal scope merges fine (merge = Contents:write, verified),
  but the **post-merge audit comment** (`github.py:863` `pr.create_issue_comment`) is
  **best-effort** (try/except — a 403 logs a warning, does NOT lose the merge), so it will
  likely 403 under the minimal PAT. Document this; if the audit comment is wanted, add
  `Pull requests: write` — otherwise accept the warning.
  Then `setup_secrets.sh … <new-pat>` (or `gcloud secrets versions add github-pat`)
  + disable the old classic version. Coordinator picks it up on next revision.
- **Code:** none (the `github_token` field already carries whatever the secret holds).
- **Doc corrections (ship in the C5f PR):**
  - `docs/architecture/iam-matrix.md` line 11 + 122-132: drop "read-only / Pull
    requests: Read only / coordinator never writes"; state the coordinator now
    legitimately needs **Contents:write + PR:read + Checks:read**
    (C5e merges IaC PRs), keep single-repo + no-admin + no-2nd-repo negative space;
    **add a `tofu-apply-sa` row** (currently absent).
  - `docs/runbooks/deploy.md` lines 28-51 (PAT box): same correction; keep the
    "rotate from classic" instruction (now a live action item).
  - `docs/runbooks/tofu-apply.md` §1.3: repoint the documented actAs target from the
    default compute SA → `payment-demo-runtime@…`; note `plan_approvals` named DB.
  - `infra/scripts/setup_secrets.sh` line 9 comment: "Classic PAT … read-only PR
    search" → fine-grained, single-repo, Contents:write+PR/Checks/statuses:read.

---

## 3. Script changes (setup_secrets.sh / setup_iac_backend.sh)

1. **§4 SA loop:** add `payment-demo-runtime-sa`? — NO. Keep the runtime SA name
   `payment-demo-runtime` (not `…-sa`, to read naturally as a runtime identity) and
   create it in a small dedicated idempotent block near §7b (it is payment-demo-specific,
   not a driftscribe worker). Define `PD_RUNTIME_SA_DEDICATED="payment-demo-runtime@${PROJECT}.iam.gserviceaccount.com"`.
2. **§7b rewrite:** create the dedicated SA (idempotent); grant **both** `tofu-apply-sa`
   AND `rollback-agent-sa` `iam.serviceAccountUser` on it; keep the resource-scoped
   `run.developer` on payment-demo for `tofu-apply-sa`. Keep the live-resolve of the
   *actual* runtime SA as a cross-check, but grant actAs on the KNOWN dedicated SA
   name (the chicken-and-egg: actAs must exist before the apply that wires the SA).
3. **Named DB + conditioned grants:** create `plan-approvals` (describe-or-create,
   asia-northeast1, idempotent — runs un-gated so fresh bootstraps are isolated-by-default
   and the conditioned tofu-apply grant has a target). Replace the 5 un-conditioned
   `datastore.user` add-lines (§2.2 Codex BLOCKER-1) with the conditioned form (always
   asserted, additive). The **removal** of the lingering un-conditioned binding is gated
   behind `SETUP_PLAN_APPROVALS_DB=1` (deliberate cutover, like `force-unlock`).
4. **`setup_prod_project.sh` rollback-actAs heredoc (lines 279-288) [Codex IMPORTANT-5]:**
   still prints `RUNTIME_SA="${PROJECT_NUMBER}-compute@developer…"` — update it to the
   dedicated `payment-demo-runtime@…` so the fresh-prod operator instructions match.
5. **`setup_secrets.sh` re-runs use `SETUP_EVENTARC=0`** (verbatim parent constraint)
   and must still pass the real GITHUB_TOKEN as `$2` (mandatory `${2:?}`, re-versioned
   each run — so pass the NEW fine-grained PAT when rotating, else read+re-pass the
   current value to avoid clobbering).

---

## 4. Live execution sequence (operator-driven, by the agent w/ ADC)

**[Codex BLOCKER-2]** unambiguous order; the risky coordinator re-conditioning is last,
gated, reversible. **Freeze IaC applies for the whole cutover** — the live C4 worker still
writes `plan_approvals` to `(default)` until step 4 redeploys it, so no apply may run
between steps 3 and 4 (none is planned; stated to be explicit).

1. **Coordinator `objectViewer` on tofu-artifacts** (additive, zero-risk) — unblocks
   C5e GET/POST artifact fetch. Verify GET `/iac-approvals/<pr>` renders.
2. **Create `payment-demo-runtime` SA** + actAs grants (tofu-apply-sa **and**
   rollback-agent-sa). No service change yet → no drift yet.
3. **Create `plan-approvals` named DB** (asia-northeast1) + the tofu-apply-sa
   `datastore.user` grant **conditioned to the named DB**. Run the **empirical CEL proof**
   (throwaway probe SA, SDK-impersonated) NOW — before any coordinator change.
4. **Redeploy the tofu-apply worker** from main via `cloudbuild.tofu-apply.yaml`
   (C5 code + CF env + `_PLAN_APPROVALS_DB=plan-approvals` + explicit `--ingress=internal`,
   2Gi/2cpu). The worker now boots already isolated (writes `plan_approvals` to the named
   DB). Smoke: reachability probe → 405.
5. **Coordinator + rollback `datastore.user` re-conditioning to `(default)`**
   (bind-before-remove; `SETUP_PLAN_APPROVALS_DB=1` removal of the un-conditioned binding).
   **← CONFIRM WITH USER FIRST (R1 big decision: touches the prod coordinator data path;
   OD-2).** Verify chat (`/chat`→pong, a drift recheck writes `events`/`decisions`)
   immediately after; rollback = re-add the un-conditioned binding.
6. **github-pat rotation** — hand to operator (mint fine-grained PAT); on receipt,
   add the secret version + disable the classic version; next coordinator revision picks
   it up. **Operator-blocked step.**

Then **C5g** (§5).

---

## 5. C5g — live smoke matrix (worker reachable)

On a **throwaway no-op iac/ PR** (and the runtime-SA PR for the positive), with the
worker on C5 code:

| Case | Setup | Expected |
|------|-------|----------|
| **Positive (in-place UPDATE)** | the `service_account` PR → C2 dispatch → GET /iac-approvals → approve (CF) → propose → apply → merge | `/apply` 200 `{applied}`, apply_audit phase=applied exit 0, PR merged at head_sha, post-apply `tofu plan` empty |
| denylist→422 | a PR whose plan trips a denylist rule (e.g. touches a control-plane resource) | `/apply` (or propose) 422, no mutation |
| tampered artifact→403 | flip a byte / wrong generation vs signed digests | `integrity_refused` 403, pre-claim |
| expired→403 | approval window elapsed | 403 (signed-window expiry) |
| forged/absent CF JWT→403 | POST with a forged or missing operator_jwt | worker re-verify fail-closed 403 (REQUIRES the C5 worker redeploy) |
| (already C4-proven) lock_refused→423 | optional, only if a lock is contended | 423 distinct from 502 |

Cleanup: close throwaway PRs; keep GCS artifacts as immutable evidence.

**C5g config prereq:** `IAC_REQUIRED_CHECKS` must be set on the coordinator for the
positive merge to be enabled (empty ⇒ merge-disabled per C5e). Verify/set the
required check names (e.g. `lint-test`) before the positive test.

---

## 6. Test plan (offline, in the C5f code PR)

- `PlanApprovalStore(database=…)` threads to `firestore.Client(database=…)`; default
  `None` → `(default)` (back-compat); injected `client` ignores `database`.
- worker `_get_plan_approval_store` passes `PLAN_APPROVALS_DB`; absent/empty → None.
- cloudbuild structure test: `_PLAN_APPROVALS_DB` substitution present, threaded into
  `--set-env-vars`; CF env still present.
- doc-lint / structural: iam-matrix has a `tofu-apply-sa` row; no "read-only … Pull
  requests: Read only" wording remains for the coordinator github-pat.
- setup-script static guards: §7b grants actAs to BOTH apply+rollback SAs on the
  dedicated runtime SA; §9b conditioned-grant CEL strings well-formed; `SETUP_PLAN_APPROVALS_DB`
  gate defaults off.
- Full suite green + ruff; no `c2.v1`/`c3.v1` wire change; `iac/` still only payment-demo
  (the cloudrun.tf `service_account` line lands in the SEPARATE C5g PR, not here).

---

## 7. Risks & confirmation gates

- **R1 (highest): coordinator datastore.user re-conditioning** could break the prod
  coordinator's `(default)` access (events/decisions/sessions → chat) if the CEL is
  wrong/unsupported for the data plane. Mitigations: (a) empirical CEL proof on a
  throwaway SA first; (b) bind-before-remove; (c) immediate post-change chat + drift
  verify; (d) instant rollback = re-add the un-conditioned binding. **→ user confirm.**
- **R2: positive apply drift** — if the dedicated SA or actAs is missing at apply
  time, `tofu apply` 5xxs `actAs denied`. Mitigation: step 2 before the apply +
  explicit grant verification.
- **R3: worker redeploy** in enforce mode fails to boot without CF env — already in
  cloudbuild line 92; verify boot + reachability before C5g.
- **R4: PAT rotation** is operator-blocked; until done, the live merge (positive
  test) needs Contents:write — if the classic PAT is still live it *works* (over-scoped)
  but the negative-space claim stays weakened. Sequence: ideally rotate before the
  positive merge; acceptable to run the positive on the classic PAT then rotate, as
  long as the doc/secret end-state is the fine-grained PAT.
- **R5: named-DB region** must match `(default)` (asia-northeast1) — pin explicitly.

---

## 8. Open decisions surfaced to the operator

- **OD-2 (now being executed):** isolate `plan_approvals` into a named DB with
  conditioned IAM (this plan) — confirm proceeding with the coordinator re-conditioning
  (R1) vs documenting the residual.
- **PAT timing:** rotate before or after the C5g positive merge (R4).

---

## 9. Codex round-1 review (thread 019e7739) — findings folded

- **BLOCKER-1** — scripts re-add un-conditioned `datastore.user` (5 sites) → would undo
  isolation on rerun. **Folded** (§2.2, §3): replace those add-lines with the conditioned
  form (always asserted); gate the un-conditioned REMOVAL behind `SETUP_PLAN_APPROVALS_DB=1`.
- **BLOCKER-2** — §4 sequencing self-contradicted (DB vs worker redeploy). **Folded** (§4):
  DB+tofu-apply-grant (step 3) before worker redeploy (step 4); freeze applies during cutover.
- **IMPORTANT-1** — proof must be data-plane/client-library, not Console; `(default)` literal
  is correct. **Folded** (§2.2): SDK-impersonated probe-SA proof.
- **IMPORTANT-2** — IAM Deny is not a cleaner substitute (no `resource.name` in deny
  conditions). **Folded** (§2.2): conditioned-allow confirmed as the mechanism.
- **IMPORTANT-3** — `cloudbuild.tofu-apply.yaml` deploy doesn't set `--ingress=internal`.
  **Folded** (§2.2): add it explicitly.
- **IMPORTANT-4** — post-merge audit comment may 403 under the minimal PAT (best-effort,
  won't lose the merge). **Folded** (§2.3): documented; optional `Pull requests: write`.
- **IMPORTANT-5** — `setup_prod_project.sh:279-288` rollback-actAs heredoc still points at the
  default compute SA. **Folded** (§3 item 4): repoint to `payment-demo-runtime@…`.
- **NIT-1/2** (confirmations): separate-PR for the cloudrun.tf `service_account` line is
  correct; no extra prod identity needed for the tofu-set-SA path (Cloud Build's project-wide
  actAs already covers a future `gcloud run deploy payment-demo` — note only).

---

## 10. Codex completed-work review + 4-lens adversarial review — findings folded

Implemented on `feat/iac-phase-c5f-hardening`; full suite 1630 green, ruff clean.

**Codex completed-work (thread 019e7739):**
- **BLOCKER** — runbook never removed `tofu-apply-sa`'s un-conditioned `datastore.user`
  (step 5 only did coordinator+rollback). **Fixed:** runbook step 5 now runs BOTH
  `setup_secrets.sh` AND `setup_iac_backend.sh` with `SETUP_PLAN_APPROVALS_DB=1` + an
  isolation-verify loop + a checklist item; rollback re-adds all three.
- **BLOCKER** — CEL proof ordered before the named DB existed (a denial vs a nonexistent DB
  is `NotFound`, not IAM). **Fixed:** runbook §3 reordered — create DB (3a) THEN proof (3b).
- **IMPORTANT** — `setup_iac_backend.sh` created Firestore without enabling its API.
  **Fixed:** added `firestore.googleapis.com` to its enable list.
- **IMPORTANT** — reachability smoke vantage ambiguous (405 only makes sense in-VPC).
  **Fixed:** runbook §4 probes via the coordinator's `GET /iac-apply/reachability`.
- **NIT** — `Commit statuses: Read` documented but coordinator reads check-runs only.
  **Fixed:** dropped from the minimal PAT scope across iam-matrix/deploy/setup_secrets/runbook/plan.

**4-lens adversarial Workflow (iam-isolation / code-datapath / ops-safety / spec-compliance),
adversarially verified — 2 confirmed:**
- **IMPORTANT** — `TOFU_APPLY_IAM_MODE=editor` re-grants `roles/editor` (unconditioned
  all-database Firestore), forfeiting the apply SA's named-DB fencing. Correctly scoped: does
  NOT reintroduce B3 (coordinator never runs editor mode). **Fixed:** loud warning in the editor
  branch + explicit caveat in iam-matrix.md + tofu-apply.md (isolation holds in hardened mode only).
- **IMPORTANT** — fresh-bootstrap rollback heredoc hardcoded `payment-demo-runtime` (not-yet-live
  on a fresh bootstrap → rollback `actAs denied` until C5g applies). **Fixed:** heredoc now
  resolves the LIVE `template.serviceAccount` and grants actAs on BOTH the live-resolved SA and
  the dedicated SA. (Does not affect the live C5f cutover, which only ADDS the dedicated grant.)
