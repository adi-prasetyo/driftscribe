"""GitHub side-effect functions for DriftScribe.

Wraps PyGithub `Repository` operations behind small, testable functions.
Each operation accepts a `dry_run` flag — when true, no API calls are made
and a structured preview dict is returned instead.

Note: file commits are authored by the identity behind the configured
GITHUB_TOKEN. For the hackathon deploy that token is owned by ``adi-prasetyo``;
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


def _is_already_exists(e: GithubException) -> bool:
    """True if a GithubException is GitHub's "already exists" 422.

    Scoped narrowly (status 422 AND an "already exists" message) so it only
    matches the two idempotent cases we care about — ``create_git_ref``
    ("Reference already exists") and ``create_pull`` ("A pull request already
    exists for …") — and never swallows an unrelated 422 (e.g. a validation
    error on the PR body).
    """
    if e.status != 422:
        return False
    data = e.data if isinstance(e.data, dict) else {}
    blob = f"{data.get('message', '')} {data.get('errors', '')}".lower()
    return "already exists" in blob


def _find_open_pr_for_head(repo: Repository, branch: str) -> Any:
    """Return the first open PR whose head is ``branch``, or ``None``.

    ``get_pulls`` wants the head as ``owner:ref``; the owner comes from
    ``repo.full_name`` (already loaded) rather than ``repo.owner`` (a possible
    lazy API fetch). Works for both user- and org-owned repos.
    """
    owner = repo.full_name.split("/")[0]
    for pr in repo.get_pulls(state="open", head=f"{owner}:{branch}"):
        return pr
    return None


def _finalize_pr(pr: Any, reused: bool) -> dict[str, Any]:
    """Best-effort label, then build the standard PR result dict.

    Run for BOTH freshly-created and reused PRs (``add_to_labels`` is
    idempotent), so a reused PR's ``labeled`` reflects a real attempt rather
    than an optimistic assumption.
    """
    labeled = True
    label_error: str | None = None
    try:
        pr.add_to_labels("driftscribe", "docs")
    except GithubException as e:
        labeled = False
        label_error = str(e)
        log.warning("failed to label PR #%s: %s", pr.number, e)
    return {
        "dry_run": False,
        "url": pr.html_url,
        "number": pr.number,
        "labeled": labeled,
        "label_error": label_error,
        "reused": reused,
    }


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
    try:
        repo.create_git_ref(ref=f"refs/heads/{branch}", sha=base_ref.commit.sha)
    except GithubException as e:
        # Idempotency: the upgrade workload derives a DETERMINISTIC branch
        # name (``upgrade/{pkg}-{ver}``), so re-running the same upgrade hits
        # an already-created branch and GitHub answers create_git_ref with
        # 422 "Reference already exists". Treat that as "already proposed":
        # if an open PR exists for the branch, return it unchanged rather than
        # 500-ing the worker (which the coordinator maps to a 502 on /chat).
        # We deliberately do NOT rewrite the open PR's branch on a retry.
        if not _is_already_exists(e):
            raise
        existing_pr = _find_open_pr_for_head(repo, branch)
        if existing_pr is not None:
            return _finalize_pr(existing_pr, reused=True)
        # Branch exists but no open PR (a dangling branch from a prior failed
        # run) — fall through to update the file and open a fresh PR.

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

    try:
        pr = repo.create_pull(title=title, body=body, head=branch, base=base)
        reused = False
    except GithubException as e:
        # Backstop for the dangling-branch path above (and any create race):
        # GitHub rejects a duplicate PR for the same head with 422 "A pull
        # request already exists". Return the existing one rather than failing.
        if not _is_already_exists(e):
            raise
        pr = _find_open_pr_for_head(repo, branch)
        if pr is None:
            raise
        reused = True

    # Labels are best-effort (see _finalize_pr) and run for new + reused PRs.
    return _finalize_pr(pr, reused=reused)
