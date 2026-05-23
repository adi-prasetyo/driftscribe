"""GitHub helpers for E2E. PR-matching keyed off branch prefix 'upgrade/'."""
import os
import re

from github import Github


def _github_client():
    token = os.environ.get("DRIFTSCRIBE_E2E_GITHUB_TOKEN")
    if not token:
        raise RuntimeError("DRIFTSCRIBE_E2E_GITHUB_TOKEN required for GitHub E2E")
    return Github(token)


def _is_upgrade_pr(pr) -> bool:
    """True if this PR's branch matches the worker's stable prefix."""
    return pr.head.ref.startswith("upgrade/")


def list_open_upgrade_prs(repo: str):
    repo_obj = _github_client().get_repo(repo)
    return [pr for pr in repo_obj.get_pulls(state="open") if _is_upgrade_pr(pr)]


def count_open_upgrade_prs(repo: str) -> int:
    return len(list_open_upgrade_prs(repo))


def sweep_upgrade_prs(repo: str) -> None:
    """Close every open PR whose branch starts with 'upgrade/' and delete the branch."""
    repo_obj = _github_client().get_repo(repo)
    for pr in repo_obj.get_pulls(state="open"):
        if not _is_upgrade_pr(pr):
            continue
        branch = pr.head.ref
        try:
            pr.edit(state="closed")
        except Exception:
            pass
        try:
            repo_obj.get_git_ref(f"heads/{branch}").delete()
        except Exception:
            pass


def github_cleanup_pr(pr_url: str) -> None:
    match = re.match(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", pr_url)
    if not match:
        return
    repo_name, pr_num = match.group(1), int(match.group(2))
    repo = _github_client().get_repo(repo_name)
    pr = repo.get_pull(pr_num)
    branch = pr.head.ref
    try:
        pr.edit(state="closed")
    except Exception:
        pass
    try:
        repo.get_git_ref(f"heads/{branch}").delete()
    except Exception:
        pass
