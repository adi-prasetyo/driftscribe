# Phase C5e — Coordinator `/iac-approvals` orchestration (implementation plan)

**Status:** rev-2 (Codex round-1 folded: 4 blockers + 6 importants + 3 nits).
Author: agent. Date: 2026-05-30.
**Parent plan:** `docs/plans/2026-05-30-infra-iac-phase-c5-coordinator-integration.md`
(§3.1 propose-on-approve, §3.3 apply-then-merge, §3.6 idempotency). C5a/C5d/C5b
merged (`c7db05c`, `45bbb4c`, `50c4008`, `eef2531`); suite 1452 green.

Coordinator-side orchestration ONLY. Does NOT touch the sole mutator
(`workers/tofu_apply/`). C5c (VPC reachability) and C5g (live smoke) are
operator-live, out of this slice.

---

## 1. What C5e adds

A coordinator `/iac-approvals/{pr_number}` route family: **GET** renders a read-only
approval page from the already-produced C2 artifact (no mint, no token, no
`plan_approvals` read), embedding the **exact artifact identity** in a signed form;
**POST** performs propose-on-approve under a *mandatory* verified Cloudflare-Access
operator identity, acting on **exactly the artifact the page showed** — verify
head + checks, idempotency-claim, `/propose`→`/apply` (forwarding the operator JWT),
then merge the **exact applied head_sha** and reconcile on merge failure.

---

## 2. The orchestration state machine (the correctness core — Codex round-1)

Worker outcomes and the coordinator's response. The pivot is the worker's **claim/burn**
at `workers/tofu_apply/main.py:454`: everything before it is non-mutating; everything
after may have mutated.

| `/apply` result | worker burned approval? | infra mutated? | coordinator action |
|---|---|---|---|
| `200 {applied}` | yes | yes | merge; on merge-fail → reconcile doc, keep event |
| `403` / `404` (pre-claim: bad/expired/wrong token, operator-verify, not-pending) | **no** | no | **release event** + best-effort `call_plan_deny` (clean the orphaned pending we just minted) + map→403 |
| `422` integrity/fidelity/verify (post-claim, pre-tofu) | yes | no | **release event** (infra unchanged; burned approval is dead) + map→403; operator re-clicks → fresh mint |
| `423` lock_refused (post-claim, pre-tofu) | yes | no | **release event** + map→423 ("force-unlock then re-approve") |
| `409` drift_refused (post-claim, pre-tofu) | yes | no | **release event** + map→409 ("re-run C2") |
| `502` failed (post-claim, **tofu apply itself failed — possible partial mutation**) | yes | **maybe** | **do NOT release**; decision `apply_status="failed"`; alert; no merge; **terminal/manual** |
| transport timeout / synthetic 503 after send | unknown | **unknown** | **do NOT release**; decision `apply_status="ambiguous"`; alert; **504**; **terminal/manual** |

**Rationale (Codex blockers #2/#3):** releasing on a post-claim *mutating-or-ambiguous*
outcome would let a re-click re-mint+re-apply over a possibly-changed world. Releasing on
post-claim *non-mutating refusals* (422/423/409 — all happen before `tofu apply` runs) is
safe and gives the low-friction re-click the propose-on-approve design wants. `502`
(`phase="failed"`) and timeout are the only genuinely ambiguous cases → terminal + alert,
never auto-retried.

**Idempotency key (blocker #4):** `sha256(json({repo, pr_number, head_sha,
generation_metadata}))` — **NOT** keyed on `approver` (two operators must not double-mint
the same artifact). Approver recorded in the event payload + audit, not the key.

**Reconcile (blocker #2):** only a decision with `apply_status=="applied"` AND
`merge_state=="failed"` is eligible for **merge-only retry** on re-POST. `apply_status` in
`{"failed","ambiguous"}` is **terminal** — re-POST returns the recorded state, never merges.

---

## 3. Decomposition (3 reviewable PRs, test-first)

### C5e-1 — Foundation: artifact helpers + config + lib edits (no route)
1. **`agent/iac_artifacts.py`** (new):
   - `parse_c2_pr_comment(body) -> C2CommentRef | None` — parse the C2 marker
     `### DriftScribe IaC — \`tofu plan\` (Phase C2 plan-builder)`; extract all three
     artifact URIs + generations, `head_sha`, `plan_sha256`, `plan_json_sha256`,
     `opentofu_version`, and the `<details>` tofu-show text. Pure; fail-closed (`None`).
   - `find_latest_c2_comment(repo, pr_number) -> C2CommentRef | None` — list PR issue
     comments (PyGithub), pick the **latest** matching the marker.
   - `fetch_gcs_object(bucket_name, object_name, generation) -> bytes` — lazy
     `storage.Client(project=gcp_project)`, generation-pinned raw download. Validate the
     object path against the worker's regex
     `^pr-\d+/[0-9a-f]{40}/run-\d+-\d+/(metadata\.json|plan\.tfplan|plan\.json)$` and
     bucket == artifacts-bucket BEFORE fetch. Fail-closed `IacArtifactError`.
   - `load_plan_view(ref, settings) -> IacPlanView` — fetch+parse metadata.json @gen →
     assert c2.v1 via `iac_plan_metadata.build_metadata` round-trip (15 fields + formats)
     → fetch plan.json @gen → **recompute `plan_json_sha256`, constant-time compare** →
     run C1 denylist (`iac_plan_denylist.load_plan_json` + `evaluate`) → return the 15
     fields + diff text + `integrity_ok` + `denylist_violations` + `unverifiable`.
     Advisory at GET; re-verified in the worker at POST.
2. **`agent/config.py`**: `tofu_artifacts_bucket` (+ `artifacts_bucket(s)` →
   `... or f"{s.gcp_project}-tofu-artifacts"`), `iac_required_checks` (CSV; **empty ⇒
   merge disabled**), `iac_merge_method="squash"`, `coordinator_origin` (empty ⇒ POST 403).
3. **`agent/worker_client.py`** — per-call timeout (BLOCKER fix):
   `call(..., *, timeout=None)`; `call_apply` uses `_APPLY_HTTPX_TIMEOUT =
   httpx.Timeout(connect=10.0, read=920.0, write=30.0, pool=10.0)` (≥ worker `--timeout=900`
   + margin). `call_propose`/`call_plan_deny` keep 30s.
4. **`agent/state_store.py`** — make `FirestoreStateStore.record_decision` **atomic**
   (Codex important): write the decision doc + the event→decision pointer in one
   `client.batch()` (today they are two separate writes, :185/:186 — a crash between them
   orphans the reconcile pointer). Also store `event_key` **inside** the decision doc and
   add a query fallback in `find_decision_for_event` (belt-and-suspenders recovery).
   InMemory store already atomic; mirror the `event_key` field.

### C5e-2 — GET route + template + artifact-bound CSRF
- **`agent/templates/iac_approval.html`** (mirror `approval.html`): 15 c2.v1 fields,
  head_sha, denylist result, integrity status, collapsible tofu-show diff, required-checks
  note, hidden signed form token + Approve/Reject. Copy states **final acceptance happens
  on Approve (worker re-verifies)** (nit). Suppress Approve on unverifiable / denylist /
  integrity failure.
- **`@app.get("/iac-approvals/{pr_number}")`** — read-only via C5e-1. **Always 200**.
  Headers: reuse `_apply_approval_security_headers` + strict CSP
  (`default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none';
  frame-ancestors 'none'`). Mints the signed form token. **No mint, no token, no
  `plan_approvals` read.**
- **Signed form token binds the EXACT artifact identity (blocker #1):** token =
  `b64(payload . hex(HMAC(k, payload)))` where `payload` encodes `{pr_number, head_sha,
  artifact_uri_metadata, generation_metadata, plan_sha256, plan_json_sha256, comment_id,
  exp}` (short TTL, e.g. 30 min). This is the CSRF token AND the artifact pin in one. POST
  acts on **this** artifact (re-fetch by these URIs+generations, re-verify integrity), so a
  C2 rerun between GET and POST cannot swap the artifact under the operator. `k =
  HMAC(driftscribe_token, b"iac-csrf-key")` (derived; static token never leaves server).
  **Fail closed (503) if `driftscribe_token` is unset** — `require_cf_operator` does not
  require it, so this route needs its own guard (Codex important).

### C5e-3 — POST orchestration + merge helper + error mapper + reconcile
- **`driftscribe_lib/github.py`**:
  - Make `_assert_pr_eligible` accept `required_label: str | None` (None ⇒ skip the label
    check at :277 — Codex blocker #5; today it unconditionally requires a label).
  - `merge_pr_at_sha(repo, *, pr_number, expected_head_sha, required_checks, merge_method,
    dry_run, required_base="main") -> dict`: fetch PR, then **order matters (Codex r2):**
    (i) assert `base==required_base` + **`pr.head.sha == expected_head_sha`** FIRST (else
    `PrMergeBlockedError` 409 — stale); (ii) ONLY THEN allow the idempotent already-merged
    short-circuit (so a manually-merged *newer* head is NOT mistaken for a successful
    reconcile of an older artifact — do not reuse `_assert_merge_preconditions`' early
    `pr.merged` return, which fires before the head check at `github.py:425`); (iii)
    open/not-draft/mergeable + every `required_checks` green on `expected_head_sha` (reuse
    `_latest_check_runs`, `_MERGE_ALLOWED_STATES`); (iv) `pr.merge(sha=expected_head_sha,
    merge_method=...)`. Empty `required_checks` ⇒ refuse.
- **`_map_tofu_apply_error(e, *, action)`** — preserve **423** (lock_refused) and **409**
  (drift_refused) distinctly; 422 → 403 (don't leak which check); 5xx → handled by the
  state machine (NOT a blanket 502 — see table); 404/403 → 403.
- **`@app.post("/iac-approvals/{pr_number}")`** with
  `operator_email = Depends(require_cf_operator)` + raw
  `cf_access_jwt = Header(alias="Cf-Access-Jwt-Assertion")` to forward:
  1. **Exact `Origin` check** — parse URL, compare scheme+host+port **exactly** to
     `settings.coordinator_origin`; **reject if `Origin` missing or mismatched** (no Referer
     fallback — Codex important). Verify the signed form token (CSRF + artifact pin).
  2. Load **the form-pinned artifact** (re-fetch by the token's URIs+generations,
     re-verify integrity + denylist). If unverifiable/denylist → 403 re-render.
  3. `assert_pr_ready_at_sha`: **`pr.head.sha == head_sha`** (else 409 stale) **+ required
     checks green on that sha** (else 409). (`merge_pr_at_sha` re-checks at merge time too.)
  4. **Idempotency:** `record_event(event_key, {approver, head_sha, ...})`. If not claimed →
     `find_decision_for_event`: `merge_state=="merged"` → return done; `apply_status=="applied"
     and merge_state=="failed"` → **merge-only reconcile** (skip propose/apply; go to step 7);
     `apply_status in {"failed","ambiguous"}` → return recorded terminal state; else → 409.
  5. `call_propose(..., approver=operator_email, operator_jwt=cf_access_jwt)`. On error →
     `release_event` + `_map_tofu_apply_error` (propose mints last; failure ⇒ no approval).
  5b. **Second head re-check (Codex r2):** immediately before `/apply`, re-read
     `pr.head.sha` and require `== head_sha`. A push between propose and apply would
     otherwise apply a stale saved plan (old head) then fail merge → divergence. On
     mismatch → best-effort `call_plan_deny(approval_id, approval_token)` (the approval is
     pending, not yet applied) + `release_event` + **409** "head moved; re-approve".
  6. `call_apply(approval_id, approval_token, operator_jwt=cf_access_jwt)` (long timeout).
     Dispatch strictly per the **§2 table** (release only pre-claim 403/404 + non-mutating
     422/423/409; never release 502/timeout).
  7. **Merge:** `merge_pr_at_sha(expected_head_sha=head_sha, ...)`. OK →
     `record_decision(apply_status="applied", merge_state="merged")`; FAIL →
     `record_decision(apply_status="applied", merge_state="failed", apply_attempt_id,
     applied_at)` (the **reconcile doc**) + `notifier` alert; return **200** "applied; merge
     pending reconcile" (apply succeeded — not an operator error; Decision-#6 residual).

---

## 4. Carry-forward blockers addressed (`c5_carry_forward_blockers`)
- **`call_apply` 30s timeout** → long per-call timeout (C5e-1).
- **operator_jwt non-null in prod** → `require_cf_operator` guarantees a verified JWT
  (401 if absent); POST always forwards a real one. No coordinator e2e/None path.
- **Preserve 423/409** → `_map_tofu_apply_error` + the §2 table.

## 5. Test plan (offline, CI-green)
- C5e-1: comment-parse golden + malformed→None; GCS path/bucket validation fail-closed;
  integrity-mismatch→refuse; denylist re-run pass/violation; worker_client `call_apply`
  long-timeout plumbed (assert via injected client); `record_decision` atomic (batch) +
  `event_key` recoverable.
- C5e-2: GET renders 15 fields; **asserts no `plan_approvals` read** + no mint; always-200
  on missing comment; CSP+no-store+frame-deny; signed form token present + artifact-bound;
  503 when `driftscribe_token` unset; Approve suppressed on denylist/integrity failure.
- C5e-3: `merge_pr_at_sha` head-mismatch 409 / checks-not-green 409 / empty-checks refuse /
  base!=main reject / already-merged idempotent / happy merge; CF-mandatory (401/403/503);
  Origin missing/mismatch→403; forged/stale form token→403; **artifact-swap after GET →
  POST uses pinned artifact** (regression for blocker #1); head-mismatch→409;
  checks-not-green→409; idempotency double-click no double-mint; **release matrix per §2
  table** (pre-claim 403/404 release+deny; 422/423/409 release; 502/timeout no-release);
  apply-error mapping 423/409 preserved; happy applied→merge→merged; merge-FAIL→park+notify
  +200 + `merge_state="failed"`; re-POST→merge-only reconcile; **apply_ambiguous terminal
  (never merges on re-POST)**; 502 terminal.

## 6. Resolved decisions (Codex round-1)
- **OD-A (merge provenance):** `merge_pr_at_sha` drops docs-PR label/prefix; provenance =
  (a) a valid C2 artifact verifies for this exact head_sha ⟹ the plan-builder iac-only
  diff-guard passed on this head (it refuses to emit otherwise), (b) `pr.head.sha ==
  head_sha` at merge, (c) **`base=="main"`** (added), (d) `iac_required_checks` green on the
  head (configure to the PR-head checks `static-gate`,`tofu`). Label optional.
- **OD-B (ambiguous):** 504 + `apply_status="ambiguous"` decision + alert, no merge, no
  release, terminal. No coordinator `plan_approvals` read (keeps §3.7 isolation).
- **OD-C (CSRF):** signed stateless token (also the artifact pin) + **exact Origin**
  (load-bearing — CF Access does NOT stop CSRF). No double-submit cookie.
- **OD-D (GET cost):** GitHub + 2 GCS reads per render; operator-initiated, low volume —
  acceptable, no cache for now.
- **OD-E (IAM, C5f/live):** coordinator SA needs `storage.objectViewer` on the artifacts
  bucket; `github-pat` needs merge (Contents:write). Code assumes; grants are operator/C5f.
