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


def test_propose_rollback_tool_sends_target_revision_and_safe_reason():
    """The worker payload carries the target_revision and a SAFE reason derived
    only from it — NOT the model-authored ``reason`` (PR 2: the rollback worker
    renders ``reason`` on the operator approval page, and the chat LLM sees raw
    env, so its prose must not reach that page)."""
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

    # The FIRST call is to "rollback" — check it specifically.
    rollback_call = next(c for c in m.call_args_list if c.args[0] == "rollback")
    worker, payload = rollback_call.args
    assert worker == "rollback"
    assert payload["target_revision"] == "payment-demo-00002-bbb"
    assert "payment-demo-00002-bbb" in payload["reason"]  # safe, revision-derived
    assert out["approval_id"] == "id1"


def test_propose_rollback_tool_does_not_forward_secret_reason():
    """A secret quoted in the model-authored ``reason`` must NOT reach the
    worker (and thus the approval page). The reader returns env unredacted, so
    the model can see and quote any secret form — bare token or credentialed
    URL — hence we drop the prose entirely rather than value-scrub it."""
    from agent.adk_tools import propose_rollback_tool

    secret_token = "sk-CHAT-LEAK-8421"
    secret_url = "https://admin:hunter2CHAT@svc.internal/api"
    with patch("agent.adk_tools.worker_client.call") as m:
        m.return_value = {"approval_id": "id1", "approval_url": "u", "expires_at": "x"}
        propose_rollback_tool(
            target_revision="payment-demo-00002-bbb",
            reason=f"rolling back because TOKEN={secret_token} and DSN={secret_url}",
        )

    # Check the rollback call specifically — there's also a notifier call now.
    rollback_call = next(c for c in m.call_args_list if c.args[0] == "rollback")
    reason = rollback_call.args[1]["reason"]
    assert secret_token not in reason
    assert secret_url not in reason
    assert "hunter2CHAT" not in reason


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


# --------------------------------------------------------------------------- #
# open_infra_pr_tool — pending-approval notifications (Wave 2 item 7)
# --------------------------------------------------------------------------- #


def _confirmed_open_pr_result(pr_number=42, pr_url="https://github.com/owner/repo/pull/42"):
    return {
        "status": "opened",
        "pr_number": pr_number,
        "pr_url": pr_url,
        "branch": "infra/add-bucket-1-ab",
    }


def _patch_open_infra_pr_call(monkeypatch, *, result=None, exc=None):
    """Patch the call_open_infra_pr seam used by open_infra_pr_tool."""
    import importlib

    def _fake(target_repo, branch, title, body, files):
        if exc is not None:
            raise exc
        return result or _confirmed_open_pr_result()

    monkeypatch.setattr(
        importlib.import_module("agent.worker_client"),
        "call_open_infra_pr",
        _fake,
    )


def _open_infra_pr_args():
    return dict(
        files=[{"path": "iac/bucket.tf", "content": "resource bucket {}"}],
        title="Add staging bucket",
        body="Creates the staging bucket.",
    )


def test_open_infra_pr_tool_notifies_on_confirmed_pr(monkeypatch):
    """Confirmed PR → exactly ONE notifier call with channel=approval,
    severity=medium, and body containing the approval URL, title, pr_url,
    and the honest C2-dispatch instruction."""
    from agent.adk_tools import open_infra_pr_tool
    from agent.config import get_settings

    monkeypatch.setenv("COORDINATOR_ORIGIN", "https://driftscribe.example.com")
    get_settings.cache_clear()
    _patch_open_infra_pr_call(monkeypatch)

    notifier_calls = []

    def _fake_call(worker, payload):
        if worker == "notifier":
            notifier_calls.append(payload)
        return {"status": "sent"}

    with patch("agent.adk_tools.worker_client.call", side_effect=_fake_call):
        out = open_infra_pr_tool(**_open_infra_pr_args())

    assert len(notifier_calls) == 1, f"expected 1 notifier call, got {len(notifier_calls)}"
    n = notifier_calls[0]
    assert n["channel"] == "approval"
    assert n["severity"] == "medium"
    assert "/iac-approvals/42" in n["body"]
    assert "Add staging bucket" in n["body"]
    assert "https://github.com/owner/repo/pull/42" in n["body"]
    assert "dispatch the C2 plan-builder" in n["body"]
    # Tool return value unchanged
    assert out["pr_number"] == 42
    assert out["status"] == "opened"


def test_open_infra_pr_tool_notify_body_absolute_url_when_origin_set(monkeypatch):
    """When coordinator_origin is set, the body contains the ABSOLUTE approval URL."""
    from agent.adk_tools import open_infra_pr_tool
    from agent.config import get_settings

    monkeypatch.setenv("COORDINATOR_ORIGIN", "https://driftscribe.example.com")
    get_settings.cache_clear()
    _patch_open_infra_pr_call(monkeypatch)

    notifier_calls = []

    def _fake_call(worker, payload):
        if worker == "notifier":
            notifier_calls.append(payload)
        return {"status": "sent"}

    with patch("agent.adk_tools.worker_client.call", side_effect=_fake_call):
        open_infra_pr_tool(**_open_infra_pr_args())

    assert len(notifier_calls) == 1
    assert "https://driftscribe.example.com/iac-approvals/42" in notifier_calls[0]["body"]


def test_open_infra_pr_tool_notify_body_relative_url_when_origin_empty(monkeypatch):
    """When coordinator_origin is empty, the body contains the relative approval path."""
    from agent.adk_tools import open_infra_pr_tool
    from agent.config import get_settings

    monkeypatch.setenv("COORDINATOR_ORIGIN", "")
    get_settings.cache_clear()
    _patch_open_infra_pr_call(monkeypatch)

    notifier_calls = []

    def _fake_call(worker, payload):
        if worker == "notifier":
            notifier_calls.append(payload)
        return {"status": "sent"}

    with patch("agent.adk_tools.worker_client.call", side_effect=_fake_call):
        open_infra_pr_tool(**_open_infra_pr_args())

    assert len(notifier_calls) == 1
    body = notifier_calls[0]["body"]
    # Relative path, no scheme/host
    assert "/iac-approvals/42" in body
    assert "https://" not in body.split("/iac-approvals/")[0]


def test_open_infra_pr_tool_notify_title_clamped_at_200_chars(monkeypatch):
    """A title longer than 200 chars is clamped with '…' in the notify body;
    the body stays within the notifier's 10k cap."""
    from agent.adk_tools import open_infra_pr_tool
    from agent.config import get_settings

    monkeypatch.setenv("COORDINATOR_ORIGIN", "https://coord.example.com")
    get_settings.cache_clear()
    long_title = "A" * 250
    _patch_open_infra_pr_call(monkeypatch)

    notifier_calls = []

    def _fake_call(worker, payload):
        if worker == "notifier":
            notifier_calls.append(payload)
        return {"status": "sent"}

    args = dict(
        files=[{"path": "iac/bucket.tf", "content": "resource bucket {}"}],
        title=long_title,
        body="body",
    )
    with patch("agent.adk_tools.worker_client.call", side_effect=_fake_call):
        open_infra_pr_tool(**args)

    assert len(notifier_calls) == 1
    body = notifier_calls[0]["body"]
    # The title should be clamped with '…'
    assert "…" in body
    assert "A" * 201 not in body  # not the full 250-char title
    assert len(body) <= 10000


@pytest.mark.parametrize(
    "worker_result",
    [
        # missing pr_number
        {"status": "opened", "pr_url": "https://github.com/x/y/pull/1"},
        # bool pr_number (True is a subclass of int — must be excluded)
        {"status": "opened", "pr_number": True, "pr_url": "https://github.com/x/y/pull/1"},
        # pr_number == 0
        {"status": "opened", "pr_number": 0, "pr_url": "https://github.com/x/y/pull/1"},
        # missing pr_url
        {"status": "opened", "pr_number": 42},
        # empty pr_url
        {"status": "opened", "pr_number": 42, "pr_url": ""},
    ],
)
def test_open_infra_pr_tool_no_notify_on_unconfirmed_result(monkeypatch, worker_result):
    """Unconfirmed/malformed worker results → ZERO notifier calls; tool return
    value is unchanged (the pointer is absent)."""
    from agent.adk_tools import open_infra_pr_tool
    from agent.config import get_settings

    monkeypatch.setenv("COORDINATOR_ORIGIN", "https://coord.example.com")
    get_settings.cache_clear()
    _patch_open_infra_pr_call(monkeypatch, result=worker_result)

    notifier_calls = []

    def _fake_call(worker, payload):
        if worker == "notifier":
            notifier_calls.append(payload)
        return {"status": "sent"}

    with patch("agent.adk_tools.worker_client.call", side_effect=_fake_call):
        out = open_infra_pr_tool(**_open_infra_pr_args())

    assert notifier_calls == [], f"expected no notifier calls, got {notifier_calls}"
    # Return value carries whatever came from the worker
    assert out["pr_number"] == worker_result.get("pr_number")


def test_open_infra_pr_tool_notifier_worker_error_suppressed(monkeypatch, caplog):
    """WorkerClientError from notifier → suppressed, tool returns normal result,
    WARNING iac_pending_notify_failed logged."""
    import logging

    from agent.adk_tools import open_infra_pr_tool
    from agent.config import get_settings
    from agent.worker_client import WorkerClientError

    monkeypatch.setenv("COORDINATOR_ORIGIN", "https://coord.example.com")
    get_settings.cache_clear()
    _patch_open_infra_pr_call(monkeypatch)

    def _fake_call(worker, payload):
        if worker == "notifier":
            raise WorkerClientError(503, "unavailable", "notifier")
        return {}

    with caplog.at_level(logging.WARNING, logger="driftscribe.agent.adk_tools"):
        with patch("agent.adk_tools.worker_client.call", side_effect=_fake_call):
            out = open_infra_pr_tool(**_open_infra_pr_args())

    assert out["pr_number"] == 42
    assert any("iac_pending_notify_failed" in r.message for r in caplog.records)


def test_open_infra_pr_tool_notifier_generic_error_suppressed(monkeypatch, caplog):
    """Generic Exception from notifier → suppressed, tool returns normal result,
    WARNING iac_pending_notify_failed logged."""
    import logging

    from agent.adk_tools import open_infra_pr_tool
    from agent.config import get_settings

    monkeypatch.setenv("COORDINATOR_ORIGIN", "https://coord.example.com")
    get_settings.cache_clear()
    _patch_open_infra_pr_call(monkeypatch)

    def _fake_call(worker, payload):
        if worker == "notifier":
            raise RuntimeError("network timeout")
        return {}

    with caplog.at_level(logging.WARNING, logger="driftscribe.agent.adk_tools"):
        with patch("agent.adk_tools.worker_client.call", side_effect=_fake_call):
            out = open_infra_pr_tool(**_open_infra_pr_args())

    assert out["pr_number"] == 42
    assert any("iac_pending_notify_failed" in r.message for r in caplog.records)


def test_open_infra_pr_tool_call_raises_no_notify(monkeypatch):
    """If call_open_infra_pr itself raises, no notifier call is made and the
    exception propagates (order pin)."""
    from agent.adk_tools import open_infra_pr_tool
    from agent.config import get_settings
    from agent.worker_client import WorkerClientError

    monkeypatch.setenv("COORDINATOR_ORIGIN", "https://coord.example.com")
    get_settings.cache_clear()
    _patch_open_infra_pr_call(monkeypatch, exc=WorkerClientError(503, "down", "tofu_editor"))

    notifier_calls = []

    def _fake_call(worker, payload):
        notifier_calls.append(worker)
        return {}

    with patch("agent.adk_tools.worker_client.call", side_effect=_fake_call):
        with pytest.raises(WorkerClientError):
            open_infra_pr_tool(**_open_infra_pr_args())

    assert notifier_calls == []


def test_open_infra_pr_tool_return_value_deep_equal_all_cases(monkeypatch):
    """Tool return value is byte-identical to today in ALL cases — the
    notification is a pure side-effect."""
    from agent.adk_tools import open_infra_pr_tool
    from agent.config import get_settings

    monkeypatch.setenv("COORDINATOR_ORIGIN", "https://coord.example.com")
    get_settings.cache_clear()
    _patch_open_infra_pr_call(monkeypatch)

    # Capture the return with notify succeeding
    with patch("agent.adk_tools.worker_client.call", return_value={"status": "sent"}):
        out_success = open_infra_pr_tool(**_open_infra_pr_args())

    # Capture the return with notify failing
    def _raise_notifier(worker, payload):
        if worker == "notifier":
            raise RuntimeError("boom")
        return {}

    _patch_open_infra_pr_call(monkeypatch)
    with patch("agent.adk_tools.worker_client.call", side_effect=_raise_notifier):
        out_failed = open_infra_pr_tool(**_open_infra_pr_args())

    assert out_success == out_failed


# --------------------------------------------------------------------------- #
# propose_rollback_tool — pending-approval notifications (Wave 2 item 7)
# --------------------------------------------------------------------------- #


def _rollback_worker_response(
    approval_url="https://driftscribe.example.com/approvals/id1?t=tok",
    expires_at="2026-01-01T00:15:00+00:00",
):
    return {
        "approval_id": "id1",
        "approval_token": "tok",
        "approval_url": approval_url,
        "expires_at": expires_at,
    }


def test_propose_rollback_tool_notifies_on_success(monkeypatch):
    """Worker success → exactly ONE notifier call, severity=high, body contains
    approval_url, expires_at, and target_revision; reason NOT in body."""
    from agent.adk_tools import propose_rollback_tool
    from agent.config import get_settings

    monkeypatch.setenv("COORDINATOR_ORIGIN", "https://driftscribe.example.com")
    get_settings.cache_clear()

    notifier_calls = []

    def _fake_call(worker, payload):
        if worker == "notifier":
            notifier_calls.append(payload)
            return {"status": "sent"}
        return _rollback_worker_response()

    with patch("agent.adk_tools.worker_client.call", side_effect=_fake_call):
        out = propose_rollback_tool(
            target_revision="payment-demo-00010-abc",
            reason="SECRET-SENTINEL-do-not-leak",
        )

    assert len(notifier_calls) == 1, f"expected 1 notifier call, got {len(notifier_calls)}"
    n = notifier_calls[0]
    assert n["channel"] == "approval"
    assert n["severity"] == "high"
    assert "payment-demo-00010-abc" in n["body"]
    assert "https://driftscribe.example.com/approvals/id1?t=tok" in n["body"]
    assert "2026-01-01T00:15:00+00:00" in n["body"]
    # SECURITY: the reason (which may contain secrets) must NEVER appear in the body
    assert "SECRET-SENTINEL-do-not-leak" not in n["body"]
    # Tool return value still contains the worker response
    assert out["approval_id"] == "id1"
    assert out["approval_url"] == "https://driftscribe.example.com/approvals/id1?t=tok"


def test_propose_rollback_tool_safe_reason_still_sent_to_worker():
    """The existing safe_reason behavior must not regress: the worker payload
    still carries a safe reason derived from target_revision (not the model reason)."""
    from agent.adk_tools import propose_rollback_tool

    with patch("agent.adk_tools.worker_client.call") as m:
        m.return_value = _rollback_worker_response()
        propose_rollback_tool(
            target_revision="payment-demo-00010-abc",
            reason="SECRET-SENTINEL-do-not-leak",
        )

    # The first call is to "rollback" worker
    rollback_call = next(c for c in m.call_args_list if c.args[0] == "rollback")
    payload = rollback_call.args[1]
    assert payload["target_revision"] == "payment-demo-00010-abc"
    assert "payment-demo-00010-abc" in payload["reason"]
    assert "SECRET-SENTINEL-do-not-leak" not in payload["reason"]


@pytest.mark.parametrize(
    "worker_resp",
    [
        # missing approval_url
        {"approval_id": "id1", "approval_token": "tok", "expires_at": "2026-01-01T00:15:00+00:00"},
        # empty approval_url
        {"approval_id": "id1", "approval_url": "", "expires_at": "2026-01-01T00:15:00+00:00"},
        # non-str approval_url (None)
        {"approval_id": "id1", "approval_url": None, "expires_at": "2026-01-01T00:15:00+00:00"},
        # missing expires_at
        {"approval_id": "id1", "approval_url": "https://coord/approvals/id1?t=tok"},
        # empty expires_at
        {"approval_id": "id1", "approval_url": "https://coord/approvals/id1?t=tok", "expires_at": ""},
        # non-str expires_at (int)
        {"approval_id": "id1", "approval_url": "https://coord/approvals/id1?t=tok", "expires_at": 12345},
    ],
)
def test_propose_rollback_tool_no_notify_when_approval_fields_missing(monkeypatch, caplog, worker_resp):
    """Missing/empty/non-str approval_url or expires_at → ZERO notifier calls
    + WARNING rollback_propose_notify_failed."""
    import logging

    from agent.adk_tools import propose_rollback_tool

    notifier_calls = []

    def _fake_call(worker, payload):
        if worker == "notifier":
            notifier_calls.append(payload)
            return {"status": "sent"}
        return worker_resp

    with caplog.at_level(logging.WARNING, logger="driftscribe.agent.adk_tools"):
        with patch("agent.adk_tools.worker_client.call", side_effect=_fake_call):
            propose_rollback_tool(
                target_revision="payment-demo-00010-abc",
                reason="any reason",
            )

    assert notifier_calls == [], f"expected no notifier calls, got {notifier_calls}"
    assert any("rollback_propose_notify_failed" in r.message for r in caplog.records)


def test_propose_rollback_tool_worker_raises_no_notify():
    """If the rollback worker itself raises, no notifier call is made and the
    exception propagates (order pin)."""
    from agent.adk_tools import propose_rollback_tool
    from agent.worker_client import WorkerClientError

    notifier_calls = []

    def _fake_call(worker, payload):
        if worker == "rollback":
            raise WorkerClientError(503, "down", "rollback")
        notifier_calls.append(worker)
        return {}

    with patch("agent.adk_tools.worker_client.call", side_effect=_fake_call):
        with pytest.raises(WorkerClientError):
            propose_rollback_tool(
                target_revision="payment-demo-00010-abc",
                reason="reason",
            )

    assert notifier_calls == []


def test_propose_rollback_tool_notifier_raises_suppressed():
    """If the notifier raises, the exception is suppressed and the tool returns
    the worker response unchanged."""
    from agent.adk_tools import propose_rollback_tool
    from agent.worker_client import WorkerClientError

    def _fake_call(worker, payload):
        if worker == "rollback":
            return _rollback_worker_response()
        if worker == "notifier":
            raise WorkerClientError(503, "down", "notifier")
        return {}

    with patch("agent.adk_tools.worker_client.call", side_effect=_fake_call):
        out = propose_rollback_tool(
            target_revision="payment-demo-00010-abc",
            reason="reason",
        )

    assert out["approval_id"] == "id1"
