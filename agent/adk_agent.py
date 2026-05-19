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

Worker-delegating tools (4):
- ``read_live_env_tool`` → Reader Agent ``/read``
- ``propose_rollback_tool`` → Rollback Agent ``/propose`` (HITL-gated)
- ``patch_docs_tool`` → Docs Agent ``/patch``
- ``notify_tool`` → Notifier Agent ``/notify``

Coordinator-internal read-only tools (2):
- ``search_recent_prs_tool`` (read-only GitHub via coordinator PAT)
- ``load_contract_tool`` (reads baked-in ops contract)

That's 6 tools, period. Anything else the model wants to do is denied
by capability — there is no general "execute shell" or "make HTTP
request" surface.

**Per-workload tool inventories (Phase 17.A.4):**
:data:`DRIFT_WORKLOAD_TOOL_NAMES` and :data:`UPGRADE_WORKLOAD_TOOL_NAMES`
mirror each workload YAML's ``enabled_tool_names`` field — the symbolic
filter applied per workload over the global registry. They are distinct
from :data:`COORDINATOR_TOOLS` (the Python-callable registration manifest);
see the block comment around the constants below for the rationale, the
tuple-vs-frozenset choice, and the three-way YAML ⇄ code ⇄ runtime
equality enforced by ``tests/unit/test_coordinator_tool_inventory.py``.
"""

import json
import re
import uuid

from google.adk import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from agent.adk_tools import (
    load_contract_tool,
    notify_tool,
    patch_docs_tool,
    propose_rollback_tool,
    read_live_env_tool,
    search_recent_prs_tool,
)
from agent.models import DecisionProposal
from agent.workloads import WorkloadResolution, load_workload

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
]


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
#   Drift's six callables happen to coincide with the global registration
#   (drift wires every tool the coordinator owns today); upgrade adds
#   four reserved-not-yet symbols that 17.B/17.C will flip from ``None``
#   to real callables.
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
)

UPGRADE_WORKLOAD_TOOL_NAMES: tuple[str, ...] = (
    "upgrade_read_dependencies",
    "upgrade_propose_pr",
    "notify",
    "search_recent_prs",
    "search_developer_docs",
    "retrieve_developer_doc",
    "get_session_state",
    "set_session_state",
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
# ``SYSTEM_PROMPT_CHAT`` (below) is intentionally NOT moved into the
# workload manifest in 17.A.3. Rationale:
#
# - The /chat free-form prompt is currently coordinator-wide, not
#   workload-specific (it explains the four worker tools, none of which
#   are upgrade-flavored yet).
# - Moving it into ``workloads/drift/`` now would force a parallel
#   move into ``workloads/upgrade/`` before 17.C had decided whether
#   /chat is even a meaningful surface for upgrade.
# - Schema commitment punted: no ``chat_system_prompt_file`` field
#   added to :class:`~agent.workloads.WorkloadSpec` yet.
#
# Follow-up: when 17.C introduces an upgrade-flavored chat prompt,
# either (a) add ``chat_system_prompt_file: str | None`` to the
# WorkloadSpec and migrate both drift's and upgrade's prompts, or
# (b) keep SYSTEM_PROMPT_CHAT coordinator-wide if upgrade's chat mode
# turns out to want the same wording. The decision is small enough to
# defer.
# TODO(17.C): revisit when upgrade's chat behavior is concrete.


# --------------------------------------------------------------------------- #
# Free-form chat agent (/chat)
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT_CHAT = """\
You are DriftScribe's coordinator agent. Your job is to help an on-call
operator detect, triage, and respond to drift between a Cloud Run service's
live state and its declared operations contract.

CRITICAL constraint: You cannot mutate any system directly. You can ONLY
call worker tools. Each tool is delegated to a separate worker service with
its own scoped IAM and payload-intent policy. You are deliberately built
without direct GCP or GitHub mutation access.

Tools available to you:
- read_live_env_tool() — ask the Reader Agent for the live env + revision
- propose_rollback_tool(target_revision, reason) — ask Rollback Agent to
  create an approval. Rollbacks REQUIRE human approval; you do NOT execute
  them. Return the approval URL to the operator and explain that they must
  click it and press Approve.
- patch_docs_tool(file_path, new_content, title, body) — ask Docs Agent to
  open a docs PR. Path must be under demo/docs/*.md.
- notify_tool(channel, severity, body) — ask Notifier Agent to post a
  webhook. Channel: info|alert|approval. Severity: low|medium|high|critical.
- search_recent_prs_tool(keywords, days=7) — read-only PR history
- load_contract_tool() — read the baked-in ops contract

Rules:
- If asked to do something destructive (rollback, redeploy, delete), use
  propose_rollback_tool and explain that human approval is required.
  NEVER attempt to bypass the approval gate.
- If a tool returns an error, surface it to the operator clearly. Do NOT
  pretend the action succeeded.
- Be concise. The operator is on-call and wants the answer, not prose.
"""

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


def build_agent(workload: WorkloadResolution) -> Agent:
    """Construct the /recheck-flavored ADK Agent for the given workload.

    Takes an already-loaded :class:`~agent.workloads.WorkloadResolution`
    so the caller (``agent.main``) controls workload selection per
    request. The factory itself is a pure function over the resolution —
    no env reads, no module-level state — so the same resolution always
    yields an agent with the same prompt and tools.

    Tool set: ``workload.tools.values()`` — the workload-specific list
    resolved by :func:`agent.workloads.load_workload`. Phase 17.A.3
    (Codex review): this used to be :data:`COORDINATOR_TOOLS` (the
    union surface). Switching to the per-workload filtered list makes
    the capability-bound invariant "the LLM is never even shown a
    cross-workload tool" hold today — not "once upgrade tools ship".
    For drift the two surfaces are byte-identical (6 callables either
    way); for upgrade the registry refuses to resolve until 17.B/17.C
    flips the reserved ``None`` entries to real callables, so passing
    ``workload.tools.values()`` to ADK can't accidentally hand the
    LLM a partial upgrade surface.

    ADK requires agent names to be valid Python identifiers (letters,
    digits, underscores; no hyphens). The workload name is from the
    closed Literal ``{"drift", "upgrade"}``, both identifier-safe.
    """
    return Agent(
        name=f"driftscribe_{workload.spec.name}",
        model="gemini-2.5-flash",
        instruction=workload.system_prompt,
        tools=list(workload.tools.values()),
    )


def build_chat_agent(workload: WorkloadResolution) -> Agent:
    """Construct the /chat-flavored ADK Agent for the given workload.

    Same workload parameter as :func:`build_agent`. The system prompt
    here is the coordinator-wide :data:`SYSTEM_PROMPT_CHAT` (see the
    block-comment above for the rationale on NOT moving it into the
    workload manifest in 17.A.3). Tool list is per-workload — same
    Phase 17.A.3 rationale as :func:`build_agent`.
    """
    return Agent(
        name=f"driftscribe_chat_{workload.spec.name}",
        model="gemini-2.5-flash",
        instruction=SYSTEM_PROMPT_CHAT,
        tools=list(workload.tools.values()),
    )


async def run_agent(
    user_msg: str, *, workload: str = "drift"
) -> DecisionProposal:
    """Run the ADK agent against `user_msg` and parse the final response.

    Builds a fresh `InMemorySessionService` per call — DriftScribe is
    stateless across recheck invocations (idempotency lives at the
    StateStore layer, not in agent memory).

    ``workload`` selects the workload-scoped agent. Defaults to ``"drift"``
    for backward compatibility with pre-17.A.3 callers; new callers pass
    it explicitly via :func:`agent.main._run_adk_agent`.
    """
    resolution = load_workload(workload)
    agent = build_agent(resolution)
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
    async for event in runner.run_async(
        user_id="driftscribe-runtime",
        session_id=session_id,
        new_message=msg,
    ):
        if event.is_final_response() and event.content and event.content.parts:
            parts_text = [
                part.text for part in event.content.parts if getattr(part, "text", None)
            ]
            if parts_text:
                final_text = "".join(parts_text)

    if not final_text:
        raise RuntimeError("ADK agent produced no final response")
    return _parse_response(final_text)


async def run_chat(
    prompt: str,
    session_id: str | None = None,
    *,
    workload: str = "drift",
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
    """
    resolution = load_workload(workload)
    agent = build_chat_agent(resolution)
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
    async for event in runner.run_async(
        user_id="driftscribe-runtime",
        session_id=sid,
        new_message=msg,
    ):
        # Record any function (tool) calls the agent made. ADK exposes
        # these on event.content.parts as function_call entries.
        if event.content and event.content.parts:
            for part in event.content.parts:
                fc = getattr(part, "function_call", None)
                if fc and getattr(fc, "name", None):
                    tool_calls.append(fc.name)
        # Collect the final natural-language response.
        if event.is_final_response() and event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    reply_chunks.append(part.text)

    reply = "".join(reply_chunks).strip()
    if not reply:
        # Surface as RuntimeError so /chat's outer try/except maps to 502.
        raise RuntimeError("ADK chat agent produced no final response")
    return {
        "reply": reply,
        "tool_calls": tool_calls,
        "session_id": sid,
    }
