"""Unit tests for agent/autonomy.py — the autonomy dial state + tool filter.

Mirrors tests/unit/test_pause.py's structure: plain InMemoryStateStore,
throw-on-get stub for fail-closed cases, no monkeypatching.
"""
import pytest

from agent.autonomy import (
    AUTONOMY_MODES,
    DEFAULT_MODE,
    FAIL_CLOSED_REASON,
    AutonomyState,
    autonomy_apply_blocked_detail,
    autonomy_instruction_note,
    filter_tools_for_mode,
    mode_allows,
    read_autonomy_state,
)
from agent.state_store import InMemoryStateStore


class _BoomStore:
    def get_autonomy(self):
        raise RuntimeError("firestore unavailable")


class _DocStore:
    def __init__(self, doc):
        self._doc = doc

    def get_autonomy(self):
        return self._doc


class TestReadAutonomyState:
    def test_absent_doc_defaults_to_propose_apply(self):
        st = read_autonomy_state(InMemoryStateStore())
        assert st == AutonomyState(mode="propose_apply")
        assert DEFAULT_MODE == "propose_apply"

    def test_storage_error_fails_closed_to_observe(self):
        st = read_autonomy_state(_BoomStore())
        assert st.mode == "observe"
        assert st.read_error is True
        assert st.reason == FAIL_CLOSED_REASON

    @pytest.mark.parametrize(
        "doc",
        [
            {},                          # clobbered empty doc
            {"mode": "yolo"},            # unknown mode string
            {"mode": 3},                 # wrong type
            {"mode": None},
            "not-a-dict",
            ["propose"],
        ],
    )
    def test_malformed_doc_fails_closed_to_observe(self, doc):
        st = read_autonomy_state(_DocStore(doc))
        assert st.mode == "observe"
        assert st.read_error is True

    @pytest.mark.parametrize("mode", list(AUTONOMY_MODES))
    def test_valid_doc_round_trips(self, mode):
        store = InMemoryStateStore()
        store.set_autonomy(mode=mode, reason="testing", actor="op@example.com")
        st = read_autonomy_state(store)
        assert st.mode == mode
        assert st.reason == "testing"
        assert st.actor == "op@example.com"
        assert st.read_error is False
        assert st.updated_at is not None

    def test_state_is_frozen(self):
        st = AutonomyState(mode="observe")
        with pytest.raises(Exception):
            st.mode = "propose_apply"  # type: ignore[misc]


class TestModeAllows:
    def test_observe_allows_only_report(self):
        assert mode_allows("observe", "report") is True
        assert mode_allows("observe", "propose") is False
        assert mode_allows("observe", "apply") is False

    def test_propose_allows_report_and_propose(self):
        assert mode_allows("propose", "report") is True
        assert mode_allows("propose", "propose") is True
        assert mode_allows("propose", "apply") is False

    def test_propose_apply_allows_everything(self):
        for tier in ("report", "propose", "apply"):
            assert mode_allows("propose_apply", tier) is True

    def test_unknown_tier_or_mode_fails_closed(self):
        assert mode_allows("propose_apply", "banana") is False
        assert mode_allows("banana", "report") is False


class TestFilterToolsForMode:
    TOOLS = {"a_read": (lambda: 1), "b_propose": (lambda: 2), "c_apply": (lambda: 3)}
    TIERS = {"a_read": "report", "b_propose": "propose", "c_apply": "apply"}

    def test_observe_keeps_only_report(self):
        out = filter_tools_for_mode(self.TOOLS, self.TIERS, "observe")
        assert set(out) == {"a_read"}

    def test_propose_strips_apply_only(self):
        out = filter_tools_for_mode(self.TOOLS, self.TIERS, "propose")
        assert set(out) == {"a_read", "b_propose"}

    def test_propose_apply_keeps_everything_in_order(self):
        out = filter_tools_for_mode(self.TOOLS, self.TIERS, "propose_apply")
        assert list(out) == list(self.TOOLS)  # insertion order preserved

    def test_tool_missing_from_tiers_is_treated_as_apply(self):
        # Fail-closed: a new tool cannot leak into a restricted mode by
        # missing its tier assignment.
        out = filter_tools_for_mode({"mystery": (lambda: 0)}, {}, "propose")
        assert out == {}
        out2 = filter_tools_for_mode({"mystery": (lambda: 0)}, {}, "propose_apply")
        assert set(out2) == {"mystery"}


class TestCopy:
    def test_instruction_note_names_the_mode_and_the_control(self):
        note = autonomy_instruction_note("observe")
        assert "Observe" in note
        assert "autonomy" in note.lower()

    def test_apply_blocked_detail_says_how_to_enable(self):
        detail = autonomy_apply_blocked_detail("propose")
        assert "Propose" in detail
        assert "Propose + Apply" in detail

    @pytest.mark.parametrize("text_fn", [autonomy_instruction_note, autonomy_apply_blocked_detail])
    @pytest.mark.parametrize("banned", ["risk", "danger", "unsafe", "rampage"])
    def test_copy_is_factual_not_alarming(self, text_fn, banned):
        assert banned not in text_fn("observe").lower()
