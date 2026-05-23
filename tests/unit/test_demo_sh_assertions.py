"""Meta-tests for scripts/demo.sh opt-in smoke assertions (Phase 20.2).

These tests are static — they read scripts/demo.sh as text and verify that
the opt-in ASSERT path is wired up correctly. They do NOT execute demo.sh
or hit any deployed service. Live verification is the operator's job
(see plan §Task 20.2 Step 4).
"""

from pathlib import Path


def test_demo_sh_has_both_assertion_helpers():
    body = Path("scripts/demo.sh").read_text()
    assert "assert_recheck_action" in body
    assert "assert_chat_reply" in body


def test_demo_sh_assert_is_opt_in_default_off():
    body = Path("scripts/demo.sh").read_text()
    assert 'ASSERT="${ASSERT:-0}"' in body or 'ASSERT=${ASSERT:-0}' in body


def test_recheck_beats_use_recheck_assertion():
    """beat-a..d hit /recheck — they must assert on .action, not .reply."""
    body = Path("scripts/demo.sh").read_text()
    # Each beat function should call assert_recheck_action with the expected action.
    assert "assert_recheck_action no_op" in body            # beat-a
    assert "assert_recheck_action drift_issue" in body      # beat-b
    assert "assert_recheck_action docs_pr" in body          # beat-d
    # beat-c is ADK-non-deterministic — assert any valid action via the helper's
    # "ANY" sentinel.
    assert "assert_recheck_action ANY" in body              # beat-c


def test_chat_beats_use_chat_reply_assertion():
    """beat-e + upgrade-* hit /chat — assert on .reply present."""
    body = Path("scripts/demo.sh").read_text()
    # Count call sites (one per chat beat).
    chat_assert_count = body.count("assert_chat_reply")
    # beat-e + upgrade-a + upgrade-b + upgrade-c = 4 minimum (one helper definition
    # + >=4 call sites).
    assert chat_assert_count >= 5, f"expected >=5 (definition + 4 calls), got {chat_assert_count}"
