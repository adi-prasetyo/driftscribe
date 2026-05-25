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
    base = MagicMock()
    base.commit.sha = "sha-1"
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
    base = MagicMock()
    base.commit.sha = "sha-1"
    repo.get_branch.return_value = base
    existing = MagicMock()
    existing.sha = "file-sha"
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


def _ref_exists_exc() -> GithubException:
    # Shape of GitHub's create_git_ref 422 when the branch already exists.
    return GithubException(422, {"message": "Reference already exists"}, {})


def test_open_docs_pr_reuses_existing_pr_when_branch_exists():
    # Deterministic upgrade branch + open PR already present: a re-run must
    # return the existing PR (idempotent), NOT 422/500. The file is left
    # untouched and create_pull is never called.
    repo = MagicMock()
    repo.full_name = "owner/repo"
    base = MagicMock()
    base.commit.sha = "sha-1"
    repo.get_branch.return_value = base
    repo.create_git_ref.side_effect = _ref_exists_exc()
    existing_pr = MagicMock()
    existing_pr.html_url = "https://x/pull/1"
    existing_pr.number = 1
    repo.get_pulls.return_value = [existing_pr]

    res = open_docs_pr(
        repo=repo, branch="upgrade/lodash-4-17-21", base="main",
        title="t", body="b", file_path="demo/upgrade-target/package.json",
        new_content="{}", dry_run=False,
    )

    repo.create_pull.assert_not_called()
    repo.create_file.assert_not_called()
    repo.update_file.assert_not_called()
    existing_pr.add_to_labels.assert_called_once()  # labels run for reused PRs too
    assert res["url"].endswith("pull/1")
    assert res["number"] == 1
    assert res["reused"] is True


def test_open_docs_pr_create_pull_backstop_returns_existing_pr():
    # Branch creation succeeds but a PR already exists for the head (race or
    # dangling state) — create_pull's 422 backstop returns the existing PR.
    repo = MagicMock()
    repo.full_name = "owner/repo"
    base = MagicMock()
    base.commit.sha = "sha-1"
    repo.get_branch.return_value = base
    existing = MagicMock()
    existing.sha = "file-sha"
    repo.get_contents.return_value = existing
    repo.create_pull.side_effect = GithubException(
        422, {"message": "A pull request already exists for owner:branch."}, {}
    )
    existing_pr = MagicMock()
    existing_pr.html_url = "https://x/pull/5"
    existing_pr.number = 5
    repo.get_pulls.return_value = [existing_pr]

    res = open_docs_pr(
        repo=repo, branch="upgrade/lodash-4-17-21", base="main",
        title="t", body="b", file_path="p.json", new_content="{}", dry_run=False,
    )

    assert res["reused"] is True
    assert res["url"].endswith("pull/5")


def test_open_docs_pr_dangling_branch_opens_fresh_pr():
    # Branch exists but NO open PR (dangling) — fall through, update the file,
    # and open a new PR (reused=False).
    repo = MagicMock()
    repo.full_name = "owner/repo"
    base = MagicMock()
    base.commit.sha = "sha-1"
    repo.get_branch.return_value = base
    repo.create_git_ref.side_effect = _ref_exists_exc()
    repo.get_pulls.return_value = []  # no existing PR
    existing = MagicMock()
    existing.sha = "file-sha"
    repo.get_contents.return_value = existing
    pr = MagicMock()
    pr.html_url = "https://x/pull/8"
    pr.number = 8
    repo.create_pull.return_value = pr

    res = open_docs_pr(
        repo=repo, branch="upgrade/lodash-4-17-21", base="main",
        title="t", body="b", file_path="p.json", new_content="{}", dry_run=False,
    )

    repo.update_file.assert_called_once()
    repo.create_pull.assert_called_once()
    assert res["reused"] is False
    assert res["url"].endswith("pull/8")


def test_open_docs_pr_propagates_unrelated_422_from_create_git_ref():
    # A 422 that is NOT "already exists" (e.g. invalid sha) must propagate,
    # not be mistaken for the idempotent path.
    import pytest
    repo = MagicMock()
    base = MagicMock()
    base.commit.sha = "sha-1"
    repo.get_branch.return_value = base
    repo.create_git_ref.side_effect = GithubException(
        422, {"message": "Invalid request. sha is not a valid SHA."}, {}
    )

    with pytest.raises(GithubException):
        open_docs_pr(
            repo=repo, branch="b", base="main", title="t", body="b",
            file_path="x.md", new_content="y", dry_run=False,
        )
    repo.create_pull.assert_not_called()
