"""Duplicate-adoption guard: propose_adoption_tool refuses a second adoption of a
resource that already has an OPEN adoption PR (defense in depth alongside the
Infra-panel UI guard). The probe itself is fail-OPEN: a GitHub hiccup never blocks
provisioning.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

_PROJECT = "driftscribe-hack-2026"


def _happy_path_setup(monkeypatch, adk_tools, worker_calls):
    """Wire the propose_adoption happy path so execution reaches the dupe guard
    (which sits AFTER preflight_conflicts and BEFORE the PR is opened)."""

    def _fake_worker(*, target_repo, branch, title, body, files, dispatch_plan_builder=False):
        worker_calls.append(dict(title=title, files=files))
        return {"status": "opened", "pr_number": 42, "pr_url": "https://u/42", "branch": branch}

    def _fake_fetch(target_repo):
        return {"iac/variables.tf": f'variable "project_id" {{\n  default = "{_PROJECT}"\n}}\n'}

    monkeypatch.setattr(adk_tools.worker_client, "call_open_infra_pr", _fake_worker)
    monkeypatch.setattr(adk_tools, "_fetch_main_iac_tree", _fake_fetch)
    monkeypatch.setattr(adk_tools, "notify_iac_pr_pending", lambda *a, **kw: None)
    monkeypatch.setattr(
        "agent.config.Settings.gcp_project",
        property(lambda self: _PROJECT),
        raising=False,
    )


def test_rejects_when_open_adopt_pr_exists(monkeypatch):
    from agent import adk_tools

    worker_calls: list = []
    _happy_path_setup(monkeypatch, adk_tools, worker_calls)
    monkeypatch.setattr(adk_tools, "find_open_adopt_pr_for_resource", lambda at, rn, repo: 168)

    result = adk_tools.propose_adoption_tool("google_pubsub_topic", "my-topic")

    assert result["status"] == "rejected"
    assert "168" in result["reason"]
    assert len(worker_calls) == 0  # never opened a duplicate


def test_proceeds_when_no_open_adopt_pr(monkeypatch):
    from agent import adk_tools

    worker_calls: list = []
    _happy_path_setup(monkeypatch, adk_tools, worker_calls)
    monkeypatch.setattr(adk_tools, "find_open_adopt_pr_for_resource", lambda at, rn, repo: None)

    result = adk_tools.propose_adoption_tool("google_pubsub_topic", "my-topic")

    assert result["status"] == "opened"
    assert len(worker_calls) == 1


def test_find_open_adopt_pr_fails_open_on_github_error(monkeypatch):
    from agent import adk_tools

    def boom(*a, **k):
        raise RuntimeError("github down")

    monkeypatch.setattr(adk_tools, "get_repo", boom)
    # Any GitHub error inside the probe → None (never blocks provisioning).
    assert (
        adk_tools.find_open_adopt_pr_for_resource(
            "pubsub.googleapis.com/Topic", "my-topic", "owner/repo"
        )
        is None
    )


def test_find_open_adopt_pr_matches_resource_identity(monkeypatch):
    from agent import adk_tools

    issues = [
        SimpleNamespace(
            number=168,
            title="Adopt topic",
            body="**Import id:** `projects/p/topics/my-topic`",
            html_url="https://gh/168",
            pull_request=SimpleNamespace(),
        ),
    ]
    fake_repo = SimpleNamespace(get_issues=lambda **kw: issues)
    monkeypatch.setattr(adk_tools, "get_repo", lambda *a, **k: fake_repo)

    # Same resource identity → returns the open PR number.
    assert (
        adk_tools.find_open_adopt_pr_for_resource(
            "pubsub.googleapis.com/Topic", "my-topic", "owner/repo"
        )
        == 168
    )
    # Different resource → no match.
    assert (
        adk_tools.find_open_adopt_pr_for_resource(
            "pubsub.googleapis.com/Topic", "other", "owner/repo"
        )
        is None
    )
    # Blank identity or blank repo → never probes, no match.
    assert adk_tools.find_open_adopt_pr_for_resource("", "my-topic", "owner/repo") is None
    assert (
        adk_tools.find_open_adopt_pr_for_resource("pubsub.googleapis.com/Topic", "my-topic", "")
        is None
    )


def test_find_open_adopt_pr_queries_the_given_repo(monkeypatch):
    """The probe lists issues from the repo it is handed (the iac-editor target),
    not the deployment's settings.github_repo (Codex review)."""
    from agent import adk_tools

    captured: dict = {}

    def fake_get_repo(token, repo):
        captured["repo"] = repo
        return SimpleNamespace(get_issues=lambda **kw: [])

    monkeypatch.setattr(adk_tools, "get_repo", fake_get_repo)
    adk_tools.find_open_adopt_pr_for_resource(
        "pubsub.googleapis.com/Topic", "my-topic", "owner/editor-target"
    )
    assert captured["repo"] == "owner/editor-target"


def test_dupe_guard_checks_the_editor_target_repo_not_github_repo(monkeypatch):
    """The dupe lookup must query the repo the PR will OPEN against (the iac-editor
    target), not the deployment's GITHUB_REPO — they can diverge via
    IAC_EDITOR_TARGET_REPO_OVERRIDE (Codex review)."""
    from agent import adk_tools
    from agent.workloads.registry import resolve_iac_editor_target

    monkeypatch.setenv("IAC_EDITOR_TARGET_REPO_OVERRIDE", "owner/editor-target")
    monkeypatch.setattr(
        "agent.config.Settings.github_repo",
        property(lambda self: "owner/deployment-repo"),
        raising=False,
    )
    worker_calls: list = []
    _happy_path_setup(monkeypatch, adk_tools, worker_calls)

    captured: dict = {}
    monkeypatch.setattr(
        adk_tools,
        "find_open_adopt_pr_for_resource",
        lambda at, rn, repo: captured.update(repo=repo) or None,
    )

    adk_tools.propose_adoption_tool("google_pubsub_topic", "my-topic")

    # The guard was handed the editor-target repo (where the PR opens), NOT the
    # deployment's github_repo.
    assert captured["repo"] == resolve_iac_editor_target()
    assert captured["repo"] == "owner/editor-target"
