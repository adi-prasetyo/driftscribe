from unittest.mock import MagicMock

from github import GithubException, UnknownObjectException

from agent.github_actions import open_docs_pr, open_drift_issue, open_escalation_issue


def test_open_drift_issue_creates_labeled_issue():
    repo = MagicMock()
    open_drift_issue(repo, title="t", body="b", dry_run=False)
    repo.create_issue.assert_called_once()
    kw = repo.create_issue.call_args.kwargs
    assert "driftscribe" in kw["labels"]


def test_dry_run_skips_github_call():
    repo = MagicMock()
    res = open_drift_issue(repo, title="t", body="b", dry_run=True)
    repo.create_issue.assert_not_called()
    assert res["dry_run"] is True
    assert res["url"] is None


def test_open_escalation_issue_uses_escalation_label():
    repo = MagicMock()
    open_escalation_issue(repo, title="t", body="b", dry_run=False)
    kw = repo.create_issue.call_args.kwargs
    assert "escalation" in kw["labels"]
    assert "driftscribe" in kw["labels"]


def test_open_docs_pr_creates_branch_updates_file_and_opens_pr():
    repo = MagicMock()
    base = MagicMock()
    base.commit.sha = "sha-1"
    repo.get_branch.return_value = base
    existing = MagicMock()
    existing.sha = "file-sha"
    repo.get_contents.return_value = existing
    pr = MagicMock()
    pr.html_url = "https://...pull/42"
    pr.number = 42
    repo.create_pull.return_value = pr

    res = open_docs_pr(
        repo=repo,
        branch="b",
        base="main",
        title="t",
        body="b",
        file_path="demo/docs/runbook.md",
        new_content="content",
        dry_run=False,
    )
    repo.create_git_ref.assert_called_once()
    repo.update_file.assert_called_once()
    repo.create_pull.assert_called_once()
    # PyGithub's create_pull does not accept labels kwarg; labels are added separately.
    assert "labels" not in repo.create_pull.call_args.kwargs
    assert res["url"].endswith("pull/42")


def test_open_docs_pr_creates_file_if_not_existing():
    repo = MagicMock()
    base = MagicMock()
    base.commit.sha = "sha-1"
    repo.get_branch.return_value = base
    # get_contents raises 404 (file doesn't exist on the new branch)
    repo.get_contents.side_effect = UnknownObjectException(404, "not found", {})
    pr = MagicMock()
    pr.html_url = "https://x/pull/7"
    pr.number = 7
    repo.create_pull.return_value = pr

    res = open_docs_pr(
        repo=repo,
        branch="b",
        base="main",
        title="t",
        body="b",
        file_path="docs/new.md",
        new_content="hi",
        dry_run=False,
    )
    repo.create_file.assert_called_once()
    repo.update_file.assert_not_called()
    assert res["url"].endswith("pull/7")


def test_open_docs_pr_propagates_non_404_errors_from_get_contents():
    # A rate-limit or 5xx must NOT silently fall through to create_file
    repo = MagicMock()
    base = MagicMock(); base.commit.sha = "sha-1"
    repo.get_branch.return_value = base
    repo.get_contents.side_effect = GithubException(403, "rate limit", {})

    import pytest
    with pytest.raises(GithubException):
        open_docs_pr(
            repo=repo, branch="b", base="main", title="t", body="b",
            file_path="x.md", new_content="y", dry_run=False,
        )
    repo.create_file.assert_not_called()


def test_open_docs_pr_returns_url_even_when_labeling_fails():
    # Labeling is best-effort — a label that doesn't exist yet shouldn't lose
    # the PR URL.
    repo = MagicMock()
    base = MagicMock(); base.commit.sha = "sha-1"
    repo.get_branch.return_value = base
    existing = MagicMock(); existing.sha = "file-sha"
    repo.get_contents.return_value = existing
    pr = MagicMock()
    pr.html_url = "https://x/pull/99"
    pr.number = 99
    pr.add_to_labels.side_effect = GithubException(422, "label not found", {})
    repo.create_pull.return_value = pr

    res = open_docs_pr(
        repo=repo, branch="b", base="main", title="t", body="b",
        file_path="docs/r.md", new_content="content", dry_run=False,
    )
    assert res["url"].endswith("pull/99")
    assert res["number"] == 99
    assert res["labeled"] is False
    assert res["label_error"]  # non-empty


def test_open_docs_pr_dry_run_returns_preview():
    repo = MagicMock()
    res = open_docs_pr(
        repo=repo,
        branch="driftscribe/x",
        base="main",
        title="t",
        body="b",
        file_path="demo/docs/runbook.md",
        new_content="the patched runbook content",
        dry_run=True,
    )
    repo.create_git_ref.assert_not_called()
    assert res["dry_run"] is True
    assert res["url"] is None
    assert res["branch"] == "driftscribe/x"
    assert "the patched runbook content" in res["preview"]
