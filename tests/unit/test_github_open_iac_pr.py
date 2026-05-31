"""Unit tests for driftscribe_lib.github.open_iac_pr (Phase D1-2).

Mirror ``test_github_actions.py``: mock PyGithub's ``Repository`` so no
network calls are made. ``open_iac_pr`` commits a LIST of file writes onto
ONE branch and opens ONE PR labeled ``driftscribe-infra`` (NOT ``docs``).
Covers: single branch ref off ``base``, create-vs-update routing per file
existence, multi-file -> multi-commit -> one create_pull, the infra label,
the return-dict shape, the create_pull already-exists idempotency backstop,
and the dry_run no-op preview.
"""

from unittest.mock import MagicMock

from github import GithubException, UnknownObjectException
from driftscribe_lib import github as gh


def _make_github_exception(status: int, message: str) -> GithubException:
    """Build a real GithubException carrying GitHub's error JSON shape."""
    return GithubException(status, {"message": message}, None)


def _fake_pr(number: int = 7, html_url: str = "https://github.test/pr/7"):
    """A minimal stand-in for a PyGithub PullRequest."""
    pr = MagicMock()
    pr.number = number
    pr.html_url = html_url
    return pr


def _fake_branch():
    branch = MagicMock()
    branch.commit.sha = "basesha123"
    return branch


def _fake_repo(*, branch_exists: bool = False, missing_paths: tuple = ()):
    """A Repository mock wired for the open_iac_pr happy + idempotent paths.

    ``missing_paths`` are paths that ``get_contents`` should 404 on (so the
    helper routes them to ``create_file``); everything else returns an object
    with a ``.sha`` (so they route to ``update_file``).
    """
    repo = MagicMock()
    repo.full_name = "owner/repo"
    repo.get_branch.return_value = _fake_branch()
    if branch_exists:
        repo.create_git_ref.side_effect = _make_github_exception(
            422, "Reference already exists"
        )

    def _get_contents(path, ref=None):
        if path in missing_paths:
            raise UnknownObjectException(404, {"message": "Not Found"}, None)
        existing = MagicMock()
        existing.sha = f"sha-{path}"
        return existing

    repo.get_contents.side_effect = _get_contents
    return repo


_FILES = [
    {"path": "iac/cloudrun.tf", "content": "resource a {}\n"},
    {"path": "iac/README.md", "content": "# docs\n"},
]


def _call(repo, *, branch="infra/add-x-20260601-ab12cd", files=None, dry_run=False):
    return gh.open_iac_pr(
        repo,
        branch=branch,
        base="main",
        title="Author infra change",
        body="body",
        files=files if files is not None else _FILES,
        dry_run=dry_run,
    )


class TestOpenIacPr:
    def test_creates_one_branch_off_base(self):
        repo = _fake_repo()
        repo.create_pull.return_value = _fake_pr()
        _call(repo)
        repo.create_git_ref.assert_called_once_with(
            ref="refs/heads/infra/add-x-20260601-ab12cd", sha="basesha123"
        )

    def test_routes_create_vs_update_per_existence(self):
        # README.md is missing on the branch -> create_file; cloudrun.tf exists -> update_file
        repo = _fake_repo(missing_paths=("iac/README.md",))
        repo.create_pull.return_value = _fake_pr()
        _call(repo)

        update_paths = {c.kwargs["path"] for c in repo.update_file.call_args_list}
        create_paths = {c.kwargs["path"] for c in repo.create_file.call_args_list}
        assert update_paths == {"iac/cloudrun.tf"}
        assert create_paths == {"iac/README.md"}
        # commits land on the feature branch, not on base; and existence is
        # probed on the feature branch (ref=branch), not base.
        for call in repo.update_file.call_args_list + repo.create_file.call_args_list:
            assert call.kwargs["branch"] == "infra/add-x-20260601-ab12cd"
            assert call.kwargs["message"] == f"feat(iac): author {call.kwargs['path']}"
        for call in repo.get_contents.call_args_list:
            assert call.kwargs["ref"] == "infra/add-x-20260601-ab12cd"
        # update_file gets the existing sha
        assert repo.update_file.call_args_list[0].kwargs["sha"] == "sha-iac/cloudrun.tf"

    def test_multiple_files_multiple_commits_one_pull(self):
        repo = _fake_repo()  # both files exist -> two update_file commits
        repo.create_pull.return_value = _fake_pr()
        _call(repo)
        assert repo.update_file.call_count == 2  # one commit per file
        repo.create_file.assert_not_called()
        repo.create_git_ref.assert_called_once()  # one branch
        repo.create_pull.assert_called_once_with(
            title="Author infra change",
            body="body",
            head="infra/add-x-20260601-ab12cd",
            base="main",
        )

    def test_applies_infra_label_not_docs(self):
        repo = _fake_repo()
        pr = _fake_pr()
        repo.create_pull.return_value = pr
        result = _call(repo)
        pr.add_to_labels.assert_called_once_with("driftscribe-infra")
        assert result["labeled"] is True

    def test_return_shape(self):
        repo = _fake_repo()
        repo.create_pull.return_value = _fake_pr(number=7, html_url="https://github.test/pr/7")
        result = _call(repo)
        assert result == {
            "url": "https://github.test/pr/7",
            "number": 7,
            "branch": "infra/add-x-20260601-ab12cd",
            "labeled": True,
            "reused": False,
        }

    def test_label_best_effort_on_failure(self):
        repo = _fake_repo()
        pr = _fake_pr()
        pr.add_to_labels.side_effect = _make_github_exception(403, "no perms")
        repo.create_pull.return_value = pr
        result = _call(repo)
        assert result["labeled"] is False
        assert result["reused"] is False

    def test_branch_already_exists_returns_existing_pr(self):
        # Re-run hits an already-created branch -> find the open PR, reuse it.
        repo = _fake_repo(branch_exists=True)
        existing = _fake_pr(number=9, html_url="https://github.test/pr/9")
        repo.get_pulls.return_value = iter([existing])
        result = _call(repo)
        assert result["reused"] is True
        assert result["number"] == 9
        repo.create_pull.assert_not_called()

    def test_dangling_branch_no_open_pr_writes_files_and_opens_fresh_pr(self):
        # Branch exists (create_git_ref 422) but NO open PR (dangling from a
        # prior failed run) -> fall through, (re)write the files on the branch,
        # and open a fresh PR (reused=False). Mirrors open_docs_pr's coverage.
        repo = _fake_repo(branch_exists=True)
        repo.get_pulls.return_value = iter([])  # no open PR for the head
        repo.create_pull.return_value = _fake_pr(number=15)
        result = _call(repo)
        assert result["reused"] is False
        assert result["number"] == 15
        assert repo.update_file.call_count == 2  # files (re)written on the branch
        repo.create_pull.assert_called_once()

    def test_create_pull_already_exists_returns_existing_pr(self):
        repo = _fake_repo()
        repo.create_pull.side_effect = _make_github_exception(
            422, "A pull request already exists for owner:branch"
        )
        existing = _fake_pr(number=12)
        repo.get_pulls.return_value = iter([existing])
        result = _call(repo)
        assert result["reused"] is True
        assert result["number"] == 12

    def test_dry_run_no_mutating_calls(self):
        repo = MagicMock()
        result = _call(repo, dry_run=True)
        assert result["dry_run"] is True
        assert result["branch"] == "infra/add-x-20260601-ab12cd"
        repo.create_git_ref.assert_not_called()
        repo.create_file.assert_not_called()
        repo.update_file.assert_not_called()
        repo.create_pull.assert_not_called()
