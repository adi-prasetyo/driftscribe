"""Unit tests for the ADK agent's response-parsing seam.

`run_agent` itself needs a live Gemini call to test end-to-end, but the
parsing logic is a pure function on text. These tests pin the shapes we
accept from the model (raw JSON, fenced JSON, JSON with leading prose) and
the failure modes we surface distinctly to the integration layer.
"""

import json

import pytest
from pydantic import ValidationError

from agent.adk_agent import _parse_response
from agent.models import DecisionAction, DecisionProposal

# Minimal valid `DecisionProposal` payload reused across happy-path tests.
_VALID_PAYLOAD = {
    "action": "drift_issue",
    "env_diffs": [
        {
            "name": "PAYMENT_MODE",
            "expected": "mock",
            "live": "live",
            "contract_status": "present_disallow_manual",
            "debug_config_value": None,
            "recent_pr_match": None,
        }
    ],
    "target_docs_file": None,
    "target_docs_section": None,
    "rationale": "PAYMENT_MODE drifted from mock to live; manual change disallowed.",
    "confidence": 0.92,
    "requires_human_review": True,
}


def test_parses_raw_json():
    text = json.dumps(_VALID_PAYLOAD)
    proposal = _parse_response(text)
    assert isinstance(proposal, DecisionProposal)
    assert proposal.action is DecisionAction.DRIFT_ISSUE


def test_parses_json_inside_code_fence():
    # Model often wraps structured output in a ```json ... ``` fence even when
    # told not to. The greedy `\{.*\}` regex strips fence markers naturally.
    text = "```json\n" + json.dumps(_VALID_PAYLOAD) + "\n```"
    proposal = _parse_response(text)
    assert proposal.action is DecisionAction.DRIFT_ISSUE
    assert proposal.env_diffs[0].name == "PAYMENT_MODE"


def test_parses_json_with_leading_prose():
    text = "Sure, here's the decision:\n" + json.dumps(_VALID_PAYLOAD)
    proposal = _parse_response(text)
    assert proposal.action is DecisionAction.DRIFT_ISSUE
    assert proposal.confidence == pytest.approx(0.92)


def test_unparseable_response_raises_runtime_error():
    # No `{...}` substring at all — distinct failure mode from "JSON but wrong shape".
    with pytest.raises(RuntimeError, match="did not contain a JSON object"):
        _parse_response("I cannot help with that request.")


def test_empty_response_raises_runtime_error():
    with pytest.raises(RuntimeError, match="no final response"):
        _parse_response("")


def test_malformed_json_raises_json_decode_error():
    # JSON-shaped substring exists (matching `\{.*\}`), but it's syntactically
    # broken — unquoted key + trailing comma. Distinct failure mode from "no
    # JSON at all" (RuntimeError) and from "well-formed JSON but bad schema"
    # (ValidationError) — the integration layer can log all three differently.
    with pytest.raises(json.JSONDecodeError):
        _parse_response('Here is the result: {action: "drift_issue",}')


def test_valid_json_but_bad_schema_raises_validation_error():
    # Missing required `action`. Distinguishable from "no JSON" so the
    # integration layer can log "LLM hallucinated bad schema" separately.
    bad = {"env_diffs": [], "rationale": "...", "confidence": 0.1}
    with pytest.raises(ValidationError):
        _parse_response(json.dumps(bad))


def test_action_string_coerced_to_decision_action_enum():
    # `_perform_action` and `_render_for` branch on `proposal.action == DecisionAction.X`,
    # so the enum coercion has to happen in `model_validate` — pin it explicitly.
    text = json.dumps(_VALID_PAYLOAD)
    proposal = _parse_response(text)
    assert proposal.action == DecisionAction.DRIFT_ISSUE
    assert proposal.action.value == "drift_issue"
