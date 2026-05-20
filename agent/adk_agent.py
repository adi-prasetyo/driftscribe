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

Worker-delegating tools (4 drift + 2 upgrade = 6):
- ``read_live_env_tool`` → Reader Agent ``/read``
- ``propose_rollback_tool`` → Rollback Agent ``/propose`` (HITL-gated)
- ``patch_docs_tool`` → Docs Agent ``/patch``
- ``notify_tool`` → Notifier Agent ``/notify``
- ``upgrade_read_dependencies_tool`` → Upgrade Reader Agent ``/read``
  (Phase 17.C.4). Authority-clean: no LLM-controllable args.
- ``upgrade_propose_pr_tool`` → Upgrade Docs Agent ``/patch``
  (Phase 17.C.4). Authority-clean: LLM picks decision content only;
  repo / lockfile path / branch / base / title derived server-side.

Coordinator-internal read-only tools (2):
- ``search_recent_prs_tool`` (read-only GitHub via coordinator PAT)
- ``load_contract_tool`` (reads baked-in ops contract)

Developer Knowledge MCP wrappers (2, Phase 17.B.3):
- ``search_developer_docs`` → Developer Knowledge MCP ``search_documents``
- ``retrieve_developer_doc`` → Developer Knowledge MCP ``get_documents``

That's 10 tools, period (Phase 17.C.4 grew it from 8 → 10). Anything
else the model wants to do is denied by capability — there is no
general "execute shell" or "make HTTP request" surface.

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
import logging
import re
import uuid

from google.adk import Agent
from google.adk.planners.built_in_planner import BuiltInPlanner
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from google.genai.types import ThinkingConfig

from agent.adk_tools import (
    load_contract_tool,
    notify_tool,
    patch_docs_tool,
    propose_rollback_tool,
    read_live_env_tool,
    search_recent_prs_tool,
    upgrade_propose_pr_tool,
    upgrade_read_dependencies_tool,
)
from agent.mcp.developer_knowledge import (
    retrieve_developer_doc,
    search_developer_docs,
)
from agent.models import DecisionProposal
from agent.secret_guard import redact_dict, redact_event, redact_text
from agent.workload_context import current_workload
from agent.workloads import WorkloadResolution, load_workload
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
    For drift the two surfaces are byte-identical (8 callables either
    way post-17.B.3); for upgrade the registry refuses to resolve until
    17.B/17.C flips the reserved ``None`` entries to real callables, so
    passing ``workload.tools.values()`` to ADK can't accidentally hand
    the LLM a partial upgrade surface.

    ADK requires agent names to be valid Python identifiers (letters,
    digits, underscores; no hyphens). The workload name is from the
    closed Literal ``{"drift", "upgrade"}``, both identifier-safe.
    """
    return Agent(
        name=f"driftscribe_{workload.spec.name}",
        model="gemini-2.5-flash",
        instruction=workload.system_prompt,
        tools=list(workload.tools.values()),
        # 18.B.1: surface Gemini 2.5 Flash's thought summaries. The model
        # already spends thinking tokens at default-dynamic budget; this
        # only changes whether the summaries are *returned*.
        planner=BuiltInPlanner(
            thinking_config=ThinkingConfig(include_thoughts=True),
        ),
    )


def build_chat_agent(workload: WorkloadResolution) -> Agent:
    """Construct the /chat-flavored ADK Agent for the given workload.

    Same workload parameter as :func:`build_agent`. The system prompt
    here is :attr:`~agent.workloads.WorkloadResolution.chat_system_prompt`
    — Phase 17.C.4 (Option A from the plan) moved the previously
    coordinator-wide ``SYSTEM_PROMPT_CHAT`` constant into per-workload
    files (``workloads/drift/chat_system_prompt.md`` and
    ``workloads/upgrade/chat_system_prompt.md``) so the upgrade chat
    surface gets upgrade-flavored instructions, not drift's. Workloads
    that want the same prompt on both surfaces leave
    ``chat_system_prompt_file`` unset in YAML — the registry falls back
    to ``system_prompt``. Tool list is per-workload — same Phase 17.A.3
    rationale as :func:`build_agent`.
    """
    return Agent(
        name=f"driftscribe_chat_{workload.spec.name}",
        model="gemini-2.5-flash",
        instruction=workload.chat_system_prompt,
        tools=list(workload.tools.values()),
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


def _emit_event_logs(event, *, tool_calls: list[str] | None = None) -> None:
    """Emit ``llm_thought`` / ``tool_call`` / ``tool_result`` log lines
    for one ADK event's part list.

    Callers must apply the partial-event dedup gate (``event.partial is
    not True``) before invoking this — the helper assumes the event is
    a merged non-partial event whose parts are eligible to log.

    18.B.2: only ``thought`` parts that carry ``text`` emit
    ``llm_thought`` (a thought without text has nothing to log; the
    function_call/function_response checks below still run for parts
    that happen to have both).

    19.A.3: every emit goes through :func:`redact_event` at the
    boundary so the durable Cloud Logging copy never carries
    credentials.
    """
    for part in event.content.parts:
        if getattr(part, "thought", False) and getattr(part, "text", None):
            # A thought part without text has nothing to log; the
            # falls-through path below still handles function_call.
            _log.info(
                "llm_thought",
                extra=redact_event({
                    "event": "llm_thought",
                    "trace_id": current_trace_id_or_new(),
                    "workload": current_workload(),
                    "thought_text": part.text,
                }),
            )
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
            _log.info(
                "tool_call",
                extra=redact_event({
                    "event": "tool_call",
                    "trace_id": current_trace_id_or_new(),
                    "workload": current_workload(),
                    "tool_name": fc.name,
                    "tool_args": redact_dict(args),
                }),
            )
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
            safe_response = redact_event(response)
            preview = json.dumps(safe_response, default=str)[:2000]
            result_ok = not (
                isinstance(response, dict)
                and ("error" in response or "errors" in response)
            )
            _log.info(
                "tool_result",
                extra=redact_event({
                    "event": "tool_result",
                    "trace_id": current_trace_id_or_new(),
                    "workload": current_workload(),
                    "tool_name": fr.name,
                    "result_preview": preview,
                    "result_ok": result_ok,
                }),
            )
            continue


def _emit_llm_usage(event) -> None:
    """Emit one ``llm_usage`` log line if the event carries usage metadata.

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
        return
    _log.info(
        "llm_usage",
        extra=redact_event({
            "event": "llm_usage",
            "trace_id": current_trace_id_or_new(),
            "workload": current_workload(),
            "prompt_token_count": getattr(usage, "prompt_token_count", None),
            "candidates_token_count": getattr(usage, "candidates_token_count", None),
            "thoughts_token_count": getattr(usage, "thoughts_token_count", None),
            "total_token_count": getattr(usage, "total_token_count", None),
        }),
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
                        # Redact BEFORE truncating: if a credentialed URL
                        # straddles the 2000-char boundary, truncating
                        # first could cut the userinfo mid-segment and
                        # leak a partial credential (the regex wouldn't
                        # match anymore). Redact-then-truncate keeps the
                        # invariant; the outer ``redact_event(extra)`` is
                        # defense-in-depth.
                        safe_text = redact_text(accepted_text) or ""
                        response_preview = safe_text[:2000]
                        response_kind = (
                            "json" if accepted_text.lstrip().startswith("{") else "text"
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
    # 19.A.3: ``final_response_logged`` guards against a malformed ADK
    # runner that yields more than one ``is_final_response()`` event —
    # the transparency-UI completion gate relies on exactly-one emit.
    final_response_logged = False
    async for event in runner.run_async(
        user_id="driftscribe-runtime",
        session_id=sid,
        new_message=msg,
    ):
        # 18.B.2: emit structured logs for thought summaries + tool calls.
        # Gate on event.partial is not True to dedup ADK's streaming
        # partials — only the merged non-partial event carries the
        # complete thought summary. function_calls don't arrive as
        # partials in practice, but applying the same guard uniformly
        # keeps the loop shape consistent. ``tool_calls`` is the public-
        # contract list surfaced in the /chat response — the helper
        # appends each ``function_call.name`` to it as a side effect;
        # :func:`run_agent` (which has no such list) passes ``None``.
        if event.content and event.content.parts and getattr(event, "partial", None) is not True:
            _emit_event_logs(event, tool_calls=tool_calls)
        # Collect the final natural-language response.
        if event.is_final_response() and event.content and event.content.parts:
            for part in event.content.parts:
                # 18.B.1: skip thought parts (same rationale as run_agent).
                if getattr(part, "thought", False):
                    continue
                if getattr(part, "text", None):
                    reply_chunks.append(part.text)
            # 19.A.3: emit ``final_response`` exactly once, gated on
            # non-empty accepted text. The flag prevents a second emit
            # if a malformed runner yields multiple final events; the
            # ``strip()`` precondition guards against the v2 bug of
            # emitting ``response_preview=""`` on the no-text edge case
            # (where the loop raises immediately after). The emit lives
            # inside the same ``is_final_response()`` branch so
            # ``reply_chunks`` reflects only the run's accumulated text
            # at this point — the flag ensures we emit only the first
            # time the gate fires.
            accepted_text = "".join(reply_chunks)
            if accepted_text.strip() and not final_response_logged:
                # Redact BEFORE truncating: if a credentialed URL
                # straddles the 2000-char boundary, truncating first
                # could cut the userinfo mid-segment and leak a partial
                # credential (the regex wouldn't match anymore).
                # Redact-then-truncate keeps the invariant; the outer
                # ``redact_event(extra)`` is defense-in-depth.
                safe_text = redact_text(accepted_text) or ""
                response_preview = safe_text[:2000]
                response_kind = (
                    "json" if accepted_text.lstrip().startswith("{") else "text"
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
        # :func:`run_agent` via :func:`_emit_llm_usage`.
        _emit_llm_usage(event)

    reply = "".join(reply_chunks).strip()
    if not reply:
        # Surface as RuntimeError so /chat's outer try/except maps to 502.
        raise RuntimeError("ADK chat agent produced no final response")
    return {
        "reply": reply,
        "tool_calls": tool_calls,
        "session_id": sid,
    }
