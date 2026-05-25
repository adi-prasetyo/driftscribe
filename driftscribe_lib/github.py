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


class PrNotEligibleError(Exception):
    """A PR cannot be closed under the caller's eligibility policy.

    Carries a ``status_code`` so the worker boundary can map it to the
    right HTTP status (403 for a policy bounce — missing label / wrong
    branch / wrong base; 404 when the PR doesn't exist). Mirrors the
    transport-agnostic ``UpgradeValidationError`` pattern: the library
    stays framework-free and the worker converts to ``HTTPException``.
    """

    def __init__(self, reason: str, *, status_code: int = 403):
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


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


def close_pr(
    repo: Repository,
    *,
    pr_number: int,
    reason: str,
    dry_run: bool,
    required_label: str = "driftscribe",
    required_head_prefix: str | None = None,
    required_base: str | None = None,
) -> dict[str, Any]:
    """Close an open PR after proving it was opened by this system.

    Provenance gate (ALL must hold before any mutation): the PR must
    carry ``required_label``, its head ref must start with
    ``required_head_prefix`` (when given), and its base ref must equal
    ``required_base`` (when given). The single-repo PAT already bounds
    the blast radius to one repository; these checks add that the PR is
    one *this workload* produced, not an arbitrary collaborator's PR that
    happens to live in the same repo. A failing gate raises
    :class:`PrNotEligibleError` (403) and performs no write.

    Idempotent: an already-closed PR (that passes the gate) returns a
    success result without re-editing or commenting. The audit comment is
    posted *after* the close succeeds (best-effort) so a failed
    ``edit(state="closed")`` can't leave a misleading "Closed by …"
    comment behind.
    """
    if dry_run:
        return {"dry_run": True, "number": pr_number, "would_close": True}

    try:
        pr = repo.get_pull(pr_number)
    except UnknownObjectException as e:
        raise PrNotEligibleError(
            f"PR #{pr_number} not found", status_code=404
        ) from e

    labels = {lbl.name for lbl in pr.get_labels()}
    if required_label not in labels:
        raise PrNotEligibleError(
            f"PR #{pr_number} is not a DriftScribe PR "
            f"(missing {required_label!r} label)"
        )
    head_ref = pr.head.ref
    if required_head_prefix is not None and not head_ref.startswith(
        required_head_prefix
    ):
        raise PrNotEligibleError(
            f"PR #{pr_number} head {head_ref!r} is not a DriftScribe "
            f"branch (expected prefix {required_head_prefix!r})"
        )
    base_ref = pr.base.ref
    if required_base is not None and base_ref != required_base:
        raise PrNotEligibleError(
            f"PR #{pr_number} base {base_ref!r} is not {required_base!r}"
        )

    if pr.state == "closed":
        return {
            "dry_run": False,
            "closed": True,
            "already_closed": True,
            "url": pr.html_url,
            "number": pr.number,
            "comment_posted": False,
        }

    pr.edit(state="closed")

    comment_posted = True
    comment_error: str | None = None
    try:
        pr.create_issue_comment(f"Closed by DriftScribe: {reason}")
    except GithubException as e:
        comment_posted = False
        comment_error = str(e)
        log.warning("failed to comment on closed PR #%s: %s", pr.number, e)

    return {
        "dry_run": False,
        "closed": True,
        "already_closed": False,
        "url": pr.html_url,
        "number": pr.number,
        "reason": reason,
        "comment_posted": comment_posted,
        "comment_error": comment_error,
    }
