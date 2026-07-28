"""Crew handoff — the propose/mint/redeem/burn primitives (slice 1).

Pure-logic pins for :mod:`agent.handoff`. The HTTP surface, the tool, and the
StateStore transactions are covered in ``test_chat_handoff_endpoint.py`` and
``test_state_store_handoff.py``; this module owns validation, the nonce, and
expiry — the parts that must be correct before anything persists.
"""
import datetime as dt

import pytest

from agent.handoff import (
    HANDOFF_FIRST_TURN_DENIED_TOOLS,
    HANDOFF_TARGETS,
    HANDOFF_TTL_MINUTES,
    MAX_BRIEF_CHARS,
    MAX_REASON_CHARS,
    HandoffValidationError,
    build_pending_handoff,
    handoff_nonce_digest,
    is_handoff_expired,
    mint_handoff_nonce,
    validate_handoff_proposal,
    verify_handoff_nonce,
)

NOW = dt.datetime(2026, 7, 28, 12, 0, tzinfo=dt.timezone.utc)


# --- validation ------------------------------------------------------------

def test_targets_are_exactly_the_four_deployed_crews():
    # Symbolic workload names are frozen (they are the registry authority key).
    assert HANDOFF_TARGETS == frozenset({"drift", "upgrade", "provision", "explore"})


def test_valid_proposal_normalizes_and_echoes_the_route():
    out = validate_handoff_proposal(
        target="drift", reason="  fixing this needs a rollback  ",
        brief="ORDER_TIMEOUT is 90s; the contract pins 30s.",
        current_workload="explore",
    )
    assert out["from"] == "explore"
    assert out["to"] == "drift"
    assert out["reason"] == "fixing this needs a rollback"
    assert out["brief"] == "ORDER_TIMEOUT is 90s; the contract pins 30s."


def test_unknown_target_is_refused():
    with pytest.raises(HandoffValidationError):
        validate_handoff_proposal(
            target="anchor", reason="r", brief="b", current_workload="explore",
        )


def test_handoff_to_the_current_crew_is_refused():
    """A crew that proposes itself would burn a turn and change nothing."""
    with pytest.raises(HandoffValidationError):
        validate_handoff_proposal(
            target="explore", reason="r", brief="b", current_workload="explore",
        )


def test_empty_reason_is_refused():
    """The chip text derives from ``reason`` — an empty one renders a blank
    confirmation, which is exactly the thing a human must be able to read."""
    with pytest.raises(HandoffValidationError):
        validate_handoff_proposal(
            target="drift", reason="   ", brief="b", current_workload="explore",
        )


def test_reason_and_brief_are_control_char_stripped_and_capped():
    """Both fields are model-authored and reach a human (chip) and a crew
    (prompt), so they get the same flatten+cap treatment as every other
    untrusted string the crews relay."""
    out = validate_handoff_proposal(
        target="drift",
        reason="line one\nline two‮RTL",
        brief="x" * (MAX_BRIEF_CHARS + 500),
        current_workload="explore",
    )
    assert "\n" not in out["reason"]
    assert "‮" not in out["reason"]
    assert len(out["reason"]) <= MAX_REASON_CHARS
    assert len(out["brief"]) <= MAX_BRIEF_CHARS


def test_brief_may_be_empty():
    """A handoff with no brief is degraded, not invalid — replayed history
    still carries context up to ``MAX_SEED_TURNS``."""
    out = validate_handoff_proposal(
        target="drift", reason="needs a rollback", brief="",
        current_workload="explore",
    )
    assert out["brief"] == ""


# --- nonce -----------------------------------------------------------------

def test_mint_returns_a_secret_and_its_digest_only():
    nonce, digest = mint_handoff_nonce()
    assert nonce and digest
    # The secret is never derivable from what we persist.
    assert nonce not in digest
    assert handoff_nonce_digest(nonce) == digest


def test_distinct_mints_never_collide():
    assert len({mint_handoff_nonce()[0] for _ in range(50)}) == 50


def test_verify_accepts_the_matching_nonce_and_rejects_others():
    nonce, digest = mint_handoff_nonce()
    other, _ = mint_handoff_nonce()
    assert verify_handoff_nonce(nonce, digest) is True
    assert verify_handoff_nonce(other, digest) is False
    assert verify_handoff_nonce("", digest) is False
    assert verify_handoff_nonce(nonce, "") is False
    assert verify_handoff_nonce(nonce, None) is False


# --- pending doc + expiry --------------------------------------------------

def test_pending_doc_carries_the_digest_never_the_nonce():
    nonce, digest = mint_handoff_nonce()
    proposal = validate_handoff_proposal(
        target="drift", reason="needs a rollback", brief="b",
        current_workload="explore",
    )
    pending = build_pending_handoff(proposal, digest=digest, now=NOW)
    assert pending["nonce_digest"] == digest
    assert nonce not in str(pending)
    assert pending["from"] == "explore"
    assert pending["to"] == "drift"
    assert pending["created_at"] == NOW
    assert pending["expires_at"] == NOW + dt.timedelta(minutes=HANDOFF_TTL_MINUTES)


def test_ttl_matches_the_rollback_approval_window():
    """Do not invent a second expiry vocabulary — the operator already learned
    15 minutes from the HMAC approval gate."""
    from driftscribe_lib.approvals import _MAX_APPROVAL_TTL_MINUTES

    assert HANDOFF_TTL_MINUTES == _MAX_APPROVAL_TTL_MINUTES


def test_expiry_is_inclusive_of_the_boundary():
    _, digest = mint_handoff_nonce()
    proposal = validate_handoff_proposal(
        target="drift", reason="r", brief="b", current_workload="explore",
    )
    pending = build_pending_handoff(proposal, digest=digest, now=NOW)
    assert is_handoff_expired(pending, now=NOW) is False
    assert is_handoff_expired(
        pending, now=NOW + dt.timedelta(minutes=HANDOFF_TTL_MINUTES - 1)
    ) is False
    # At the boundary the window is over — fail closed, same as the approval gate.
    assert is_handoff_expired(
        pending, now=NOW + dt.timedelta(minutes=HANDOFF_TTL_MINUTES)
    ) is True


def test_a_pending_doc_with_no_expiry_is_treated_as_expired():
    """Fail closed on a malformed / partially-written doc rather than granting
    an unbounded transition credential."""
    assert is_handoff_expired({"nonce_digest": "x"}, now=NOW) is True
    assert is_handoff_expired({}, now=NOW) is True
    assert is_handoff_expired(None, now=NOW) is True


# --- first-turn denylist ---------------------------------------------------

def test_first_turn_denylist_covers_both_pr_mutating_tools():
    """The confirm click runs a turn the operator never typed. Merging or
    closing an existing PR are the two single-call actions that change a PR's
    state, so neither may be reachable from that turn — enforced here in code,
    never by prompt wording (``build_chat_agent``'s demo-anon denylist
    explicitly PRESERVES ``upgrade_merge_pr``, and ``propose_apply`` mode has
    no denylist at all)."""
    assert HANDOFF_FIRST_TURN_DENIED_TOOLS == frozenset(
        {"upgrade_merge_pr", "upgrade_close_pr"}
    )


def test_denied_tools_are_real_registry_names():
    from agent.workloads.registry import TOOL_REGISTRY

    assert HANDOFF_FIRST_TURN_DENIED_TOOLS <= set(TOOL_REGISTRY)


# --- the joining crew's first turn -----------------------------------------

def test_crew_display_names_match_the_manifests():
    """The synthetic prompt names crews the way their own prompts do ("You are
    Anchor"), not by the frozen symbolic workload name. Pin the map against the
    manifests so a display rename cannot silently desync."""
    import pathlib

    import yaml

    from agent.handoff import CREW_DISPLAY_NAMES

    repo = pathlib.Path(__file__).resolve().parents[2]
    for name in HANDOFF_TARGETS:
        spec = yaml.safe_load(
            (repo / "workloads" / name / "workload.yaml").read_text()
        )
        assert CREW_DISPLAY_NAMES[name] == spec["display_name"]


def test_joining_prompt_quotes_the_brief_as_data_and_names_the_handing_crew():
    from agent.handoff import handoff_prompt

    _, digest = mint_handoff_nonce()
    pending = build_pending_handoff(
        validate_handoff_proposal(
            target="drift", reason="fixing this needs a rollback",
            brief="ORDER_TIMEOUT is 90s; the contract pins 30s.",
            current_workload="explore",
        ),
        digest=digest, now=NOW,
    )
    text = handoff_prompt(pending)

    assert "Explore" in text
    assert "ORDER_TIMEOUT is 90s" in text
    assert "fixing this needs a rollback" in text
    # The brief is model-authored text arriving from another crew. It must read
    # as quoted DATA, never as instructions addressed to the joining crew.
    assert "instructions" in text.lower() or "data" in text.lower()
    # Never leak the credential into the prompt the model sees.
    assert digest not in text


def test_joining_prompt_survives_an_empty_brief():
    from agent.handoff import handoff_prompt

    _, digest = mint_handoff_nonce()
    pending = build_pending_handoff(
        validate_handoff_proposal(
            target="upgrade", reason="needs a version bump", brief="",
            current_workload="explore",
        ),
        digest=digest, now=NOW,
    )
    text = handoff_prompt(pending)
    assert "needs a version bump" in text
    assert text.strip()
