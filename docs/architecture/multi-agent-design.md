# DriftScribe multi-agent architecture

> **Status:** Live, built out past Phase 17. **Four workloads ship:** **drift** (Cloud Run env vs ops contract), **upgrade** (npm `package.json` vs GitHub Advisory DB), **explore** (chat-only, strictly read-only investigation across infra + code), and **provision** (chat-only, authors `iac/`-only OpenTofu PRs for the gated apply pipeline). The coordinator routes per `workload=<name>` and only ever shows the LLM that workload's tool subset. The MCP attaches at the coordinator only. The drift/upgrade framework is Phase 17 (`docs/plans/2026-05-19-driftscribe-phase17-framework-mcp.md`); explore/provision and the infra workers (`infra-reader`, `tofu-editor`, `tofu-apply`) come from the infra-IaC initiative (`docs/plans/2026-05-27-infra-iac-agent-design.md` and the `infra-iac-phase-*` plans). For a plain-English tour see [`../OVERVIEW.md`](../OVERVIEW.md).

---

## 1. System topology

DriftScribe is a coordinator + per-workload worker fleet. Each service runs on its own Cloud Run service with its own dedicated service account. The coordinator is the only public-facing entrypoint; workers refuse direct human traffic. The coordinator additionally attaches Google's Developer Knowledge MCP as a reasoning-time tool.

The diagram below shows the original drift + upgrade topology extended with the infra workloads (**explore**, **provision**) and their workers (`infra-reader`, `tofu-editor`). One worker — `tofu-apply`, the sole live-infra mutator — is deliberately **not** in the coordinator's worker registry: the chat agent can only ever open an `iac/` PR via `tofu-editor`, and `tofu-apply` runs **downstream** behind a plan-bound, HMAC-signed operator approval (see §3 and the `infra-iac-phase-c*` plans). The full live service inventory is in the table after the diagram.

```mermaid
flowchart LR
    Human["Operator / demo curl"]
    Eventarc_GCP["Eventarc\n(Cloud Run audit logs)"]
    Webhook["External webhook\n(webhook.site for demo)"]
    GitHub["GitHub Contents/PR API\n+ Advisory DB"]
    CloudRunAdmin["Cloud Run admin API\n(payment-demo)"]
    CAI["Cloud Asset Inventory\n(whole-project read)"]
    GCPResources["GCP resource APIs\n(Cloud Run, GCS, Pub/Sub…)"]
    DKMCP["Developer Knowledge MCP\n(googleapis.com)"]

    subgraph Coordinator["driftscribe-agent (coordinator)"]
        Chat["/chat"]
        Recheck["/recheck"]
        Eventarc["/eventarc"]
        Approvals["/approvals/{id} + /iac-approvals/{n}"]
        subgraph Drift["Drift agent\n(prompt + 8 tools)"]
            ADKd["ADK Agent"]
        end
        subgraph Upgrade["Upgrade agent\n(prompt + 8 tools)"]
            ADKu["ADK Agent"]
        end
        subgraph Explore["Explore agent — chat-only, read-only\n(prompt + 6 read tools)"]
            ADKe["ADK Agent"]
        end
        subgraph Provision["Provision agent — chat-only, 1 mutation tool\n(prompt + 6 tools)"]
            ADKp["ADK Agent"]
        end
        Chat --> ADKd
        Chat --> ADKu
        Chat --> ADKe
        Chat --> ADKp
        Recheck --> ADKd
        Eventarc --> ADKd
        %% /recheck serves drift; upgrade is rechecked via GitHub branch
        %% observation. explore + provision are chat-only —
        %% agent/main.py::CHAT_ONLY_WORKLOAD_NAMES refuses /recheck for them.
    end

    subgraph DriftWorkers["Drift workers (--no-allow-unauthenticated)"]
        Reader["driftscribe-reader\n/read"]
        Docs["driftscribe-docs\n/patch"]
        Rollback["driftscribe-rollback\n/propose, /execute"]
    end
    subgraph UpgradeWorkers["Upgrade workers (--no-allow-unauthenticated)"]
        UReader["driftscribe-upgrade-reader\n/read"]
        UDocs["driftscribe-upgrade-docs\n/patch, /close, /merge"]
    end
    subgraph InfraWorkers["Infra workers (--no-allow-unauthenticated)"]
        InfraReader["driftscribe-infra-reader\n/describe"]
        TofuEditor["driftscribe-tofu-editor\n/open-pr"]
    end
    Notifier["driftscribe-notifier\n/notify (shared)"]
    TofuApply["driftscribe-tofu-apply\n/apply — sole live-infra mutator\n(downstream, NOT coordinator-invoked)"]

    Human -- "X-DriftScribe-Token" --> Coordinator
    Eventarc_GCP -- "Google-signed ID token" --> Eventarc
    ADKd -- "Bearer ID token" --> Reader
    ADKd -- "Bearer ID token" --> Docs
    ADKd -- "Bearer ID token" --> Rollback
    ADKu -- "Bearer ID token" --> UReader
    ADKu -- "Bearer ID token" --> UDocs
    ADKe -- "Bearer ID token" --> Reader
    ADKe -- "Bearer ID token" --> UReader
    ADKe -- "Bearer ID token" --> InfraReader
    ADKp -- "Bearer ID token" --> Reader
    ADKp -- "Bearer ID token" --> InfraReader
    ADKp -- "Bearer ID token" --> TofuEditor
    ADKd -- "Bearer ID token" --> Notifier
    ADKu -- "Bearer ID token" --> Notifier
    ADKd -- "X-Goog-Api-Key" --> DKMCP
    ADKu -- "X-Goog-Api-Key" --> DKMCP
    ADKe -- "X-Goog-Api-Key" --> DKMCP
    ADKp -- "X-Goog-Api-Key" --> DKMCP
    Reader --> CloudRunAdmin
    Docs --> GitHub
    Rollback --> CloudRunAdmin
    UReader --> GitHub
    UDocs --> GitHub
    InfraReader --> CAI
    TofuEditor --> GitHub
    Notifier --> Webhook
    Human -- "Approve / Reject rollback\n(HMAC-signed)" --> Approvals
    Human -. "Approve infra plan\n(plan-bound HMAC) → gated apply" .-> TofuApply
    TofuApply --> GitHub
    TofuApply -. "tofu apply" .-> GCPResources
```

### Service inventory

| Service | Public? | Workload | Owns | Notes |
| --- | --- | --- | --- | --- |
| `driftscribe-agent` (coordinator) | Yes — `--allow-unauthenticated` + `X-DriftScribe-Token` | all 4 | ADK agent loop, intent classification, approval HTML/HMAC, Firestore session + approval state, Developer Knowledge MCP attach, infra resource-map serve | Single entrypoint for humans, Eventarc, and demo scripts. 14 wired callables in `TOOL_REGISTRY` (+2 reserved session-memory slots); the LLM only ever sees the per-workload subset — 8 drift, 8 upgrade, 6 explore (all read-only), 6 provision (one of them the `provision_open_infra_pr` mutation tool). See §4. |
| `driftscribe-reader` | No | drift | Reading live Cloud Run env + revision of `payment-demo` | Hardcoded target — request body is rejected if it tries to override service/region/project. |
| `driftscribe-docs` | No | drift | Patching runbook files under `demo/docs/`, opening PRs against a single repo | Path allowlist regex `^demo/docs/[^/]+\.md$`. Refuses `ops-contract.yaml`, `.github/`, `infra/`, anything `.py`. |
| `driftscribe-rollback` | No | drift | `/propose` → operator approval → `/execute` or `/deny` (HMAC-bound, single-use, 15-min TTL) on `payment-demo` only | Approval UI lives on the **coordinator** so the gated page can be reached by a human. **Both decision paths** verify the HMAC on this worker — the coordinator never validates the approval token itself, by design. |
| `driftscribe-upgrade-reader` | No | upgrade | Reading `package.json` from a pinned repo and looking up matching GitHub Advisory DB entries | Hardcoded target via env-pinned `UPGRADE_TARGET_REPO`. Read-only PAT scope. See §3. |
| `driftscribe-upgrade-docs` | No | upgrade | Bumping a single `dependencies[package_name]` entry in the pinned lockfile and opening a PR (`/patch`), plus closing (`/close`) or CI-gated squash-merging (`/merge`) a PR this workload opened | Same `UPGRADE_TARGET_REPO` env pin; branch must start `upgrade/`. Post-LLM deterministic validator (semver, GHSA URL shape) runs before any GitHub write. `/merge` fails closed unless the required check is green on head and there's no conflict. See §3. |
| `driftscribe-infra-reader` | No | explore + provision | Whole-project resource enumeration via Cloud Asset Inventory `searchAllResources` (`/describe`); also resolves which live resources are declared in `iac/` (managed) vs. unmanaged (drift) | Read-only — `roles/cloudasset.viewer` + `serviceusage.serviceUsageConsumer` only. No write surface of any kind. Backs the operator UI's infra resource-map panel. |
| `driftscribe-tofu-editor` | No | provision | Authoring `iac/`-only HCL file writes and opening **one** PR (`/open-pr`); runs `tofu fmt` on the authored files first | Re-validates every file before any GitHub call: `iac/` prefix, foundation-file ban, secret ban, AGENT-mode static gate. `target_repo`/`branch`/`base`/`label` are server-derived, never LLM-supplied. A bad request surfaces as 403/422 the model can react to. |
| `driftscribe-tofu-apply` | No | provision (downstream) | The **sole live-infra mutator**: `tofu apply` of an approved plan (`/apply`) | **NOT in the coordinator's `WORKER_REGISTRY`** — the chat agent cannot call it. Reached only by the gated plan-build → approve → apply pipeline, behind a plan-bound, HMAC-signed operator approval (`/iac-approvals/{n}`). Claim-first single-flight; verified end-to-end live. See the `infra-iac-phase-c*` plans. |
| `driftscribe-notifier` | No | drift + upgrade (shared) | Posting normalized payload to a single env-injected webhook URL | Caller-supplied `url` is ignored — the worker's identity *is* the URL. Reused unchanged by the upgrade workload. |

---

## 2. Auth layers (two distinct boundaries)

DriftScribe has **two non-overlapping** auth mechanisms. Mixing them up has been the source of more than one self-inflicted outage in similar projects, so they are deliberately documented as separate concerns.

### Layer A — Operator → Coordinator: `X-DriftScribe-Token`

- **Where:** `agent/auth.py::verify_token` wired via `Depends(verify_token)` on `/recheck` and `/chat`.
- **Mechanism:** Shared random URL-safe token with 32 bytes of entropy (`python -c 'import secrets; print(secrets.token_urlsafe(32))'` → 43-character string; do NOT use `token_urlsafe(24)` which produces a 32-*character* string with less entropy), generated once by the operator and stored in Secret Manager (`coordinator-shared-token`). Cloud Run injects it via `--set-secrets=DRIFTSCRIBE_TOKEN=coordinator-shared-token:latest`. The same token is pasted into the operator's `curl` invocations.
- **Comparison:** `secrets.compare_digest(provided.encode(), expected.encode())` — never `==`. The unit test `tests/integration/test_token_guard.py::test_constant_time_compare_is_used` enforces this mechanically by patching `agent.auth.secrets.compare_digest` and asserting it was called.
- **Status codes:** 503 if `DRIFTSCRIBE_TOKEN` is unset (fail closed — see `agent/auth.py`), 401 if header missing, 403 if mismatch. The 403 response never echoes the supplied token back.
- **Scope:** Operator-facing endpoints only. `/healthz`, `/runs/{id}`, `/eventarc`, and `/approvals/*` are **not** guarded by this layer — they use Cloud Run health probes (open), best-effort public reads, Google-signed ID tokens from Eventarc, and per-approval HMAC tokens respectively.

### Layer B — Coordinator → Worker: audience-bound Google ID tokens

- **Where:** `agent/worker_client.py`. Spike 11.0 proved the mechanism end-to-end before it was built into the workers (verified gotchas: audience must be the worker's *root* URL, not a path; metadata server caches tokens for ~3500s). The spike itself (`spikes/cloud_run_auth/`) was retired 2026-05-30 once the production path superseded it; see git history for the original caller/callee + README.
- **Mechanism:** Coordinator mints an ID token via `google.oauth2.id_token.fetch_id_token(Request(), audience=<worker root URL>)` and sends it as `Authorization: Bearer <token>`. The worker calls `verify_oauth2_token` with the same audience and asserts the email claim is the coordinator's service-account email.
- **Why two checks (audience + caller allowlist):** Audience binding alone prevents token replay against the wrong service. Caller-email allowlist additionally prevents a different Cloud Run service in the same project from calling the worker with a valid-but-foreign token.
- **Scope:** Every coordinator → worker hop — including the two upgrade workers added in Phase 17. Workers are deployed with `--no-allow-unauthenticated`, so an attacker without a coordinator-SA-minted token gets a 403 from Cloud Run before even reaching the worker process.

### Why both layers coexist

- Layer A keeps the **public surface** small: only the coordinator, and only via a token the operator controls.
- Layer B keeps the **internal surface** small: even if the coordinator is compromised, the worker still verifies that the caller is the coordinator's SA and that the token was minted for *this* worker's audience.

There is no path where Layer A's token alone unlocks worker access, nor where Layer B's ID token grants `/recheck`. The two were considered for collapsing into one shared secret during the v3 plan review; the conclusion (recorded in the plan's Codex review notes) was that Google's identity primitives for internal traffic are stronger than any shared-HMAC scheme we'd reinvent, while human-facing endpoints need a string we can paste into a curl. Hence: two layers.

---

## 3. Worker interfaces

Each worker has a tiny REST surface with a hardcoded "payload-intent policy" — the request body cannot select a different target than the worker's deploy-time configuration. The policy is what makes the worker safe to expose even if the coordinator misbehaves. Workers MUST NOT import `agent.*` — they are isolated processes; the workload-registry pins this invariant in §5.

### Reader — `driftscribe-reader`

- **Endpoint:** `POST /read`
- **Request:** `{}` (empty object). Any extra fields → 400.
- **Response:** `{ "env": { "VAR": "value", ... }, "revision": "..." }`
- **Hardcoded policy:** `target_service=payment-demo`, `region=asia-northeast1`, `project=$PROJECT_ID` — all loaded from env at boot, all rejected if present in the request body.

### Docs — `driftscribe-docs`

- **Endpoint:** `POST /patch`
- **Request:** `{ "file": "demo/docs/runbook.md", "section": "...", "new_content": "...", "title": "...", "body": "..." }`
- **Response:** `{ "pr_url": "..." }` (or `{ "dry_run": true, "preview": "..." }`)
- **Hardcoded policy:** `repo=adi-prasetyo/driftscribe` (env). Path allowlist regex `^demo/docs/[^/]+\.md$`. Refuses `ops-contract.yaml`, `.github/`, `infra/`, `Dockerfile`, `*.py`. Path traversal (`..`) is normalized-then-checked.
- **Auth to GitHub:** Fine-grained PAT scoped to single repo, `Contents: Read & write`, `Pull requests: Read & write`. Stored as Secret Manager `docs-agent-github-pat`.

### Rollback — `driftscribe-rollback`

- **Endpoints:** `POST /propose`, `POST /execute`, `POST /deny`
- **Propose request:** `{ "target_revision": "...", "reason": "..." }` → returns `{ "approval_id": "...", "approval_url": "https://<coordinator>/approvals/<id>" }`
- **Execute request:** `{ "approval_id": "...", "approval_token": "<HMAC>" }`
- **Hardcoded policy:** `target_service=payment-demo` (env). Target revision must exist on the service AND not be the active revision. Approval token is HMAC'd with `approval-hmac-key`, single-use (Firestore transaction flips `pending → used`), 15-min TTL.
- **Approval UI:** Lives on the **coordinator** (`/approvals/{id}`). Rollback worker is private — it cannot host a public page.

### Notifier — `driftscribe-notifier`

- **Endpoint:** `POST /notify`
- **Request:** `{ "channel": "info|alert|approval", "severity": "...", "body": "..." }`
- **Response:** `{ "delivered": true }` (or error envelope)
- **Hardcoded policy:** Outbound URL = `$NOTIFY_WEBHOOK_URL` from Secret Manager. Caller-supplied `url` is silently dropped. Channel values are constrained to a closed enum. Shared between both workloads as-is.

### Upgrade Reader — `driftscribe-upgrade-reader`

- **Endpoint:** `POST /read`
- **Request:** `{ "target_repo": "...", "lockfile_path": "..." }` (closed schema; extra fields → 422).
- **Response:** `{ "target_repo": "...", "lockfile_path": "...", "dependencies": [{ "name": "...", "version": "...", "advisories": [...] }] }`
- **Hardcoded policy:** `UPGRADE_TARGET_REPO` env-pinned at boot; the request body's `target_repo` is re-validated against the env value (defense in depth — a misconfigured coordinator cannot redirect this worker). `lockfile_path` must match `^demo/upgrade-target/package\.json$` with a normalize-then-`..`-segment guard run before the regex. Advisory source is hardcoded to GitHub Advisory DB (`https://api.github.com/advisories`).
- **Auth to GitHub:** Fine-grained PAT scoped to the single repo, `Contents: Read` only. Stored as Secret Manager `upgrade-reader-github-pat`.

### Upgrade Docs — `driftscribe-upgrade-docs`

- **Endpoint:** `POST /patch`
- **Request:** `{ "target_repo", "lockfile_path", "package_name", "target_version", "advisory_url", "branch", "base", "title", "body" }` (closed schema; extra fields → 422).
- **Response:** PR URL + metadata from `driftscribe_lib.github.open_docs_pr` (or `{ "dry_run": ... }`).
- **Hardcoded policy:** Same env-pinned `UPGRADE_TARGET_REPO` re-validation. Same lockfile-path regex + traversal guard. `branch` must start with `upgrade/` and the suffix matches `[A-Za-z0-9._/-]{1,200}`. `base` must equal `main`. `title` must start with `upgrade`. The patch mutates ONLY `dependencies[package_name]`; every other key in the file is preserved as-is. Policy bounces are 403 (vs the reader's 400) — the worker is on the write path and treats every policy violation as a deny.
- **Post-LLM deterministic validator** (`workers/upgrade_docs/validator.py`): runs after the lockfile read and before the GitHub write. Five rules, short-circuiting on first failure:
  1. `lockfile_path` matches the regex (defense-in-depth duplicate of the handler guard).
  2. `package_name` exists in the current lockfile's `dependencies` (no new-dep adds).
  3. `target_version > current_version` (semver; equality also refused — equal is not an upgrade).
  4. `version_jump` ∈ {`patch`, `minor`} — major bumps refused with a message that names `escalation` (matches `ACTION_REGISTRY`).
  5. `advisory_url` matches `^https://github\.com/advisories/GHSA-[A-Za-z0-9-]+$`.

  The validator is transport-agnostic: it raises `UpgradeValidationError(status_code, reason)`; the FastAPI handler converts to `HTTPException` at the boundary. Policy → 403, schema-shaped → 422.
- **Auth to GitHub:** Fine-grained PAT scoped to the single repo, `Contents: Read & write` + `Pull requests: Read & write`. Stored as Secret Manager `upgrade-docs-github-pat`.

### Infra Reader — `driftscribe-infra-reader`

- **Endpoint:** `POST /describe`
- **Request:** `{}` (empty object; `extra="forbid"` → 422 on any field). The worker's whole job is fixed at deploy time — there is nothing for the body to select.
- **Response:** a bounded project-inventory summary, IaC-labeled — managed (declared in the baked-in `iac/`) vs. unmanaged (drift) resource counts, with a `declared_set_status` that degrades independently if any `*.tf` fails to parse. CAI permission/availability failures **soft-fail to a 200** carrying `{ "error": "cloud_asset_unavailable", ... }` so a missing grant degrades the UI panel rather than crashing the request.
- **Hardcoded policy:** project-scoped read of `$GCP_PROJECT` via Cloud Asset Inventory `searchAllResources` with a minimal read-mask. No write surface of any kind.
- **Auth to GCP:** `roles/cloudasset.viewer` + `serviceusage.serviceUsageConsumer` — strictly read-only. Backs both the `explore` and `provision` workloads' `read_project_inventory` tool and the operator UI's infra resource-map panel.

### Tofu Editor — `driftscribe-tofu-editor`

- **Endpoint:** `POST /open-pr`
- **Request:** `{ "target_repo", "branch", "base", "title", "body", "files": [{ "path", "content" }, ...] }` (closed schema; all fields required; extra fields → 422).
- **Response:** `{ "status", "pr_number", "pr_url", "branch" }` on success.
- **Hardcoded policy (fail-closed by construction — every check runs BEFORE any GitHub call, so a rejected request leaves no side effect):** `target_repo` is re-validated against the env-pinned `TARGET_REPO`. Each file must be a traversal-free, `iac/`-prefixed `.tf`/`.md` path that is **not** one of the operator-only foundation files; paths are deduplicated, non-empty, and size-bounded. `branch`/`base` are policy-checked; `title`/`body` are size-bounded. Authored files are run through `tofu fmt` first. Policy violations → **403**, schema-shaped → **422**. It writes HCL and opens a PR — it **never** touches live infra.
- **Auth to GitHub:** Secret-Manager `GITHUB_TOKEN`; even a fully compromised coordinator can only ever cause an `iac/`-only PR against the pinned repo.

### Tofu Apply — `driftscribe-tofu-apply` (downstream, not coordinator-invoked)

- **Endpoint:** `POST /apply` — the **sole live-infra mutator** (`tofu apply` of an already-approved plan).
- **Not in the coordinator's tool/worker registry.** No chat agent can reach it. It sits at the end of the gated pipeline (trusted plan-build → plan-bound, HMAC-signed operator approval at `/iac-approvals/{n}` → apply), proven end-to-end live. Claim-first single-flight guards against double-apply. Full interface, IAM, and the C2→C4 flow are documented in the `infra-iac-phase-c*` plans under [`../plans/`](../plans/).

---

## 4. Layer 0 — capability-bounded tool registry

The coordinator's ADK agent operates against an explicit, hardcoded list of tools — `agent.adk_agent.COORDINATOR_TOOLS`. The LLM cannot invoke anything outside this list; no `execute_shell`, no `arbitrary_http_request`, no direct GCP/GitHub SDK calls.

The 14 wired tools (plus 2 reserved session-memory slots, `get_session_state` / `set_session_state`, that fail closed if a YAML enables them before they're implemented):

| Tool | Purpose | Routes to | Workload(s) |
|---|---|---|---|
| `read_live_env_tool` | Read Cloud Run service env + revision | Reader (`/read`) | drift, explore, provision |
| `propose_rollback_tool` | Create an approval doc for a rollback | Rollback (`/propose`) | drift |
| `patch_docs_tool` | Open a docs PR | Docs (`/patch`) | drift |
| `notify_tool` | Post to webhook | Notifier (`/notify`) | drift, upgrade |
| `search_recent_prs_tool` | Read-only PR history | Coordinator-internal (read-only GitHub token) | drift, upgrade |
| `load_contract_tool` | Read the baked-in ops contract | Coordinator-internal (filesystem) | drift, explore, provision |
| `search_developer_docs` | Search Developer Knowledge corpus | Developer Knowledge MCP (Streamable HTTP) | all 4 |
| `retrieve_developer_doc` | Fetch a single doc body by name | Developer Knowledge MCP | all 4 |
| `upgrade_read_dependencies_tool` | List deps + advisories | Upgrade Reader (`/read`) | upgrade, explore |
| `upgrade_propose_pr_tool` | Bump a dep + open PR | Upgrade Docs (`/patch`) | upgrade |
| `upgrade_close_pr_tool` | Close an upgrade PR this workload opened | Upgrade Docs (`/close`) | upgrade |
| `upgrade_merge_pr_tool` | CI-gated squash-merge of an upgrade PR | Upgrade Docs (`/merge`) | upgrade |
| `read_project_inventory_tool` | Whole-project resource inventory (read-only) | Infra Reader (`/describe`) | explore, provision |
| `open_infra_pr_tool` | Author `iac/`-only HCL + open ONE PR (the only mutation tool outside drift/upgrade) | Tofu Editor (`/open-pr`) | provision |

**Per-workload tool scoping (Phase 17.A.4):** `COORDINATOR_TOOLS` is the *global registration manifest* — the universe of callables the coordinator may wire to ANY workload. Each workload's YAML (`workloads/<name>/workload.yaml`) carries `enabled_tool_names`, a symbolic filter that picks a per-workload subset from `agent.workloads.registry.TOOL_REGISTRY`. `Agent(tools=...)` receives ONLY the workload-scoped list at runtime, so **the LLM never sees a cross-workload tool**. `tests/unit/test_coordinator_tool_inventory.py` pins a three-way equality: YAML ⇄ the `DRIFT_WORKLOAD_TOOL_NAMES` / `UPGRADE_WORKLOAD_TOOL_NAMES` / `EXPLORE_WORKLOAD_TOOL_NAMES` / `PROVISION_WORKLOAD_TOOL_NAMES` tuples in `agent/adk_agent.py` ⇄ runtime resolution via `load_workload(name)`. The same test pins the **read-only / mutation disjointness** invariants: `explore`'s tools must be disjoint from the mutation-tool set, while `provision`'s set must include `open_infra_pr_tool` (and its `tofu_editor` mutation worker).

**MCP tools are Layer 0, scoped per workload.** The Developer Knowledge MCP is connected at the coordinator only (see §6). Workers have no MCP access. The two MCP-derived tools (`search_developer_docs`, `retrieve_developer_doc`) currently appear in both workloads' `enabled_tool_names`, but the scoping mechanism is the same as for any other tool — a future workload that doesn't need MCP grounding can simply omit them from its YAML.

**Enforcement:** `tests/unit/test_coordinator_tool_inventory.py` pins this set. Adding or removing a tool requires updating the `EXPECTED_TOOL_NAMES` constant in that test. A second test asserts no tool name matches a dangerous-capability pattern (`shell|exec|subprocess|os_command|delete|drop|destroy|sudo|raw_http|arbitrary|run_command|eval`). A third test extends the same logic to parameter names — `inspect.signature` enumerates each tool's params and rejects any matching `cmd|command|shell_cmd|url|endpoint|raw_url|payload|raw_request|script|eval|expr`. A fourth smoke test asserts that importing `agent.adk_agent` does not pull in remote-execution SDKs (`paramiko`, `fabric`, `pexpect`).

If you add a tool in a future PR:
1. Implement it in `agent/adk_tools.py` (or wire an MCP wrapper into `agent/mcp/`).
2. Add it to `COORDINATOR_TOOLS` in `agent/adk_agent.py`.
3. Register the symbolic name in `_TOOL_REGISTRY` (`agent/workloads/registry.py`).
4. Add it to the relevant workload YAML's `enabled_tool_names` AND the matching `*_WORKLOAD_TOOL_NAMES` tuple in `agent/adk_agent.py`.
5. Update `EXPECTED_TOOL_NAMES` in `tests/unit/test_coordinator_tool_inventory.py` and this section.
6. Justify the addition in the PR description against Layer 0's threat model.

Layer 0 is the *first* safety net. Even if a prompt-injection attack convinces the agent to "rm -rf /", the agent simply does not have a tool that can. Layers 1 (per-SA IAM, see [`iam-matrix.md`](./iam-matrix.md)), 2 (worker payload-intent policies — see §3, plus the upgrade-docs post-LLM validator), and 3 (HITL approval on rollback — see §7) sit underneath.

### Layer 1 caveats (Phase 11.9 carry-overs)

The coordinator's Layer 1 claim is overstated in one narrow way carried since Phase 11: the coordinator's `github-pat` MUST be a read-only fine-grained PAT. The application code only ever calls GitHub's PR list/read APIs (via `search_recent_prs_tool`), but the IAM scope of the secret is whatever PAT the operator stored. The Phase 11.9 deploy runbook (`docs/runbooks/deploy.md`) requires a fine-grained PAT — operators who deployed earlier should rotate. The temporary `roles/run.viewer` grant from the legacy classifier path is gone; the iam-matrix.md negative-space row now reads "**NOT** `roles/run.viewer`".

See [`iam-matrix.md`](./iam-matrix.md) §"Phase 11.9 carry-overs" for the full statement.

---

## 5. Workload abstraction

A **workload** is a named bundle of {system prompt, chat system prompt, tool inventory, worker set, action set, optional contract}. Four ship today: `drift`, `upgrade`, `explore`, and `provision`. The coordinator routes `POST /chat workload=<name>` per-request to a workload-specific agent; `POST /recheck workload=<name>` serves the autonomous path (`explore` and `provision` are chat-only and `/recheck` refuses them — `agent/main.py::CHAT_ONLY_WORKLOAD_NAMES`). `explore` narrows to a strictly read-only tool subset; `provision` is chat-only too but carries the single `open_infra_pr_tool` mutation, which writes `iac/` HCL and opens a PR — it never touches live infra.

**Data model** (`agent/workloads/spec.py` + `agent/workloads/registry.py`):

- `WorkloadSpec` — pydantic `BaseModel` with `extra="forbid"`, parsed from `workloads/<name>/workload.yaml`. Carries only *symbolic* names — `enabled_tool_names`, `worker_names`, `action_names`. No URLs, secrets, or repos live in YAML. The `name` field is a `Literal["drift", "upgrade", "explore", "provision"]` so a YAML typo fails at parse time.
- `WorkloadResolution` — frozen dataclass holding the parsed spec plus resolved callables. The three name→object fields (`tools`, `workers`, `actions`) are exposed as `MappingProxyType` views over private dicts so a caller cannot widen authority by in-place mutation.
- Three code-side allowlists in `registry.py`: `TOOL_REGISTRY` (16 entries — 14 wired callables plus 2 `None`-reserved session-memory slots, `get_session_state` / `set_session_state`, that fail with `ReservedToolNotImplementedError` if a future YAML enables them before a Phase-N PR flips them to real callables; the manifest the YAML's `enabled_tool_names` resolves against), `WORKER_REGISTRY` (8 entries — `drift_reader`/`drift_docs`/`drift_rollback`/`infra_reader`/`notifier`/`upgrade_reader`/`upgrade_docs`/`tofu_editor`; each carries its URL env var name; note `tofu-apply` is deliberately absent — it is not coordinator-callable), `ACTION_REGISTRY` (6 entries; each carries `requires_approval`). The security property is the inverse of "YAML drives behavior": *flipping a YAML value can choose from the allowlist, but it cannot introduce a new URL, secret, repo, or callable.* A fourth allowlist, `UPGRADE_TARGET_REGISTRY`, pins the upgrade workload's `(target_repo, lockfile_path, advisory_source)` for the same reason.

**Routing.** `agent.main`'s `/chat` and `/recheck` handlers extract the `workload` field, call `load_workload(name)`, and pass the `WorkloadResolution` to `build_chat_agent` / `build_agent` in `agent/adk_agent.py`. The factory hands `Agent(tools=...)` the workload's filtered tool list (`list(workload.tools.values())`), NOT the global union. The system prompt comes from the workload directory: `workloads/<name>/system_prompt.md` for `/recheck` and `workloads/<name>/chat_system_prompt.md` (falling back to `system_prompt.md`) for `/chat`. The LLM literally never sees a cross-workload tool or prompt.

**Worker isolation invariant.** Workers MUST NOT import `agent.workloads.registry` or any other `agent.*` module. The registry drags in coordinator-only deps via `agent.adk_tools`; an inadvertent import would balloon worker images and couple deploy cadences. The upgrade workers enforce this with a subprocess-based test (`test_worker_does_not_import_coordinator_registry`) that imports the worker module in a fresh interpreter and inspects `sys.modules`. The same applies to the post-LLM validator — `workers/upgrade_docs/validator.py` hardcodes the `escalation` action name as a string rather than importing it from the registry.

**Workload ContextVar** (`agent/workload_context.py`). Per-request workload identity propagates through the async call tree via a module-level `ContextVar` with default `"unknown"`. The `/chat` and `/recheck` handlers call `set_workload(name)` and `reset_workload(token)` in a `try/finally`. The MCP wrapper reads this ContextVar to tag every MCP-call log line with the caller workload — separating "which MCP we called" (`mcp_server`) from "who asked us to call it" (`workload`) is what makes the observability dashboards sliceable. Living at the package root (not under `agent.workloads`) is a circular-import dodge — see the module docstring.

**Adding a new workload (`workload-N`).** Create `workloads/workload-N/` with `workload.yaml` + `system_prompt.md` (+ optional `chat_system_prompt.md` + optional contract file). Add the action callables to `agent/adk_tools.py`. Register them in `_TOOL_REGISTRY` and `_WORKER_REGISTRY`. Extend `WorkloadSpec.name`'s `Literal`. Add the symbolic names to `COORDINATOR_TOOLS` and to a new `WORKLOAD_N_TOOL_NAMES` tuple in `agent/adk_agent.py`. Update `tests/unit/test_coordinator_tool_inventory.py`. Update §1 and §4 of this doc.

---

## 6. Developer Knowledge MCP grounding

Google's Developer Knowledge MCP is attached at the coordinator only via `agent/mcp/developer_knowledge.py`. Streamable HTTP to `https://developerknowledge.googleapis.com/mcp`; auth via `X-Goog-Api-Key` header, key sourced from Secret Manager (`developer-knowledge-api-key`). The wrapper exposes two callables to the LLM — `search_developer_docs(query)` and `retrieve_developer_doc(name)` — both registered in `TOOL_REGISTRY` like any other tool.

Wrapper guardrails on top of the raw MCP calls:

- **10s wall-clock timeout** per call (`asyncio.wait_for`), separate from the SDK's connection timeout. Both apply.
- **60s in-process response cache** keyed by `(tool_name, query|name)`. Bounded at 1024 entries with FIFO eviction; expired entries swept on lookup. Saves cost and latency when the LLM searches the same term twice in one turn.
- **Result truncation**: 5 documents per response, 4000 chars per document body. Truncated content gets a clear `... [truncated N/M]` suffix so the LLM sees the clip.
- **Fail-closed error translation**: timeouts return `{"error": "mcp_timeout", ...}`; other MCP errors return `{"error": "mcp_error", ...}`. The agent's LLM sees a structured failure result it can reason about — never a raw exception bubbling out of a tool call and crashing the chat handler.
- **Structured log per call**: `{event, trace_id, workload, mcp_server, mcp_tool, query_or_names, doc_count, latency_ms}` (`event="mcp_call"` is the family tag added in 18.B.2 so this line joins the same `jsonPayload.event` query family as `llm_thought` / `tool_call` / `llm_usage`). `trace_id` from `driftscribe_lib.logging`'s ContextVar (same source as worker calls), `workload` from `agent.workload_context.current_workload()`.

**Configuration error mode.** Missing `DEVELOPER_KNOWLEDGE_API_KEY` raises `MissingDeveloperKnowledgeApiKeyError(RuntimeError)`. `agent/main.py` traps this explicitly *before* the broader `RuntimeError → 502` mapping and returns **503** with a clear "API key not configured" detail. Operators see the missing-config message immediately in Cloud Run logs.

**Why coordinator-only, not per-worker.** The MCP is a Layer 0 attached tool — a capability surface. Giving every worker its own MCP attach would multiply:

- The **auth surface** (an API key in every worker's Secret Manager binding),
- The **network surface** (every worker can now make outbound HTTPS calls to googleapis.com, where today most workers only talk to a single hardcoded GCP or GitHub endpoint),
- The **observability surface** (per-worker MCP logs to correlate).

The coordinator's reasoning step is where doc citations matter; workers execute already-decided actions and don't need to reason about docs. Drift cites authoritative Cloud Run env-variable guidance in docs PR bodies; upgrade cites migration guides for the package being bumped. Both per the workload's system prompt rule.

### Reasoning observability (Phase 18.B)

Every `/chat` and `/recheck` invocation can emit up to four structured
JSON log-line shapes, all keyed by `trace_id` (bound by the request
middleware) and `workload` (bound by the request handler before the
agent runs):

| event           | additional fields                                        |
| --------------- | -------------------------------------------------------- |
| `llm_thought`   | `thought_text` — Gemini's own summary of its reasoning   |
| `tool_call`     | `tool_name`                                              |
| `llm_usage`     | `prompt_token_count`, `candidates_token_count`, `thoughts_token_count`, `total_token_count` |
| `mcp_call`      | (pre-existing, Phase 17.B.4) `mcp_tool`, `query_or_names`, `doc_count`, `latency_ms`, `error?` |

Thought summaries come from Gemini 2.5 Flash's built-in thinking,
surfaced via ADK's `BuiltInPlanner(ThinkingConfig(include_thoughts=True))`.
The model already spent thinking tokens at the SDK-default dynamic
budget before Phase 18 — `include_thoughts=True` only changes whether
the summaries are returned. `thoughts_token_count` on each `llm_usage`
line is what lets the operator confirm cost behaviour empirically
rather than from documentation.

Streaming dedup: ADK emits partial events as a thought summary is
generated, then re-emits the merged summary as a non-partial event.
`agent/adk_agent.py` filters on `event.partial` so a single thought
summary maps to a single log line.

Retention: all of the above ride Cloud Logging's `_Default` bucket,
extended to 365 days by `infra/scripts/setup_secrets.sh` (Phase 18.A).
Storage past day 30 is billed at `$0.01/GiB-month`. Operators replay
full agent traces with Logs Explorer queries like
`jsonPayload.event=("llm_thought" OR "tool_call" OR "llm_usage" OR "mcp_call") AND jsonPayload.trace_id="<id>"`.

---

## 7. HITL (human-in-the-loop) approval flow

> **Status:** Shipped (Phase 11.5 + Phase 11.9). The flow below matches what's live in `agent/main.py::approval_get` / `approval_post` and `agent/templates/approval.html`. This Firestore-backed approval gates the **drift** workload's `rollback` action. The `upgrade` workload uses the post-LLM validator (§3) plus the operator-visible PR as its safety gates instead. The **provision** / infra-apply path has its *own*, separate HITL gate — a plan-bound, HMAC-signed approval at `/iac-approvals/{n}` that gates the downstream `tofu-apply` worker (the agent only ever opens an `iac/` PR; it cannot apply); see the `infra-iac-phase-c*` plans for that flow.

1. Coordinator's ADK agent decides a rollback is warranted and calls `propose_rollback_tool(target_revision, reason)`.
2. Rollback worker writes `approvals/{id}` to Firestore with `status=pending`, mints a one-time random token, stores its HMAC alongside the approval doc, and returns `{ approval_id, approval_url }`. The approval URL is `https://<coordinator>/approvals/<id>?t=<raw-token>`. The HMAC (bound to `(approval_id, target_revision, expires_at)`) lives server-side; the URL carries only the raw token so the worker can `hmac.compare_digest(stored_hmac, hmac(presented_token))` on `/execute`.
3. Coordinator surfaces the approval URL to the operator in the `/chat` response (and/or via the Notifier worker).
4. Operator opens `https://<coordinator>/approvals/<id>?t=<raw-token>`. Coordinator renders the rollback plan server-side. The page has no external assets, `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, `X-Robots-Tag: noindex`.
   - The raw token rides in the `?t=` query param so the operator only has to click one link; the no-referrer header keeps it from leaking via the `Referer` of any same-tab navigation, and the 15-min single-use TTL bounds the blast radius if the URL is captured in an access log. The server side stores only the HMAC of the token, so an access-log capture still requires the original token to validate. (Moving the token to a same-origin cookie + CSRF header on POST would be the production-grade alternative; out of scope for the hackathon.)
5. Operator clicks **Approve** or **Reject** — browser POSTs to `/approvals/<id>` with the token in a hidden form field (`name="t"`) and a `decision=approve|reject` field.
6. The **coordinator does NOT verify the HMAC itself** (Phase 11.9 split — only the Rollback worker holds the HMAC key). It forwards `(approval_id, t)` to the Rollback worker:
   - approve → `worker_client.call_execute(approval_id, t)` → worker's `/execute`
   - reject → `worker_client.call_deny(approval_id, t)` → worker's `/deny`
7. Rollback worker verifies the HMAC + TTL, transactionally flips Firestore (`pending → used` on execute, `pending → denied` on deny), then — for execute only — calls Cloud Run admin to flip traffic to `target_revision`. Replay on either endpoint returns 403.

The single-worker-side transaction is what makes the "compromised coordinator cannot mint OR silently deny executions" property hold: the coordinator can only initiate either action when an operator with a valid token-in-URL clicks the button, and the worker is the only service that can validate the HMAC.

---

## 8. Cross-references

- Plain-English system tour: [`../OVERVIEW.md`](../OVERVIEW.md)
- Multi-agent framework plan: [`docs/plans/2026-05-19-driftscribe-phase17-framework-mcp.md`](../plans/2026-05-19-driftscribe-phase17-framework-mcp.md)
- Infra-IaC initiative (explore/provision + infra workers): [`docs/plans/2026-05-27-infra-iac-agent-design.md`](../plans/2026-05-27-infra-iac-agent-design.md) and the `infra-iac-phase-*` plans under [`../plans/`](../plans/)
- IAM matrix (per-SA grants + negative space): [`iam-matrix.md`](./iam-matrix.md)
- Architecture diagram (SVG, self-contained): [`architecture.html`](./architecture.html)
- Workload data model: `agent/workloads/spec.py`, `agent/workloads/registry.py`
- Workload ContextVar: `agent/workload_context.py`
- Developer Knowledge MCP wrapper: `agent/mcp/developer_knowledge.py`
- Upgrade workers: `workers/upgrade_reader/main.py`, `workers/upgrade_docs/main.py`
- Upgrade post-LLM validator: `workers/upgrade_docs/validator.py`
- Infra workers: `workers/infra_reader/main.py`, `workers/tofu_editor/main.py`, `workers/tofu_apply/main.py`
- Cloud Run inter-service auth proof: spike 11.0 (`spikes/cloud_run_auth/`, retired 2026-05-30 — superseded by the production workers; see git history)
- Token guard implementation: `agent/auth.py`, `tests/integration/test_token_guard.py`
