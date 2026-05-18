"""GitHub side-effect functions for DriftScribe.

Wraps PyGithub `Repository` operations behind small, testable functions.
Each operation accepts a `dry_run` flag — when true, no API calls are made
and a structured preview dict is returned instead.

Note: file commits are authored by the identity behind the configured
GITHUB_TOKEN. For the demo deploy that token is owned by ``theghostsquad00``;
downstream users should bind a bot identity if they want commits authored
under a service account name.
"""

import logging
from typing import Any

from github import Github, GithubException, UnknownObjectException
from github.Repository import Repository

_PREVIEW_CHARS = 4000
log = logging.getLogger(__name__)


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
    up to the first ``_PREVIEW_CHARS`` characters of ``new_content``.
    """
    if dry_run:
        preview = new_content[:_PREVIEW_CHARS]
        return {
            "dry_run": True,
            "url": None,
            "branch": branch,
            "preview": preview,
            "preview_truncated": len(new_content) > _PREVIEW_CHARS,
        }

    base_ref = repo.get_branch(base)
    repo.create_git_ref(ref=f"refs/heads/{branch}", sha=base_ref.commit.sha)

    try:
        existing = repo.get_contents(file_path, ref=branch)
    except UnknownObjectException:
        # 404 — file genuinely doesn't exist on the branch. Create it.
        repo.create_file(
            path=file_path,
            message=f"docs(driftscribe): initial {file_path}",
            content=new_content,
            branch=branch,
        )
    else:
        repo.update_file(
            path=file_path,
            message=f"docs(driftscribe): update {file_path}",
            content=new_content,
            sha=existing.sha,
            branch=branch,
        )

    pr = repo.create_pull(title=title, body=body, head=branch, base=base)
    # Labels are best-effort: if labeling fails (e.g. label doesn't exist yet),
    # the PR is still successfully created and we return its URL. Caller can
    # add labels manually.
    try:
        pr.add_to_labels("driftscribe", "docs")
    except GithubException as e:
        log.warning("failed to label PR #%s: %s", pr.number, e)
    return {"dry_run": False, "url": pr.html_url, "number": pr.number}
