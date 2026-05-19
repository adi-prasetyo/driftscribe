# DriftScribe v3 — Multi-Agent Implementation Plan (revised after Codex review)

> **For Claude:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` to implement this plan task-by-task. Per-phase Codex review on thread `019e3af3-f679-7d20-bff1-328295c8f5df` after each phase commits.

**Submission deadline:** 2026-07-10. Today: 2026-05-19. **Budget: ~52 calendar days, working evenings/weekends.**

**Revision history:** v3.0 (initial plan, 2026-05-19) had a confused-deputy hole and several wrong assumptions about Cloud Run/Eventarc auth. Codex flagged them on thread `019e3af3-f679-7d20-bff1-328295c8f5df`. This v3.1 incorporates the fixes and cuts to fit the part-time calendar.

**Goal:** Promote DriftScribe from "single-agent classifier-with-LLM-polish" to a **multi-agent system with four-layer safety** — capability-bounded tool registry on the coordinator *plus* IAM-scoped service accounts *plus* per-worker payload-intent policies *plus* HITL approval gates for destructive actions. Add a natural-language operator interface, Eventarc auto-trigger, and submission-quality polish.

## The four layers of safety (the headline story)

The threat model we're defending: **accidental damage from the LLM doing reasonable-looking-but-wrong things**, not full prompt-injection compromise (a fully jailbroken coordinator with infinite retries is cooked no matter what — but that's not the realistic failure mode). Each layer reduces the blast radius of an unintended action.

| Layer | Where enforced | What it stops |
|---|---|---|
| **0. Tool inventory (capability-bounded registry)** | Coordinator process at startup | LLM cannot even *attempt* an action whose tool isn't registered. No `execute_shell`, no `arbitrary_http_request`, no direct GCP/GitHub SDK calls. Only `delegate_to_<worker>`, `load_contract`, `search_recent_prs` (read-only), session memory I/O. A test asserts the registered tool list is exactly this set and that no tool name matches dangerous patterns (`*shell*`, `*exec*`, `*delete*`, `*subprocess*`). |
| **1. IAM scoping** | GCP control plane | Even if a tool *were* improperly added, the coordinator's Cloud Run SA lacks the IAM permission to do damage directly. |
| **2. Per-worker payload-intent policy** | Worker app code | Confused-deputy attacks: a coordinator can call workers, but each worker hardcodes what arguments it'll accept. Docs Agent refuses paths outside `demo/docs/*.md`, Rollback hardcodes `payment-demo`, Reader ignores caller-supplied service/region. |
| **3. HITL approval gate** | Firestore transaction (one-time HMAC token) | Destructive ops (rollback) require a human-clicked Approve button. Tokens are single-use and transaction-backed; replay returns 403. |

This is the architectural property judges actually evaluate. Each layer is independently provable (Layer 0 is a unit test, Layer 1 is `gcloud projects get-iam-policy`, Layer 2 is per-worker negative tests, Layer 3 is the Firestore audit trail).

---

## Architecture Summary

**Coordinator Agent** (ADK + Gemini, public Cloud Run with X-DriftScribe-Token guard). Receives:
- `POST /chat` — natural-language operator prompts (with token header)
- `POST /recheck` — direct API trigger (with token header)
- `POST /eventarc` — CloudEvents from Eventarc, authenticated via Google-signed ID token (bearer auth)
- `GET /approvals/{id}`, `POST /approvals/{id}` — HITL approval UI for rollback decisions

Coordinator has **read-only** access to: its own Firestore session/state, recent PRs on the configured repo (via read-only GitHub token), its own secrets (token guard, approval HMAC key, GitHub read token). It has **NO** ability to mutate GCP or GitHub directly; it can only request work from a worker.

**Four worker agents** as separate Cloud Run services with `--no-allow-unauthenticated`. Each has its own service account, minimal scoped IAM, AND a hardcoded payload-intent policy that enforces what arguments it'll accept regardless of who's calling.

| Worker | IAM scope | Hardcoded payload policy (defense against confused-deputy) | Secrets |
|---|---|---|---|
| Reader | `roles/run.viewer` on project | Reads ONLY `{project: $PROJECT, region: asia-northeast1, service: payment-demo}`. All other params 403. | none |
| Docs | none (uses GitHub PAT) | Writes ONLY to `adi-prasetyo/driftscribe`, ONLY paths matching `demo/docs/*.md`. Refuses `ops-contract.yaml`, `.github/**`, `infra/**`, `Dockerfile*`, `*.py`. | fine-grained PAT scoped to single repo, `contents:write` + `pull-requests:write` only |
| Rollback | `roles/run.developer` on `payment-demo` service ONLY (resource-scoped binding, not project) | Acts ONLY on `payment-demo`. Target revision MUST be in the service's current revision list AND not the active revision. HITL token required. | HMAC key for approval tokens |
| Notifier | none | Posts ONLY to `$NOTIFY_WEBHOOK_URL` (loaded from Secret Manager). Caller-supplied URLs ignored. | webhook URL |

**Why this is stronger than IAM alone:** Even if a prompt-injected coordinator successfully asks Docs to "patch ops-contract.yaml to set allow_manual_change=true everywhere," Docs refuses because the payload policy rejects that path. Even if it asks Rollback to roll a *different* service, Rollback refuses because the policy hardcodes `payment-demo`. The IAM scope is the *outer* boundary; the policy is the *inner* boundary. The HITL gate is the *third* layer for destructive ops. And before any of those layers even gets exercised, **the coordinator's tool registry (Layer 0) doesn't contain a path to most damaging actions in the first place** — the LLM literally has nothing to call.

**Inter-service auth:** Cloud Run IAM (`roles/run.invoker` granted only to the coordinator's SA on each worker). Coordinator mints audience-bound Google ID tokens via `google.oauth2.id_token.fetch_id_token(audience=<worker_root_url>)` from the metadata server. Workers verify via `google.oauth2.id_token.verify_oauth2_token` (audience match + caller email match against allowlist of `coordinator-sa@$PROJECT.iam.gserviceaccount.com`).

**Tech stack additions:** ADK sub-agent delegation pattern, Cloud Run-to-Cloud Run ID token auth, fine-grained GitHub PAT, Eventarc Cloud Run audit-log trigger, HMAC-signed transactional HITL approval, structured JSON logging w/ trace IDs.

---

## Critical Path

```
Phase 11 (multi-agent + policies + token guard, 6-8d)
                                       │
                                       ├─ Phase 13 (HITL rollback, 2-3d)
                                       ├─ Phase 14 (Eventarc bonus, 1-2d)
                                       ├─ Phase 15 (hardening: CI + logs, 1-2d)
                                       │
                                       └─ Phase 16 (submission artifacts, 4d)
                                                  │
                                                  └─ Phase 18 (final submission, 1d)
```

**Estimated effort: 14–20 working days.** Calendar with evenings/weekends only: realistic in 5–7 weeks. ~10 days of slack before deadline.

**Cut from v3.0** (per Codex review):
- ~~Phase 12.2: Firestore-backed ADK Sessions~~ — sessions stay in-memory. Cross-session memory doesn't move the demo needle for judges in a 90s video. Phase 12.1 (the NL `/chat` endpoint) folds into Phase 11.6 since coordinator already does intent classification via ADK.
- ~~Phase 17: Multi-service contract support~~ — out of scope.
- ~~Phase 15.3 deployed e2e in CI~~ — keep the test file as a manual smoke harness; don't gate CI on it.
- ~~Phase 16.3 full benchmark page~~ — replaced with a 3-number "Cost & Latency" section in the README.

---

## Phase 11 — Multi-Agent Skeleton with Layered Safety (6–8 days)

Make the architecture real. Five Cloud Run services, four service accounts, IAM-bounded inter-service calls, payload-policy validation in every worker, token guard on coordinator.

### Task 11.0: Spike — Cloud Run-to-Cloud Run auth (½ day)

**Before writing the design doc, prove the assumption.**

**Files:**
- Create: `spikes/cloud_run_auth/caller/main.py`, `spikes/cloud_run_auth/callee/main.py`, `spikes/cloud_run_auth/README.md`

**Steps:**
1. Deploy two minimal FastAPI services. Caller has SA `spike-caller-sa@...`. Callee has SA `spike-callee-sa@...`, `--no-allow-unauthenticated`, with `roles/run.invoker` granted to `spike-caller-sa`.
2. Caller mints an ID token via `google.oauth2.id_token.fetch_id_token(Request(), audience=callee_root_url)` and calls `POST $CALLEE_URL/work` with `Authorization: Bearer <token>`.
3. Callee uses `google.oauth2.id_token.verify_oauth2_token(token, request=Request(), audience=callee_root_url)` and asserts the `email` claim is in an allowlist.
4. Verify: caller → 200. gcloud-as-user → 403. Caller with no token → 401. Caller with token for wrong audience → 401.
5. Document any gotchas in `spikes/cloud_run_auth/README.md` (e.g., metadata server delays, audience must be root service URL not path, caching behavior).

**Step 6: Commit.** Then optionally delete the spike services to keep costs zero; the README is the artifact.

### Task 11.1: Token guard on coordinator + design docs

**Files:**
- Modify: `agent/main.py` (add auth dependency for `/recheck` and `/chat`)
- Create: `agent/auth.py` (token verification dep)
- Create: `tests/integration/test_token_guard.py`
- Create: `docs/architecture/multi-agent-design.md`
- Create: `docs/architecture/iam-matrix.md`

**Steps:**
1. **TDD** `test_token_guard.py`: requests without `X-DriftScribe-Token` return 401; with wrong token return 403 (constant-time comparison via `secrets.compare_digest`); with correct token (loaded from Secret Manager `coordinator-shared-token`) succeed. `/eventarc` and `/approvals/*` are exempt.
2. Implement `auth.py` and wire as FastAPI dependency on `/recheck` and `/chat`.
3. **Add the secret to Secret Manager:** `gcloud secrets create coordinator-shared-token`, generate a random token, store as version 1. Update `cloudbuild.yaml`'s `--set-secrets`.
4. Write `multi-agent-design.md` (interfaces) and `iam-matrix.md` (per-SA grants, including negative-space documentation: "Coordinator SA does NOT have `run.developer`, `datastore.user`, project-level Secret Manager access").
5. **Commit.**

### Task 11.2: Shared library — `driftscribe_lib`

**Files:**
- Create: `driftscribe_lib/__init__.py`, `driftscribe_lib/cloud_run.py`, `driftscribe_lib/github.py`, `driftscribe_lib/auth.py`, `driftscribe_lib/logging.py`
- Create: `driftscribe_lib/pyproject.toml`
- Modify: root `pyproject.toml` to include `driftscribe_lib` as a workspace member
- Modify: `agent/cloud_run_client.py`, `agent/github_actions.py` — import from `driftscribe_lib` and become thin wrappers

**Steps:**
1. Extract shared functions: Cloud Run admin client, GitHub client setup, Google ID token verification helpers, structured logger setup.
2. **All Dockerfiles** (agent + workers) build from repo root with `COPY driftscribe_lib/ ./driftscribe_lib/` and `pip install ./driftscribe_lib/` (or include in pyproject as a path dependency).
3. **TDD:** existing tests must still pass after the refactor (no behavior change). Add `tests/unit/test_driftscribe_lib_smoke.py` that imports each module.
4. **Commit.**

### Task 11.3: Reader Agent

**Files:**
- Create: `workers/reader/main.py`, `workers/reader/pyproject.toml`, `workers/reader/Dockerfile`
- Create: `workers/reader/tests/test_read.py`

**Hardcoded policy:** target service = `payment-demo`, region = `asia-northeast1`, project = `$PROJECT_ID`. All three are loaded from env at boot; the request body has NO service/region/project fields — they're not negotiable.

**Steps:**
1. **TDD** `test_read.py`: `POST /read` with empty body returns env+revision for the configured target. With any extra fields → 400. Missing bearer token → 401. Wrong-audience token → 401. Caller email not in allowlist → 403.
2. Implement using `driftscribe_lib.cloud_run.read_live_env`.
3. **Add to cloudbuild.yaml:** new build step + deploy step with `--service-account=reader-agent-sa@...`, `--no-allow-unauthenticated`. SA gets `roles/run.viewer` on the project.
4. Deploy. Smoke test from coordinator's SA → 200; gcloud as user → 403.
5. **Commit.**

### Task 11.4: Docs Agent

**Files:**
- Create: `workers/docs/main.py`, `workers/docs/pyproject.toml`, `workers/docs/Dockerfile`
- Create: `workers/docs/tests/test_patch.py`
- Create: `workers/docs/tests/test_path_allowlist.py`

**Hardcoded policy:** repo = `adi-prasetyo/driftscribe` (env), path allowlist matches regex `^demo/docs/[^/]+\.md$`, refuses `ops-contract.yaml`, `.github/`, `infra/`, `Dockerfile`, `*.py`, anything outside the allowlist.

**Steps:**
1. **TDD** `test_path_allowlist.py` — comprehensive negative tests: `ops-contract.yaml` → 403, `demo/docs/../infra/foo.md` → 403 (normalize-and-check), `.github/workflows/x.yml` → 403, `demo/docs/runbook.md` → 200.
2. **TDD** `test_patch.py` — happy path: patches runbook, opens PR.
3. Implement using `driftscribe_lib.github`.
4. **Add a fine-grained GitHub PAT** scoped to single repo: instruct user to create at https://github.com/settings/personal-access-tokens with `Repository access: Only select repositories: adi-prasetyo/driftscribe`, `Repository permissions: Contents: Read & write`, `Pull requests: Read & write`. Store as Secret Manager `docs-agent-github-pat`.
5. Deploy with own SA (`docs-agent-sa@...`), no project-level IAM grants, `--no-allow-unauthenticated`, `docs-agent-github-pat` injected via `--set-secrets`.
6. **Commit.**

### Task 11.4b: Layer 0 — coordinator tool inventory test (½ day, can run in parallel with 11.5/11.6)

**Files:**
- Create: `tests/unit/test_coordinator_tool_inventory.py`
- Modify: `agent/adk_agent.py` (export `COORDINATOR_TOOLS` as a module-level constant so the test imports the canonical list)

**Why this task is separate:** Layer 0 (capability-bounded tool registry) needs explicit enforcement so a future "let me add a quick helper tool" PR fails CI. The test is the load-bearing artifact for the architectural property.

**Steps:**
1. **In `agent/adk_agent.py`**, factor the tool list into a module-level constant `COORDINATOR_TOOLS = [delegate_to_reader, delegate_to_docs, delegate_to_rollback, delegate_to_notifier, load_contract, search_recent_prs, get_session_state, set_session_state]`. The Agent constructor receives `tools=COORDINATOR_TOOLS`.
2. **TDD `test_coordinator_tool_inventory.py`:**
   - **Positive list assertion:** `{t.__name__ for t in COORDINATOR_TOOLS}` equals exactly the expected set. Hardcode the expected set in the test. New tools fail CI unless the expected set is updated *intentionally*.
   - **Negative pattern assertion:** for every tool, `re.search(r"shell|exec|subprocess|os_command|delete|sudo|raw_http|arbitrary", t.__name__, re.I)` returns None. Catches "I added `delete_old_pr` and now coordinator has a delete tool" mistakes.
   - **Module-import smoke:** `agent.adk_agent` imports cleanly; no top-level side effects that would pull in dangerous SDKs (e.g., assert `subprocess` is not in `sys.modules` after the import — flush + reimport pattern).
3. **In `docs/architecture/multi-agent-design.md`**, add a section "Layer 0: capability-bounded tool registry" with the exhaustive list and a 2-line explanation. Cross-reference the test as the enforcement mechanism.
4. **Commit.**

### Task 11.5: Rollback Agent (execute-only, no UI)

**Files:**
- Create: `workers/rollback/main.py`, `workers/rollback/pyproject.toml`, `workers/rollback/Dockerfile`
- Create: `workers/rollback/tests/test_rollback.py`

**Hardcoded policy:** target service = `payment-demo` (env), target revision must currently exist in the service's revision list AND not be the active revision. Approval token mandatory, single-use, transaction-backed.

**Approval UI lives on the Coordinator, NOT the Rollback Agent** (per Codex review — Rollback is private, can't host a public approval page).

**Steps:**
1. **TDD** `test_rollback.py`:
   - `POST /propose` with `{target_revision, reason}` creates Firestore `approvals/{id}` doc with status=`pending`, returns `{approval_id, approval_url}` where `approval_url` is the coordinator's URL.
   - `POST /execute` requires `{approval_id, approval_token}`. Token must HMAC-verify against `approval-hmac-key` from Secret Manager and match the stored doc. Token has 15-min TTL. Firestore transaction flips status `pending → used` atomically — replay returns 403.
   - Execution calls Cloud Run admin API to update traffic to target revision.
   - Negative tests: missing token, wrong-revision token, expired token, replayed token, target revision == active revision, target revision not in service.
2. Implement.
3. Deploy with `rollback-agent-sa@...`, resource-scoped IAM: `gcloud run services add-iam-policy-binding payment-demo --member=serviceAccount:rollback-agent-sa@... --role=roles/run.developer`. NO project-wide run.developer. `--no-allow-unauthenticated`.
4. SA also needs `roles/datastore.user` for the `approvals` collection (per-collection IAM not available; project-level grant accepted but documented as a known constraint).
5. **Commit.**

### Task 11.6: Notifier Agent

**Files:**
- Create: `workers/notifier/main.py`, `workers/notifier/pyproject.toml`, `workers/notifier/Dockerfile`
- Create: `workers/notifier/tests/test_notify.py`

**Hardcoded policy:** outbound URL = `$NOTIFY_WEBHOOK_URL` from Secret Manager. Caller cannot supply or override.

**Steps:**
1. **TDD** `test_notify.py`: `POST /notify` with `{channel, severity, body}` posts to the env-configured URL with normalized payload. Caller-supplied `url` field is ignored. Channel values constrained to `info|alert|approval`.
2. Implement.
3. Deploy with `notifier-agent-sa@...`. **No GCP roles needed** — service account only used for inter-service auth identity. The Notifier *does* receive a webhook URL secret via `--set-secrets`, which means it needs `roles/secretmanager.secretAccessor` on **that specific secret only** (resource-scoped binding via `gcloud secrets add-iam-policy-binding driftscribe-webhook-url --member=...`).
4. For the demo, use a free webhook test endpoint (e.g., webhook.site) so judges can see the notification fire.
5. **Commit.**

### Task 11.7: Coordinator rewrite — ADK delegation + approval UI

**Files:**
- Modify: `agent/main.py`, `agent/adk_agent.py`, `agent/adk_tools.py`
- Create: `agent/worker_client.py`, `agent/approvals.py`, `agent/templates/approval.html`
- Create: `tests/unit/test_worker_client.py`, `tests/integration/test_approvals.py`

**Steps:**
1. **TDD `test_worker_client.py`:** `WorkerClient.call(worker_name, payload)` fetches an audience-bound ID token (mock metadata server), includes correct headers, parses response, surfaces errors.
2. **TDD `test_approvals.py`:**
   - `GET /approvals/{id}` returns HTML page with the approval details, Approve and Reject buttons, no external assets, `Cache-Control: no-store`, `Referrer-Policy: no-referrer`.
   - `POST /approvals/{id}` requires HMAC-signed token in body (not URL — avoids referrer leaks). On approve: verify token, flip Firestore `pending → approved` in transaction, then call Rollback Agent's `/execute` with the *stored canonical request* (not browser-supplied data).
   - Replay attempt: returns 403.
3. **Replace ADK tools** in `adk_tools.py`:
   - `read_live_env_tool` → `worker_client.call("reader", {})`
   - `propose_rollback_tool(target_revision, reason)` → `worker_client.call("rollback", {...})` returns approval URL
   - `patch_docs_tool(file, section, new_value, rationale)` → `worker_client.call("docs", {...})` (replaces direct PR creation)
   - `notify_tool(severity, body)` → `worker_client.call("notifier", {...})`
   - Keep: `search_recent_prs_tool` (coordinator-internal, uses read-only GitHub token, no worker needed), `load_contract_tool` (coordinator reads its baked-in contract).
4. **Update `SYSTEM_PROMPT`** — teach the new tool set. Key line: "You cannot mutate any system directly. You can ONLY call worker tools. Rollbacks require human approval — propose, then the human decides."
5. **Reduce coordinator's IAM:**
   - Keep: `roles/secretmanager.secretAccessor` (scoped to coordinator-only secrets via per-secret IAM), `roles/run.invoker` on each worker, `roles/datastore.user` (for own session + approval state).
   - Remove: `roles/run.viewer`, `roles/datastore.user` project-wide → scope to `approvals` and `sessions` collections only (Firestore doesn't support collection-scope IAM, so accept project-wide datastore.user and document).
6. **Add `/chat` endpoint** to `main.py`: thin wrapper that packages prompt + optional session_id and invokes the ADK runner. In-memory sessions only (cross-call memory deferred / out of scope).
7. **Commit.**

### Task 11.8: First multi-agent deploy + end-to-end smoke

**Files:**
- Modify: `infra/cloudbuild.yaml` (now builds + deploys 5 services with per-SA flags)
- Modify: `infra/scripts/setup_secrets.sh` (creates 4 worker SAs, applies per-SA IAM, creates per-worker secrets)

**Steps:**
1. Update cloudbuild.yaml — new build + push + deploy steps for each worker. Each deploy gets `--service-account=<worker>-agent-sa@...`, `--no-allow-unauthenticated` (except coordinator), and worker-specific env+secrets.
2. Update setup_secrets.sh — creates Reader/Docs/Rollback/Notifier SAs idempotently, applies per-SA IAM (including the resource-scoped Rollback grant on `payment-demo` only).
3. Deploy.
4. **End-to-end smoke:**
   - `curl -X POST $COORDINATOR/chat -H "X-DriftScribe-Token: ..." -d '{"prompt":"recheck payment-demo"}'` → coordinator delegates to Reader → response includes worker call trace.
   - Negative test: `curl -X POST $COORDINATOR/recheck` without token → 401.
   - Negative test: `curl -X POST $READER_URL/read` (no token) → 401.
   - Negative test: `gcloud auth print-identity-token` (as user) → call worker → 403.
   - Try to prompt-inject coordinator to call `worker_client.call("docs", {"file": "ops-contract.yaml", ...})` → Docs Agent returns 403 (path allowlist).
5. **Commit.**

### Phase 11 Codex review

Send Phase 11 diff + IAM matrix + 4 negative-test results to Codex thread. Apply findings before Phase 13.

### Task 11.9: Apply Codex Phase 11 review (critical + watch fixes)

**Files modified:**
- `driftscribe_lib/approvals.py` — `compute_token_hmac` now binds `approval_id` alongside `target_revision`
- `workers/rollback/main.py` — add `POST /deny` mirroring `/execute` (HMAC verify + transactional flip; no Cloud Run traffic mutation)
- `agent/worker_client.py` — add `call_deny()` wrapper
- `agent/main.py` — reject path now calls `worker_client.call_deny`; add `_map_worker_error` so 409/5xx pass through instead of collapsing to 403
- `agent/approvals.py` — delete `deny()` helper (security path moved to worker)
- `docs/runbooks/deploy.md` — require fine-grained read-only PAT, not classic
- `docs/architecture/iam-matrix.md` — coordinator-row negative-space note; "Phase 11.9 carry-overs" section
- `docs/architecture/multi-agent-design.md` — rollback row notes `/deny`; "Layer 1 caveats" subsection
- `tests/unit/test_approval_store.py`, `workers/rollback/tests/test_rollback.py`, `tests/integration/test_approvals.py` — HMAC signature update + new `/deny` tests + worker error-mapping tests

**Critical fixes applied:**
1. `POST /approvals/{id}` reject path no longer bypasses HMAC verification — the rollback worker's new `/deny` endpoint validates the operator's token before flipping `pending → denied`. Closes the HITL availability bug.
2. Documented Layer 1 weakening: fine-grained PAT requirement, `run.viewer` carry-over.

**Cheap watch-item fixes applied:**
- HMAC binding now `token | approval_id | target_revision` (cross-approval replay defense in depth).
- Coordinator's worker-error mapping splits 409 (passthrough) / 5xx (502) / other 4xx (403).

**Deferred to Phase 13** — see the carry-over block below.

---

## Phase 13 — Self-Healing Rollback Decision Path (2–3 days)

The Rollback Agent (11.5) executes; this phase wires the coordinator's *decision* to propose rollback when appropriate.

### Carry-over from Phase 11 Codex review (apply in 13)

Phase 11.9 applied the two critical Codex findings (reject-path token bypass + Layer 1 doc-overstatement) and two cheap watch-item fixes (HMAC binds `approval_id`; worker 409/5xx no longer collapse to 403). Three watch items were explicitly deferred to Phase 13 — these are the closures needed before the multi-agent claim is unconditionally true:

1. ~~**Migrate classifier `/recheck` to use the Reader Worker.**~~ **DONE in Phase 13** (commit `fix(13): migrate classifier /recheck to Reader Worker — close run.viewer carry-over`). Both `/recheck` paths now delegate live-state reads to the Reader Worker; `roles/run.viewer` removed from `setup_secrets.sh`'s coordinator grants AND actively removed via `gcloud projects remove-iam-policy-binding ... || true` so existing deploys clean up on re-run. The Layer 1 negative-space claim "coordinator cannot read other services' state" now holds in IAM, not just in docs.

2. ~~**Layer 0 tool-signature whitelist.**~~ **DONE in Phase 13** (commit `test(13): pin coordinator tool param signatures — Layer 0 carry-over`). The inventory test in `tests/unit/test_coordinator_tool_inventory.py` (Phase 11.4b) catches dangerous tool *names* via a regex. Codex flagged a residual capability-escape: a safely-named tool that accepts a `cmd`, `url`, `endpoint`, `payload`, or `raw_request` parameter could let the LLM widen capability through the parameter rather than the tool name. Phase 13 added `test_no_tool_has_dangerous_parameter_name` (parametrized over `COORDINATOR_TOOLS`) plus a regex smoke test pinning the positive/negative cases. The current 6-tool registry passes cleanly; the test pins the property as the registry grows.

3. ~~**`DecisionAction.ROLLBACK` must preserve the worker/HITL boundary.**~~ **DONE in Phase 13** (commit `feat(13.3): end-to-end rollback control flow + HITL boundary test`). `_do_recheck` branches to `_do_rollback` after validation — the LLM emits the decision JSON, the coordinator routes through Rollback Worker `/propose` (HMAC-token mint + approval URL) and Notifier `/notify` with channel=approval, and the response carries `approval_url` (token embedded as `?t=`) but NOT `approval_token` as a separate field. The integration test `tests/integration/test_rollback_e2e.py::test_rollback_decision_does_not_execute_the_rollback` explicitly asserts that `call_execute` and `call_deny` are never invoked on the recheck path — Cloud Run mutation lives behind the operator's `/approvals/{id}` POST, which the existing 11.9 handler routes to `call_execute` / `call_deny`. The HITL gate is now structurally mandatory: no code path in `agent/main.py` reaches `call_execute` without an operator-initiated POST with the token.

These are tracked in this carry-over block so the Phase 13 PR description can check them off.

### Task 13.1: `DecisionAction.ROLLBACK` + validator policy ~~~~ **DONE in Phase 13**

**Files:**
- Modify: `agent/models.py`, `agent/validator.py`
- Create: `tests/unit/test_validator_rollback.py`

**Validator policy:**
- Accept `rollback` for diffs where `contract_status == present_disallow_manual` AND a previous revision exists.
- Reject `rollback` if `contract_status == present_allow_manual` (use `docs_pr` there instead).
- Reject `rollback` without `requires_human_review=true`.

**Steps:** TDD-style, implement, commit.

### Task 13.2: Renderer outputs approval URL for rollback ~~~~ **DONE in Phase 13**

**Files:** Modify `agent/renderer.py`, create `tests/unit/test_renderer_rollback.py`.

Rendered body for a rollback decision includes the approval URL pointing at the coordinator's `/approvals/{id}` (NOT the Rollback Agent — it's private), a clear CTA, the canonical rollback details (service, target_revision, reason), and a 15-minute expiry note.

### Task 13.3: End-to-end rollback flow ~~~~ **DONE in Phase 13**

**Files:** Create `tests/integration/test_rollback_e2e.py`.

Mock Reader → drift on `present_disallow_manual` → coordinator's ADK proposes `rollback` → validator accepts → renderer produces approval URL → Notifier called with severity=`approval` and the URL. Then: human POSTs to `/approvals/{id}` with valid token → coordinator calls Rollback Agent's `/execute` → assert Rollback Agent's execute was called with the canonical stored payload.

Implementation notes:
- `_do_recheck` in `agent/main.py` branches to `_do_rollback` after `validate()`. The ROLLBACK control flow is structurally different from the other actions: `claim_event → propose → render → notify` (instead of `render → claim_event → perform_action`), because render needs the approval URL minted by the worker's response.
- Response schema diverges: rollback returns an `"approval": {approval_id, approval_url, expires_at}` block in place of the `"github"` block. `approval_token` is deliberately NOT echoed — it's already embedded in `approval_url` as `?t=<token>`, and exposing it separately would double the leak surface.
- `SYSTEM_PROMPT_RECHECK` (`agent/adk_agent.py`) gained `rollback` as a valid action with `target_revision` field. The prompt explicitly forbids the LLM from inferring a revision name — only propose rollback when a concrete previous revision came back from tool output. (Phase 13 limitation: Reader Worker doesn't yet return previous revisions; future phase will extend it.)
- 9 integration tests in `tests/integration/test_rollback_e2e.py`: happy path, HITL boundary safety property (carry-over #3 closure), operator approve POST, operator reject POST, notifier-failure claim release, propose-failure claim release, malformed-propose-response 502, idempotent retry, and a defensive 500 for ROLLBACK on the non-ADK path.

### Phase 13 Codex review ~~~~ **DONE in Phase 13**

Codex review of the full Phase 13 commit set (`9e7dd37` → `bd05097`) verified all three Phase 11.9 carry-overs as genuinely closed and surfaced four follow-up findings. A second-pass review of the applied fixes flagged one more (W2-concurrency) for Phase 14.

Applied immediately (commit `32203fa`):

- **W2 (correctness, applied):** cached rollback decisions outlived their 15-min TTL — a `/recheck` retried after expiry returned the dead approval URL from cache. `_cached_rollback_is_expired()` helper drops the cache hit (releases the event claim) for expired rollback decisions so the next `/recheck` re-proposes a fresh approval. Pinned by `test_cached_rollback_with_expired_approval_re_proposes`.
- **W4 (test hardening, applied):** the HITL-boundary test only checked worker NAMES (`{"reader","rollback","notifier"}`), so a future `worker_client.call("rollback", payload, endpoint="/execute")` would have silently passed. Added an explicit assertion in `test_rollback_decision_does_not_execute_the_rollback` that no `m_call.call_args_list` entry has `kwargs["endpoint"]` set to `/execute` or `/deny`.

Promoted to **Phase 14 blockers** by Codex's second-pass review:

- **W2-concurrency (Phase 14 blocker):** my W2 fix calls `state.release_event(event_key)` unconditionally before re-claiming. Safe on `InMemoryStateStore` (single-process tests), but on real Firestore two concurrent retries can race: A deletes and re-claims; B (holding a stale read) then deletes A's fresh claim. Could double-mint approvals or corrupt the event→decision pointer. HITL-safe (operator still gates `/execute`), but weakens idempotency exactly under retry pressure. **Fix before Phase 14:** add a compare-and-delete `evict_cached_decision(event_key, decision_id)` method to the StateStore protocol, implemented as a Firestore transaction that deletes the event doc only if its `decision_id` still equals the expired cached decision's id. Concurrent losers will then either get a 409 or see the fresh decision.
- **W3 (Phase 14 blocker — promoted from "deferred"):** the Reader Worker returns `latest_ready_revision`, not the actual traffic-serving revision. After a successful rollback, `/recheck` can keep reporting drift because it sees the newer-but-not-serving revision. For manual `/chat` flows this is documentation-only; for **Eventarc auto-trigger** loops it's a real risk of re-proposing rollback against a revision already not-serving. **Fix before Phase 14:** read `service.traffic[].revision` (the serving revision) in `driftscribe_lib/cloud_run.py::read_live_state`.

Deferred to Phase 15:

- **W1 (defense-in-depth, deferred):** the rollback validator only checks that all `EnvDiff.contract_status` fields say `PRESENT_DISALLOW_MANUAL`; it does NOT re-derive that from `contract.expected_env`. An ADK proposal could mislabel a var and pass the validator. HITL still prevents automatic mutation; small fix worth doing when Phase 14 validation work happens.
- **W2-malformed-expires (Phase 15 polish):** `_cached_rollback_is_expired` returns False (treats as still-valid) for malformed/missing `expires_at`. Codex suggests inverting this in Phase 15: treat malformed as expired so a corrupted cache entry can't strand the operator on a dead URL.
- **Nit (Phase 15 polish):** `_do_rollback`'s malformed-propose-response handling rejects missing `approval_url`/`approval_id` but a non-dict JSON response would raise before releasing the claim. Easy to harden; low probability with the current worker.

---

## Phase 14 — Eventarc Auto-Trigger (1–2 days, bonus)

**Important per Codex review:** Eventarc audit-log triggers can be delayed, deduplicated, or filtered wrong. Manual `/chat` stays the primary demo path; Eventarc is a **bonus proof** shown second.

### Task 14.1: Discover the real Eventarc audit-log shape

**Steps:**
1. Manually update payment-demo's env: `gcloud run services update payment-demo --update-env-vars=DEMO=1`.
2. Pull the corresponding audit log entry: `gcloud logging read 'resource.type=cloud_run_revision AND protoPayload.methodName=~"Services\.UpdateService"' --limit 1 --format=json`.
3. Inspect the actual `methodName` (it may be `google.cloud.run.v2.Services.UpdateService` or `google.cloud.run.v1.Services.ReplaceService` depending on which API path the CLI used). Record the exact value in the plan and code.

### Task 14.2: `/eventarc` endpoint with bearer auth

**Files:**
- Modify: `agent/main.py`
- Create: `tests/integration/test_eventarc.py`

**Auth model:** `/eventarc` requires `Authorization: Bearer <id-token>`. The token is verified via `google.oauth2.id_token.verify_oauth2_token`. The email claim must match `eventarc-trigger-sa@$PROJECT.iam.gserviceaccount.com`. Anything else → 401/403. This works EVEN THOUGH coordinator is `--allow-unauthenticated`, because the auth is enforced at the application layer.

**Steps:** TDD, implement, deploy.

### Task 14.3: Create the trigger

```bash
# Add to setup_secrets.sh
gcloud iam service-accounts create eventarc-trigger-sa --project "$PROJECT"
gcloud run services add-iam-policy-binding driftscribe-agent \
  --member=serviceAccount:eventarc-trigger-sa@$PROJECT.iam.gserviceaccount.com \
  --role=roles/run.invoker \
  --region=asia-northeast1

gcloud eventarc triggers create driftscribe-cloudrun-changes \
  --project "$PROJECT" \
  --location=asia-northeast1 \
  --destination-run-service=driftscribe-agent \
  --destination-run-path=/eventarc \
  --event-filters="type=google.cloud.audit.log.v1.written" \
  --event-filters="serviceName=run.googleapis.com" \
  --event-filters="methodName=<DISCOVERED-IN-14.1>" \
  --service-account=eventarc-trigger-sa@$PROJECT.iam.gserviceaccount.com
```

### Task 14.4: E2E smoke

`gcloud run services update payment-demo --update-env-vars=NEW_THING=test` → poll for up to 60s → check Firestore (or coordinator Cloud Run logs, since DRY_RUN=true demo deploys use InMemoryStateStore) for a new decision document with `trigger="eventarc"`. Document observed latency in `docs/benchmarks.md`. The poll budget is 60s (not 30s) because Eventarc cold-start + audit-log → trigger SA invocation latency is occasionally several seconds on top of `/eventarc` processing; 60s leaves head-room without making FAIL ambiguous.

### Task 14.5: Migrate coordinator LLM auth from AI Studio API key to Vertex AI ADC

**Why:** The coordinator currently reads `GOOGLE_API_KEY` and `google-genai` routes through AI Studio — separate billing/quota/revocation surface from GCP. Switching to Vertex AI consolidates everything under the project's existing billing and the `driftscribe-agent-sa` ADC. Removes the AI Studio top-up + leaked-key-revocation chores from the operator action list.

**Mechanism:** `google-genai` auto-detects the path via env: setting `GOOGLE_GENAI_USE_VERTEXAI=true` + `GOOGLE_CLOUD_PROJECT` + `GOOGLE_CLOUD_LOCATION` flips it to Vertex AI. **No Python code changes** — `agent/adk_agent.py`'s `Agent(model="gemini-2.5-flash", ...)` calls route through the SDK's env-driven provider selection.

**Files (10):** `agent/config.py` (drop `google_api_key` field), `infra/cloudbuild.yaml` (drop secret mapping, add three env vars), `infra/scripts/setup_secrets.sh` (drop positional arg + secret create + bind; add `aiplatform.googleapis.com` API enable + `roles/aiplatform.user` to COORD_SA), `.env.example` (drop `GOOGLE_API_KEY`, add ADC hint), `docs/runbooks/deploy.md` (drop `GEMINI_KEY` prereq, add local ADC note, update quota wording), `docs/architecture/iam-matrix.md` (drop secret, add role), `agent/main.py` and `infra/scripts/e2e_smoke.sh` (wording fixes Codex caught), this plan doc.

**Verification:**
- Test suite: 424 still passing (no Python touched).
- `bash -n infra/scripts/setup_secrets.sh` parses clean.
- Post-deploy: `gcloud run services describe driftscribe-agent` shows no `GOOGLE_API_KEY` and the three new `GOOGLE_GENAI_*` env vars.
- Post-deploy: `RUN_POSITIVE=1 ./infra/scripts/e2e_smoke.sh` exercises the Vertex AI path end-to-end (single `/chat` call).

**Operator cleanup (manual):** orphaned `gemini-api-key` secret in Secret Manager can be deleted via `gcloud secrets delete gemini-api-key --project=$PROJECT` after the Vertex AI deploy is verified. The script intentionally does NOT auto-delete (rollback safety).

**Quota note:** Vertex AI Gemini quota is per-project/region/model — separate from AI Studio. Not necessarily larger. Check the GCP Console's Vertex AI quota dashboard for `generate-content` in `asia-northeast1` before relying on heavy use.

### Phase 14 Codex review ~~~~ **DONE in Phase 14**

Codex review of the full Phase 14 commit set (`58e2957` → `85203d3`, 13 commits, 393 → 424 tests) on thread `019e3af3-f679-7d20-bff1-328295c8f5df`.

**Verified clean:**

- **W3 traffic-serving fix** (commit `96b58cb`): `traffic_statuses → traffic → latest_ready_revision → template` priority is correct; empty `traffic_statuses` doesn't crash; `_highest_percent_entry` is deterministic on ties.
- **/eventarc auth surface**: missing/malformed bearer → 401 with no oracle, wrong principal → 403 without echoing the email, target service/region mismatch → 200 ignored (Eventarc-friendly), valid path dispatches `_do_recheck("eventarc")`.
- **`EVENTARC_AUDIENCE` per-request resolution**: pragmatic choice (startup enforcement would fight the post-deploy URL stamping in `cloudbuild.yaml`); 503 fail-closed at request time is acceptable.
- **Smoke harness `a503040` fixes**: cleanup-failure no longer swallowed, exact-name match, INT/TERM exits after cleanup, latency assertion is real.

**Deferred to Phase 15:**

- **W2 CAS-loser fallthrough (Phase 15 polish):** when `evict_cached_decision` returns `False` (we lost the race) and the immediate re-read finds no fresh decision yet, the request currently falls through and may become the new proposer. `record_event` still serializes claims so this does NOT double-mint approvals, but the documented intent ("loser sees fresh decision") is weaker than the code delivers. Cleaner shape: on CAS-False + no non-expired hit, return `409 event in-progress, retry`. Low-stakes because HITL still gates `/execute`; defer.
- **/eventarc post-auth 400 retry storm (Phase 15 polish):** post-auth malformed-payload responses return 400, but Eventarc retries non-2xx for up to ~24h. If Google ever ships an audit-log schema change, our endpoint becomes a retry-loop sink. Fix: change post-auth malformed-body responses from `400` → `200 {"ignored":"malformed-payload"}` with structured logging. Low-probability today (Eventarc never sends malformed audit logs in normal operation); defer.
- **Smoke harness probe specificity (acknowledged limitation):** the log-based PASS path proves `/eventarc` returned 200 after `record_iso`, not that the decision corresponds to the exact `NEW_THING` mutation. Acceptable for a one-shot smoke probe; tighten only if false positives surface.
- **Trigger filter empirical confirmation (operator action):** `setup_secrets.sh` §10 hardcodes v2 `UpdateService` + exact `resourceName=payment-demo`. The audit-log filter shape was committed to per `docs/architecture/eventarc-payload.md` but not yet empirically validated against a real `gcloud logging read` from the deployed project. `docs/runbooks/deploy.md` Step 7 captures this as an operator-side verification step with the v1 `ReplaceService` fallback diff. Treat 14.3 as provisional until the operator confirms.
- Carry-overs unchanged: **W1 rollback validator re-derive**, **email-claim `==` (timing)**, **JSON parse error string leak**, **W2-malformed-expires** all remain Phase 15 polish.

**Operator action (post-14.5):** revoke the leaked AI Studio API key at https://aistudio.google.com — it's no longer read by DriftScribe but remains valid anywhere else it was used until revoked. Then delete the orphaned `gemini-api-key` Secret Manager entry, and remove its IAM binding from `driftscribe-agent-sa` (`gcloud secrets remove-iam-policy-binding gemini-api-key …`) so the SA's negative-space claim is enforced at the IAM layer, not just by the missing env var. Task 14.5 supersedes the older "Top up Gemini credits" and "Revoke leaked Gemini API key" operator chores — both surfaces are gone now that the coordinator uses Vertex AI ADC.

---

## Phase 15 — Hardening (1–2 days)

### Task 15.1: GitHub Actions CI

**Files:** `.github/workflows/ci.yml`

Runs on PR + push to main. Steps: checkout, setup Python 3.12, `uv sync`, `uv run ruff check`, `uv run pytest -q`. Add status badge to README.

### Task 15.2: Structured JSON logging with trace IDs

**Files:** Modify `driftscribe_lib/logging.py`, modify every `main.py`.

- Each `/chat`, `/recheck`, `/eventarc`, worker request gets a unique `trace_id` (UUIDv4).
- Trace ID propagates to worker calls via `X-Trace-Id` header.
- Worker logs adopt the inbound trace_id (or generate one if absent).
- Output is JSON (one event per line) with `trace_id`, `service`, `level`, `msg`, plus structured fields.

### Task 15.3: Phase 14 + 14.5 follow-up watch list

Codex's Phase 14.5 post-impl review surfaced four items worth a Phase 15 pass:

- **Deployed positive smoke for Vertex AI `/chat`:** unit tests mock `_run_adk_agent` and so don't actually exercise Vertex AI auth. Add a `RUN_POSITIVE=1` step in the E2E harness that captures the first end-to-end `/chat` round-trip post-Vertex-migration. (The smoke step already exists at `e2e_smoke.sh:85` as `[1] /chat with token`; operator just needs to run it once with `RUN_POSITIVE=1` after deploy. Documenting here so it doesn't fall off the radar.)
- **Confirm no `GOOGLE_API_KEY` env or secret mapping** on the active revision via `gcloud run services describe driftscribe-agent`. If it still appears (e.g., from a stale revision pinned by traffic split), force-deploy a new revision.
- **Delete or unbind `gemini-api-key`:** `gcloud secrets remove-iam-policy-binding gemini-api-key --member=serviceAccount:driftscribe-agent-sa@... --role=roles/secretmanager.secretAccessor` and then `gcloud secrets delete gemini-api-key`. Closes the IAM-layer enforcement of the no-LLM-key claim.
- **Vertex AI quota in `asia-northeast1`:** track GCP Console's Vertex AI Quotas dashboard for `generate-content` requests/minute. Not necessarily larger than AI Studio's free tier; surprises before the demo would be bad.
- **(Carry-over from Phase 14 review)** Tighten `/eventarc` post-auth malformed payload handling: flip 400 → 200 with `{"ignored":"malformed-payload"}` to avoid the Eventarc retry-storm risk if Google ever ships an audit-log schema change.
- **(Carry-over from Phase 14 review)** W2 CAS-loser: on `evict_cached_decision` returning `False` + no fresh decision, return `409 event in-progress, retry` instead of falling through to become the new proposer.
- **(Carry-over)** `/eventarc` email-claim timing-attack `==` → `hmac.compare_digest`. Plus the JSON parse error string leak hardening.
- **(Carry-over)** W1 rollback validator re-derive `contract_status` from `contract.expected_env` instead of trusting the LLM proposal's label.

### Phase 15 Codex review

Codex thread `019e4013-46d6-7571-845e-8d4312bfe816` (2026-05-19, post Phase 15.3). All four code-side carry-overs from 14/14.5 confirmed in place; cross-task integration (trace middleware × early-return 200s) holds. Findings to act on:

**Bugs (real, cheap):**
- `/eventarc` `claims.get("email") or ""` passes truthy non-string (e.g. `123`, `[]` would be `""` post-coerce but `["x"]` slips through) into `hmac.compare_digest` and 500s. If `email` is missing or not a string, return 403 instead. (`agent/main.py:838-840`.)
- `/eventarc` truthy non-string `service_name` / `location` labels (e.g. `["payment-demo"]`, `{"name": "x"}`) currently fall to the `non-target-service` 200-ignored branch and are echoed back in the response body — partially defeating the 15.3 "fixed short reason, no payload echo" intent. Falsy non-strings (`[]`, `{}`) happened to be caught by the `not service` truthiness check, but only by accident — type contract isn't pinned. Normalize with `isinstance(..., str)` up front so both shapes land in the `missing_service_or_region` malformed-payload reason. (`agent/main.py:870-878`.)

**Watch-list tests (3):**
- Cross-task integration: `/eventarc` ignored paths preserve inbound `X-Trace-Id` and reset the ContextVar afterward.
- Targeted malformed-label test: `service_name=[]` → `malformed-payload` (not `non-target-service`).
- Optional: verified claims with `email=123` should not 500 (folded into the bug fix above).

**Promoted to later phases:**
- Phase 18: optional startup canary that fails loudly if `USE_ADK=true` AND `GOOGLE_API_KEY` is set OR Vertex env vars are absent. Operator-side `gcloud run services describe` check covers the hackathon path; canary is for post-submission survivability.
- Phase 16.2 scenario runner should print the `X-Trace-Id` response header on each beat — turns Phase 15 logging into a demoable asset.
- Phase 16.x README polish: remove the stale "Under construction" pointer to the May 18 MVP plan so the new CI badge doesn't sit above outdated project-status copy.

---

## Phase 16 — Submission Polish (4 days)

### Task 16.1: HTML architecture diagram

**Files:** Create `docs/architecture/architecture.html`.

Single self-contained HTML file. Inline SVG. Two diagrams:
1. **Trigger fan-in** — boxes for Eventarc, NL `/chat`, manual `/recheck` arrows into Coordinator → fan-out to four workers → outcomes (Firestore, GitHub PR, Cloud Run rollback, webhook).
2. **Layered safety boundary** — for each worker: dotted IAM-scope box outside, hardcoded-policy box inside, plus an "HITL gate" diamond between Coordinator and Rollback for destructive ops. Coordinator drawn with an *empty* mutation-permission box to highlight the negative space.

Style: minimalist, two-color, no animation, mobile-friendly. Linked from README.

### Task 16.2: Scenario runner CLI + English demo-script

**Files:** Create `scripts/demo.sh`, `docs/demo-script.md`.

`scripts/demo.sh` Bash, well-commented, demoable line-by-line:
- `beat-a` — baseline check (expect `no_op`)
- `beat-b` — flip `PAYMENT_MODE=live` (expect `drift_issue`)
- `beat-c` — flip an unknown var like `NEW_THING=test` (Beat C, expect either ADK reasoning or escalation depending on USE_ADK)
- `beat-d` — flip `FEATURE_NEW_CHECKOUT=true` (expect `docs_pr` with preview)
- `beat-e` — combo: drift on `present_disallow_manual` AND request rollback via `/chat` (expect `rollback` with approval URL)
- `cleanup` — restore baseline

`docs/demo-script.md` — written for the operator at the keyboard. Screen layout (left: terminal, right: browser with `architecture.html` open). Second-level timing. Exact commands. What the audience sees on each transition.

### Task 16.3: Japanese README + demo-script

**Files:** `README.ja.md`, `docs/demo-script.ja.md`.

Translate. Cross-link top-of-file.

### Task 16.4: Cost & Latency micro-section in README

**Steps:** Run 20 `/chat` calls back-to-back; record p50/p95 latency. Pull GCP cost-per-call from the billing breakdown. Add a 4-line section to README:

> **Cost & Latency** — Per /chat call: ~$0.0002 GCP + ~$0.0001 Gemini = ~$0.0003. p50 latency: <Xms classifier-path, <Yms ADK-path. p95: <Z. Idle cost at min-instances=0: $0. Demo total spend over hackathon: $<actual>.

### Task 16.5: Comparison table in README

**Steps:** Add a section to README comparing DriftScribe with Drift (CloudPosse), Steampipe, Cloud Custodian, AWS Config Rules. Honest axes: AI-driven, HITL gates, OS-enforced + policy-enforced safety, multi-cloud, open source, deployment surface, target user. DriftScribe wins on AI + layered safety; loses on multi-cloud + maturity.

### Task 16.6: ProtoPedia submission text (Japanese + English)

**Files:** `docs/submission/protopedia.ja.md`, `docs/submission/protopedia.en.md`.

Standard ProtoPedia sections: タイトル / Title, 概要 / Summary, ハイライト / Highlights (multi-agent layered safety, HITL, ADK), 技術スタック / Stack, デモ / Demo, リポジトリ / Repo URL, デプロイ済みURL / Deployed URLs.

### ~~Task 16.7: 90-second video script + recording plan~~ (deferred)

**Status (2026-05-19):** Deferred — user expects more features to land before submission;
writing a beat-by-beat video script now would go stale. The video script will be
written closer to the actual recording (Phase 18.2), once feature scope is final.
Beat structure preserved here for reference when the task is revived:

- 0:00–0:10 — opening hook ("operators have a config-drift problem; here's an AI agent that fixes it safely")
- 0:10–0:30 — show `/chat` and the multi-agent architecture diagram side-by-side
- 0:30–0:50 — Beat B (drift_issue) + Beat D (docs_pr)
- 0:50–1:15 — Beat E (rollback) — show the approval link → human approval → execution
- 1:15–1:30 — the "jailbreak fails" beat: try to prompt-inject coordinator into deleting payment-demo. Show it fail. Roll credits.

Narration in English; Japanese subtitles. Tools: OBS Studio + DaVinci Resolve.

### Phase 16 Codex review

---

## Phase 18 — Final Submission (1 day, ~July 7)

### Task 18.1: Verify everything still works
- Confirm Vertex AI Gemini quota for `generate-content` on `gemini-2.5-flash` in `asia-northeast1` is healthy (Phase 14.5 replaced the AI Studio top-up chore with a per-project Vertex AI quota check — see GCP Console → Vertex AI → Quotas).
- Run `scripts/demo.sh` through all beats.
- Confirm `architecture.html` renders on mobile + desktop.

### Task 18.2: Record video (USER ACTION)

Per the script.

### Task 18.3: Submit to ProtoPedia + Findy (USER ACTION)

Paste prepared text. Attach video. Cross-link the deployed URLs + architecture diagram + repo.

---

## Out of Scope (Deliberately)

- ~~Firestore-backed ADK Sessions~~ — in-memory only.
- ~~Multi-service contract support~~ — `payment-demo` only.
- ~~Real Slack/Discord integration~~ — generic webhook to webhook.site is enough.
- ~~Multi-cloud (AWS/Azure adapters)~~ — Google Cloud Japan hackathon.
- ~~`DRY_RUN=false` as default~~ — stays true; the path to flip safely is documented but not shipped.
- ~~Production hardening (rate limits, retries, circuit breakers)~~ — architectural points are judged, not production-readiness.

## Risks + Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Vertex AI Gemini quota throttles during demo | Med | High | Phase 14.5 routes auth through Vertex AI ADC, so quota is per-project/region/model — check GCP Console → Vertex AI → Quotas for `generate-content` on `gemini-2.5-flash` in `asia-northeast1` and request an increase if the demo will burst. Set a budget alert on the project. Beat C smoke test before recording. |
| Cloud Run-to-Cloud Run auth spike reveals incompatibility | Low | High | **Spike is Task 11.0.** Falls back to shared HMAC headers if it fails — design is unchanged but signature different. |
| Eventarc latency >30s ruins live demo | Med | Low | Manual `/chat` is primary; Eventarc is bonus. |
| HITL approval UI feels clunky on video | Low | Low | Page is intentionally single-button, no external assets, pre-opened browser tab. |
| Phase 11 overruns the 6–8 day estimate | Med | Med | Cut Phase 13.3 e2e test or Phase 14 entirely if blocked past 12 days on Phase 11. |
| Confused-deputy attacks slip through worker policy validation | Med | High | Each worker has exhaustive negative tests (Task 11.3–11.6). Codex reviews per phase catch design omissions. |
| Approval token replay or CSRF | Low | High | Transactional one-time HMAC tokens, POST not GET, no-store cache, no-referrer policy. |
