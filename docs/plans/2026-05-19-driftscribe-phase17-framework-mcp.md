# DriftScribe Phase 17 — Multi-Agent Framework + Developer Knowledge MCP + Dependency Upgrade Workload

> **For Claude:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` to execute this plan task-by-task. Each task gets a fresh implementer + spec reviewer + code-quality reviewer cycle.

**Goal:** Reframe DriftScribe from "drift detection tool" to "multi-agent coordinator/worker framework for safe AI-driven DevOps on Cloud Run." Ship two concrete workloads (drift + dependency upgrades) and ground agent reasoning in Google's official docs via the Developer Knowledge MCP server.

**Architecture:**
- **Workers stay narrow and per-workload.** Each worker keeps its hardcoded policy (target service, path allowlist, webhook URL). Layer 1 (policy-bounded payload validation) is unchanged — the framework abstraction lives at the *coordinator* level, not inside workers.
- **Coordinator becomes workload-aware** via a `WorkloadSpec` manifest. Each request names a workload; the coordinator loads the spec, picks the tool subset, picks the system prompt, and routes to the right worker endpoints.
- **WorkloadSpec YAML carries symbolic names only — never authority.** Worker endpoints, secret names, repo names, advisory sources, and policy regexes live in a central code-side `WORKLOAD_REGISTRY` allowlist. YAML may select from that allowlist by name; an unknown name fails boot. This means flipping a YAML value cannot expand the agent's capabilities or redirect it at a different repo/service. (Codex blocker.)
- **Developer Knowledge MCP attaches to the coordinator's ADK agent only.** Workers do not reason and therefore do not query docs. Preserves the "execute-only worker" property and Layer 0 (capability-bounded tool registry). If a worker needs to cite docs (e.g., docs worker writing a PR), the coordinator retrieves the doc and passes the sanitized URL/text in the worker payload. (Codex confirmation.)
- **Two workloads ship:** `drift` (existing — Cloud Run env vs declared contract) and `upgrade` (new — repo lockfile vs vulnerability advisories). Upgrade is **scoped to `package.json` only** for Phase 17 — `requirements.txt`/`pyproject.toml`/`uv.lock` add parser/versioning risk without improving submission. (Codex important.)
- **Tool names are workload-prefixed** (`drift_read_live_env`, `upgrade_read_dependencies`, etc.) — no shared generic names across workloads, to prevent prompt/tool confusion. (Codex important.)
- **`workload` propagates through observability**: Firestore decision docs, trace logs, GitHub branch/title prefixes, demo output. (Codex important.)

**Tech Stack:** Pydantic v2 (WorkloadSpec schema), Google ADK MCP-tool integration, Google Developer Knowledge API (`developerknowledge.googleapis.com`), FastAPI, Firestore (unchanged), existing `driftscribe_lib`.

**Scope boundaries:**
- No third workload (cert rotation, IAM audit, cost watch) — kept as future work.
- No runtime worker hot-swapping. Workers are deployed once per workload; coordinator routes between them but does not spawn them dynamically.
- No multi-tenant coordinator (a single deployment serves a fixed set of workloads).
- MCP integration uses Google Developer Knowledge **only** — no other MCP servers wired in.

**Estimated effort:** 10–15 working days. Calendar with evenings/weekends: 3–5 weeks. Submission 2026-07-10 leaves ~2–4 weeks of slack for Phase 18.

---

## Phase 17 dependency graph

```
17.A (framework refactor) ─┬─→ 17.B (MCP) ─┐
                           ├─→ 17.C (upgrade workload)
                           └─→ 17.D (docs/framing)
                                            ├─→ 17.E (deploy infra)
                                            └─→ 17.F (Codex review pass)
```

17.B can run in parallel with 17.C once 17.A is done. 17.D and 17.E should land after both 17.B and 17.C.

---

## Sub-phase 17.A — Workload manifest + coordinator refactor (3–4 days)

The point: the existing single-workload coordinator becomes a router. Existing drift behavior is unchanged at runtime; the refactor preserves all 477 tests.

### Task 17.A.1: Define `WorkloadSpec` schema + central `WORKLOAD_REGISTRY`

**Files:**
- Create: `agent/workloads/__init__.py`, `agent/workloads/spec.py`, `agent/workloads/registry.py`
- Create: `tests/unit/test_workload_spec.py`, `tests/unit/test_workload_registry.py`
- Create: `workloads/drift/workload.yaml`, `workloads/upgrade/workload.yaml` (upgrade is a stub until 17.C)

**Schema (pydantic) — symbolic names only:**
```python
class WorkloadSpec(BaseModel):
    name: Literal["drift", "upgrade"]
    display_name: str
    description: str
    system_prompt_file: str          # path relative to workload dir
    contract_file: str | None        # YAML contract for the workload
    enabled_tool_names: list[str]    # symbolic names; resolved via registry to real callables
    worker_names: list[str]          # symbolic; resolved to WorkerEndpoint via registry
    observation_kind: Literal["cloud_run_env", "repo_lockfile"]
    action_names: list[str]          # symbolic; resolved to ActionSpec via registry
```

**Central code-side registry (`agent/workloads/registry.py`) — the authority:**
```python
WORKER_REGISTRY: dict[str, WorkerEndpoint] = {
    "drift_reader":   WorkerEndpoint(url=..., sa_email=..., audience=...),
    "drift_docs":     WorkerEndpoint(...),
    "drift_rollback": WorkerEndpoint(...),
    "upgrade_reader": WorkerEndpoint(...),
    "upgrade_docs":   WorkerEndpoint(...),
    "notifier":       WorkerEndpoint(...),  # shared
}
TOOL_REGISTRY: dict[str, Callable] = {
    "drift_read_live_env":      drift_read_live_env_tool,
    "drift_patch_docs":         drift_patch_docs_tool,
    "drift_propose_rollback":   drift_propose_rollback_tool,
    "upgrade_read_dependencies": upgrade_read_dependencies_tool,
    "upgrade_propose_pr":       upgrade_propose_pr_tool,
    "notify":                   notify_tool,
    "search_developer_docs":    search_developer_docs_tool,
    "retrieve_developer_doc":   retrieve_developer_doc_tool,
    "load_contract":            load_contract_tool,
    "search_recent_prs":        search_recent_prs_tool,
    "get_session_state":        get_session_state_tool,
    "set_session_state":        set_session_state_tool,
}
ACTION_REGISTRY: dict[str, ActionSpec] = {...}
```

**Loader fails boot on unknown symbolic names. URLs/secrets/repo names NEVER appear in workload YAML.**

**Steps:**
1. **TDD `test_workload_spec.py`:** valid drift YAML parses; missing required fields raise `ValidationError`; unknown workload name rejected at `Literal` layer.
2. **TDD `test_workload_registry.py`:** `load_workload("drift")` succeeds; YAML with a tool name not in `TOOL_REGISTRY` raises `UnknownToolError` at boot; YAML with worker name not in `WORKER_REGISTRY` raises `UnknownWorkerError` at boot.
3. Implement `spec.py` + `registry.py`. Cache loaded WorkloadSpecs at module level (read-only after boot).
4. Write `workloads/drift/workload.yaml` — symbolic references only, matches existing hardcoded behavior after resolution.
5. Write `workloads/upgrade/workload.yaml` stub.
6. **Commit.**

### Task 17.A.2: Move drift system prompt + tool inventory into the workload

**Files:**
- Move: contents of `SYSTEM_PROMPT` from `agent/adk_agent.py` → `workloads/drift/system_prompt.md`
- Move: existing `demo/ops-contract.yaml` → `workloads/drift/contract.yaml` (symlink or copy + reference update)
- Modify: `agent/adk_agent.py` to load prompt and contract via `WorkloadSpec`
- Modify: `tests/unit/test_coordinator_tool_inventory.py` to assert *drift workload* tool list matches the expected set
- Create: `tests/integration/test_drift_recheck_deterministic.py`

**Golden-test layering (per Codex):** byte-for-byte prompt/contract goldens are necessary but not sufficient. Add an integration golden for the deterministic `/recheck` path (mocked Reader response → classifier decision → assert decision + worker call sequence is identical to pre-17). **Do NOT** attempt to golden-test ADK trace identity for the LLM-reasoned path — it's non-deterministic.

**Steps:**
1. **TDD `test_coordinator_tool_inventory.py`:** drift's enabled_tools matches existing hardcoded list.
2. **TDD `tests/unit/test_workload_loads_drift.py`:** load drift workload → prompt text + contract dict match the previous hardcoded values byte-for-byte (golden file test).
3. **TDD `tests/integration/test_drift_recheck_deterministic.py`:** mock Reader; assert `/recheck` decision + downstream worker calls are byte-identical to a pre-17 baseline captured before the refactor.
4. Refactor without changing behavior.
5. Run full test suite — 477 + new tests passing.
6. **Commit.**

### Task 17.A.3: Coordinator routing — `workload` parameter on `/chat`, `/recheck` only

**`/eventarc` hardcodes `workload="drift"` server-side.** (Codex blocker.) Audit-log events are a drift trigger; an event-triggered upgrade workload, if ever added, will get its own endpoint with its own server-side workload binding. Caller-selected workload on `/eventarc` is rejected — the trigger payload doesn't carry one.

**Files:**
- Modify: `agent/main.py` — add `workload: str = "drift"` query param / body field on `/chat`, `/recheck`. `/eventarc` hardcodes `workload="drift"`.
- Modify: `agent/adk_agent.py` — agent factory takes `WorkloadSpec`, returns workload-scoped agent
- Create: `tests/integration/test_workload_routing.py`

**Steps:**
1. **TDD `test_workload_routing.py`:**
   - `POST /chat` with `workload=drift` (or no workload — defaults to drift) → coordinator loads drift spec, agent has drift tools.
   - `POST /chat` with `workload=upgrade` → coordinator loads upgrade spec, agent has upgrade tools.
   - `POST /chat` with `workload=does_not_exist` → 422.
   - `POST /chat` with drift workload trying to call upgrade-only tool via prompt injection → tool not in agent's registered tools, fails closed.
   - `POST /eventarc` with payload containing `workload=upgrade` → ignored; routed to drift. Assert the dispatched agent has drift tools regardless.
2. Implement workload selection in `main.py`. Cache loaded WorkloadSpecs at module level (read-only after boot — no hot reload).
3. **Commit.**

### Task 17.A.4: Coordinator tool inventory test — multi-workload

**Files:**
- Modify: `tests/unit/test_coordinator_tool_inventory.py`
- Modify: `agent/adk_agent.py` — export per-workload tool lists

**Steps:**
1. Update Layer 0 test to assert *each workload's* enabled_tools set matches an expected hardcoded set in the test.
2. Negative test: no workload may enable tools matching `re.search(r"shell|exec|subprocess|os_command|delete|sudo|raw_http|arbitrary", tool_name, re.I)`.
3. Cross-workload negative test: assert `drift.enabled_tools ∩ upgrade-only-tools = ∅` (drift cannot accidentally call the upgrade reader).
4. **Commit.**

### Task 17.A.5: IAM matrix update (drift workload only, upgrade comes in 17.C)

**Files:**
- Modify: `docs/architecture/iam-matrix.md`

**Steps:**
1. Add a "Workload scope" column to each SA row.
2. Note in the negative-space section: coordinator's `roles/run.invoker` on drift workers does NOT extend to upgrade workers — must be granted separately when upgrade lands.
3. **Commit.**

---

## Sub-phase 17.B — Developer Knowledge MCP integration (1–2 days)

### Task 17.B.1: API key provisioning + Secret Manager + IAM

**Files:**
- Modify: `infra/scripts/setup_secrets.sh` — create `developer-knowledge-api-key` secret
- Modify: `docs/runbooks/deploy.md` — instruct operator to enable the Developer Knowledge API + create restricted API key

**Steps:**
1. Operator-side instructions: enable `developerknowledge.googleapis.com` on the project, create an API key restricted to that API only. Per Google docs, also run `gcloud beta services mcp enable developerknowledge.googleapis.com --project=$PROJECT_ID` (auto-enabled after 2025-03-17 when API is enabled, but keep explicit for portability across fresh deploys).
2. `setup_secrets.sh` adds idempotent creation of `developer-knowledge-api-key` Secret Manager entry, grants `roles/secretmanager.secretAccessor` to the coordinator SA on that specific secret only. Script also runs the `gcloud beta services mcp enable` line idempotently.
3. `cloudbuild.yaml` coordinator deploy adds `--set-secrets=DEVELOPER_KNOWLEDGE_API_KEY=developer-knowledge-api-key:latest`.
4. **Commit.**

### Task 17.B.2: ADK MCP tool binding via `McpToolset` + Streamable HTTP

**Integration shape verified against authoritative Google docs (`adk.dev/tools-custom/mcp-tools`, `developers.google.com/knowledge/mcp`) on 2026-05-20:**
- Developer Knowledge MCP is a **remote Streamable HTTP** server at `https://developerknowledge.googleapis.com/mcp` (exact path).
- Auth: `X-Goog-Api-Key: <key>` header.
- Tools exposed by the server: `search_documents`, `get_documents`, `answer_query` (the last marked **Preview**).
- ADK supports it via `McpToolset(connection_params=StreamableHTTPConnectionParams(url=..., headers=...), tool_filter=[...])`. Import paths: `from google.adk.tools.mcp_tool import McpToolset` and `from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams`.
- **Expose `search_documents` + `get_documents` only** via `tool_filter`. Skip `answer_query` — it's a second LLM reasoning surface we don't want, and Preview status adds churn risk.

**Files:**
- Create: `agent/mcp/__init__.py`, `agent/mcp/developer_knowledge.py`
- Create: `tests/unit/test_mcp_developer_knowledge.py`
- Modify: `agent/adk_agent.py` — wire `McpToolset` into agent construction
- Modify: `pyproject.toml` — `google-adk[mcp]` (or whatever the ADK MCP extra is named in the installed version)

**Steps:**
1. **Verify timeout API shape on installed ADK version.** Adspirer/Linear ADK docs show no `timeout=` kwarg on `StreamableHTTPConnectionParams` (only on `StdioConnectionParams` wrappers). Before writing the TDD test, run `python -c "from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams; help(StreamableHTTPConnectionParams)"` to confirm whether timeout is a `StreamableHTTPConnectionParams` kwarg, a `McpToolset` kwarg, or must be enforced at the underlying httpx layer. Document the chosen path in `agent/mcp/developer_knowledge.py` module docstring.
2. **TDD `test_mcp_developer_knowledge.py`:**
   - Mock the MCP Streamable HTTP server. Assert `search_documents(query)` returns a list of doc refs (with `parent`, `content`, `id` fields per the real API).
   - Assert `get_documents(names=[...])` returns full text.
   - Assert `tool_filter` excludes `answer_query` (test invokes `McpToolset.list_tools()` and asserts `answer_query` not present, `search_documents` + `get_documents` present).
   - Missing `DEVELOPER_KNOWLEDGE_API_KEY` env → `ConfigError` at boot (fail closed).
   - MCP server times out → tool returns a structured error to the agent, not a hang. (Implementation per the timeout-shape decision in step 1.)
3. **Implement** the toolset wrapper:
   ```python
   from google.adk.tools.mcp_tool import McpToolset
   from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams

   def build_developer_knowledge_toolset() -> McpToolset:
       api_key = os.environ.get("DEVELOPER_KNOWLEDGE_API_KEY")
       if not api_key:
           raise ConfigError("DEVELOPER_KNOWLEDGE_API_KEY missing")
       return McpToolset(
           connection_params=StreamableHTTPConnectionParams(
               url="https://developerknowledge.googleapis.com/mcp",
               headers={"X-Goog-Api-Key": api_key},
           ),
           tool_filter=["search_documents", "get_documents"],
       )
   ```
   Wrap the returned tools so each call passes through a thin layer that enforces: 10s timeout (mechanism per step 1), 60s in-process response cache keyed on `(tool_name, query_or_names_tuple)`, max 5 documents per response, max 4000 chars per document, structured log on every call: `{trace_id, workload, mcp_tool, query_or_names, doc_count, latency_ms}`.
4. Tools are added to `TOOL_REGISTRY` under symbolic names `search_developer_docs` and `retrieve_developer_doc` (workload prompts use the symbolic names). The symbolic names map to the wrapped ADK tools, not the raw `McpToolset` entries.
5. **Commit.**

### Task 17.B.3: Per-workload prompt updates + Layer 0 expansion

**Files:**
- Modify: `workloads/drift/system_prompt.md` — add: "When proposing a docs_pr, first call `search_developer_docs` to find authoritative Cloud Run env-variable guidance; cite the URL in the PR body."
- Modify: `workloads/upgrade/system_prompt.md` (stub for 17.C) — add: "Before proposing an upgrade PR, call `search_developer_docs` for migration guides on the bumped package."
- Modify: `agent/adk_agent.py` — `COORDINATOR_TOOLS` includes the 2 new MCP tools
- Modify: `tests/unit/test_coordinator_tool_inventory.py` — expected set includes new tools

**Steps:**
1. Update Layer 0 expected-set assertion to include `search_developer_docs` and `retrieve_developer_doc`.
2. Update negative-pattern regex check — these names pass since they contain none of the forbidden substrings.
3. **Commit.**

### Task 17.B.4: Integration test — drift workload uses MCP during reasoning

**Files:**
- Create: `tests/integration/test_drift_uses_mcp.py`

**Steps:**
1. Mock MCP responses. Send a drift `/chat` request that triggers `docs_pr`. Assert agent's trace shows a `search_developer_docs` call before the `delegate_to_docs` call.
2. Negative test: `no_op` decision path does NOT call MCP (latency optimization — no need to search docs when nothing changed).
3. **Commit.**

---

## Sub-phase 17.C — Dependency upgrade workload (3–4 days)

The upgrade workload watches a target repo's lockfiles + an advisory feed, and proposes upgrade PRs with HITL gating for major version bumps. Reuses the coordinator + Notifier; adds two new workers (`upgrade-reader`, `upgrade-docs`).

### Task 17.C.1: Upgrade contract schema + demo target repo

**Codex blocker (2026-05-20) — authority must NOT live in YAML.** The original draft of this task put `target_repo`, `lockfile_path`, and `advisory_source` in `workloads/upgrade/contract.yaml`. That contradicts 17.A.1's symbolic-names-only rule: a flip of `target_repo` in YAML could redirect the agent at a different repository. Per Codex 2026-05-20 review: those three fields must move to the central code-side registry or worker-hardcoded policy. YAML keeps only descriptive decision rules.

**Files:**
- Create: `workloads/upgrade/contract.yaml` — **decision rules only** (severity thresholds, version-jump policy, requires_approval flags). NO repo/path/advisory-source fields.
- Modify: `agent/workloads/registry.py` — extend (or add a sibling) `UPGRADE_TARGET_REGISTRY: dict[str, UpgradeTarget]` keyed by a symbolic target name (e.g. `"phase17_demo"`). `UpgradeTarget` holds the real `target_repo`, `lockfile_path`, `advisory_source`. YAML references the symbolic name via a new `target_name: Literal["phase17_demo"]` field (constrained to known names).
- Create: `demo/upgrade-target/package.json` (or `pyproject.toml`) with an intentionally-vulnerable dependency version
- Create: `tests/unit/test_upgrade_contract.py`
- Create: `tests/unit/test_upgrade_target_registry.py`

**YAML schema (Codex-revised, authority-clean):**
```yaml
target_name: phase17_demo                      # symbolic; resolves to UPGRADE_TARGET_REGISTRY entry
decisions:
  no_op:      { severity_max: low }
  docs_pr:    { severity_min: medium, version_jump: [patch, minor] }
  upgrade_pr: { severity_min: medium, version_jump: [patch, minor], requires_approval: false }
  escalation: { version_jump: [major] }
```

**Code-side `UPGRADE_TARGET_REGISTRY` entry (the authority):**
```python
UPGRADE_TARGET_REGISTRY: Mapping[str, UpgradeTarget] = MappingProxyType({
    "phase17_demo": UpgradeTarget(
        target_repo="adi-prasetyo/driftscribe",            # same repo, demo path
        lockfile_path="demo/upgrade-target/package.json",
        advisory_source="github",                          # github | osv ; github only for v1
    ),
})
```

Loader fails boot on unknown `target_name` (mirrors the symbolic-name pattern for tools/workers).

**Steps:**
1. **TDD `test_upgrade_target_registry.py`:** `UPGRADE_TARGET_REGISTRY["phase17_demo"]` resolves; YAML with unknown `target_name` raises `UnknownUpgradeTargetError` at load.
2. **TDD `test_upgrade_contract.py`:** valid decision YAML parses; missing required fields raise; severity/version_jump enums constrained.
3. Pick a vulnerable package + version for the demo target (something with a published advisory; pin to that). Document choice in `demo/upgrade-target/README.md`.
4. **Commit.**

### Task 17.C.2: `upgrade-reader` worker (npm/`package.json` only for Phase 17)

**Files:**
- Create: `workers/upgrade_reader/main.py`, `workers/upgrade_reader/pyproject.toml`, `workers/upgrade_reader/Dockerfile`
- Create: `workers/upgrade_reader/tests/test_read.py`

**Hardcoded policy:** repo allowlist is **a single value pinned at deploy time via env var** `UPGRADE_TARGET_REPO` (set by `infra/cloudbuild.yaml` and verified at boot — the env value is the worker's only source of truth for the allowed repo). The worker **MUST NOT** import `agent.workloads.registry` or any coordinator module — workers are isolated from coordinator code (see comment at `agent/workloads/registry.py:429-440`). A CI guard test (created in 17.C.5) compares the env-pinned value in `infra/cloudbuild.yaml` against `UPGRADE_TARGET_REGISTRY["phase17_demo"].target_repo` so a drift between coordinator authority and worker deploy intent is caught in CI, not at runtime. Advisory source is hardcoded in the worker (GitHub Advisory DB only). `lockfile_path` comes from request body BUT must match the regex `^demo/upgrade-target/package\.json$` — **single ecosystem only for Phase 17** (per Codex). Other ecosystems are post-submission work. The worker re-validates the `target_repo` it receives against its env-pinned value — defense-in-depth so a coordinator misconfiguration cannot redirect the worker.

**Steps:**
1. **TDD `test_read.py`:**
   - `POST /read` with valid `{lockfile_path}` → returns parsed deps + matched advisories.
   - Path outside allowlist → 400.
   - Path with traversal (`../`, `demo/upgrade-target/../infra/X`) → normalize-and-reject → 400.
   - Missing bearer token → 401. Wrong audience → 401. Wrong caller email → 403.
2. Implement using `driftscribe_lib.github` for the lockfile read. GitHub Advisory query via REST.
3. Deploy with `upgrade-reader-sa@...`, `roles/secretmanager.secretAccessor` scoped to a read-only PAT (`upgrade-reader-github-pat`) only.
4. **Commit.**

### Task 17.C.3: `upgrade-docs` worker (npm/`package.json` only)

**Files:**
- Create: `workers/upgrade_docs/main.py`, `workers/upgrade_docs/pyproject.toml`, `workers/upgrade_docs/Dockerfile`
- Create: `workers/upgrade_docs/tests/test_patch.py`, `workers/upgrade_docs/tests/test_path_allowlist.py`, `workers/upgrade_docs/tests/test_post_llm_validator.py`

**Hardcoded policy:** Same repo as upgrade-reader. Path allowlist: `^demo/upgrade-target/package\.json$` only. Action: bump the version of a named package to a named target version, open PR. Cannot touch other files. Cannot delete the dep.

**Steps:**
1. **TDD `test_path_allowlist.py`:** any path outside `demo/upgrade-target/package.json` → 403. `package-lock.json` → 403. `infra/cloudbuild.yaml` → 403.
2. **TDD `test_patch.py`:** happy path bumps version; verifies PR body cites the advisory URL. Branch + PR title prefixed with `upgrade/` (observability per Codex).
3. Implement using `driftscribe_lib.github`. Separate fine-grained PAT (`upgrade-docs-github-pat`) with `Contents: read+write`, `Pull requests: read+write` on the same repo.
4. **Commit.**

### Task 17.C.3a: Post-LLM deterministic validator on upgrade-docs (Codex blocker)

Opening a PR is reversible — but it's still a repo write, and the LLM should not be the only gate. Validator runs **after** the agent's decision, **before** GitHub API call.

**Files:**
- Modify: `workers/upgrade_docs/main.py` — add `validate_upgrade_request()` invoked in `/patch` handler before any GitHub call
- Create: `workers/upgrade_docs/validator.py`
- Create: `workers/upgrade_docs/tests/test_post_llm_validator.py`

**Validator rules (deterministic, no LLM):**
- `lockfile_path` matches the worker's hardcoded regex.
- `package_name` exists in the current lockfile (read once, verify).
- `target_version` > current version (semver — no downgrades).
- `version_jump` ∈ {patch, minor} — major version bumps refused at the validator (agent should have routed those to `escalation`/notifier; if it didn't, validator fails closed).
- `advisory_url` is a `https://github.com/advisories/GHSA-...` URL — no caller-supplied arbitrary URLs in the PR body citation.
- Unknown decision keys or action names in the contract → hard config error at validator startup (Codex 2026-05-20 follow-up): the bundled contract is currently pinned by `tests/unit/test_upgrade_contract.py` cross-checks, but the validator must also reject any unknown key it sees so a future hand-edit of `contract.yaml` cannot silently bypass the action allowlist.

**Steps:**
1. **TDD `test_post_llm_validator.py`:** every rule has positive + negative tests. Major-version request → 403 with reason. Path traversal in lockfile_path → 403. Downgrade attempt → 403. Non-GHSA advisory URL → 403.
2. Implement validator. Validator is pure (no I/O except reading the lockfile via the worker's already-bound PAT for the rule-2 check).
3. **Commit.**

### Task 17.C.4: Coordinator wiring — upgrade workload routing

**Files:**
- Modify: `workloads/upgrade/workload.yaml` — fill in real symbolic worker names (placeholders → actual)
- Modify: `workloads/upgrade/system_prompt.md` — instruct agent on the 4-action decision space
- Modify: `agent/adk_tools.py` — add `delegate_to_upgrade_reader`, `delegate_to_upgrade_docs`
- Modify: `tests/unit/test_coordinator_tool_inventory.py` — expected sets updated

**Steps:**
1. Add the two new tools to `COORDINATOR_TOOLS`.
2. Drift workload's `enabled_tools` excludes the upgrade tools; upgrade workload's `enabled_tools` excludes drift tools (reader/rollback). Both include MCP tools + notifier. **`load_contract` is intentionally omitted from upgrade's `enabled_tools`** because the current `load_contract_tool` hardcodes `Settings.contract_path` (the drift ops contract) — wiring it for upgrade would point the LLM at the wrong contract. The upgrade workload doesn't need a "load contract" tool surfaced to the LLM because its policy lives in the post-LLM validator (17.C.3a) and in code-side `UPGRADE_TARGET_REGISTRY` (17.C.1). If a workload-aware contract loader becomes a real requirement, fix it in a future task (likely a `load_workload_contract_tool` wrapper that reads the current workload's `contract_file` via the ContextVar).
3. **Keep authority fields out of the LLM-facing tool surface (Codex 2026-05-20 follow-up).** The worker APIs accept `target_repo`, `lockfile_path`, `branch`, `base`, `title` because the workers re-validate them defensively — but the coordinator tools `delegate_to_upgrade_reader` and `delegate_to_upgrade_docs` MUST derive these from authoritative sources, not let the LLM choose them:
   - `target_repo` and `lockfile_path` come from `load_upgrade_contract(workload.contract_path).resolve_target()` — call this once at coordinator setup and pass the resulting `UpgradeTarget` through the tool's closure.
   - `branch` is generated server-side from the package + version (e.g. `f"upgrade/{package_name}-{target_version.replace('.', '-')}"`). Do NOT expose `branch` as an LLM-controllable tool argument.
   - `base` is hardcoded to `"main"` in the tool wrapper.
   - `title` is generated server-side (e.g. `f"upgrade({package_name}): {current_version} -> {target_version}"`). Note: the worker now enforces the `"upgrade"` title prefix at request time as defense-in-depth (added during 17.C.3a follow-up).
   - The LLM only chooses `package_name`, `target_version`, and `advisory_url` (the actual decision content). The validator (17.C.3a) and the worker policy checks (17.C.3) keep those honest.
   The worker-side revalidation stays as defense-in-depth; it must not become the primary authority boundary.
4. **Eagerly resolve upgrade contract during setup, not lazily (Codex 2026-05-20 follow-up).** `load_workload("upgrade")` resolves the workload manifest and its `contract_file` path but does NOT parse `workloads/upgrade/contract.yaml`. The new `load_upgrade_contract()` (added in 17.C.1) calls `resolve_target()` at load time and fails immediately on unknown target_name. So the coordinator's upgrade entry path (whatever wraps `/chat` and `/recheck` for upgrade) must call `load_upgrade_contract(workload.contract_path)` during setup — before the LLM starts acting. Do not lazy-load after the agent begins reasoning, because then a bad contract surfaces as a mid-conversation runtime error instead of a clean 503 at request entry.
5. **Resolve `SYSTEM_PROMPT_CHAT` deferral (Codex 2026-05-20 Important #3).** Today's chat prompt is drift-flavored ("detect, triage, and respond to drift") and `build_chat_agent` uses it for every workload — so once `POST /chat workload=upgrade` is reachable, the upgrade agent gets drift-flavored instructions with upgrade tools (semantically incoherent). Pick one:
   - **Option A** — extend `WorkloadSpec` with `chat_system_prompt_file: str | None`; populate for drift (move current `SYSTEM_PROMPT_CHAT` into `workloads/drift/chat_system_prompt.md`); for upgrade, decide whether `/chat` is a meaningful surface (probably yes — operator can ask the agent to triage a vuln report) and write an upgrade-flavored prompt.
   - **Option B** — explicitly 503 `POST /chat workload=upgrade` in `main.py` until chat semantics are defined for upgrade.
   - Recommend Option A; Option B is the escape hatch if 17.C runs long.
6. **Clean stale references in `workloads/upgrade/system_prompt.md`.** The current upgrade system prompt still mentions `get_session_state` / `set_session_state` reserved blockers from before the 17.B cleanup. Drop those mentions; the prompt is being rewritten in this task anyway.
7. **Commit.**

### Task 17.C.5: End-to-end integration test — upgrade workload

**Files:**
- Create: `tests/integration/test_upgrade_e2e.py`
- Create: `tests/integration/test_upgrade_deploy_pin.py` (or similar — CI guard test)

**Steps:**
1. Mock upgrade-reader response (returns one vulnerable package). Mock upgrade-docs response (returns PR URL).
2. `POST /chat` with `workload=upgrade, prompt="check demo target"` → coordinator calls upgrade-reader → agent calls search_developer_docs → agent decides upgrade_pr → coordinator calls upgrade-docs → coordinator calls notifier.
3. Assert the full call sequence and that no drift worker was touched.
4. **CI guard test (Codex 2026-05-20 follow-up): pin BOTH `target_repo` AND `lockfile_path`.** Parse `infra/cloudbuild.yaml` and assert the env vars `UPGRADE_TARGET_REPO` and `UPGRADE_TARGET_LOCKFILE_PATH` (if used) for both upgrade workers match `UPGRADE_TARGET_REGISTRY["phase17_demo"]`. Also pin that the registry's `lockfile_path` is accepted by `workers.upgrade_reader.main._LOCKFILE_PATH_RE.fullmatch` AND `workers.upgrade_docs.main._LOCKFILE_PATH_RE.fullmatch`. Otherwise a future registry path change would fail safely at runtime, but only after coordinator wiring sends requests that the workers reject (slower feedback loop than CI).
5. **Commit.**

### Task 17.C.6: Demo runner — new beats for upgrade workload

**Files:**
- Modify: `scripts/demo.sh` — add `upgrade-a` (no vulns → no_op), `upgrade-b` (vuln detected → propose_upgrade_pr), `upgrade-c` (major-version vuln → escalate)
- Modify: `docs/demo-script.md` — operator runbook for upgrade beats
- Modify: `docs/demo-script.ja.md` — JP translation

**Steps:**
1. New beats follow the same `reset_baseline` + `call_coordinator` pattern as drift beats.
2. `reset_baseline` for upgrade = ensure the demo target's package.json is at the vulnerable version (a fresh-checkout state).
3. **Commit.**

---

## Sub-phase 17.D — Framing pivot + documentation (1–2 days)

### Task 17.D.1: README rewrite (EN + JP)

**Files:**
- Modify: `README.md`, `README.ja.md`

**Steps:**
1. Headline shift: "Multi-agent coordinator/worker pattern for safe AI-driven DevOps on Cloud Run. Two demo workloads: live drift detection + dependency upgrade reviews."
2. New "Pattern" section that calls out: workload-aware coordinator, narrow per-workload workers, Layer 0/1/2 safety, MCP-grounded reasoning.
3. Move "drift detection" prose into a "Workload 1: Drift" subsection. Add "Workload 2: Dependency Upgrades" subsection mirroring it.
4. Update demo command list to include the upgrade beats.
5. Update Cost & Latency to note MCP adds 1 call per drift_pr / upgrade_pr path (still in the $0.0003-ish ballpark).
6. **Commit.**

### Task 17.D.2: Architecture diagram update

**Files:**
- Modify: `docs/architecture/architecture.html`

**Steps:**
1. New top row: two workload boxes (Drift, Upgrade) feeding into the coordinator.
2. New left-side branch on the coordinator: Developer Knowledge MCP.
3. Worker columns split: drift-side (Reader, Docs, Rollback) and upgrade-side (Upgrade Reader, Upgrade Docs). Notifier stays as a shared bottom row.
4. Layered-safety diagram (the second diagram) gets a Layer 0 note: "MCP tools count as Layer 0 attached tools, scoped per workload."
5. Render-check on mobile and desktop before commit.
6. **Commit.**

### Task 17.D.3: `multi-agent-design.md` update

**Files:**
- Modify: `docs/architecture/multi-agent-design.md`

**Steps:**
1. Update status banner to "Phase 17 complete."
2. New section: "§N. Workload abstraction" — explains WorkloadSpec, the routing model, why workers stay narrow.
3. New section: "§N. Developer Knowledge MCP grounding" — explains attachment at coordinator only, why workers don't get MCP.
4. Update service inventory table — add the 2 new workers.
5. Update Layer 0 description to mention MCP tools are workload-scoped.
6. **Commit.**

### Task 17.D.4: ProtoPedia text rewrite (EN + JP)

**Files:**
- Modify: `docs/submission/protopedia.en.md`, `docs/submission/protopedia.ja.md`

**Steps:**
1. New title/tagline: pattern-led, not drift-led.
2. ハイライト section: 3 bullets — multi-agent pattern, Developer Knowledge MCP grounding, two workloads.
3. デモ section: list both workloads, both demo beat sequences, link to architecture.html.
4. **Commit.**

### Task 17.D.5: IAM matrix — upgrade workload

**Files:**
- Modify: `docs/architecture/iam-matrix.md`

**Steps:**
1. Add rows for `upgrade-reader-sa@...` and `upgrade-docs-sa@...` with their grants.
2. Coordinator row: add `roles/run.invoker` on the 2 new workers, `roles/secretmanager.secretAccessor` on `developer-knowledge-api-key`.
3. Negative-space additions: upgrade-reader does NOT have project-wide GitHub access (only the scoped PAT for its one repo).
4. **Commit.**

---

## Sub-phase 17.E — Cloud Build + deploy infra (1 day)

### Task 17.E.1: `cloudbuild.yaml` — 2 new service deploys

**Files:**
- Modify: `infra/cloudbuild.yaml`

**Steps:**
1. Add build + push + deploy steps for `upgrade-reader` and `upgrade-docs`.
2. Each gets per-SA flag, `--no-allow-unauthenticated`, workload-specific env + secrets.
3. **Commit.**

### Task 17.E.2: `setup_secrets.sh` — 2 new SAs + MCP key + API enablement

**Files:**
- Modify: `infra/scripts/setup_secrets.sh`

**Steps:**
1. Enable `developerknowledge.googleapis.com` on the project (idempotent — `gcloud services enable`).
2. Create `upgrade-reader-sa@...` and `upgrade-docs-sa@...` idempotently.
3. Create per-SA secret bindings:
   - `upgrade-reader-github-pat` (read-only PAT): `gcloud secrets add-iam-policy-binding` with `roles/secretmanager.secretAccessor` for `upgrade-reader-sa` only.
   - `upgrade-docs-github-pat` (read+write PAT): same pattern, bound to `upgrade-docs-sa` only.
   - `developer-knowledge-api-key`: bound to coordinator-sa only.
4. Grant coordinator-sa `roles/run.invoker` on the 2 new workers. **This binding requires the worker services to exist** — same two-pass setup as Phase 11.8 (the script logs a hint and exits cleanly if the workers haven't been deployed yet; operator re-runs after first `gcloud builds submit`).
5. Document the API key restriction requirement: operator-side step is to restrict the API key to `developerknowledge.googleapis.com` only via Console (gcloud API key restriction is awkward; Console flow is more reliable).
6. **Commit.**

### Task 17.E.3: `e2e_smoke.sh` — positive + negative for upgrade workload

**Files:**
- Modify: `infra/scripts/e2e_smoke.sh`

**Steps:**
1. `POST /chat workload=upgrade prompt="check demo target"` → 200, trace shows expected worker calls.
2. Negative: `POST $UPGRADE_READER_URL/read` without token → 401.
3. Negative: prompt-inject upgrade workload to call drift's reader → drift reader tool not in upgrade's enabled_tools → fails closed at the agent layer.
4. **Commit.**

---

## Sub-phase 17.F — Codex review + apply fixes (1–2 days)

### Task 17.F.1: Send full Phase 17 diff to Codex thread

Use `mcp__codex__codex-reply` on existing thread `019e3af3-f679-7d20-bff1-328295c8f5df`. Bundle:
- All commits from Phase 17.
- The full WorkloadSpec schema + an example drift YAML + an example upgrade YAML.
- The two new workers' policy enforcement code.
- The MCP integration module.
- 4 negative-test results (workload routing, tool inventory cross-check, path allowlist, prompt-injection).

Ask Codex for:
- Architectural review of the workload abstraction (is the boundary in the right place?).
- Layer 0/1/2 safety review (does MCP break anything?).
- IAM review (any per-resource bindings missing?).
- Specific bug hunt in the upgrade workers' policy validation.

### Task 17.F.2: Apply Codex findings

Per the 3-tier rubric: blockers and importants fixed before close; nice-to-haves bundled or dropped.

### Task 17.F.3: Update `2026-05-19-driftscribe-v3-multi-agent.md`

**Files:**
- Modify: `docs/plans/2026-05-19-driftscribe-v3-multi-agent.md`

**Steps:**
1. Replace the "~~Phase 17: Multi-service contract support~~ — out of scope" line with a pointer: "Phase 17 (multi-agent framework + MCP + upgrade workload) — see `2026-05-19-driftscribe-phase17-framework-mcp.md`."
2. Update the phase-chain diagram to include Phase 17 between 16 and 18.
3. **Commit.**

---

## Out of scope (deliberately)

- Third workload (cert rotation, IAM audit, cost watch). Kept as post-submission ideas.
- Workload hot-reload. Add a workload = new deploy.
- Multi-tenant coordinator. Single deployment, fixed workload set.
- Other MCP servers beyond Developer Knowledge.
- ADK MCP tool caching (rely on whatever ADK ships).

## Risks + mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| ~~ADK's MCP support is stdio-only, not remote~~ | — | — | **Resolved 2026-05-20:** ADK docs confirm `StreamableHTTPConnectionParams` is supported and is the documented production pattern. No fallback needed. |
| Timeout enforcement shape varies across ADK versions | Low | Low | 17.B.2 step 1 verifies the actual kwarg on the installed ADK version before TDD. Fallback: enforce at httpx client layer in the wrapper. |
| Developer Knowledge API quota limits hit during demo | Low | Low | API key restricted to that one API, monitored via the existing budget alert. Cache last response per query for 60s to absorb retries. |
| Upgrade workload's advisory source returns no data for chosen vulnerable package | Med | Low | Pre-pick a package with a stable known advisory. Document the choice + verify in 17.C.1. |
| Workload-aware coordinator regresses existing drift tests | Low | High | 17.A.2 + 17.A.3 are gated on existing 477 tests passing byte-for-byte. |
| MCP responses leak into agent context unboundedly | Low | Med | `retrieve_developer_doc` truncates to N tokens before returning to the agent. Test asserts truncation. |
| Two workers' fine-grained PATs accidentally granted to the wrong SA | Low | High | Per-secret IAM binding in `setup_secrets.sh`; assertion in `e2e_smoke.sh` that an unintended SA can't access a sibling worker's PAT secret. |

## Success criteria

- All 477 existing tests still pass + ~50 new tests added.
- `POST /chat workload=drift` behavior identical to pre-17 (byte-for-byte prompt/contract + integration golden on `/recheck` deterministic path).
- `POST /chat workload=upgrade prompt="..."` end-to-end works against the demo target.
- `/eventarc` ignores caller-supplied workload and always dispatches as drift.
- WorkloadSpec YAML carries only symbolic names; all worker URLs/secrets/repos/paths live in `agent/workloads/registry.py`.
- Upgrade workload limited to `package.json` for Phase 17.
- Post-LLM deterministic validator gates upgrade-docs PR creation.
- Tool names workload-prefixed; no shared generic names across workloads.
- `workload` propagated in Firestore decisions, structured logs, GitHub branch+title prefixes, demo output.
- Architecture diagram renders correctly on mobile + desktop showing 2 workloads.
- Codex review pass returns no blockers or importants after fix commit.
- README/ProtoPedia framing pivot complete; drift no longer leads the pitch.

---

## Appendix: Codex review findings (plan-stage)

Plan was sent to Codex on the existing DriftScribe thread before user presentation. Findings applied above. Summary:

**Blockers (all addressed in plan):**
1. WorkloadSpec YAML must carry symbolic names only, never authority. Central code registry owns real URLs/secrets/repos. — Applied in 17.A.1.
2. `/eventarc` must not accept caller-selected workload — always routes to drift. — Applied in 17.A.3.
3. Upgrade-docs needs a post-LLM deterministic validator (path, package, no-downgrade, patch/minor only, GHSA URL check). — New task 17.C.3a.

**Importants (all addressed):**
- MCP integration shape confirmed: remote Streamable HTTP, `X-Goog-Api-Key` header, ADK `McpToolset` + `tool_filter` to expose `search_documents` + `get_documents` only (skip `answer_query`). — Applied in 17.B.2.
- MCP stays coordinator-only confirmed; if workers need citations, coordinator retrieves and passes sanitized text. — Confirmed in architecture banner.
- Integration goldens added for deterministic `/recheck` path; ADK trace identity NOT golden-tested. — Applied in 17.A.2.
- Upgrade scoped to `package.json` only for Phase 17. — Applied in 17.C.2 / 17.C.3.
- Tool names workload-prefixed. — Applied in registry definition in 17.A.1.
- `workload` propagated through Firestore + logs + GitHub branch/title + demo output. — Threaded into 17.C.3 and architecture banner.

**Nice-to-haves (folded in):**
- MCP timeout (10s), 60s in-process cache, max 5 docs/response, max 4000 chars/doc, structured log per call. — Applied in 17.B.2.
- HITL skipped for patch/minor upgrade PRs; major-version → escalate path. — Already in original plan; Codex confirmed.
- setup_secrets.sh enables API, restricts key, documents two-pass run. — Applied in 17.E.2.
