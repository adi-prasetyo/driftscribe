# Phase C5 — Coordinator integration + operator-auth hardening + apply-then-merge

**Status:** DRAFT rev-2 (Codex round-1 folded: 7 blockers + 8 importants + 3 nits).
Author: agent. Date: 2026-05-30.
**Predecessors (all merged; C4 proven live):** C1 `ed26d7a`, C2 `a689d8e`, C3 `180281c`,
C4 `fd9bc32` (live 2026-05-29). Eventarc resolved `3e95ba9` (PR #20).
**Design parent:** `docs/plans/2026-05-27-infra-iac-agent-design.md` (trust boundary §1/§9,
Decision #6 §11). **Consumes:** C3 `driftscribe_lib/approvals.py` (`c3.v1`) + C4
`workers/tofu_apply/`.

---

## 1. What C5 is

C4 built and live-proved the **sole mutator** (`driftscribe-tofu-apply`): claim-first burn of a
single-use, plan-bound, HMAC-signed C3 approval, then exactly one `tofu apply <saved-plan>` (no
re-plan) against a build-time-baked, code-reviewed `iac/` from `main`. C4 deferred the
human-facing + operational glue to C5. **C5 is orchestration + operator-auth + ops around an
already-proven worker, with the worker's security core left UNCHANGED except one required,
separately-reviewed edit (`lock_refused`).** It does not re-litigate the locked floor (denylist
re-run, saved-plan-apply-no-re-plan, freshness gate, claim-first single-use burn, private
ingress, HMAC binding/domain separation, the frozen `c2.v1`/`c3.v1` wire formats). Design §8
Phase D reuses the Phase-C apply path *unchanged*.

---

## 2. Scope and the C5↔C6 line

**IN C5:**
- **C5a — Coordinator→worker wiring + the approval flow.** `worker_client` `tofu_apply` entry
  (`_WORKER_URL_ENV`/`WORKER_ENDPOINTS` + hardcoded-endpoint `call_apply`/`call_plan_deny`,
  never ADK-exposed); a new **`/iac-approvals` route family using the propose-on-approve flow**
  (§3.1) — GET renders read-only from the **C2 artifact** (NOT from `plan_approvals`), POST
  mints+applies inside one CF-authenticated request; a coordinator-side **idempotency event**
  (§3.6) so a plan is never double-proposed.
- **C5b — Operator-auth hardening (the headline; partial §4 closure — read §3.1 carefully).**
- **C5c — Coordinator→internal-worker connectivity (the live BLOCKER).** §3.2.
- **C5d — `lock_refused` status in the worker** (the one required worker edit). §3.5.
- **C5e — Apply-then-merge + reconcile (Decision #6).** §3.3.
- **C5f — IAM/secret hardening:** dedicated minimal `payment-demo` runtime SA; rotate the
  over-scoped coordinator `github-pat` to fine-grained `Contents: write`; (hardening) isolate
  `plan_approvals` so the coordinator SA cannot write it. §3.7.
- **C5g — Live exercise of the §8 negatives** (test-proven in C4, never live-proven).

**NOT in C5 → C6 (head-config delivery for resource-set-CHANGING plans):** creates / undeclared
addresses / `module.*` / provider changes — C4's fidelity guard fail-closes these because the
worker bakes `iac/` from `main`. C5 ships exactly the live-proven set: **no-ops + in-place
UPDATES of `main`-declared resources**, adding zero new config-delivery surface to the mutator.
C6 delivers resource-set changes via **merge-then-apply-from-main** (§3.4), not tarball/checkout.
Confirm the near-term need is in-place updates (OD-3).

---

## 3. Design decisions

### 3.1 Operator-auth → **propose-on-approve + worker-side CF-JWT re-verify (a real hardening; NOT full §4 closure — see residual)**

**Problem (C3 §4 / C4 §6):** `/apply` enforces `caller == signed approver`, but `caller` is the
coordinator SA and `approver` is free text the coordinator asserted into `/propose`. A
compromised coordinator can spend any approval with no human.

**Two corrections from Codex round-1 that reshape this:**

- **(B2) Sign the approver, not the proposer.** C3 intended *propose-on-approve*
  (C3 §391). So:
  **the GET approval page is read-only and rendered from the ALREADY-PRODUCED C2 artifact** —
  the `plan.json` + the `tofu show` diff comment C2 already posted to the PR (the **coordinator
  does NOT run `tofu`/`tofu show`** — no new execution surface); it shows the 15 `c2.v1` fields +
  the C2 denylist result — **no mint, no token, no `plan_approvals` read** (resolves B3 and B7).
  The page **recomputes `plan_json_sha256` from the fetched bytes before rendering (or marks the
  summary unverifiable)** and states that **final acceptance is decided at Approve** — the POST
  re-runs integrity + denylist + fidelity in the worker, so the GET summary is advisory; on a POST
  refusal the page re-renders with the worker's refusal reason. The actual **`/propose` happens
  INSIDE the CF-authenticated approve POST, immediately followed by `/apply`**, both using the
  *approving* human's CF identity. The signed `approver` is therefore the human who clicked, the
  15-min window starts at approve (not at request), and **the raw apply token is minted and
  consumed server-side within one request — it never reaches the browser or a URL.** **Reject** is
  a **coordinator-only "no apply" audit event** — under propose-on-approve no approval exists yet,
  so there is nothing to `/deny`; the worker `/deny` endpoint is retained UNCHANGED only to clean
  up a rare orphaned pending approval (propose succeeded, apply failed).

- **(B1) A forwarded bearer JWT does NOT close §4.** Forwarding `Cf-Access-Jwt-Assertion` to the
  worker proves *only* that Cloudflare issued a token for that email *sometime* — it is a bearer,
  not bound to this approval. A compromised coordinator already sees live operator JWTs on
  `/chat`/`/recheck` (`agent/auth.py:47` accepts the header, `:62` discards the claims) and can
  **replay** one into propose+apply for an arbitrary plan. So worker-side re-verify is a genuine
  **hardening** — it defeats a coordinator that fabricates a *fictitious* / non-CF / stale
  approver and ties an apply to a *currently-valid* CF identity equal to the signed approver —
  but it is **NOT** non-repudiation: within a live JWT's TTL a compromised coordinator can still
  act as that human.

**What C5 ships (the hardening), worker-side:**
1. Promote `agent/cf_access.py` verification into `driftscribe_lib/` (reused by coordinator +
   worker). **Add `httpx` + `PyJWT` as direct runtime deps to the worker Dockerfile + worker
   `pyproject.toml`** (the worker ships neither today) (I3).
2. Coordinator: stop discarding CF claims in `verify_token` (`agent/auth.py:62`); add a
   **CF-Access-MANDATORY** dependency for the approve POST (not the OR-fallback to
   `X-DriftScribe-Token`) + **CSRF token + exact `Origin` check + `Content-Security-Policy`**
   (B7), keeping the existing `no-store`/`no-referrer`/`DENY` headers.
3. Forward the raw CF JWT as an **additive endpoint field** `operator_jwt` on `/propose` and
   `/apply` (N1: this is an endpoint-contract addition under `extra="forbid"`, not a `c3.v1`
   wire change — `approver` is already signed).
4. Worker **re-verifies** `operator_jwt` against Cloudflare's JWKS and binds *verified-email ==
   `signed_payload["approver"]`*, **replacing** the tautological `caller==approver` as the human
   check (the SA `verify_caller` allowlist still gates inter-service auth, unchanged). Re-verify
   at **`/apply`** (not only `/propose`) so a JWT that expires inside the window fails *pre-claim*
   (403, nothing burned). Worker egress to the public JWKS works by default (Cloud Run internet
   egress is independent of `ingress=internal`) — **no worker networking change** (verified).
5. **Canonical subject rule (I4):** require `email` present, string, length-bounded (≤320),
   normalized identically at sign-time and compare-time; record `operator_email` AND `caller_sa`
   in `apply_audit`/`used_by` (N2).

**Residual / Open Decision OD-1 — true non-repudiation.** Full §4 closure requires a proof the
coordinator cannot replay: either **(i)** a proof-of-possession the human's browser computes over
`{approval_id, action}` (WebAuthn) and the worker verifies, or **(ii)** restructuring so the
human's authenticated request reaches a *trusted, minimal* boundary directly (a thin
CF-Access-fronted approval broker that holds no apply power but issues the worker an
approval-bound, broker-signed assertion) — a blast-radius reduction, not elimination. Both are
heavier than the hackathon posture. **Recommend: ship propose-on-approve + worker re-verify in
C5 (substantial, honest hardening) and decide whether PoP/broker is a C5 sub-deliverable or a
documented residual fast-follow.** This is a real decision for the operator (OD-1).

### 3.2 Connectivity → **Direct VPC egress (`private-ranges-only`) + a `run.app` Cloud DNS zone → the private.googleapis.com VIP**

**Live BLOCKER (confirmed):** worker is `--ingress=internal`; **neither service has a VPC**
(`vpcAccess=None`) and **`compute`/`vpcaccess` APIs are DISABLED**, so the coordinator cannot
reach the worker at the network layer (a valid OIDC token does not help — ingress is a network
control). `run.invoker` + the metadata-server OIDC mint are already in place, so **only the
network layer is missing**.

**Decision — Direct VPC egress on the coordinator, worker stays `ingress=internal`** (no
always-on connector-VM cost; only `compute` needed; an internal ALB doesn't even remove the VPC
requirement; reverting to `ingress=all` is forbidden).

**Complete recipe (the DNS rewrite is the load-bearing piece — I6):** `*.run.app` resolves to a
*public* Google IP, so `private-ranges-only` alone routes it *around* the VPC and the
internal-ingress gate refuses it. The documented Cloud-Run→internal-Cloud-Run pattern adds a DNS
redirect of `*.run.app` to a Private-Google-Access VIP (a private range), which
`private-ranges-only` then sends *through* the VPC, arriving as internal — **preserving the
coordinator's direct public egress (Vertex AI, GitHub, Cloudflare, Notifier) with NO Cloud NAT**:

1. Enable `compute.googleapis.com` + `dns.googleapis.com`.
2. VPC `driftscribe-vpc` + regional subnet in `asia-northeast1`, **Private Google Access ON**.
3. **Cloud DNS private zone for `run.app.`** attached to the VPC: `run.app` + `*.run.app` →
   **`private.googleapis.com` VIP `199.36.153.8/30`** (B4: `private` = `.8/30`; `restricted` =
   `.4/30` and is for VPC-SC perimeters — we are NOT in a perimeter, so use `private`). Add the
   `199.36.153.8/30` → default-internet-gateway route if not auto-created.
4. Redeploy coordinator with `--network=driftscribe-vpc --subnet=<subnet>
   --vpc-egress=private-ranges-only` + `TOFU_APPLY_URL=<worker root URL>`.
5. `compute.networkUser` on the subnet for the coordinator-deploy principal.
6. **Worker: NO change** (stays internal; default internet egress for the CF JWKS fetch).

**Out-of-band, NOT in `iac/`** (would be a create + undeclared address → C4 fidelity refusal, and
breaks the zero-diff import). New script `infra/scripts/setup_coordinator_vpc.sh`.

**Two explicit live gates (I7 + the live-only-bug class that bit C2/C4/eventarc):**
- A real `curl` coordinator→worker `/healthz` **before any apply wiring is trusted** — a failed
  network gate returns 403/404 *pre-app*, trivially mistaken for auth failure.
- **GO/NO-GO:** the `run.app` zone routes **every** coordinator worker call
  (reader/docs/rollback/notifier/infra-reader) through the VIP — confirm each still works before
  proceeding (internal routing is accepted by `ingress=all` services, but verify, don't assume).

### 3.3 Apply-then-merge (Decision #6) → **apply-first + reconcile state machine**

Locked: apply the approved head, then merge with `sha=head_sha`; on merge failure, alert + leave
the PR open as the reconciliation record. Apply-first is the safe direction.

1. **Before `/propose`** (inside the approve POST): verify PR head `== planned head_sha` + required
   checks green (worker has no GitHub token — this is a C5/CI check).
2. propose→apply (§3.1) → `{status:"applied"}`.
3. `merge_pr(sha=head_sha)` via REST `PUT /pulls/{n}/merge` — **token at the coordinator, never
   the worker** (invariant #10).
4. **Reconcile doc (Firestore):** on merge failure park + alert + bounded-retry; idempotent,
   crash-recoverable; records `{approval_id, head_sha, apply_attempt_id, applied_at, merge_state}`.

**Accepted residual (Decision #6 acknowledges):** apply-OK-merge-FAIL leaves live ahead of `main`
until reconciliation; no auto-revert of a successful apply.

### 3.4 Head-config delivery (the C6 mechanism, decided now) → **merge-then-apply-from-main**

C6 merges the approved head to `main` first, re-bakes the worker `iac/` from the new `main` (the
existing Dockerfile bake + `cloudbuild.tofu-apply.yaml` — trusted, CODEOWNERS-/branch-protected,
CI-static-gated), then `/apply` against a baked `main` that now declares the new resources — so
creates pass the *existing* fidelity gate unchanged, with **no new credential, no new egress, no
request-time PR-controlled HCL in the mutator, no wire change**. Beats tarball-to-worker and
worker-checkout (both re-introduce the PR-controlled-HCL code-execution surface the design closed;
checkout also violates invariant #10), and the "bake into the C2 artifact" variant (breaks the
frozen `c2.v1`/`c3.v1`).

**C6's load-bearing additions (flagged, not built in C5):** it inverts ordering to merge-BEFORE-
apply for create-plans (needs a reconcile/revert story for post-merge apply failure), and needs a
**"baked `iac/` tree matches the approved head's `iac/` tree" gate before `/apply`**. **(I5) Gate
on the `iac/` SUBTREE content hash, NOT a commit-SHA equality** — the repo squash-merges, so the
merged-`main` commit SHA ≠ `head_sha`; only the `iac/` tree content is invariant (the `iac.yml`
diff-guard already proves the PR touches ONLY `iac/`, so head-`iac/` == merged-main-`iac/`). C6
must re-assert the diff-guard as a precondition.

### 3.5 `lock_refused` worker edit (C4 Codex C5-input)

Today a `tofu` lock-acquire failure is an indistinct `TofuStepError` → `phase="failed"` + 502
(this bit C4-live when an OOM orphaned the state lock). Add a **distinct `lock_refused` phase +
HTTP 423**, **NO auto-unlock** (force-unlock stays operator-only). **(I2) Classify lock
contention on ALL of `init`, `refresh-only`, and `apply`** (a dedicated `LockRefused` in
`tofu_runner` parsing the GCS-lock stderr), mapped in `main.py`. **(I1) Detect AFTER the claim
only** — do NOT add a pre-claim lock probe (it would let a valid token repeatedly trigger
lock-work without consuming the approval; a transactional probe-lease is out of C5 scope). With
propose-on-approve, a burned approval on a transient lock just means the operator re-clicks
Approve — low friction. **This edits the sole mutator → its own adversarial review pass** (with
§3.1's worker re-verify).

### 3.6 `/propose` idempotency (B6)

`PlanApprovalStore.create()` always mints a fresh UUID/token, so a duplicate approve-click or
retry could mint **multiple valid apply credentials** for one plan. Define a **deterministic
coordinator idempotency key** over `{repo, pr_number, head_sha, generation_metadata, approver}`
and claim a coordinator-side **idempotency event (reuse `state.record_event`, the `_do_rollback`
pattern) BEFORE `/propose`**, releasing on failure; on a concurrent/duplicate, do not mint a
second approval (return the in-flight one or refuse). The single-use burn is a backstop, not the
primary guard.

### 3.7 IAM / secret hardening (C5f)

- **Dedicated minimal `payment-demo` runtime SA** replacing the default compute SA; repoint
  `tofu-apply-sa`'s scoped `actAs` (`setup_secrets.sh §7b`) and the service runtime SA. Shrinks
  the `actAs` blast radius.
- **Rotate `github-pat`** (live finding: it is a CLASSIC PAT with `repo`/`workflow`/`admin`/`push`
  on `driftscribe` AND `driftscribe-e2e-target` — contradicts `iam-matrix.md`). C5 needs merge
  capability, so mint a **fine-grained PAT scoped to `adi-prasetyo/driftscribe` ONLY with
  `Contents: write`** (B5: `merge` creates a commit → needs Contents:write, NOT "PR:write") +
  the minimum reads to inspect PR/check state (`Pull requests: read`, `Checks: read`/`Commit
  statuses: read`). Or isolate a write PAT to a distinct SA/secret (OD-2). **Out-of-band secret
  rotation — operator action regardless.**
- **(B3 hardening) Isolate `plan_approvals` from coordinator writes.** Firestore IAM is
  database-level, not collection-level, and the coordinator SA has project-wide `datastore.user`
  — so a compromised coordinator with a still-valid raw token could flip a `plan_approvals`
  status `used→pending` and re-spend (C3 §364). Move `plan_approvals` to a **separate named
  Firestore database** with per-database IAM denying the coordinator SA write (the worker SA is
  the only writer). Recommend doing this in C5 since C5 first wires the coordinator near this
  collection; if deferred, document the residual explicitly (OD-2).
- **No new call-path IAM** (`run.invoker` present; OIDC self-mint). Do NOT grant the coordinator
  `secretAccessor` on `plan-hmac-key`. `setup_secrets.sh` re-runs use **`SETUP_EVENTARC=0`**.
- **(I8) Worker `COORDINATOR_URL`** is hard-required at import but stuck at `placeholder.invalid`
  and unused under propose-on-approve (no approval_url is minted anywhere). **Drop the hard
  import requirement** (make it optional) or set it real in deploy — do not keep a required
  placeholder in the sole mutator.

---

## 4. End-to-end flow (C5, propose-on-approve, in-place-update class)

```
operator (CF Access session) ─ opens PR; C2 plan-builder dispatch → c2.v1 triplet in GCS
  │
  ├─ chat/UI "show apply plan for PR #N" ─► coordinator (CF-authenticated)
  │     renders READ-ONLY /iac-approvals page from the C2 artifact: tofu-show diff +
  │     15 c2.v1 fields + denylist result. NO mint, NO token, NO plan_approvals read.   [C5a]
  │
  └─ operator clicks Approve ─► POST /iac-approvals  (CF-Access MANDATORY + CSRF + Origin) [C5b]
        coordinator, inside this one CF-authenticated request:
          1. verify PR head==planned head_sha + checks green                              [C5e]
          2. claim deterministic idempotency event {repo,pr,head_sha,gen,approver}        [C5a]
          3. POST /propose {artifact_uri_metadata, generation_metadata,
                            approver=<approving CF email>, operator_jwt=<this req's JWT>}  [C5b]
                └─ worker: re-verify artifact+denylist+fidelity → mint c3.v1, token (in-proc)
          4. POST /apply {approval_id, approval_token, operator_jwt=<same JWT>}           [C5b]
                └─ worker §3.6: verify HMAC → signed window → RE-VERIFY operator_jwt,
                     verified-email==signed approver → claim/burn → integrity → denylist →
                     fidelity → freshness → tofu apply <saved plan> →
                     {applied} | lock_refused 423 | drift_refused 409 | …                 [C5d]
          5. on applied ─► merge_pr(sha=head_sha); on fail → reconcile doc + alert        [C5e]
        token is minted (3) and consumed (4) server-side; it never reaches the browser/URL.
```

The step-3/4 calls traverse the §3.2 VPC path.

---

## 5. Wire / endpoint changes (all additive; `c3.v1` signed payload UNCHANGED)

- **Worker `ProposeRequest`** += `operator_jwt: str | None`; **`/apply` `TokenRequest`** +=
  `operator_jwt: str | None`. **`/deny` `TokenRequest` UNCHANGED** (reject is a coordinator audit
  event; `/deny` is cleanup-only for an orphaned pending approval and needs no operator binding).
  N1: endpoint-contract addition under `extra="forbid"` (not a wire-format change). `None` allowed
  only in the e2e-gated fallback.
- **Worker `/apply` human check** = verified-CF-email == signed approver (was `caller==approver`).
- **Worker `apply_audit`**: new `lock_refused` phase (HTTP 423); `used_by`/audit records both
  `operator_email` and `caller_sa` (N2).
- **Worker deps**: `httpx`, `PyJWT` added to Dockerfile + `pyproject` (I3). `COORDINATOR_URL`
  import requirement dropped (I8).
- **`agent/worker_client.py`**: `tofu_apply` URL/endpoint entries + `call_apply` + `call_plan_deny`
  (cleanup-only) (hardcoded `/apply`/`/deny`; never ADK-exposed).
- **`agent/auth.py`**: surface verified CF claims; CF-Access-mandatory + CSRF dep for the approve
  POST.
- **`driftscribe_lib/`**: promoted CF-Access verification (coordinator + worker).
- **New env**: coordinator `TOFU_APPLY_URL`; worker `CF_ACCESS_TEAM_DOMAIN`/`CF_ACCESS_AUD_TAG` +
  an `IAC_OPERATOR_AUTH_MODE` gate for e2e.

---

## 6. Test plan

**Offline (CI green, ~1395 today):** worker_client wiring + ADK-non-exposure negative;
`/iac-approvals` GET read-only-from-artifact (no `plan_approvals` read) always-200; approve POST
CF-mandatory + CSRF + Origin reject paths; propose-on-approve orchestration (idempotency-event
claim/release, no double-mint); CF verify promoted to lib (golden-parity; worker re-verify
valid→email, forged→fail-closed, absent→fail-closed, expired→pre-claim-403); `/apply` binding
verified-email==approver (pass/mismatch/e2e-gate); `lock_refused` on init+refresh+apply distinct
from `failed`; apply-then-merge + reconcile (merge-OK / merge-FAIL park+retry / crash-idempotent);
structural invariants (`c2.v1`/`c3.v1` unchanged; four locked-floor primitives untouched; `iac/`
still only `payment-demo`).

**Live §8 negatives (C5g):** on a throwaway no-op PR with the worker reachable —
denylist→422, tampered-artifact→403/integrity_refused, expired→403, forged/absent CF JWT→403,
PLUS a positive in-place-update apply→200 `{applied}` + merge.

---

## 7. Live deploy / smoke (operator-driven, by the agent w/ ADC)

**Reachability FIRST (N3 — it is the live blocker):**
1. `infra/scripts/setup_coordinator_vpc.sh` (enable `compute`+`dns`; VPC+subnet+PGA; `run.app`
   zone → `private.googleapis.com` `199.36.153.8/30`; `compute.networkUser`). **Pin the VIP
   value live.**
2. Redeploy coordinator with `--network/--subnet/--vpc-egress=private-ranges-only` + `TOFU_APPLY_URL`.
3. **`curl` coordinator→worker `/healthz`** + GO/NO-GO that existing worker calls still work.
   Do not proceed until green.

Then: 4. deploy the worker (`cloudbuild.tofu-apply.yaml`) with CF env + `operator_jwt` +
`lock_refused`, stays `--ingress=internal`; `SETUP_EVENTARC=0` on any `setup_secrets.sh` re-run.
5. dedicated `payment-demo` runtime SA + `github-pat` rotation + `plan_approvals` DB isolation
(C5f). 6. §8-negative + positive smoke (C5g); clean up (close PR; keep GCS artifacts as evidence).

---

## 8. Open decisions for the operator

- **OD-1 (true §4 closure):** ship propose-on-approve + worker CF re-verify as the C5 hardening
  and either (a) **accept the bearer-replay residual as a documented fast-follow** *(recommended
  for C5 timeline)*, or (b) build proof-of-possession (WebAuthn) / a minimal CF-fronted approval
  broker now. The bearer-forward defeats fabricated/non-CF/stale approvers but a compromised
  coordinator can replay a live operator JWT within its TTL.
- **OD-2 (`plan_approvals` isolation + merge token):** isolate `plan_approvals` into a separate
  named Firestore DB (deny coordinator write) in C5 *(recommended)* vs document as residual; and
  fine-grained `Contents: write` PAT on `driftscribe` at the coordinator *(recommended)* vs a
  write PAT isolated to a distinct SA.
- **OD-3 (C5↔C6 line):** confirm near-term need is in-place updates (head-config delivery waits
  for C6). If create-heavy Phase-D fan-out is wanted sooner, pull §3.4 forward.

## 9. Risks & residuals

- §3.1 bearer-replay residual (OD-1) — the honest limit of forwarded-JWT auth.
- §3.2 reachability is a live-only correctness risk (network gate failures look like auth
  failures) — explicit `curl /healthz` gate + run.app-zone GO/NO-GO before any apply smoke.
- §3.1 worker now has a JWKS dependency on the apply critical path (fail-closed; ~1h cache).
- §3.3 apply-OK-merge-FAIL divergence is Decision-#6-accepted; reconcile doc mitigates.
- §3.7 Firestore status-flip re-spend until `plan_approvals` is isolated (OD-2).
- The two sole-mutator edits (§3.1 worker re-verify, §3.5 `lock_refused`) get a dedicated
  multi-lens adversarial review.
- In-worker compromise remains un-gatable by the approval flow (design §9) — minimization only.
- GCS artifact-deletion availability (versioning ≠ immutability) — bucket retention is a separate
  follow-up, not C5.

## 10. Process

Per CLAUDE.md: this plan is Codex-reviewed before the operator sees it (round-1 folded here);
implementation is reviewed on the same thread (`codex-reply`); the two sole-mutator edits get a
multi-lens adversarial Workflow review. **Implementation order (reachability de-risked early):**
**C5c VPC + `/healthz` reachability proof (live, first)** → C5a wiring → C5d `lock_refused`
(isolated mutator edit) → C5b operator-auth (propose-on-approve) → C5e merge+reconcile → C5f
IAM/PAT/Firestore → C5g live §8 smoke. C5a/d/b/e/f are code+test PRs; C5c/g are operator-live.
```
