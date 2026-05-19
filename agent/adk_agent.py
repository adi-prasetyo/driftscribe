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
# 17.A.3 will introduce a per-request workload parameter on /chat and
# /recheck; until then the agent factory hardcodes ``"drift"``.
#
# ``SYSTEM_PROMPT_CHAT`` (below) remains inline pending 17.A.3. Moving
# it now would require committing to a ``system_prompt_chat_file:``
# field in the :class:`~agent.workloads.WorkloadSpec` YAML schema
# before 17.A.3 has decided how per-workload chat-mode prompts work —
# or whether a chat prompt is even a per-workload concern vs. a
# coordinator-wide one. 17.A.3 owns the routing design and will make
# the final call; locking the schema here would force a follow-up
# migration if that call goes the other way.
# TODO(17.A.3): resolve.


def _drift_system_prompt() -> str:
    """Return the drift workload's system prompt.

    Lazy because the underlying :func:`load_workload` resolves worker URL
    env vars; tests that set those via ``monkeypatch.setenv`` need the
    resolution to happen *after* the patch applies. The
    ``WorkloadResolution`` itself is memoized inside ``load_workload``,
    so per-call cost is a dict lookup once the first call has succeeded.
    """
    from agent.workloads import load_workload
    return load_workload("drift").system_prompt


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


def build_agent() -> Agent:
    """Construct the /recheck-flavored ADK Agent with the full coordinator
    tool set wired in. Uses the drift workload's structured-JSON system
    prompt loaded from ``workloads/drift/system_prompt.md``."""
    return Agent(
        name="driftscribe",
        model="gemini-2.5-flash",
        instruction=_drift_system_prompt(),
        tools=COORDINATOR_TOOLS,
    )


def build_chat_agent() -> Agent:
    """Construct the /chat-flavored ADK Agent — same tool set, different
    system prompt that allows free-form natural-language responses and
    tool chaining within a single turn."""
    return Agent(
        name="driftscribe_chat",
        model="gemini-2.5-flash",
        instruction=SYSTEM_PROMPT_CHAT,
        tools=COORDINATOR_TOOLS,
    )


async def run_agent(user_msg: str) -> DecisionProposal:
    """Run the ADK agent against `user_msg` and parse the final response.

    Builds a fresh `InMemorySessionService` per call — DriftScribe is
    stateless across recheck invocations (idempotency lives at the
    StateStore layer, not in agent memory).
    """
    agent = build_agent()
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


async def run_chat(prompt: str, session_id: str | None = None) -> dict:
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
    """
    agent = build_chat_agent()
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
