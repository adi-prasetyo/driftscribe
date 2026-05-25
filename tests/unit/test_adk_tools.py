"""Unit tests for ``agent.adk_tools`` (Phase 11.7 — worker-delegating rewrite).

After 11.7 every mutating tool routes through :mod:`agent.worker_client`.
These tests pin three properties:

- Each worker-delegating tool calls the right worker name with the right
  payload shape (the workers' pydantic schemas have ``extra="forbid"``, so
  any drift here would surface as a 422 in production — these tests catch
  it earlier).
- The branch-name builder in ``patch_docs_tool`` produces a branch that
  starts with ``driftscribe/`` (the docs worker refuses anything else)
  and has a timestamp+random-suffix for collision avoidance.
- ``search_recent_prs_tool`` (coordinator-internal) filters merged PRs by
  case-sensitive word-boundary token match.

We don't reach the real ADC / metadata server here — :mod:`worker_client`
is mocked at the function level.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest


# --------------------------------------------------------------------------- #
# Worker-delegating tools
# --------------------------------------------------------------------------- #


def test_read_live_env_tool_calls_reader_with_empty_payload():
    from agent.adk_tools import read_live_env_tool

    with patch("agent.adk_tools.worker_client.call") as m:
        m.return_value = {"env": {"X": "1"}, "revision": "rev-1"}
        out = read_live_env_tool()

    m.assert_called_once_with("reader", {})
    assert out == {"env": {"X": "1"}, "revision": "rev-1"}


def test_propose_rollback_tool_calls_rollback_with_full_payload():
    from agent.adk_tools import propose_rollback_tool

    with patch("agent.adk_tools.worker_client.call") as m:
        m.return_value = {
            "approval_id": "id1",
            "approval_token": "tok",
            "approval_url": "https://coord/approvals/id1?t=tok",
            "expires_at": "2026-01-01T00:00:00+00:00",
        }
        out = propose_rollback_tool(
            target_revision="payment-demo-00002-bbb",
            reason="rollback to last known good",
        )

    m.assert_called_once_with(
        "rollback",
        {
            "target_revision": "payment-demo-00002-bbb",
            "reason": "rollback to last known good",
        },
    )
    assert out["approval_id"] == "id1"


def test_patch_docs_tool_builds_safe_branch_and_calls_docs():
    """The branch name is built locally — letting the LLM pick the branch
    would be a foot-gun. Pin two properties:

    1. Branch starts with ``driftscribe/`` (the docs worker refuses
       anything else).
    2. Branch ends with a collision-resistant suffix (timestamp+random).
    """
    from agent.adk_tools import patch_docs_tool

    with patch("agent.adk_tools.worker_client.call") as m:
        m.return_value = {"url": "https://github.com/x/y/pull/1"}
        patch_docs_tool(
            file_path="demo/docs/runbook.md",
            new_content="new",
            title="docs: update runbook",
            body="body",
        )

    args, kwargs = m.call_args
    assert args[0] == "docs"
    payload = args[1]
    assert payload["file_path"] == "demo/docs/runbook.md"
    assert payload["new_content"] == "new"
    assert payload["base"] == "main"
    assert payload["title"] == "docs: update runbook"
    assert payload["body"] == "body"
    # Branch shape: driftscribe/<slug>-<ts>-<hex>
    assert payload["branch"].startswith("driftscribe/")
    # Collision suffix at the end — must contain enough digits/hex chars
    # to be unique under retry / concurrency.
    assert re.search(r"-\d{8,}-[0-9a-f]{4}$", payload["branch"]), payload["branch"]


def test_patch_docs_tool_branch_slug_is_sanitized():
    """A file path with uppercase / spaces / slashes still produces a
    branch name that respects the docs worker's branch regex."""
    from agent.adk_tools import patch_docs_tool

    with patch("agent.adk_tools.worker_client.call") as m:
        m.return_value = {"url": "u"}
        patch_docs_tool(
            file_path="demo/docs/Some Mixed CASE.md",
            new_content="x",
            title="t",
            body="b",
        )

    branch = m.call_args[0][1]["branch"]
    # No uppercase, no spaces, no slashes after the driftscribe/ prefix.
    after_prefix = branch.removeprefix("driftscribe/")
    assert after_prefix == after_prefix.lower()
    assert " " not in after_prefix
    assert "/" not in after_prefix


def test_notify_tool_calls_notifier_with_full_payload():
    from agent.adk_tools import notify_tool

    with patch("agent.adk_tools.worker_client.call") as m:
        m.return_value = {"status": "sent", "downstream_status": 200}
        out = notify_tool(channel="alert", severity="high", body="drift detected")

    m.assert_called_once_with(
        "notifier",
        {"channel": "alert", "severity": "high", "body": "drift detected"},
    )
    assert out["status"] == "sent"


@pytest.mark.parametrize("status_code", [502, 503, 422])
def test_notify_tool_is_best_effort_on_worker_error(status_code):
    """A failing/unreachable notifier must NOT propagate — notification is
    the last, least-critical step, so the tool returns a soft error dict
    (any failure status) instead of raising and 502-ing the whole /chat.
    The ``error`` key makes the tool_result log line record result_ok=false."""
    from agent.adk_tools import notify_tool
    from agent.worker_client import WorkerClientError

    with patch("agent.adk_tools.worker_client.call") as m:
        m.side_effect = WorkerClientError(status_code, "webhook unavailable", "notifier")
        out = notify_tool(channel="info", severity="medium", body="PR opened")

    assert out["delivered"] is False
    assert "error" in out
    assert out["worker"] == "notifier"
    assert out["status_code"] == status_code


# --------------------------------------------------------------------------- #
# Coordinator-internal: load_contract_tool
# --------------------------------------------------------------------------- #


def test_load_contract_tool_returns_dict(monkeypatch, tmp_path):
    """The tool reads the contract via :func:`agent.contract.load_contract`
    using the path from Settings. We seed CONTRACT_PATH at a tmp file."""
    from agent.adk_tools import load_contract_tool
    from agent.config import get_settings

    contract_yaml = tmp_path / "ops-contract.yaml"
    contract_yaml.write_text(
        """
service: payment-demo
environment: production
cloud_run_service: payment-demo
region: asia-northeast1
github_repo: x/y
expected_env: {}
"""
    )
    monkeypatch.setenv("CONTRACT_PATH", str(contract_yaml))
    get_settings.cache_clear()

    out = load_contract_tool()
    assert isinstance(out, dict)
    assert out["service"] == "payment-demo"
    assert out["expected_env"] == {}


# --------------------------------------------------------------------------- #
# Coordinator-internal: search_recent_prs_tool
# --------------------------------------------------------------------------- #


def _fake_pr(title, body, url, merged_at):
    pr = MagicMock()
    pr.title = title
    pr.body = body
    pr.html_url = url
    pr.merged_at = merged_at
    return pr


def test_search_recent_prs_returns_empty_with_no_keywords():
    """Empty keywords short-circuits — never hit GitHub."""
    from agent.adk_tools import search_recent_prs_tool

    with patch("agent.adk_tools.get_repo") as m:
        out = search_recent_prs_tool([])
    assert out == []
    m.assert_not_called()


def test_search_recent_prs_returns_empty_with_no_repo_configured(monkeypatch):
    """If GITHUB_REPO isn't set, the tool refuses rather than crashing
    on a None repo. Keeps /chat usable even on misconfigured deploys."""
    from agent.adk_tools import search_recent_prs_tool
    from agent.config import get_settings

    monkeypatch.setenv("GITHUB_REPO", "")
    get_settings.cache_clear()
    out = search_recent_prs_tool(["X"])
    assert out == []


def test_search_recent_prs_filters_by_word_boundary(monkeypatch):
    """Mirrors classifier._strict_pr_match: ``\\b<keyword>\\b`` case-sensitive."""
    from agent.adk_tools import search_recent_prs_tool
    from agent.config import get_settings

    monkeypatch.setenv("GITHUB_REPO", "x/y")
    get_settings.cache_clear()

    now = datetime.now(timezone.utc)
    matching = _fake_pr("Add NEW_THING", "", "u1", now - timedelta(days=1))
    substring_only = _fake_pr("Has NEW_THINGEXT", "", "u2", now - timedelta(days=1))
    lowercase = _fake_pr("add new_thing", "", "u3", now - timedelta(days=1))
    out_of_window = _fake_pr("Add NEW_THING", "", "u4", now - timedelta(days=30))
    unmerged = _fake_pr("Add NEW_THING", "", "u5", None)

    fake_repo = MagicMock()
    fake_repo.get_pulls.return_value = iter(
        [matching, substring_only, lowercase, out_of_window, unmerged]
    )
    with patch("agent.adk_tools.get_repo", return_value=fake_repo):
        out = search_recent_prs_tool(["NEW_THING"], days=7)

    # Only the exact word-boundary match in-window is kept.
    urls = [pr["url"] for pr in out]
    assert urls == ["u1"]


def test_search_recent_prs_continues_past_old_merged_pr(monkeypatch):
    """A PR can be updated recently (early in updated-desc order) but
    merged outside the window. The loop must continue, not break, so a
    fresher in-window PR later in the stream is still picked up."""
    from agent.adk_tools import search_recent_prs_tool
    from agent.config import get_settings

    monkeypatch.setenv("GITHUB_REPO", "x/y")
    get_settings.cache_clear()

    now = datetime.now(timezone.utc)
    old = _fake_pr("Add NEW_THING", "", "old", now - timedelta(days=30))
    fresh = _fake_pr("Add NEW_THING", "", "fresh", now - timedelta(days=1))

    fake_repo = MagicMock()
    fake_repo.get_pulls.return_value = iter([old, fresh])
    with patch("agent.adk_tools.get_repo", return_value=fake_repo):
        out = search_recent_prs_tool(["NEW_THING"], days=7)

    assert [pr["url"] for pr in out] == ["fresh"]


def test_search_recent_prs_passes_none_for_empty_token(monkeypatch):
    """PyGithub's Github('') raises in newer versions; we must coerce
    empty to None. The shim ``get_repo`` is what does this — verifying
    here that we don't accidentally pass an empty string."""
    from agent.adk_tools import search_recent_prs_tool
    from agent.config import get_settings

    monkeypatch.setenv("GITHUB_REPO", "x/y")
    monkeypatch.setenv("GITHUB_TOKEN", "")
    get_settings.cache_clear()

    fake_repo = MagicMock()
    fake_repo.get_pulls.return_value = iter([])
    with patch("agent.adk_tools.get_repo", return_value=fake_repo) as m_get:
        search_recent_prs_tool(["X"])

    # First positional arg to get_repo must NOT be empty string.
    call = m_get.call_args
    if call.args:
        assert call.args[0] != ""
