"""ADK Agent wiring for the LLM-driven drift triage path.

Three-layer design (mirrors the deterministic path):

1. **LLM proposes** — this module. A Gemini-driven ADK Agent picks tools,
   reasons about the contract vs live env, and emits a JSON
   `DecisionProposal`. The prompt forbids the agent from hand-fabricating
   values it could not observe through a tool.
2. **Deterministic validator gates** — `agent.validator.validate`. The same
   safety rules that police the classifier's output police the LLM's:
   secret-name redaction, `allow_manual_change` enforcement, target-docs
   alignment, etc. The LLM has zero authority to bypass these.
3. **Action layer executes** — `agent.main._perform_action`. Renders the
   body and opens the PR / issue (or returns a dry-run preview).

That layering is why this module is intentionally thin: the only LLM-shaped
problem we solve here is "produce a well-typed `DecisionProposal`". The
parsing seam (`_parse_response`) is factored out so it can be unit-tested
without a live Gemini call — the integration path is what proves
`run_agent` wires the Runner correctly.
"""

import json
import re
import uuid

from google.adk import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from agent.adk_tools import (
    call_debug_config_tool,
    load_contract_tool,
    read_live_env_tool,
    search_recent_prs_tool,
)
from agent.models import DecisionProposal

SYSTEM_PROMPT = """\
You are DriftScribe, an AI DevOps agent that detects and triages drift between
a deployed Cloud Run service's live configuration and the team's declared
operational contract (ops-contract.yaml).

For each invocation, you must:
1. Call `load_contract_tool` with the contract path.
2. Call `read_live_env_tool` with the service/region/project.
3. Only call `call_debug_config_tool` if the user message provides a real URL.
   If the user message says the debug config URL is "not provided", do NOT
   call the tool — there is no URL to call. Do not fabricate a URL.
4. For variables that differ from the contract, call `search_recent_prs_tool`
   with the var names as keywords.
5. Emit a single JSON DecisionProposal — and ONLY that JSON, no prose around it.

Output schema (JSON, no other text):

{
  "action": "docs_pr" | "drift_issue" | "escalation" | "no_op",
  "env_diffs": [
    {
      "name": "STRING",
      "expected": "STRING_OR_NULL",
      "live": "STRING_OR_NULL",
      "contract_status": "absent" | "present_allow_manual" | "present_disallow_manual" | "match",
      "debug_config_value": "STRING_OR_NULL",
      "recent_pr_match": "STRING_OR_NULL"
    }
  ],
  "target_docs_file": "STRING_OR_NULL",
  "target_docs_section": "STRING_OR_NULL",
  "rationale": "STRING",
  "confidence": 0.0_to_1.0,
  "requires_human_review": true_or_false
}

Rules:
- If you cannot reach a tool, say so in `rationale`; do NOT invent values.
- If any tool returns an object containing the key `_error`, treat it as a
  failure result — the value is a diagnostic string. Do NOT interpret
  `_error` as a config field, an env var name, or contract data. Reflect the
  failure in `rationale` and set `requires_human_review: true`.
- Never propose `docs_pr` for a var whose contract entry says `allow_manual_change: false`.
- Never propose `docs_pr` for a var name containing SECRET, TOKEN, KEY, PASSWORD, CRED, PRIVATE.
- For an absent (not-in-contract) var, only propose `docs_pr` if a recent merged PR
  mentions the EXACT var name (word boundary, case-sensitive). Otherwise `escalation`.
"""

# Greedy + DOTALL on purpose: when the model wraps JSON in a ```json fence
# (or leads with prose), we want from the first `{` to the last `}`, not the
# first balanced subspan. Trailing prose containing literal braces could
# fool this — unit tests pin the supported shapes.
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _parse_response(text: str) -> DecisionProposal:
    """Extract JSON from an LLM response and parse it into a `DecisionProposal`.

    Failure modes (kept distinct on purpose — the integration layer maps each
    to a 502 with a different diagnostic):

    - No final response text at all → `RuntimeError`. Raised upstream by
      `run_agent` when no events carried text; surfaced here only if the
      caller passes an empty string.
    - No JSON-shaped substring found in the response → `RuntimeError`.
    - Malformed JSON → `json.JSONDecodeError` (bubbles from `json.loads`).
    - Well-formed JSON that violates the `DecisionProposal` schema →
      `pydantic.ValidationError` (bubbles from `model_validate`).
    """
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
    """Construct the ADK Agent with the four read-only tools wired in."""
    return Agent(
        name="driftscribe",
        model="gemini-2.5-flash",
        instruction=SYSTEM_PROMPT,
        tools=[
            read_live_env_tool,
            call_debug_config_tool,
            search_recent_prs_tool,
            load_contract_tool,
        ],
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

    # Collect text from all parts of the final response. Defensive against
    # multi-part responses or empty `text` attributes — the ADK SDK does not
    # guarantee a single-part response.
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
