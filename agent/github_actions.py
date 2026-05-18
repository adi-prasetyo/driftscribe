"""GitHub side-effect functions for DriftScribe.

Wraps PyGithub `Repository` operations behind small, testable functions.
Each operation accepts a `dry_run` flag — when true, no API calls are made
and a structured preview dict is returned instead.
"""

from typing import Any

from github import Github
from github.Repository import Repository


def get_repo(token: str, repo_full_name: str) -> Repository:
    """Return a PyGithub `Repository` for the given full name (e.g. ``owner/repo``)."""
    return Github(token).get_repo(repo_full_name)


def _issue_result(dry_run: bool, issue: Any = None, title: str = "") -> dict[str, Any]:
    if dry_run:
        return {"dry_run": True, "url": None, "title": title}
    return {"dry_run": False, "url": issue.html_url, "number": issue.number}


def open_drift_issue(
    repo: Repository, title: str, body: str, dry_run: bool
) -> dict[str, Any]:
    """Open a GitHub issue labeled ``driftscribe`` + ``drift``."""
    if dry_run:
        return _issue_result(True, title=title)
    issue = repo.create_issue(
        title=title, body=body, labels=["driftscribe", "drift"]
    )
    return _issue_result(False, issue=issue)


def open_escalation_issue(
    repo: Repository, title: str, body: str, dry_run: bool
) -> dict[str, Any]:
    """Open a GitHub issue labeled ``driftscribe`` + ``escalation``."""
    if dry_run:
        return _issue_result(True, title=title)
    issue = repo.create_issue(
        title=title, body=body, labels=["driftscribe", "escalation"]
    )
    return _issue_result(False, issue=issue)


def open_docs_pr(
    repo: Repository,
    branch: str,
    base: str,
    title: str,
    body: str,
    file_path: str,
    new_content: str,
    dry_run: bool,
) -> dict[str, Any]:
    """Create a branch off ``base``, update (or create) ``file_path``, and open a PR.

    On ``dry_run=True`` no API calls are made — a preview dict is returned with
    the first 500 characters of ``new_content``.
    """
    if dry_run:
        return {
            "dry_run": True,
            "url": None,
            "branch": branch,
            "preview": new_content[:500],
        }

    base_ref = repo.get_branch(base)
    repo.create_git_ref(ref=f"refs/heads/{branch}", sha=base_ref.commit.sha)

    try:
        existing = repo.get_contents(file_path, ref=branch)
        repo.update_file(
            path=file_path,
            message=f"docs(driftscribe): update {file_path}",
            content=new_content,
            sha=existing.sha,
            branch=branch,
        )
    except Exception:
        # PyGithub raises GithubException (a subclass of Exception) when the
        # file is missing on the branch — fall through to creating it.
        repo.create_file(
            path=file_path,
            message=f"docs(driftscribe): initial {file_path}",
            content=new_content,
            branch=branch,
        )

    pr = repo.create_pull(title=title, body=body, head=branch, base=base)
    pr.add_to_labels("driftscribe", "docs")
    return {"dry_run": False, "url": pr.html_url, "number": pr.number}
