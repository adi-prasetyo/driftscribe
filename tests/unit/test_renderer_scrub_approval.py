"""Unit tests for the rollback-approval-link serve-time scrub (hackathon A.2).

* ``scrub_decision_approval(decision)`` — applied to anonymous demo-window
  decision reads (GET /decisions, /trace — Worker-marked) and ALWAYS on the
  unauthenticated GET /runs/{id}. Drops ``approval.approval_url`` (the rail
  must render no dead CTA) and redacts the ``?t=`` token wherever it hides in
  the doc's strings (rendered_body, echoed replies).
* ``redact_approval_tokens_deep(payload)`` — the recursive string walker, also
  applied to /trace event payloads.

Conventions under test mirror scrub_decision_rationale: identity on
no-change, copy-on-change, never mutates the input, non-dict passthrough.
"""
from __future__ import annotations

import copy

from agent.renderer import redact_approval_tokens_deep, scrub_decision_approval

_URL = "https://driftscribe.example.com/approvals/4f1c2d3e-aaaa-bbbb-cccc-1234567890ab?t=tok-SECRET_43chars_AAAAAAAAAAAAAAAAAAAAAAAA"


def _rollback_decision() -> dict:
    return {
        "decision_id": "d-1",
        "action": "rollback",
        "rationale": "drift detected",
        "rendered_body": f"Click to approve:\n\n<{_URL}>\n\nExpires in 15 minutes.",
        "approval": {
            "approval_id": "4f1c2d3e-aaaa-bbbb-cccc-1234567890ab",
            "approval_url": _URL,
            "expires_at": "2026-06-12T12:00:00Z",
        },
    }


# --------------------------------------------------------------------------- #
# scrub_decision_approval
# --------------------------------------------------------------------------- #


def test_drops_approval_url_keeps_siblings():
    out = scrub_decision_approval(_rollback_decision())
    assert "approval_url" not in out["approval"]
    # Non-secret siblings survive — the UI can still show id/expiry.
    assert out["approval"]["approval_id"] == "4f1c2d3e-aaaa-bbbb-cccc-1234567890ab"
    assert out["approval"]["expires_at"] == "2026-06-12T12:00:00Z"


def test_redacts_token_in_rendered_body():
    out = scrub_decision_approval(_rollback_decision())
    assert "tok-SECRET" not in out["rendered_body"]
    # The path stays readable; only the token value is gone.
    assert "/approvals/4f1c2d3e-aaaa-bbbb-cccc-1234567890ab?t=<redacted>" in out["rendered_body"]
    assert "Expires in 15 minutes." in out["rendered_body"]


def test_never_mutates_input():
    d = _rollback_decision()
    snapshot = copy.deepcopy(d)
    scrub_decision_approval(d)
    assert d == snapshot


def test_identity_when_nothing_to_scrub():
    d = {"decision_id": "d-2", "action": "drift_issue", "rationale": "x"}
    assert scrub_decision_approval(d) is d


def test_non_dict_passthrough():
    assert scrub_decision_approval(None) is None
    assert scrub_decision_approval("nope") == "nope"
    assert scrub_decision_approval(42) == 42


def test_no_token_anywhere_in_scrubbed_doc():
    # Belt-and-braces: serialize the whole scrubbed doc and assert the secret
    # is gone from EVERY string, not just the two fields we know about today.
    import json

    out = scrub_decision_approval(_rollback_decision())
    assert "tok-SECRET" not in json.dumps(out, default=str)


# --------------------------------------------------------------------------- #
# redact_approval_tokens_deep
# --------------------------------------------------------------------------- #


def test_deep_redacts_nested_strings():
    payload = {
        "events": [
            {"result_preview": f'{{"approval_url": "{_URL}"}}'},
            {"text": f"please visit {_URL} soon"},
        ]
    }
    out = redact_approval_tokens_deep(payload)
    assert "tok-SECRET" not in str(out)
    assert "?t=<redacted>" in out["events"][0]["result_preview"]
    assert out["events"][1]["text"].endswith("?t=<redacted> soon")


def test_deep_identity_on_no_match():
    payload = {"a": ["no links here", {"b": 1, "c": None, "d": True}]}
    assert redact_approval_tokens_deep(payload) is payload


def test_deep_never_mutates_input():
    payload = {"events": [{"text": f"link {_URL}"}]}
    snapshot = copy.deepcopy(payload)
    redact_approval_tokens_deep(payload)
    assert payload == snapshot


def test_deep_leaves_untokenized_approval_paths_alone():
    # A bare /approvals/{id} link WITHOUT ?t= carries no secret — untouched,
    # and identity is preserved.
    payload = {"text": "see /approvals/4f1c2d3e-aaaa for status"}
    assert redact_approval_tokens_deep(payload) is payload


def test_deep_scalars_pass_through():
    assert redact_approval_tokens_deep(7) == 7
    assert redact_approval_tokens_deep(None) is None
