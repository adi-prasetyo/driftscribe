from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import httpx
import pytest
import respx

from agent.adk_tools import (
    _list_recent_merged_prs,
    call_debug_config_tool,
    load_contract_tool,
    read_live_env_tool,
    search_recent_prs_tool,
)


# ---------- spec-derived tests ----------


def test_read_live_env_tool_returns_dict():
    with patch("agent.adk_tools.read_live_env") as m:
        m.return_value = {"X": "1"}
        assert read_live_env_tool("s", "r", "p") == {"X": "1"}
        m.assert_called_once_with("s", "r", "p")


def test_load_contract_tool_parses_yaml(tmp_path):
    (tmp_path / "c.yaml").write_text(
        """
service: s
environment: production
cloud_run_service: s
region: asia-northeast1
github_repo: x/x
expected_env: {}
"""
    )
    d = load_contract_tool(str(tmp_path / "c.yaml"))
    assert d["service"] == "s"
    assert d["expected_env"] == {}
    # The tool intentionally returns a raw dict (not OpsContract) — the LLM
    # reasons over the shape; the deterministic path uses agent.contract.load_contract.
    assert isinstance(d, dict)


def test_search_recent_prs_tool_filters_by_exact_token():
    fake = [
        {"title": "Add NEW_THING", "body": "", "url": "u1", "merged": True},
        {"title": "Unrelated change", "body": "", "url": "u2", "merged": True},
        # substring, NOT a token
        {"title": "Has NEW_THINGEXT", "body": "", "url": "u3", "merged": True},
    ]
    with patch("agent.adk_tools._list_recent_merged_prs") as m:
        m.return_value = fake
        result = search_recent_prs_tool("x/x", ["NEW_THING"], 7)
    urls = [r["url"] for r in result]
    assert urls == ["u1"]


# ---------- call_debug_config_tool via respx ----------


@respx.mock
def test_call_debug_config_tool_returns_parsed_json_on_200():
    respx.get("https://example.test/debug/config").respond(
        200, json={"config": {"X": "1"}}
    )
    out = call_debug_config_tool("https://example.test/debug/config")
    assert out == {"config": {"X": "1"}}


@respx.mock
def test_call_debug_config_tool_returns_error_on_500():
    respx.get("https://example.test/debug/config").respond(500, text="boom")
    out = call_debug_config_tool("https://example.test/debug/config")
    assert "_error" in out
    assert isinstance(out["_error"], str) and out["_error"]  # informative, non-empty


@respx.mock
def test_call_debug_config_tool_returns_error_on_timeout():
    respx.get("https://example.test/debug/config").mock(
        side_effect=httpx.ReadTimeout("slow")
    )
    out = call_debug_config_tool("https://example.test/debug/config")
    assert "_error" in out
    assert "slow" in out["_error"] or "timeout" in out["_error"].lower()


@respx.mock
def test_call_debug_config_tool_returns_error_on_non_json_200():
    respx.get("https://example.test/debug/config").respond(200, text="hello")
    out = call_debug_config_tool("https://example.test/debug/config")
    assert "_error" in out
    assert out["_error"]


# ---------- search_recent_prs_tool edge cases ----------


def test_search_recent_prs_tool_returns_empty_when_no_keywords():
    # Must not crash and must not call into PyGithub at all when there's
    # nothing to look for. (Patch _list_recent_merged_prs so a real call would
    # blow up if it were made — but we expect an early empty return.)
    with patch("agent.adk_tools._list_recent_merged_prs") as m:
        m.return_value = [
            {"title": "Add NEW_THING", "body": "", "url": "u1", "merged": True},
        ]
        result = search_recent_prs_tool("x/x", [], 7)
    assert result == []


def test_search_recent_prs_tool_is_case_sensitive():
    # Mirrors classifier._strict_pr_match: case-sensitive \b<token>\b.
    fake = [
        {"title": "add new_thing", "body": "", "url": "u1", "merged": True},
    ]
    with patch("agent.adk_tools._list_recent_merged_prs") as m:
        m.return_value = fake
        result = search_recent_prs_tool("x/x", ["NEW_THING"], 7)
    assert result == []


# ---------- _list_recent_merged_prs iteration semantics ----------


def _fake_pr(title, body, url, merged_at):
    pr = MagicMock()
    pr.title = title
    pr.body = body
    pr.html_url = url
    pr.merged_at = merged_at
    return pr


def test_list_recent_merged_prs_continues_past_old_merged_pr():
    """A PR can be updated recently (so it appears early in updated-desc order)
    while its merged_at is older than the window. We must `continue` past it
    rather than `break`, so a later in-window PR still gets picked up."""
    now = datetime.now(timezone.utc)
    days = 7
    old_pr = _fake_pr(
        "Old but recently touched",
        "",
        "https://github.com/x/x/pull/1",
        now - timedelta(days=days + 5),  # merged outside window
    )
    fresh_pr = _fake_pr(
        "Fresh PR",
        "body",
        "https://github.com/x/x/pull/2",
        now - timedelta(days=1),  # merged inside window
    )
    unmerged_pr = _fake_pr("Unmerged", "", "https://github.com/x/x/pull/3", None)

    fake_repo = MagicMock()
    fake_repo.get_pulls.return_value = iter([old_pr, unmerged_pr, fresh_pr])
    fake_gh = MagicMock()
    fake_gh.get_repo.return_value = fake_repo

    with patch("agent.adk_tools.Github", return_value=fake_gh):
        out = _list_recent_merged_prs("x/x", days=days, token="")

    urls = [pr["url"] for pr in out]
    assert urls == ["https://github.com/x/x/pull/2"]
    assert out[0]["title"] == "Fresh PR"
    assert out[0]["body"] == "body"
    assert out[0]["merged"] is True


def test_list_recent_merged_prs_uses_none_for_empty_token():
    """PyGithub's Github('') raises in current versions; we must pass None
    (or use no-arg construction) when the caller supplies an empty token."""
    fake_repo = MagicMock()
    fake_repo.get_pulls.return_value = iter([])
    fake_gh = MagicMock()
    fake_gh.get_repo.return_value = fake_repo

    with patch("agent.adk_tools.Github", return_value=fake_gh) as m_gh:
        _list_recent_merged_prs("x/x", days=7, token="")

    # Must NOT have been called with the empty string positionally.
    call_args = m_gh.call_args
    # Whatever the construction style, it must not pass "" as the first positional arg.
    if call_args.args:
        assert call_args.args[0] != ""
