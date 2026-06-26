"""ADK Agent wiring for the coordinator (Phase 11.7 — multi-agent rewrite).

The coordinator's brain. Two distinct LLM personalities live here:

1. :func:`run_agent` — structured drift-triage path. The ADK agent
   produces a JSON :class:`DecisionProposal` validated by
   :func:`agent.validator.validate`. Used by ``/recheck``. Same three-
   layer design as Phase 6: LLM proposes → deterministic validator
   gates → action layer executes.
2. :func:`run_chat` — free-form operator interface. The LLM picks
   tools, may call multiple workers, and produces natural-language
   text. Used by ``/chat`` (Phase 11.7). NOT gated by the structured
   validator — instead, every mutation goes through a worker, and
   each worker has its own Layer 2 payload-intent policy.

**Layer 0 (capability-bounded tool registry):**
:data:`COORDINATOR_TOOLS` below is the EXHAUSTIVE list of tools
available to either personality. Adding a tool here without updating
the Phase 11.4b inventory test (``tests/unit/test_coordinator_tool_inventory.py``,
not in this commit) triggers a CI failure. The list is intentionally
flat — no submodule grouping, no dynamic registration — so the
inventory test can do a 1:1 set comparison.

Worker-delegating tools (4 drift + 4 upgrade = 8):
- ``read_live_env_tool`` → Reader Agent ``/read``
- ``propose_rollback_tool`` → Rollback Agent ``/propose`` (HITL-gated)
- ``patch_docs_tool`` → Docs Agent ``/patch``
- ``notify_tool`` → Notifier Agent ``/notify``
- ``upgrade_read_dependencies_tool`` → Upgrade Reader Agent ``/read``
  (Phase 17.C.4). Authority-clean: no LLM-controllable args.
- ``upgrade_propose_pr_tool`` → Upgrade Docs Agent ``/patch``
  (Phase 17.C.4). Authority-clean: LLM picks decision content only;
  repo / lockfile path / branch / base / title derived server-side.
- ``upgrade_close_pr_tool`` → Upgrade Docs Agent ``/close``. Chat-only
  (CHAT_ONLY_TOOL_NAMES). Authority-clean: pr_number + reason only.
- ``upgrade_merge_pr_tool`` → Upgrade Docs Agent ``/merge`` (Phase 20.9).
  Chat-only. Authority-clean: pr_number only; squash + required-check
  allowlist pinned server-side. The one tool that mutates ``main``.

Coordinator-internal read-only tools (3):
- ``search_recent_prs_tool`` (read-only GitHub via coordinator PAT)
- ``load_contract_tool`` (reads baked-in ops contract)
- ``read_team_log_tool`` (reads the durable decision log as "team memory";
  allowlist-projected status tokens + pointers — no rationale / diffs /
  approval tokens. Coordinator-local StateStore read, no worker, no PAT —
  read-only by operation AND credential; exposed by the chat-only ``explore``
  workload).

Developer Knowledge MCP wrappers (2, Phase 17.B.3):
- ``search_developer_docs`` → Developer Knowledge MCP ``search_documents``
- ``retrieve_developer_doc`` → Developer Knowledge MCP ``get_documents``

Infra-IaC read-only inventory (1):
- ``read_project_inventory_tool`` → Infra-Reader Agent ``/describe``.
  Read-only (cloudasset.viewer + serviceUsageConsumer); exposed by the
  chat-only ``explore`` workload. Authority-clean: takes no args.

That's 14 tools, period (8 → 10 in 17.C.4 with the upgrade reader/proposer;
→ 11 with close; → 12 in 20.9 with merge; → 13 with the infra-IaC inventory
reader; → 14 with the read_team_log decision-log reader). Anything else the
model wants to do is denied by capability — there is no general "execute
shell" or "make HTTP request" surface. (This enumeration omits the
later-added ``load_iac_plan_tool`` and the two ``provision`` mutation tools,
``open_infra_pr_tool`` / ``propose_adoption_tool`` — the authoritative,
test-pinned surface is ``EXPECTED_TOOL_NAMES`` in the inventory test.)

**Per-workload tool inventories (Phase 17.A.4):**
:data:`DRIFT_WORKLOAD_TOOL_NAMES`, :data:`UPGRADE_WORKLOAD_TOOL_NAMES`, and
:data:`EXPLORE_WORKLOAD_TOOL_NAMES` mirror each workload YAML's
``enabled_tool_names`` field — the symbolic filter applied per workload
over the global registry. They are distinct from :data:`COORDINATOR_TOOLS`
(the Python-callable registration manifest); see the block comment around
the constants below for the rationale, the tuple-vs-frozenset choice, and
the three-way YAML ⇄ code ⇄ runtime equality enforced by
``tests/unit/test_coordinator_tool_inventory.py``. ``explore`` is the
chat-only, strictly read-only workload: its inventory is a read-only
SUBSET of the others PLUS the read-only callables it introduces:
``read_project_inventory`` (infra-IaC initiative) — backed by the
infra_reader worker (cloudasset.viewer + serviceUsageConsumer) — and
``read_team_log`` (coordinator-local decision-log read; no worker). Both are
strictly read-only and do not widen the mutation surface. Those two callables
are what bump the count above from 12 to 14.
"""

import json
import logging
import re
import uuid
from datetime import datetime, timezone

from google.adk import Agent
from google.adk.planners.built_in_planner import BuiltInPlanner
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from google.genai.types import ThinkingConfig

from agent.adk_tools import (
    iac_pr_pointer,
    load_contract_tool,
    load_iac_plan_tool,
    notify_tool,
    open_infra_pr_tool,
    patch_docs_tool,
    propose_adoption_tool,
    propose_rollback_tool,
    read_live_env_tool,
    read_project_inventory_tool,
    read_team_log_tool,
    search_recent_prs_tool,
    upgrade_close_pr_tool,
    upgrade_merge_pr_tool,
    upgrade_propose_pr_tool,
    upgrade_read_dependencies_tool,
)
from agent.mcp.developer_knowledge import (
    retrieve_developer_doc,
    search_developer_docs,
)
from agent.autonomy import autonomy_instruction_note, filter_tools_for_mode
from agent.models import DecisionProposal
from agent.request_context import autonomy_mode_scope
from agent.secret_guard import redact_dict, redact_event, redact_text
from agent.workload_context import current_workload
from agent.workloads import WorkloadResolution, load_workload
from agent.workloads.registry import TOOL_TIERS
from driftscribe_lib.logging import current_trace_id_or_new

# Log convention: every structured log record emits its event name in
# BOTH `msg` (human-readable, stdlib-logging-ergonomic) AND
# `extra={"event": ...}` (machine-filterable as a Cloud Logging top-level
# field — see the `jsonPayload.event=...` query in
# docs/runbooks/deploy.md Step 1b).
# `agent/mcp/developer_knowledge.py:_log_call` follows the same pattern.
_log = logging.getLogger("driftscribe.agent.adk_agent")

# --------------------------------------------------------------------------- #
# Layer 0: Capability-bounded tool registry
# --------------------------------------------------------------------------- #

COORDINATOR_TOOLS = [
    read_live_env_tool,
    propose_rollback_tool,
    patch_docs_tool,
    notify_tool,
    search_recent_prs_tool,
    load_contract_tool,
    # Developer Knowledge MCP wrappers (Phase 17.B.3). Async callables
    # wrapping the Streamable HTTP MCP server — see
    # ``agent.mcp.developer_knowledge`` for cache + timeout + log details.
    search_developer_docs,
    retrieve_developer_doc,
    # Upgrade workload tools (Phase 17.C.4). Authority-clean LLM-facing
    # surface — see ``agent.adk_tools.upgrade_read_dependencies_tool``
    # and ``agent.adk_tools.upgrade_propose_pr_tool`` for the
    # routing-fields-server-side rationale.
    upgrade_read_dependencies_tool,
    upgrade_propose_pr_tool,
    # Upgrade PR close (operator-driven withdrawal). Authority-clean:
    # the LLM picks only pr_number + reason; the worker gates the close
    # on driftscribe-label + upgrade/ branch + main base.
    upgrade_close_pr_tool,
    # Upgrade PR merge (operator-driven). Authority-clean: the LLM picks
    # only pr_number; the worker gates on the same provenance triple PLUS
    # fail-closed CI (required check green on head + no conflict) and
    # merges with a deploy-pinned squash.
    upgrade_merge_pr_tool,
    # Infra-IaC read-only inventory (whole-project resource describe).
    # Backed by the infra_reader worker (cloudasset.viewer +
    # serviceUsageConsumer) — no mutation surface. Exposed by the chat-only
    # ``explore`` workload.
    # Authority-clean: takes no args; the worker has the target project
    # pinned via env.
    read_project_inventory_tool,
    # Item 12 — read the latest verified plan artifact for a pending infra PR.
    # Coordinator-local, GCS listing only (objectViewer on artifacts bucket;
    # no GitHub PAT) — read-only by both operation and credential. Exposed by
    # the chat-only ``explore`` workload; intentionally NOT in
    # MUTATION_TOOL_NAMES.
    load_iac_plan_tool,
    # "Team memory" — read the durable decision log (allowlist-projected status
    # tokens + pointers). Coordinator-local StateStore read; no worker, no
    # GitHub PAT — read-only by operation AND credential. Exposed by the
    # chat-only ``explore`` workload; intentionally NOT in MUTATION_TOOL_NAMES.
    read_team_log_tool,
    # Provision workload (Phase D2) — author OpenTofu (IaC) edits and open
    # ONE iac/-only PR via the tofu-editor worker. Authority-clean LLM-facing
    # surface: the LLM supplies only the file writes + PR title/body; every
    # routing field (target_repo / branch / base / label) is derived
    # server-side. The tofu-editor re-validates every file before any GitHub
    # call — see ``agent.adk_tools.open_infra_pr_tool``.
    open_infra_pr_tool,
    # Adopt tool (adopt design Phase 3) — renders probe-proven zero-change
    # import HCL for one live resource and opens the PR via the same
    # tofu-editor path. Authority-clean: no live infra changes; import only.
    # Symbolic name: ``provision_propose_adoption``.
    propose_adoption_tool,
]


# Tools exposed ONLY on the interactive /chat surface — never handed to
# the autonomous /recheck agent. Closing AND merging a PR are
# operator-driven, destructive/availability actions; giving either to the
# autonomous classifier would make it a mutation surface gated only by
# prompt discipline (Codex review 2026-05-25). Merge is the more
# dangerous of the two — it writes to ``main`` — so it is even more
# important that the unattended classifier can't reach it.
# :func:`build_agent` (/recheck) filters these out by symbolic name;
# :func:`build_chat_agent` keeps them. The worker-side gate (provenance +
# fail-closed CI for merge) still applies either way — this is defense in
# depth at the routing layer, matching the per-workload capability-bound
# pattern.
CHAT_ONLY_TOOL_NAMES: frozenset[str] = frozenset(
    {"upgrade_close_pr", "upgrade_merge_pr"}
)


# --------------------------------------------------------------------------- #
# Per-workload tool inventories (Phase 17.A.4)
# --------------------------------------------------------------------------- #
#
# These tuples mirror each workload YAML's ``enabled_tool_names`` field —
# i.e. the *symbolic* names the workload references, NOT the Python
# callable names. The two surfaces are deliberately distinct:
#
# - ``COORDINATOR_TOOLS`` above is the Python-callable registration
#   manifest. Every callable ever wired to the agent factory lives here.
# - ``DRIFT_WORKLOAD_TOOL_NAMES`` / ``UPGRADE_WORKLOAD_TOOL_NAMES`` below
#   are the *per-workload symbolic filters* — the names the YAML uses to
#   pick a subset of :data:`agent.workloads.registry.TOOL_REGISTRY`.
#   Drift's eight callables (six pre-17.B.3 plus the two Developer
#   Knowledge MCP wrappers added in 17.B.3) happen to coincide with the
#   global registration today; upgrade adds two reserved-not-yet symbols
#   (``upgrade_read_dependencies``, ``upgrade_propose_pr``) that 17.C
#   will flip from ``None`` to real callables, plus two
#   reserved-not-yet session-memory symbols slated for 17.B.
#
# Why tuples (not frozensets):
# - These constants double as the *tool-order pin* (M-6 from the 17.A.3
#   Codex review): the order here equals the order ``Agent.tools``
#   receives via ``list(workload.tools.values())``, which equals the
#   order the LLM sees in its tool-list prompt. A silent YAML reorder
#   that shuffled the prompt could degrade tool selection — the inventory
#   test pins both inventory AND order against these tuples.
# - The cross-workload disjointness test treats them as sets at the call
#   site; the dual usage is fine because tuples preserve order while
#   still supporting ``set(tuple)`` membership checks.
#
# Adding a tool to either workload's YAML without updating the matching
# tuple here (or vice versa) fails the inventory test in
# ``tests/unit/test_coordinator_tool_inventory.py``. That coupling is
# the point — the test pins YAML ⇄ code constant ⇄ runtime resolution
# as a three-way equality.

DRIFT_WORKLOAD_TOOL_NAMES: tuple[str, ...] = (
    "drift_read_live_env",
    "drift_patch_docs",
    "drift_propose_rollback",
    "notify",
    "load_contract",
    "search_recent_prs",
    # Phase 17.B.3 — Developer Knowledge MCP. Drift cites authoritative
    # Cloud Run env-variable guidance in its docs PR bodies.
    "search_developer_docs",
    "retrieve_developer_doc",
)

UPGRADE_WORKLOAD_TOOL_NAMES: tuple[str, ...] = (
    "upgrade_read_dependencies",
    "upgrade_propose_pr",
    "upgrade_close_pr",
    "upgrade_merge_pr",
    "notify",
    "search_recent_prs",
    "search_developer_docs",
    "retrieve_developer_doc",
    # Session-state tools (``get_session_state`` / ``set_session_state``)
    # were previously listed here as aspirational future work; the
    # 17.B.4 follow-up review (Codex) removed them because they remain
    # ``None`` in ``TOOL_REGISTRY`` and would otherwise keep
    # ``load_workload("upgrade")`` failing with
    # ``ReservedToolNotImplementedError`` even after 17.C wires the
    # ``upgrade_*`` tools. They stay reserved in ``_TOOL_REGISTRY`` so
    # the reserved-tool inventory test still pins their names — if ADK
    # session-state becomes a real requirement, the same PR that flips
    # their registry entries to callables can re-add them here.
)

# The chat-only, strictly read-only workload. Its tools are a read-only
# SUBSET of what drift/upgrade already expose, PLUS the one read-only
# callable this workload introduces to COORDINATOR_TOOLS:
# ``read_project_inventory`` (infra-IaC initiative). That callable is
# itself strictly read-only — backed by the infra_reader worker which
# holds only cloudasset.viewer + serviceUsageConsumer — so adding it does
# NOT widen the mutation
# surface. By construction this list lists ZERO mutation tools (no
# patch/rollback/PR-open/close/merge) and not even ``notify`` or
# ``search_recent_prs`` (the latter rides the write-capable coordinator
# PAT). The read-only guarantee is pinned in
# ``tests/unit/test_coordinator_tool_inventory.py`` as a disjointness
# assertion against the mutation-tool set — see ``_MUTATION_TOOL_NAMES``
# there. Order mirrors ``workloads/explore/workload.yaml`` (tool-order pin) —
# tools are appended in YAML order; ``read_team_log`` is currently last.
EXPLORE_WORKLOAD_TOOL_NAMES: tuple[str, ...] = (
    "drift_read_live_env",
    "upgrade_read_dependencies",
    "load_contract",
    "search_developer_docs",
    "retrieve_developer_doc",
    "read_project_inventory",
    # Item 12 — pending-infra-PR plan Q&A. Read-only by credential
    # (GCS objectViewer, no GitHub PAT) — see agent/adk_tools.py.
    "load_iac_plan",
    # "Team memory" — read the durable decision log (allowlist-projected).
    # Coordinator-local StateStore read; no worker, no GitHub PAT — read-only
    # by operation AND credential. Appended LAST to match the YAML tool-order.
    "read_team_log",
)

# The chat-only IaC-authoring workload (Phase D2). Its read set is
# explore's read subset MINUS ``upgrade_read_dependencies`` (provision has
# no lockfile/dependency concern) PLUS the one MUTATION tool it introduces:
# ``provision_open_infra_pr``. That tool authors validated iac/-only file
# writes and opens ONE PR via the tofu-editor worker — it writes HCL and
# opens a PR, it never touches live infra directly. So UNLIKE ``explore``,
# provision is deliberately NOT asserted read-only: it intentionally carries
# a mutation tool (pinned in ``tests/unit/test_coordinator_tool_inventory.py``
# — ``provision_open_infra_pr`` IS in ``_MUTATION_TOOL_NAMES`` and the
# ``tofu_editor`` worker IS in ``_MUTATION_WORKER_NAMES``). Order mirrors
# ``workloads/provision/workload.yaml`` exactly (tool-order pin) — the
# read tools first, ``provision_open_infra_pr`` LAST.
PROVISION_WORKLOAD_TOOL_NAMES: tuple[str, ...] = (
    "drift_read_live_env",
    "read_project_inventory",
    "load_contract",
    "search_developer_docs",
    "retrieve_developer_doc",
    "provision_open_infra_pr",
    "provision_propose_adoption",
)


# --------------------------------------------------------------------------- #
# Structured drift-triage agent (/recheck)
# --------------------------------------------------------------------------- #
#
# Phase 17.A.2: the structured drift-triage system prompt now lives in
# ``workloads/drift/system_prompt.md`` and is loaded lazily via
# :func:`agent.workloads.load_workload`. This keeps prompt edits
# workload-scoped instead of code-scoped — the next workload (upgrade,
# Phase 17.C) brings its own prompt the same way. The unit test
# ``tests/unit/test_drift_workload_loads.py`` pins the file's content
# byte-for-byte against the pre-17 hardcoded constant so accidental edits
# during the move are caught at CI time, not at first /recheck.
#
# Lazy load (not module-import-time):
# - Reading the workload requires the four worker URL env vars to be set
#   (READER_URL etc.); tests that monkeypatch those before invoking
#   ``run_agent`` need the read to happen *after* the monkeypatch. The
#   pattern matches :mod:`agent.worker_client`'s lazy env reads.
# - Caching lives one layer down — :func:`agent.workloads.load_workload`
#   memoizes per workload name, so repeat callers pay the I/O once.
#
# 17.A.3: ``build_agent`` and ``build_chat_agent`` now take an explicit
# :class:`~agent.workloads.WorkloadResolution`. The caller (``agent.main``)
# decides which workload to load per request and passes the resolution
# in; the factory no longer makes that choice itself. This is what makes
# the per-request ``workload=`` field on /chat and /recheck meaningful —
# the agent built for workload=X carries workload=X's system prompt and
# (today) the coordinator's shared tool set.
#
# Tool set: Phase 17.A.3 hands the ADK ``Agent(tools=...)`` argument
# the workload's filtered tool list (``list(workload.tools.values())``),
# NOT the union :data:`COORDINATOR_TOOLS`. This means the LLM is never
# even shown a cross-workload tool — the capability-bound invariant
# holds at the runner layer, not just at the registry layer. The Codex
# review of 17.A.3 flagged the initial implementation (which still
# passed the union) as leaving the runner-layer invariant unfinished;
# the swap to per-workload tools closes that gap today.
#
# :data:`COORDINATOR_TOOLS` is kept as the *registration manifest* —
# the place the inventory test
# (``tests/unit/test_coordinator_tool_inventory.py``) pins the set of
# Python callables that the coordinator may ever wire to ANY workload.
# That's still a meaningful Layer 0 surface: a PR can't sneak a new
# tool in without updating that constant.
#
# Phase 17.C.4 (Option A from the plan): the ``/chat`` system prompt is
# now per-workload. Drift's prompt lives in
# ``workloads/drift/chat_system_prompt.md`` (byte-identical to the
# pre-17.C.4 ``SYSTEM_PROMPT_CHAT`` constant — pinned by
# ``tests/unit/test_drift_workload_loads.py::test_drift_chat_system_prompt_file_matches_pre17c4_constant``);
# upgrade's lives in ``workloads/upgrade/chat_system_prompt.md`` and
# describes the upgrade tool surface in operator-facing terms. The
# loader (:func:`agent.workloads.registry._load_from_path`) populates
# :class:`~agent.workloads.WorkloadResolution.chat_system_prompt`;
# :func:`build_chat_agent` reads that field. Workloads that want the
# same prompt on both ``/chat`` and ``/recheck`` can leave
# ``chat_system_prompt_file`` unset in YAML — the registry falls back
# to ``system_prompt``.


# --------------------------------------------------------------------------- #
# Free-form chat agent (/chat)
# --------------------------------------------------------------------------- #

# Greedy + DOTALL on purpose: when the model wraps JSON in a ```json fence
# (or leads with prose), we want from the first `{` to the last `}`.
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _parse_response(text: str) -> DecisionProposal:
    """Extract JSON from an LLM response and parse it into a `DecisionProposal`."""
    if not text:
        raise RuntimeError("ADK agent produced no final response")
    m = _JSON_BLOCK.search(text)
    if not m:
        raise RuntimeError(
            "ADK agent response did not contain a JSON object: "
            f"{text[:200]!r}"
        )
    raw_json = m.group(0)
    payload = json.loads(raw_json)
    return DecisionProposal.model_validate(payload)


def _dial_instruction(base_prompt: str, autonomy_mode: str) -> str:
    """Append the informational autonomy note when the dial strips tools.

    UX only — enforcement already happened at the registry filter. The note
    is added for every mode below propose_apply (the modes that actually drop
    tools); propose_apply leaves the prompt byte-identical.
    """
    if autonomy_mode == "propose_apply":
        return base_prompt
    return f"{base_prompt}\n\n{autonomy_instruction_note(autonomy_mode)}"


def build_agent(workload: WorkloadResolution, *, autonomy_mode: str) -> Agent:
    """Construct the /recheck-flavored ADK Agent for the given workload.

    Takes an already-loaded :class:`~agent.workloads.WorkloadResolution`
    so the caller (``agent.main``) controls workload selection per
    request. The factory itself is a pure function over the resolution +
    ``autonomy_mode`` — no env reads, no module-level state — so the same
    inputs always yield an agent with the same prompt and tools.

    Tool set: ``workload.tools`` minus the chat-only names (the existing
    /recheck strip) THEN filtered by the autonomy dial via
    :func:`agent.autonomy.filter_tools_for_mode` over
    :data:`agent.workloads.registry.TOOL_TIERS` (Layer 0, ClickOps item 11).
    Order is preserved through both filters. A tool with no tier assignment
    fails closed (treated as apply-tier). ``autonomy_mode`` is a REQUIRED
    keyword arg — a call site that forgets the dial fails loudly at code
    time, never silently runs at full autonomy.

    ADK requires agent names to be valid Python identifiers (letters,
    digits, underscores; no hyphens). The workload name is from the
    closed Literal ``{"drift", "upgrade", "explore", "provision"}``, all
    identifier-safe.
    """
    recheck_tools = {
        name: fn
        for name, fn in workload.tools.items()
        if name not in CHAT_ONLY_TOOL_NAMES
    }
    allowed = filter_tools_for_mode(recheck_tools, TOOL_TIERS, autonomy_mode)
    return Agent(
        name=f"driftscribe_{workload.spec.name}",
        model="gemini-2.5-flash",
        instruction=_dial_instruction(workload.system_prompt, autonomy_mode),
        tools=list(allowed.values()),
        # 18.B.1: surface Gemini 2.5 Flash's thought summaries. The model
        # already spends thinking tokens at default-dynamic budget; this
        # only changes whether the summaries are *returned*.
        planner=BuiltInPlanner(
            thinking_config=ThinkingConfig(include_thoughts=True),
        ),
    )


def build_chat_agent(workload: WorkloadResolution, *, autonomy_mode: str) -> Agent:
    """Construct the /chat-flavored ADK Agent for the given workload.

    Same workload + ``autonomy_mode`` parameters as :func:`build_agent`. The
    system prompt here is
    :attr:`~agent.workloads.WorkloadResolution.chat_system_prompt`
    — Phase 17.C.4 (Option A from the plan) moved the previously
    coordinator-wide ``SYSTEM_PROMPT_CHAT`` constant into per-workload
    files (``workloads/drift/chat_system_prompt.md`` and
    ``workloads/upgrade/chat_system_prompt.md``) so the upgrade chat
    surface gets upgrade-flavored instructions, not drift's. Workloads
    that want the same prompt on both surfaces leave
    ``chat_system_prompt_file`` unset in YAML — the registry falls back
    to ``system_prompt``.

    Tool list is the FULL per-workload set (including CHAT_ONLY_TOOL_NAMES),
    then filtered by the autonomy dial via
    :func:`agent.autonomy.filter_tools_for_mode` (Layer 0). ``autonomy_mode``
    is a REQUIRED keyword arg — same loud-fail contract as
    :func:`build_agent`.
    """
    allowed = filter_tools_for_mode(workload.tools, TOOL_TIERS, autonomy_mode)
    return Agent(
        name=f"driftscribe_chat_{workload.spec.name}",
        model="gemini-2.5-flash",
        instruction=_dial_instruction(workload.chat_system_prompt, autonomy_mode),
        tools=list(allowed.values()),
        # 18.B.1: surface Gemini 2.5 Flash's thought summaries. The model
        # already spends thinking tokens at default-dynamic budget; this
        # only changes whether the summaries are *returned*.
        planner=BuiltInPlanner(
            thinking_config=ThinkingConfig(include_thoughts=True),
        ),
    )


# --------------------------------------------------------------------------- #
# Shared per-event log emitters — used by both ``run_agent`` and ``run_chat``.
# --------------------------------------------------------------------------- #
#
# Phase 19.A.3 code-review follow-up: the ~45-line block that emits
# ``llm_thought`` / ``tool_call`` / ``tool_result`` from a single event's
# part list was byte-identical between the two event loops; same for the
# ~12-line ``llm_usage`` tail. Extracting both into module-private helpers
# closes the future-drift risk where a new field on (e.g.) ``tool_result``
# would have to be added in two places in lock-step. The ``final_response``
# emit deliberately stays inline in each loop because it depends on
# per-loop text accumulators (``parts_text`` in :func:`run_agent`,
# ``reply_chunks`` in :func:`run_chat`) plus the per-loop
# ``final_response_logged`` flag — extracting it would require passing too
# much state and lose clarity.
#
# Asymmetry: :func:`run_chat` appends each ``function_call.name`` to its
# ``tool_calls`` list (a public-contract response field, surfaced in the
# ``/chat`` JSON body since Phase 11.7). :func:`run_agent` has no such
# list. The helper threads an optional ``tool_calls`` list — ``None`` for
# the recheck path, the real list for the chat path. Byte-identical log
# emission either way.


def _emit_event_logs(
    event,
    *,
    tool_calls: list[str] | None = None,
    iac_pr_sink: dict | None = None,
) -> list[dict]:
    """Emit ``llm_thought`` / ``tool_call`` / ``tool_result`` log lines
    for one ADK event's part list.

    ``iac_pr_sink`` (Phase 3 approval-CTA): when provided, an ``open_infra_pr``
    function_response carrying a CONFIRMED PR (validated by
    :func:`agent.adk_tools.iac_pr_pointer`) overwrites the sink with
    ``{pr_number, pr_url}`` (last-write-wins). The match is on the tool NAME
    (``open_infra_pr_tool``) — NOT the result shape — so an upgrade PR (same
    pr_number/pr_url fields, different tool) never surfaces an /iac-approvals CTA.
    The sink lets :func:`run_chat_stream` attach a structured ``iac_pr`` field to
    its terminal item so the SPA can render a clickable first-authoring CTA.

    Phase 22: returns the list of redacted payloads it logged, in emit
    order (0..N). The durable Cloud Logging copy is byte-identical to the
    pre-Phase-22 behavior — the return value is purely additive so
    :func:`run_chat_stream` can yield the SAME redacted dict it logged
    (single source of truth for redaction). :func:`run_agent` ignores the
    return value.

    Callers must apply the partial-event dedup gate (``event.partial is
    not True``) before invoking this — the helper assumes the event is
    a merged non-partial event whose parts are eligible to log. The
    gate stays at the call site (rather than being absorbed in here)
    so each loop's intent — "log only on merged events" — is visible
    at the loop body level without a hop into a helper.

    18.B.2: a ``thought=True`` part with non-empty ``text`` emits
    ``llm_thought`` and then ``continue``s to the next part — the
    helper does NOT also check ``function_call`` / ``function_response``
    on the same part. In practice ADK never sets both on one part, so
    this matches the pre-refactor behavior byte-for-byte; the order
    pins which slot wins if a future ADK release ever conflates them.

    19.A.3: every emit goes through :func:`redact_event` at the
    boundary so the durable Cloud Logging copy never carries
    credentials.
    """
    emitted: list[dict] = []
    for part in event.content.parts:
        if getattr(part, "thought", False) and getattr(part, "text", None):
            # Thought-with-text wins this part — see docstring for the
            # ordering rationale. function_call / function_response on
            # the same part (not seen in practice) would be skipped.
            payload = redact_event({
                "event": "llm_thought",
                "trace_id": current_trace_id_or_new(),
                "workload": current_workload(),
                "thought_text": part.text,
            })
            _log.info("llm_thought", extra=payload)
            emitted.append(payload)
            continue
        fc = getattr(part, "function_call", None)
        if fc and getattr(fc, "name", None):
            # 19.A.3: tool_call now carries a redacted ``tool_args``
            # dict so the transparency UI can show "what did the
            # model ask the tool to do." Key-aware redaction via
            # :func:`redact_dict` runs at the boundary; the
            # outer :func:`redact_event` is defense-in-depth for
            # the rest of the structured log payload.
            args = getattr(fc, "args", None) or {}
            if tool_calls is not None:
                tool_calls.append(fc.name)
            payload = redact_event({
                "event": "tool_call",
                "trace_id": current_trace_id_or_new(),
                "workload": current_workload(),
                "tool_name": fc.name,
                "tool_args": redact_dict(args),
            })
            _log.info("tool_call", extra=payload)
            emitted.append(payload)
            continue
        fr = getattr(part, "function_response", None)
        if fr and getattr(fr, "name", None):
            # 19.A.3: brand-new ``tool_result`` event emitted on
            # every ``function_response`` part. CRITICAL (Codex
            # v2 review): redact the STRUCTURED response BEFORE
            # serializing — otherwise ``should_redact("PASSWORD",
            # ...)`` never fires on nested secret-keyed values
            # because ``json.dumps`` flattens the dict context
            # away. The double-redact-after-dumps approach only
            # catches credentialed URLs by regex.
            response = getattr(fr, "response", None) or {}
            # Capture a CONFIRMED first-authoring infra PR for the approval CTA.
            # Name-matched to the two iac-PR authoring tools (never
            # shape-matched: an upgrade PR carries the same pr_number/pr_url
            # but is not an /iac-approvals PR). propose_adoption_tool opens the
            # same approval-class PR via the shared tail, so its result must
            # populate the pointer too (Codex Phase-3 completed-work catch).
            if iac_pr_sink is not None and fr.name in (
                open_infra_pr_tool.__name__,
                propose_adoption_tool.__name__,
            ):
                pointer = iac_pr_pointer(response)
                if pointer is not None:
                    iac_pr_sink.clear()
                    iac_pr_sink.update(pointer)
            safe_response = redact_event(response)
            preview = json.dumps(safe_response, default=str)[:2000]
            result_ok = not (
                isinstance(response, dict)
                and ("error" in response or "errors" in response)
            )
            payload = redact_event({
                "event": "tool_result",
                "trace_id": current_trace_id_or_new(),
                "workload": current_workload(),
                "tool_name": fr.name,
                "result_preview": preview,
                "result_ok": result_ok,
            })
            _log.info("tool_result", extra=payload)
            emitted.append(payload)
            continue
    return emitted


def _emit_llm_usage(event) -> dict | None:
    """Emit one ``llm_usage`` log line if the event carries usage metadata.

    Phase 22: returns the redacted payload it logged (or ``None`` when the
    event has no ``usage_metadata``) so :func:`run_chat_stream` can yield
    the same dict. Logging behavior is unchanged.

    18.B.3: each Gemini call typically surfaces ``usage_metadata`` on
    its final (non-partial) event. Multi-turn runs surface it on each
    turn's final event — so the dashboards graph per-turn cost. The
    redact_event wrapper holds the 19.A.3 redact-at-source invariant
    uniformly; for plain numeric token counts it's a no-op, but a
    future caller stuffing free-form text into the usage payload would
    still be safe.
    """
    usage = getattr(event, "usage_metadata", None)
    if usage is None:
        return None
    payload = redact_event({
        "event": "llm_usage",
        "trace_id": current_trace_id_or_new(),
        "workload": current_workload(),
        "prompt_token_count": getattr(usage, "prompt_token_count", None),
        "candidates_token_count": getattr(usage, "candidates_token_count", None),
        "thoughts_token_count": getattr(usage, "thoughts_token_count", None),
        "total_token_count": getattr(usage, "total_token_count", None),
    })
    _log.info("llm_usage", extra=payload)
    return payload


def _redact_final_response(accepted_text: str) -> tuple[str, str]:
    """Return ``(response_preview, response_kind)`` for ``final_response``.

    For JSON-shaped final responses, parse → recursive
    :func:`agent.secret_guard.redact_event` → re-serialize → truncate.
    This catches NAME-keyed secrets nested anywhere in the structure
    (e.g. ``{"PASSWORD": "abc"}`` or ``{"wrapped": {"DATABASE_URL":
    "postgres://u:p@host/db"}}``) — not just credentialed URLs.

    Falls back to :func:`redact_text` on parse failure (non-JSON text,
    truncated mid-string, or malformed JSON). The fallback path can
    only catch credentialed URLs, not name-keyed secrets — but the
    fallback fires only for genuinely non-JSON content, where
    structural redaction has no schema to walk. The 365-day-durable
    Cloud Logging emit therefore covers BOTH shapes the agent might
    produce.

    Truncation is applied AFTER redaction (redact-then-truncate)
    regardless of which branch fires — preserving the v3 invariant
    that a credentialed URL straddling the 2000-char boundary still
    gets userinfo stripped, never cut mid-segment.

    Returns a 2-tuple so call sites stay symmetric:
    ``response_preview`` is the ≤2000-char string to log;
    ``response_kind`` is ``"json"`` when structural redaction fired,
    ``"text"`` otherwise.
    """
    stripped = accepted_text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(accepted_text)
            safe = redact_event(parsed)
            # ``default=str`` keeps the dump total: a future schema
            # that nests datetimes / UUIDs / Path objects shouldn't
            # crash the log emit path. The structural redaction has
            # already replaced any secret-keyed values with the
            # ``<redacted>`` sentinel, so ``default=str`` only acts
            # on benign non-secret types.
            return (json.dumps(safe, default=str)[:2000], "json")
        except (json.JSONDecodeError, ValueError):
            # Looked like JSON (leading "{" / "[") but didn't parse —
            # likely a truncated stream or a malformed emit. Fall
            # through to the text path, which still strips
            # credentialed URLs via the regex.
            pass
    return ((redact_text(accepted_text) or "")[:2000], "text")


def _emit_final_response(text: str) -> dict:
    """Redact + log + return the ``final_response`` payload.

    Single source of the ``final_response`` emit so :func:`run_chat_stream`
    and the D5 fan-out orchestrator (``agent.fanout.run_provision_fanout_stream``)
    emit the operator's natural-language final IDENTICALLY: same redaction
    (:func:`_redact_final_response` + the outer :func:`redact_event`), same
    trace/workload tagging, and the same 365-day-durable ``_log.info`` emit.
    Returns the redacted payload (so the streaming caller can yield a
    seq-augmented copy) — it does NOT add the SSE ordering metadata.
    """
    response_preview, response_kind = _redact_final_response(text)
    fr_payload = redact_event({
        "event": "final_response",
        "trace_id": current_trace_id_or_new(),
        "workload": current_workload(),
        "response_preview": response_preview,
        "response_kind": response_kind,
    })
    _log.info("final_response", extra=fr_payload)
    return fr_payload


async def run_agent(
    user_msg: str, *, workload: str = "drift", autonomy_mode: str
) -> DecisionProposal:
    """Run the ADK agent against `user_msg` and parse the final response.

    Builds a fresh `InMemorySessionService` per call — DriftScribe is
    stateless across recheck invocations (idempotency lives at the
    StateStore layer, not in agent memory).

    ``workload`` selects the workload-scoped agent. Defaults to ``"drift"``
    for backward compatibility with pre-17.A.3 callers; new callers pass
    it explicitly via :func:`agent.main._run_adk_agent`. ``autonomy_mode``
    is a REQUIRED keyword arg — forwarded to :func:`build_agent` so the dial
    filters the tool set at Layer 0.
    """
    resolution = load_workload(workload)
    agent = build_agent(resolution, autonomy_mode=autonomy_mode)
    session_service = InMemorySessionService()
    session_id = str(uuid.uuid4())
    await session_service.create_session(
        app_name="driftscribe",
        user_id="driftscribe-runtime",
        session_id=session_id,
    )
    runner = Runner(
        agent=agent,
        app_name="driftscribe",
        session_service=session_service,
    )
    msg = types.Content(role="user", parts=[types.Part(text=user_msg)])

    final_text: str | None = None
    # 19.A.3: ``final_response_logged`` guards against a malformed ADK
    # runner that yields more than one ``is_final_response()`` event —
    # the transparency-UI completion gate relies on exactly-one emit.
    final_response_logged = False
    async for event in runner.run_async(
        user_id="driftscribe-runtime",
        session_id=session_id,
        new_message=msg,
    ):
        # 18.B.2: emit structured logs for thought summaries + tool calls.
        # Same dedup gate as run_chat — partial events carry incomplete
        # thought chunks; we want one log line per merged summary. The
        # actual emit shape lives in :func:`_emit_event_logs` and is
        # shared with :func:`run_chat`.
        if event.content and event.content.parts and getattr(event, "partial", None) is not True:
            _emit_event_logs(event)
        if event.is_final_response() and event.content and event.content.parts:
            parts_text = [
                part.text
                for part in event.content.parts
                # 18.B.1: skip thought parts. With include_thoughts=True the
                # final event interleaves a thought-summary part alongside
                # the response JSON; collecting both corrupts the parse.
                if getattr(part, "text", None) and not getattr(part, "thought", False)
            ]
            if parts_text:
                accepted_text = "".join(parts_text)
                # 19.A.3: emit ``final_response`` exactly once, gated on
                # non-empty accepted text. The flag prevents a second
                # emit if a malformed runner yields multiple final
                # events; the ``strip()`` precondition guards against
                # the v2 bug of emitting ``response_preview=""`` on the
                # no-text edge case (where the loop raises immediately
                # after). ``.strip()`` also keeps a whitespace-only
                # final-event from reaching the parse path below — the
                # `not final_text` guard alone would let `"\n  \t"`
                # through to ``_parse_response`` and surface as a
                # confusing "did not contain a JSON object" error
                # instead of the documented "no final response".
                if accepted_text.strip():
                    final_text = accepted_text
                    if not final_response_logged:
                        # Redact BEFORE truncating: if a credentialed
                        # URL straddles the 2000-char boundary,
                        # truncating first could cut the userinfo
                        # mid-segment and leak a partial credential
                        # (the regex wouldn't match anymore). The
                        # helper also handles the structured-JSON
                        # path — name-keyed nested secrets
                        # (``{"PASSWORD": ...}``) get masked via
                        # :func:`redact_event` before serialize, so
                        # non-URL secrets don't leak into the durable
                        # 365-day Cloud Logging preview. The outer
                        # ``redact_event(extra)`` remains as defense
                        # in depth.
                        response_preview, response_kind = (
                            _redact_final_response(accepted_text)
                        )
                        _log.info(
                            "final_response",
                            extra=redact_event({
                                "event": "final_response",
                                "trace_id": current_trace_id_or_new(),
                                "workload": current_workload(),
                                "response_preview": response_preview,
                                "response_kind": response_kind,
                            }),
                        )
                        final_response_logged = True
        # 18.B.3: emit one log line per LLM call's usage payload so
        # post-deploy dashboards can graph thoughts_token_count vs the
        # pre-Phase-18 baseline. Each Gemini call typically surfaces
        # usage_metadata on its final (non-partial) event. Shared with
        # :func:`run_chat` via :func:`_emit_llm_usage`.
        _emit_llm_usage(event)

    if not final_text:
        raise RuntimeError("ADK agent produced no final response")
    return _parse_response(final_text)


async def run_chat_stream(
    prompt: str,
    session_id: str | None = None,
    *,
    workload: str = "drift",
    autonomy_mode: str,
):
    """Core streaming generator for the chat agent.

    Yields, in the SAME order the events are logged today:

      {"type": "event",  "event": <redacted dict + seq/insert_id/timestamp>}
      ...and finally...
      {"type": "result", "reply": str, "tool_calls": list, "session_id": str}

    Raises ``RuntimeError`` on an empty reply — identical to
    :func:`run_chat`, which is now a thin drain of this generator (so the
    JSON and SSE paths share one implementation).

    Cloud Logging emission is unchanged: :func:`_emit_event_logs` /
    :func:`_emit_llm_usage` and the inline ``final_response`` emit still
    log the redacted payload; this generator yields a COPY of that same
    redacted dict augmented with synthetic ``seq``/``insert_id``/
    ``timestamp`` fields (Cloud Logging supplies those for the ``/trace``
    polling path; SSE has to synthesize them so ``renderTimeline`` keeps
    stable expansion keys + timestamps). The yielded view is therefore
    never less-redacted than the durable log.
    """
    resolution = load_workload(workload)
    agent = build_chat_agent(resolution, autonomy_mode=autonomy_mode)
    session_service = InMemorySessionService()
    sid = session_id or str(uuid.uuid4())
    await session_service.create_session(
        app_name="driftscribe",
        user_id="driftscribe-runtime",
        session_id=sid,
    )
    runner = Runner(
        agent=agent,
        app_name="driftscribe",
        session_service=session_service,
    )
    msg = types.Content(role="user", parts=[types.Part(text=prompt)])

    reply_chunks: list[str] = []
    tool_calls: list[str] = []
    # Captures a CONFIRMED first-authoring infra PR (open_infra_pr) for the SPA
    # approval CTA; stays empty for every other run (see _emit_event_logs).
    iac_pr: dict = {}
    final_response_logged = False
    seq = 0

    def _stream(payload: dict) -> dict:
        # Augment the already-redacted, already-logged payload with
        # SSE-only ordering metadata. Shallow copy so the durable log
        # copy (already emitted) is untouched.
        nonlocal seq
        seq += 1
        return {
            **payload,
            "seq": seq,
            "insert_id": f"stream-{seq}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    with autonomy_mode_scope(autonomy_mode):
        async for event in runner.run_async(
            user_id="driftscribe-runtime",
            session_id=sid,
            new_message=msg,
        ):
            # Same partial-event dedup gate as run_chat (18.B.2): only merged
            # non-partial events are eligible to log/stream.
            if event.content and event.content.parts and getattr(event, "partial", None) is not True:
                for payload in _emit_event_logs(
                    event, tool_calls=tool_calls, iac_pr_sink=iac_pr
                ):
                    yield {"type": "event", "event": _stream(payload)}
            # Collect + emit the final natural-language response. This is the
            # SAME emit that lived in run_chat pre-Phase-22 — moved here so the
            # drain path stays byte-identical. run_agent keeps its own emit.
            if event.is_final_response() and event.content and event.content.parts:
                for part in event.content.parts:
                    if getattr(part, "thought", False):
                        continue
                    if getattr(part, "text", None):
                        reply_chunks.append(part.text)
                accepted_text = "".join(reply_chunks)
                if accepted_text.strip() and not final_response_logged:
                    fr_payload = _emit_final_response(accepted_text)
                    final_response_logged = True
                    yield {"type": "event", "event": _stream(fr_payload)}
            usage_payload = _emit_llm_usage(event)
            if usage_payload is not None:
                yield {"type": "event", "event": _stream(usage_payload)}

    reply = "".join(reply_chunks).strip()
    if not reply:
        # Surface as RuntimeError so /chat's outer try/except maps to 502.
        raise RuntimeError("ADK chat agent produced no final response")
    yield {
        "type": "result",
        "reply": reply,
        "tool_calls": tool_calls,
        "session_id": sid,
        # Only present when this run opened a confirmed infra PR — the SPA reads
        # it to render a clickable first-authoring "Review & approve" CTA.
        **({"iac_pr": dict(iac_pr)} if iac_pr else {}),
    }


async def run_chat(
    prompt: str,
    session_id: str | None = None,
    *,
    workload: str = "drift",
    autonomy_mode: str,
) -> dict:
    """Run the free-form chat agent against `prompt`.

    Returns ``{"reply": <text>, "tool_calls": [<name>, ...]}``. The
    ``tool_calls`` list is a flat record of which tools the LLM picked,
    in order — useful for the operator to see "what did the agent
    actually do" without reading the full ADK event stream.

    Sessions are in-memory per call. Cross-call agent memory is out of
    scope for Phase 11.7 — see ``docs/architecture/multi-agent-design.md``
    §"session memory". The ``session_id`` parameter is accepted for
    forward compatibility (and so the /chat schema doesn't break when
    we eventually add it) but is currently used only as a label for
    the in-memory session.

    ``workload`` selects the workload-scoped agent — drift today,
    upgrade once 17.E wires it. Defaults to ``"drift"`` for backward
    compatibility with pre-17.A.3 callers.

    Phase 22: this is now a thin drain of :func:`run_chat_stream` — the
    loop body (event logging, ``final_response`` emit, the
    ``final_response_logged`` guard, usage emission, and the empty-reply
    ``RuntimeError``) all live in the generator. Draining it here keeps
    the JSON contract byte-identical while giving the SSE path the same
    single source of truth.
    """
    async for item in run_chat_stream(
        prompt, session_id=session_id, workload=workload,
        autonomy_mode=autonomy_mode,
    ):
        if item["type"] == "result":
            return {
                "reply": item["reply"],
                "tool_calls": item["tool_calls"],
                "session_id": item["session_id"],
            }
    # run_chat_stream always ends in a "result" item or raises; this
    # guards a malformed generator that exhausts without either.
    raise RuntimeError("ADK chat agent produced no final response")
