"""read_conversations_tool — cross-crew, read-only "team memory" over the
conversations log, with ALLOWLIST-PROJECTION + untrusted-turn-text redaction.

Mirrors ``test_read_team_log_tool.py``. The load-bearing controls are the
metadata allowlist (a strict-subset test pins it) and the turn-text redaction
pipeline (the leak gate proves a rollback ``?t=`` token, a credentialed URL, a
secret-bearing ``tool_calls``, and bidi/zero-width chars all get stripped/dropped
before reaching the model).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import agent.main as _main_mod
from agent.adk_tools import read_conversations_tool

# The COMPLETE set of keys the metadata projection may emit (list + thread).
_EXPECTED_META_KEYS = {
    "conversation_id",
    "workload",
    "turn_count",
    "last_trace_id",
    "created_at",
    "updated_at",
    "title",
    # thread mode adds:
    "turns",
    "turns_omitted",
}
# The COMPLETE set of keys a projected TURN may emit.
_EXPECTED_TURN_KEYS = {"seq", "role", "workload", "trace_id", "created_at", "text", "iac_pr"}


# --------------------------------------------------------------------------- #
# Fakes / helpers
# --------------------------------------------------------------------------- #


class _FakeStore:
    """Minimal StateStore stand-in: returns exactly the conversation docs handed
    in, so a test controls every field."""

    def __init__(self, convs):
        # convs: list of dicts (each may carry "turns")
        self._convs = list(convs)

    def list_conversations(self, *, limit=50, workload=None):
        rows = [
            {k: v for k, v in c.items() if k != "turns"}  # metadata only, like the real impl
            for c in self._convs
            if workload is None or c.get("workload") == workload
        ]
        return rows[:limit]

    def get_conversation(self, conversation_id):
        for c in self._convs:
            if c.get("conversation_id") == conversation_id:
                return dict(c)
        return None


def _use_store(monkeypatch, convs):
    monkeypatch.setattr(_main_mod, "get_state", lambda: _FakeStore(convs))


def _conv(**over):
    base = {
        "conversation_id": "c-1",
        "workload": "drift",
        "title": "why is the storefront drifting",
        "turn_count": 2,
        "last_trace_id": "t" * 32,
        "created_at": datetime(2026, 6, 27, 1, 0, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 6, 27, 1, 5, 0, tzinfo=timezone.utc),
        "turns": [
            {"seq": 0, "role": "user", "text": "why is it drifting?",
             "workload": "drift", "trace_id": "t" * 32,
             "created_at": datetime(2026, 6, 27, 1, 0, 0, tzinfo=timezone.utc)},
            {"seq": 1, "role": "crew", "text": "the env var changed",
             "workload": "drift", "trace_id": "t" * 32,
             "created_at": datetime(2026, 6, 27, 1, 1, 0, tzinfo=timezone.utc),
             "iac_pr": {"pr_number": 42, "pr_url": "https://github.com/x/pull/42"},
             "tool_calls": ["read_live_env_tool"]},
        ],
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# List mode — metadata only, allowlisted, no turn text
# --------------------------------------------------------------------------- #


def test_list_projects_metadata_only(monkeypatch):
    _use_store(monkeypatch, [_conv()])
    out = read_conversations_tool()
    assert out["found"] is True
    assert out["count"] == 1
    row = out["conversations"][0]
    assert row["conversation_id"] == "c-1"
    assert row["workload"] == "drift"
    assert row["turn_count"] == 2
    assert row["last_trace_id"] == "t" * 32
    assert row["title"].startswith("why is the storefront")
    assert isinstance(row["created_at"], str) and row["created_at"].startswith("2026-06-27")
    assert isinstance(row["updated_at"], str)
    # list mode never returns turns / turn text.
    assert "turns" not in row
    assert "caveat" in out and out["caveat"]


def test_list_is_json_serializable(monkeypatch):
    _use_store(monkeypatch, [_conv()])
    json.dumps(read_conversations_tool())  # must not raise


def test_list_emits_no_key_outside_the_allowlist(monkeypatch):
    kitchen = _conv(secret_field="SHOULD_NOT_APPEAR", rendered_body="/x?t=TOK")
    _use_store(monkeypatch, [kitchen])
    for row in read_conversations_tool()["conversations"]:
        extra = set(row) - _EXPECTED_META_KEYS
        assert not extra, f"list projection emitted non-allowlisted keys: {extra}"


def test_crew_filter(monkeypatch):
    _use_store(monkeypatch, [_conv(conversation_id="d", workload="drift"),
                             _conv(conversation_id="p", workload="provision")])
    out = read_conversations_tool(crew="provision")
    assert [c["conversation_id"] for c in out["conversations"]] == ["p"]


def test_query_substring_on_title(monkeypatch):
    _use_store(monkeypatch, [
        _conv(conversation_id="a", title="adopt the assets bucket"),
        _conv(conversation_id="b", title="bump python runtime"),
    ])
    out = read_conversations_tool(query="BUCKET")  # case-insensitive
    assert [c["conversation_id"] for c in out["conversations"]] == ["a"]


# --------------------------------------------------------------------------- #
# Thread mode — turns, capped, with redaction
# --------------------------------------------------------------------------- #


def test_thread_returns_capped_turns(monkeypatch):
    _use_store(monkeypatch, [_conv()])
    out = read_conversations_tool(conversation_id="c-1")
    assert out["found"] is True
    conv = out["conversation"]
    assert [t["seq"] for t in conv["turns"]] == [0, 1]
    assert [t["role"] for t in conv["turns"]] == ["user", "crew"]
    # iac_pr surfaces pr_number ONLY (no pr_url).
    assert conv["turns"][1]["iac_pr"] == {"pr_number": 42}


def test_thread_unknown_id_is_failsoft_not_found(monkeypatch):
    _use_store(monkeypatch, [_conv()])
    out = read_conversations_tool(conversation_id="ghost")
    assert out["found"] is False
    assert "not found" in out["error"]


def test_thread_caps_to_max_turns_and_reports_omitted(monkeypatch):
    many = [
        {"seq": i, "role": "user", "text": f"m{i}", "workload": "drift"}
        for i in range(55)
    ]
    _use_store(monkeypatch, [_conv(turns=many, turn_count=55)])
    conv = read_conversations_tool(conversation_id="c-1")["conversation"]
    assert len(conv["turns"]) == 40
    assert conv["turns_omitted"] == 15
    # the newest are kept (seq 54 last).
    assert conv["turns"][-1]["seq"] == 54


def test_thread_emits_no_turn_key_outside_allowlist(monkeypatch):
    nasty_turn = {
        "seq": 0, "role": "crew", "text": "hi", "workload": "drift",
        "trace_id": "tr", "tool_calls": ["x"], "iac_pr": {"pr_number": 5, "pr_url": "u"},
        "some_future_field": "NOPE",
    }
    _use_store(monkeypatch, [_conv(turns=[nasty_turn], turn_count=1)])
    conv = read_conversations_tool(conversation_id="c-1")["conversation"]
    for t in conv["turns"]:
        extra = set(t) - _EXPECTED_TURN_KEYS
        assert not extra, f"turn projection emitted non-allowlisted keys: {extra}"
    # meta keys also stay in the allowlist.
    assert not (set(conv) - _EXPECTED_META_KEYS)


# --------------------------------------------------------------------------- #
# LEAK GATE — untrusted turn text must be scrubbed
# --------------------------------------------------------------------------- #


def test_turn_text_leaks_no_token_secret_or_control_char(monkeypatch):
    # U+202E RLO, U+200B ZWSP — category Cf, must be stripped.
    nasty = (
        "see /approvals/appr-9?t=LIVEHMACTOKEN12345 and "
        "https://driftscribe.example.com/approvals/appr-9?t=ABSTOKEN67890 — "
        "creds postgres://u:s3cr3tpw@host/db drifted ‮hidden​"
    )
    turn = {
        "seq": 0, "role": "crew", "text": nasty, "workload": "drift",
        "tool_calls": ["read_live_env_tool(secret=SHOULDNOTLEAK)"],
        "iac_pr": {"pr_number": 9, "pr_url": "https://github.com/x/pull/9"},
    }
    _use_store(monkeypatch, [_conv(turns=[turn], turn_count=1)])
    out = read_conversations_tool(conversation_id="c-1")
    blob = json.dumps(out)

    # rollback ?t= tokens — relative AND absolute forms.
    assert "LIVEHMACTOKEN12345" not in blob
    assert "ABSTOKEN67890" not in blob
    # credentialed URL userinfo.
    assert "s3cr3tpw" not in blob
    # tool_calls never surfaced (so its secret arg can't leak).
    assert "SHOULDNOTLEAK" not in blob
    assert "tool_calls" not in blob
    # pr_url excluded.
    assert "pull/9" not in blob
    # bidi/zero-width stripped.
    assert "‮" not in blob and "​" not in blob
    # But the readable content survives as data.
    text = out["conversation"]["turns"][0]["text"]
    assert "drifted" in text and "/approvals/appr-9" in text


def test_title_is_token_and_secret_redacted_in_list_mode(monkeypatch):
    # Titles come from the raw first user prompt → untrusted free text.
    nasty_title = "see /approvals/ax?t=TITLETOKEN999 creds postgres://u:titlepw@h/db"
    _use_store(monkeypatch, [_conv(title=nasty_title)])
    blob = json.dumps(read_conversations_tool())
    assert "TITLETOKEN999" not in blob
    assert "titlepw" not in blob
    # title still readable as data.
    assert "/approvals/ax" in read_conversations_tool()["conversations"][0]["title"]


def test_zero_width_cannot_reconstitute_a_secret(monkeypatch):
    # Codex: a zero-width char inside the token/URL must NOT dodge the redactor
    # and then get reconstituted by a later Cf strip. Strip MUST run first.
    zw = "​"  # ZERO WIDTH SPACE (category Cf)
    turn = {
        "seq": 0, "role": "crew", "workload": "drift",
        "text": (
            f"link /approv{zw}als/a1?t=ZWTOKEN42 and "
            f"db postgres:/{zw}/u:zwsecret@host/db"
        ),
    }
    _use_store(monkeypatch, [_conv(turns=[turn], turn_count=1)])
    blob = json.dumps(read_conversations_tool(conversation_id="c-1"))
    assert "ZWTOKEN42" not in blob, "zero-width-split ?t= token was reconstituted"
    assert "zwsecret" not in blob, "zero-width-split credentialed URL leaked"


def test_single_component_userinfo_url_is_redacted(monkeypatch):
    # secret_guard.redact_text only catches scheme://user:PASS@host; a token-only
    # userinfo (scheme://TOKEN@host) must still be redacted on this surface.
    turn = {
        "seq": 0, "role": "crew", "workload": "drift",
        "text": "cache redis://ghp_LIVETOKEN123@redis.internal:6379/0 down",
    }
    _use_store(monkeypatch, [_conv(turns=[turn], turn_count=1)])
    blob = json.dumps(read_conversations_tool(conversation_id="c-1"))
    assert "ghp_LIVETOKEN123" not in blob
    assert "<redacted>@redis.internal" in blob


def test_single_component_userinfo_url_redacted_in_title(monkeypatch):
    _use_store(monkeypatch, [_conv(title="prod amqp://APIKEY999@rabbit.internal/vh")])
    blob = json.dumps(read_conversations_tool())
    assert "APIKEY999" not in blob


def test_long_turn_text_is_capped(monkeypatch):
    turn = {"seq": 0, "role": "user", "text": "x" * 1000, "workload": "drift"}
    _use_store(monkeypatch, [_conv(turns=[turn], turn_count=1)])
    conv = read_conversations_tool(conversation_id="c-1")["conversation"]
    assert len(conv["turns"][0]["text"]) <= 401  # cap + ellipsis


def test_title_strips_bidi_and_is_capped(monkeypatch):
    _use_store(monkeypatch, [_conv(title="Adopt ‮bucket​ " + "y" * 200)])
    title = read_conversations_tool()["conversations"][0]["title"]
    assert "‮" not in title and "​" not in title
    assert len(title) <= 81


# --------------------------------------------------------------------------- #
# Validation + clamping + fail-soft
# --------------------------------------------------------------------------- #


def test_limit_is_clamped(monkeypatch):
    convs = [_conv(conversation_id=f"c{i}") for i in range(60)]
    _use_store(monkeypatch, convs)
    assert read_conversations_tool(limit=999)["count"] == 50
    assert read_conversations_tool(limit=0)["count"] == 1
    assert read_conversations_tool(limit="oops")["count"] == 10  # default


@pytest.mark.parametrize("bad", [123, ["x"], {"a": 1}])
def test_bad_crew_returns_error_not_raise(monkeypatch, bad):
    _use_store(monkeypatch, [_conv()])
    out = read_conversations_tool(crew=bad)
    assert out["found"] is False and "crew" in out["error"]


@pytest.mark.parametrize("bad", ["../etc", "a/b", "x" * 200, "has space", 123])
def test_malformed_conversation_id_returns_error(monkeypatch, bad):
    _use_store(monkeypatch, [_conv()])
    out = read_conversations_tool(conversation_id=bad)
    assert out["found"] is False and "conversation_id" in out["error"]


def test_empty_log_returns_found_true_count_zero(monkeypatch):
    _use_store(monkeypatch, [])
    out = read_conversations_tool()
    assert out["found"] is True and out["count"] == 0 and out["conversations"] == []


def test_store_error_is_failsoft(monkeypatch):
    class _Boom:
        def list_conversations(self, *, limit=50, workload=None):
            raise RuntimeError("firestore exploded")

    monkeypatch.setattr(_main_mod, "get_state", lambda: _Boom())
    out = read_conversations_tool()
    assert out["found"] is False and "error" in out
