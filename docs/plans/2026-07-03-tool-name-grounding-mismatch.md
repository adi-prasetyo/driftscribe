# Tool-name grounding mismatch: prompts teach names that don't exist at the ADK layer

**Status:** IMPLEMENTED (parts A + B) on branch `fix/tool-name-grounding-mismatch`.
Prompt-only rename of all four crews to the registered `_tool` names +
`tests/unit/test_prompt_tool_names.py` (registry-cross-check regression guard) +
golden/anchor bumps (`test_drift_workload_loads.py`, `test_explore_workload_loads.py`,
`test_fanout_decompose.py`). Option C (runtime alias guard) deliberately deferred —
the honest "Tool not found" error path already fails safe. Not yet deployed: needs
the pinned-traffic coordinator redeploy (see `driftscribe-deploy` skill).
**Origin:** live prod failure on camera during the 2026-07-03 demo re-shoot (seg-live v3), prod rev `00142-5lv`.

## Incident

During the first live take (~19:00–19:30 JST, 2026-07-03), Explore was asked the scripted
payment-demo drift question. The model emitted a function call named **`read_team_log`**.
The registered LLM-facing tool is **`read_team_log_tool`**, so the ADK runner failed the
turn with "Tool not found" on camera. The error UI handled it honestly and the errored
turn correctly persisted nothing. The retake succeeded.

Evidence caveat: the blemished conversation was deleted from Firestore afterwards (chat
hygiene for the shoot), so the surviving evidence is in **Cloud Run coordinator logs**
(`tool_call` / error lines, Jul 3 ~19:00–19:30 JST), not in the conversations store.

This is the same symptom family PR #196 (coordinator → `gemini-3.5-flash` @
`thinking_level="high"`) was meant to suppress — but #196 addressed a different failure
mode. There are two distinct modes:

1. **Wrong tool chosen** (reasoning depth) — e.g. the pre-#196 case where Explore matched
   "leftover drifts" to `read_conversations` instead of `read_project_inventory`. #196's
   reasoning floor targets this.
2. **Right tool, wrong NAME** (grounding) — this incident. No amount of thinking fixes a
   prompt that actively teaches a nonexistent name; the model must notice the
   prompt-vs-declaration discrepancy on every turn and sometimes doesn't.

## Root cause

Every tool has up to three names, and the prompts mix them:

| Layer | Example | Where |
| --- | --- | --- |
| YAML capability name | `read_team_log` | `workloads/<crew>/workload.yaml` `enabled_tool_names` |
| Registered callable = **LLM-facing tool name** | `read_team_log_tool` | `agent/workloads/registry.py` `_TOOL_REGISTRY` → the function's `__name__` (ADK registers tools by function name) |
| Prompt text | `read_team_log(pr_number, limit)` | `workloads/explore/system_prompt.md:97` |

The model sees `read_team_log_tool` in its tool declarations but the system prompt
describes the tool as `read_team_log`. MCP-attached tools (`search_developer_docs`,
`retrieve_developer_doc`) have **no** `_tool` suffix, so the naming is inconsistent even
within one crew's toolset — the model cannot infer a rule; it has to memorize which names
are real.

## Audit (2026-07-03, HEAD = 35cf642)

Cross-check of every `enabled_tool_names` entry vs its registered callable vs mentions in
that crew's prompt file(s). "BARE ONLY" = the prompt teaches a name that does not exist
at the ADK layer; a call using it fails "Tool not found".

| Crew | Capability | Registered (LLM-facing) | Prompt says |
| --- | --- | --- | --- |
| drift | `read_conversations` | `read_conversations_tool` | **BARE ONLY** (×2) |
| upgrade | `read_conversations` | `read_conversations_tool` | **BARE ONLY** (×2) |
| provision | `read_conversations` | `read_conversations_tool` | **BARE ONLY** (×2) |
| provision | `provision_open_infra_pr` | `open_infra_pr_tool` | **BARE ONLY** (×4 — prompt uses the YAML name, which is neither) |
| provision | `provision_propose_adoption` | `propose_adoption_tool` | **BARE ONLY** (×4 — same) |
| provision | `read_project_inventory` | `read_project_inventory_tool` | mixes both (bare ×1, suffixed ×2) |
| explore | `read_project_inventory` | `read_project_inventory_tool` | **BARE ONLY** (×4) |
| explore | `read_team_log` | `read_team_log_tool` | **BARE ONLY** (×3) — the on-camera failure |
| explore | `read_conversations` | `read_conversations_tool` | **BARE ONLY** (×3) |

All other tools are referenced by their registered names (e.g. `load_contract_tool`,
`propose_rollback_tool`) or genuinely have no suffix (the two MCP doc tools). Note the
pattern: the mismatches cluster on the newer tools (team memory, conversations,
inventory, provision authoring) — older prompts were written against the `_tool` names.

Reproduce the audit:

```bash
python3 - <<'EOF'
import re, pathlib, yaml
src = pathlib.Path("agent/workloads/registry.py").read_text()
reg = dict(re.findall(r'"([a-z_]+)":\s+([a-z_]+)', src.split("_TOOL_REGISTRY", 1)[1]))
for wl in ["drift", "upgrade", "provision", "explore"]:
    caps = yaml.safe_load(pathlib.Path(f"workloads/{wl}/workload.yaml").read_text())["enabled_tool_names"]
    text = "\n".join(p.read_text() for p in pathlib.Path(f"workloads/{wl}").glob("*prompt*.md"))
    for c in caps:
        fn = reg.get(c, "?")
        bare = len(re.findall(rf'(?<![a-z_]){re.escape(c)}(?!_tool)(?![a-z])', text))
        suff = len(re.findall(rf'(?<![a-z_]){re.escape(fn)}(?![a-z_])', text))
        if fn != c and bare and not suff:
            print(f"{wl}: prompt says {c!r}, registered name is {fn!r}")
EOF
```

## Recommended fix (two parts)

**A. Prompt-only rename (primary).** In all four crews' prompt files, refer to every
coordinator-local tool by its registered name (`read_team_log_tool`,
`read_conversations_tool`, `read_project_inventory_tool`, `open_infra_pr_tool`,
`propose_adoption_tool`). Prompt-only, same shape as PRs #174/#201 — but see the test
gotchas below.

**B. Regression test (cheap, prevents recurrence).** A unit test that extracts
tool-name-ish tokens from every prompt file and asserts each one is either a registered
callable name or an MCP tool name — i.e. the audit above, permanently. Without this, the
next prompt PR reintroduces the drift.

**Optional C. Runtime alias guard.** A dispatch-layer normalization (unknown function
call name + known alias, e.g. `read_team_log` → `read_team_log_tool`, retry instead of
erroring the turn). Catches model-invented bare names even with perfect prompts, but
touches the ADK event loop (`agent/adk_agent.py` run paths) — only worth it if the flake
recurs after A+B, since the honest "Tool not found" error path already fails safe.

Not recommended: renaming the Python functions to the bare names. It would change the
LLM-facing names to match the (shorter) prompt vocabulary, but the `_tool` names are
load-bearing in logs, the timeline UI ("Tools & workers" rows show e.g.
`load_contract_tool` on camera in the shipped demo video), tests, and docs.

## Gotchas for the implementing agent

- **Drift's system prompt is byte-golden-pinned**: `tests/unit/test_drift_workload_loads.py`
  holds the full prompt as a string literal — update the golden alongside the prompt.
- **Explore's test anchors on the bare name**: `tests/unit/test_explore_workload_loads.py:147`
  asserts `"read_project_inventory to situate"` — update the anchor with the rename.
  Grep all of `tests/unit/` for the bare names before assuming test impact is contained.
- **Prompts are judge-facing**: `GET /workloads/{name}/prompts` renders each crew's real
  system prompt in-app (crew prompt viewer). The `_tool` suffix reads slightly more
  machine-y; that is fine — accuracy beats polish here, and the timeline already shows
  suffixed names.
- **Deploy**: prompt files ship with the coordinator image — this needs a coordinator
  redeploy + traffic update (see the `driftscribe-deploy` skill; traffic is pinned).
- **Verify on prod**: after deploy, re-run the scripted Explore probe (ephemeral:true —
  see live-probe recipes) with an ask that exercises `read_team_log_tool` and confirm the
  timeline shows the call succeeding.
