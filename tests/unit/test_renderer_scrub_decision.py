"""Serve-time rationale-scrub helpers (PR 2 — the backend rationale scrub).

Pins ``_coerce_env_diffs`` (stored ``diffs[]`` dicts → ``EnvDiff`` objects) and
the two public entry points layered over the existing, tested
``_scrub_secret_values_from_rationale``:

* ``scrub_decision_rationale(decision)`` — applied at every decision serve/
  return boundary (GET /trace, /decisions, /runs; POST /recheck, /eventarc).
  Copy-on-change, identity-when-unchanged, never mutates, never raises.
* ``scrub_rationale_text(rationale, env_diffs)`` — for the rollback worker
  ``reason`` boundary, where we hold typed ``EnvDiff`` objects.

The decision doc is otherwise returned verbatim (unredacted by design): only the
free-text ``rationale`` is scrubbed — ``rendered_body`` (already scrubbed at
persist) and ``diffs[]`` are left untouched.
"""

import pytest

from agent.models import ContractStatus, DecisionAction, DecisionProposal, EnvDiff
from agent.renderer import (
    _coerce_env_diffs,
    scrub_decision_rationale,
    scrub_rationale_text,
)


# --------------------------------------------------------------------------- #
# _coerce_env_diffs — stored dicts → EnvDiff, defensively
# --------------------------------------------------------------------------- #


def test_coerce_well_formed_diffs():
    raw = [{"name": "API_TOKEN", "expected": "old", "live": "new",
            "contract_status": "present_disallow_manual",
            "debug_config_value": None, "recent_pr_match": None}]
    out = _coerce_env_diffs(raw)
    assert len(out) == 1 and isinstance(out[0], EnvDiff)
    assert (out[0].name, out[0].expected, out[0].live) == ("API_TOKEN", "old", "new")


def test_coerce_tolerates_missing_or_invalid_contract_status():
    raw = [{"name": "API_TOKEN", "expected": "old", "live": "new"},        # no status
           {"name": "X", "live": "y", "contract_status": "not-a-status"}]  # bad status
    out = _coerce_env_diffs(raw)
    assert len(out) == 2
    assert all(isinstance(d.contract_status, ContractStatus) for d in out)


def test_coerce_skips_non_dict_entries_and_non_list_input():
    assert _coerce_env_diffs("nope") == []
    assert _coerce_env_diffs(None) == []
    out = _coerce_env_diffs([{"name": "A", "live": "1"}, "garbage", 42, None])
    assert [d.name for d in out] == ["A"]


def test_coerce_defaults_nameless_diff_to_empty_name():
    # A diff with no string name still scrubs a credentialed-URL value:
    # should_redact("", url) is True via value_looks_credentialed.
    out = _coerce_env_diffs([{"live": "https://u:p@h/x"}])
    assert len(out) == 1 and out[0].name == "" and out[0].live == "https://u:p@h/x"


def test_coerce_coerces_non_string_value_fields_to_none():
    out = _coerce_env_diffs([{"name": "A", "expected": 123, "live": ["x"]}])
    assert out[0].expected is None and out[0].live is None


# --------------------------------------------------------------------------- #
# scrub_decision_rationale — the serve-time decision-doc helper
# --------------------------------------------------------------------------- #


def _doc(rationale, diffs):
    return {"action": "drift_issue", "trace_id": "a" * 32, "decision_id": "d1",
            "rationale": rationale, "rendered_body": "BODY", "diffs": diffs}


def test_scrub_redacts_secret_by_name_value_in_rationale():
    doc = _doc("API_TOKEN changed from sk-OLD-123456 to sk-NEW-789012.",
               [{"name": "API_TOKEN", "expected": "sk-OLD-123456", "live": "sk-NEW-789012",
                 "contract_status": "present_disallow_manual"}])
    out = scrub_decision_rationale(doc)
    assert "sk-OLD-123456" not in out["rationale"]
    assert "sk-NEW-789012" not in out["rationale"]
    assert "API_TOKEN" in out["rationale"]   # var name survives
    assert out["rendered_body"] == "BODY"    # rendered_body untouched
    assert out["diffs"] == doc["diffs"]      # diffs left raw (narrow scope)


def test_scrub_redacts_credentialed_url_value_with_nonsecret_name():
    doc = _doc("ENDPOINT now points at https://admin:hunter2@svc.internal/api.",
               [{"name": "ENDPOINT", "expected": None,
                 "live": "https://admin:hunter2@svc.internal/api", "contract_status": "absent"}])
    out = scrub_decision_rationale(doc)
    assert "hunter2" not in out["rationale"]
    assert "https://admin:hunter2@svc.internal/api" not in out["rationale"]


def test_scrub_redacts_recent_pr_match_and_debug_config_value():
    # The reused scrubber also covers recent_pr_match (secret-named var) and
    # debug_config_value — pin that the serve-time path keeps that coverage.
    doc = _doc("see PR https://github.com/x/x/pull/9?leak=zzzz9999 ; cfg was qqqq8888",
               [{"name": "OAUTH_KEY", "live": "zzzz9999",
                 "recent_pr_match": "https://github.com/x/x/pull/9?leak=zzzz9999",
                 "debug_config_value": "qqqq8888", "contract_status": "absent"}])
    out = scrub_decision_rationale(doc)
    assert "zzzz9999" not in out["rationale"]
    assert "qqqq8888" not in out["rationale"]


def test_scrub_leaves_benign_rationale_unchanged_by_identity():
    doc = _doc("Three variables drifted; secrets are redacted in the table.",
               [{"name": "LOG_LEVEL", "expected": "info", "live": "debug",
                 "contract_status": "present_allow_manual"}])
    assert scrub_decision_rationale(doc) is doc   # no needless copy


def test_scrub_is_idempotent_on_already_scrubbed_rationale():
    doc = _doc("API_TOKEN changed from sk-OLD-123456 to sk-NEW-789012.",
               [{"name": "API_TOKEN", "expected": "sk-OLD-123456", "live": "sk-NEW-789012",
                 "contract_status": "present_disallow_manual"}])
    once = scrub_decision_rationale(doc)
    # Re-scrubbing the already-redacted doc finds no raw value → identity.
    assert scrub_decision_rationale(once) is once


def test_scrub_does_not_mutate_input_doc():
    secret = "sk-OLD-123456"
    doc = _doc(f"value was {secret}",
               [{"name": "API_TOKEN", "live": secret, "contract_status": "present_disallow_manual"}])
    out = scrub_decision_rationale(doc)
    assert doc["rationale"] == f"value was {secret}"   # original untouched
    assert out is not doc and secret not in out["rationale"]


@pytest.mark.parametrize("doc", [
    None,
    {"action": "no_op"},                  # no rationale key
    {"rationale": None, "diffs": []},     # null rationale
    {"rationale": "", "diffs": []},       # empty rationale
    {"rationale": 123, "diffs": []},      # non-str rationale
    {"rationale": "hi", "diffs": None},   # no diffs
])
def test_scrub_handles_missing_or_malformed_inputs(doc):
    assert scrub_decision_rationale(doc) is doc        # never raises; identity


# --------------------------------------------------------------------------- #
# scrub_rationale_text — typed-EnvDiff entry point (rollback `reason`)
# --------------------------------------------------------------------------- #


def test_scrub_rationale_text_scrubs_against_typed_env_diffs():
    p = DecisionProposal(
        action=DecisionAction.ROLLBACK,
        env_diffs=[EnvDiff(name="API_TOKEN", expected=None, live="sk-LEAK-4242",
                           contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL)],
        target_revision="rev-abc",
        rationale="rolling back; API_TOKEN was sk-LEAK-4242.",
        confidence=0.9, requires_human_review=True,
    )
    out = scrub_rationale_text(p.rationale, p.env_diffs)
    assert "sk-LEAK-4242" not in out and "API_TOKEN" in out


def test_scrub_rationale_text_leaves_benign_unchanged():
    p = DecisionProposal(
        action=DecisionAction.ROLLBACK,
        env_diffs=[EnvDiff(name="PAYMENT_MODE", expected="mock", live="live",
                           contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL)],
        target_revision="rev-abc", rationale="PAYMENT_MODE drifted mock→live.",
        confidence=0.9, requires_human_review=True,
    )
    assert scrub_rationale_text(p.rationale, p.env_diffs) == p.rationale
