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
import time
from typing import Any

from github import Github, GithubException, UnknownObjectException
from github.Repository import Repository

_PREVIEW_CHARS = 4000
log = logging.getLogger(__name__)

# Mergeability is computed asynchronously by GitHub — the first read of
# ``pull.mergeable`` right after a PR is opened (or right after a push)
# can be ``None`` while a background job runs. Bound the wait so a /chat
# turn never becomes a polling loop: 3 attempts × 1.5s ≈ 4.5s, well
# inside the coordinator→worker 30s httpx timeout. If it's still unknown
# we refuse and tell the operator to retry rather than merging blind.
_MERGE_MERGEABILITY_RETRIES = 3
_MERGE_MERGEABILITY_DELAY = 1.5

# ``mergeable_state`` values we are willing to merge through — an
# ALLOWLIST, so an unrecognized or future GitHub state fails closed (this
# mutates ``main``, so "unknown == refuse" is the safe default):
#   - ``clean``: mergeable, all checks green — the happy path.
#   - ``unstable``: mergeable, but some check is non-green. We allow it
#     ONLY because the explicit required-check verification below governs
#     which checks must be green; a *non-required* check failing must not
#     block a merge whose required checks pass. (The required-check gate
#     still runs, so an ``unstable`` PR whose ``lint-test`` is red is
#     refused there, not here.)
# Everything else is refused as 409: ``dirty`` (conflict), ``behind``
# (out of date), ``blocked`` (branch protection / required review — we do
# NOT bypass it even though the human PAT could), ``unknown`` (mergeability
# not yet computed), ``has_hooks``, ``draft``, ``None``, or any state
# GitHub adds later.
_MERGE_ALLOWED_STATES = frozenset({"clean", "unstable"})


class PrNotEligibleError(Exception):
    """A PR fails the static *provenance* gate (not one DriftScribe owns).

    Carries a ``status_code`` so the worker boundary can map it to the
    right HTTP status (403 for a policy bounce — missing label / wrong
    branch / wrong base; 404 when the PR doesn't exist). Mirrors the
    transport-agnostic ``UpgradeValidationError`` pattern: the library
    stays framework-free and the worker converts to ``HTTPException``.

    Distinct from :class:`PrMergeBlockedError`: this is about *whether the
    PR is ours to touch at all*; that one is about *dynamic merge state*.
    """

    def __init__(self, reason: str, *, status_code: int = 403):
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


class PrMergeBlockedError(Exception):
    """A PR passes provenance but cannot be merged *right now*.

    Dynamic, retry-able conditions: checks pending / failed / missing,
    merge conflict, head behind base, branch protection ``blocked``,
    draft PR, closed-unmerged PR, mergeability still computing, or a
    head-SHA race that GitHub rejects at merge time. Defaults to **409
    Conflict** — the operator can act (rerun CI, rebase, retry) and try
    again. Kept separate from :class:`PrNotEligibleError` so the worker
    maps "not yours" (403/404) and "not yet" (409) to different statuses
    and the chat surface can word them differently.
    """

    def __init__(self, reason: str, *, status_code: int = 409):
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


def _assert_pr_eligible(
    pr: Any,
    *,
    required_label: str,
    required_head_prefix: str | None,
    required_base: str | None,
) -> None:
    """Raise :class:`PrNotEligibleError` (403) unless ``pr`` is one this
    system produced.

    Shared provenance gate for every mutation that targets an existing PR
    (:func:`close_pr`, :func:`merge_pr`). ALL conditions must hold: the PR
    carries ``required_label``, its head ref starts with
    ``required_head_prefix`` (when given), and its base ref equals
    ``required_base`` (when given). The single-repo PAT already bounds the
    blast radius to one repository; this adds that the PR is one *this
    workload* produced, not an arbitrary collaborator's PR in the same
    repo. Performs no write — callers gate on this before mutating.
    """
    labels = {lbl.name for lbl in pr.get_labels()}
    if required_label not in labels:
        raise PrNotEligibleError(
            f"PR #{pr.number} is not a DriftScribe PR "
            f"(missing {required_label!r} label)"
        )
    head_ref = pr.head.ref
    if required_head_prefix is not None and not head_ref.startswith(
        required_head_prefix
    ):
        raise PrNotEligibleError(
            f"PR #{pr.number} head {head_ref!r} is not a DriftScribe "
            f"branch (expected prefix {required_head_prefix!r})"
        )
    base_ref = pr.base.ref
    if required_base is not None and base_ref != required_base:
        raise PrNotEligibleError(
            f"PR #{pr.number} base {base_ref!r} is not {required_base!r}"
        )


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

    Provenance gate (:func:`_assert_pr_eligible`, ALL must hold before any
    mutation): the PR must carry ``required_label``, its head ref must
    start with ``required_head_prefix`` (when given), and its base ref
    must equal ``required_base`` (when given). A failing gate raises
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

    _assert_pr_eligible(
        pr,
        required_label=required_label,
        required_head_prefix=required_head_prefix,
        required_base=required_base,
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


def _check_run_order_key(cr: Any) -> float:
    """Sortable recency key for a check run.

    Prefer ``completed_at`` (final-state time), fall back to
    ``started_at``; a run with neither sorts oldest. Returns a POSIX
    timestamp (float) rather than the datetime itself so we never compare
    a tz-aware GitHub datetime against a naive sentinel.
    """
    ts = getattr(cr, "completed_at", None) or getattr(cr, "started_at", None)
    return ts.timestamp() if ts is not None else float("-inf")


def _latest_check_runs(check_runs: Any) -> dict[str, Any]:
    """Collapse a commit's check runs to the latest run per check name.

    GitHub returns one entry per *attempt*, so a re-run of ``lint-test``
    appears twice; we keep only the most recent (see
    :func:`_check_run_order_key`). All runs are already scoped to a single
    head SHA by the caller (``get_commit(sha).get_check_runs()``), so this
    only has to disambiguate re-runs, not different commits.
    """
    latest: dict[str, Any] = {}
    for cr in check_runs:
        prev = latest.get(cr.name)
        if prev is None or _check_run_order_key(cr) >= _check_run_order_key(prev):
            latest[cr.name] = cr
    return latest


def _assert_merge_preconditions(
    pr: Any,
    *,
    pr_number: int,
    required_label: str,
    required_head_prefix: str | None,
    required_base: str | None,
) -> dict[str, Any] | None:
    """Static merge gates, run on initial fetch AND re-run post-retry.

    Provenance (:func:`_assert_pr_eligible`) then open/non-draft state.
    Returns the idempotent already-merged success dict when ``pr`` is
    already merged (caller short-circuits), else ``None``. Raises
    :class:`PrNotEligibleError` (provenance) or :class:`PrMergeBlockedError`
    (closed-unmerged / draft). Idempotent — safe to call twice on the same
    object, which is the point: the second call re-authorizes the freshest
    PR after the async-mergeability retry may have re-fetched it.
    """
    _assert_pr_eligible(
        pr,
        required_label=required_label,
        required_head_prefix=required_head_prefix,
        required_base=required_base,
    )
    if pr.merged:
        return {
            "dry_run": False,
            "merged": True,
            "already_merged": True,
            "url": pr.html_url,
            "number": pr.number,
            "comment_posted": False,
        }
    if pr.state == "closed":
        raise PrMergeBlockedError(
            f"PR #{pr_number} is closed and was not merged"
        )
    if pr.draft:
        raise PrMergeBlockedError(f"PR #{pr_number} is a draft")
    return None


def merge_pr(
    repo: Repository,
    *,
    pr_number: int,
    dry_run: bool,
    merge_method: str,
    required_checks: Any,
    required_label: str = "driftscribe",
    required_head_prefix: str | None = None,
    required_base: str | None = None,
) -> dict[str, Any]:
    """Merge an upgrade PR this system opened, fail-closed.

    Two gates, two error classes:

    - **Provenance** (:func:`_assert_pr_eligible` → :class:`PrNotEligibleError`,
      403/404): the PR must carry ``required_label``, sit on a
      ``required_head_prefix`` branch, and target ``required_base`` — i.e.
      be a PR *this workload* produced, not an arbitrary collaborator's.
    - **Merge-readiness** (:class:`PrMergeBlockedError`, 409): the PR must
      be open, non-draft, have no conflict / not be ``behind`` / not be
      protection-``blocked``, have resolved mergeability, and every check
      in ``required_checks`` must have *completed successfully* on the
      current head SHA.

    The merge passes ``sha=head_sha`` so a push to the branch between the
    readiness check and the merge is rejected by GitHub. The base branch
    can still move after checks pass and before merge — a residual race
    that only "require branches up to date" branch protection or a merge
    queue can fully close; out of scope here (no protection on the demo
    repo). The merge method is fixed by the caller (deploy policy), never
    LLM-chosen.

    Idempotent: an already-merged PR (passing provenance) returns success
    without re-merging. The audit comment is posted *after* the merge
    succeeds (best-effort) so a failed merge can't leave a misleading
    comment.
    """
    if dry_run:
        return {"dry_run": True, "number": pr_number, "would_merge": True}

    try:
        pr = repo.get_pull(pr_number)
    except UnknownObjectException as e:
        raise PrNotEligibleError(
            f"PR #{pr_number} not found", status_code=404
        ) from e

    early = _assert_merge_preconditions(
        pr,
        pr_number=pr_number,
        required_label=required_label,
        required_head_prefix=required_head_prefix,
        required_base=required_base,
    )
    if early is not None:
        return early

    # Cheap config guard before any further GitHub round-trips. The worker
    # also fails fast at boot on an empty set; this keeps the lib safe to
    # call standalone — an empty allowlist means "nothing proves green",
    # so we must refuse rather than merge unverified.
    required = set(required_checks)
    if not required:
        raise PrMergeBlockedError(
            "no required checks configured — merge disabled"
        )

    # Mergeability is computed async; poll briefly rather than merge blind.
    mergeable = pr.mergeable
    attempts = 0
    while mergeable is None and attempts < _MERGE_MERGEABILITY_RETRIES:
        time.sleep(_MERGE_MERGEABILITY_DELAY)
        pr = repo.get_pull(pr_number)
        mergeable = pr.mergeable
        attempts += 1
    if mergeable is None:
        raise PrMergeBlockedError(
            f"PR #{pr_number} mergeability is still computing; retry shortly"
        )

    # Re-authorize on the freshest PR object. The retry loop may have
    # re-fetched, and a PR can be retargeted (base moved off ``main``),
    # closed, marked draft, or merged by someone else during our wait.
    # ``sha=head_sha`` at merge time only guards the HEAD — it does NOT
    # guard label / base / state — so the static gates must run again on
    # the current object before we read checks and merge. (A no-op when
    # the loop never re-fetched; the gates are idempotent.)
    early = _assert_merge_preconditions(
        pr,
        pr_number=pr_number,
        required_label=required_label,
        required_head_prefix=required_head_prefix,
        required_base=required_base,
    )
    if early is not None:
        return early

    # Allowlist (fail-closed): only ``clean`` / ``unstable`` proceed — see
    # :data:`_MERGE_ALLOWED_STATES`. An unrecognized or future state is
    # refused rather than merged blind into ``main``.
    state = pr.mergeable_state
    if mergeable is not True:
        raise PrMergeBlockedError(
            f"PR #{pr_number} is not mergeable (state={state!r})"
        )
    if state not in _MERGE_ALLOWED_STATES:
        raise PrMergeBlockedError(
            f"PR #{pr_number} cannot be merged in state {state!r}"
        )

    head_sha = pr.head.sha
    runs = _latest_check_runs(repo.get_commit(head_sha).get_check_runs())
    for name in sorted(required):
        cr = runs.get(name)
        if cr is None:
            raise PrMergeBlockedError(
                f"required check {name!r} has not reported on "
                f"{head_sha[:7]} yet"
            )
        if cr.status != "completed":
            raise PrMergeBlockedError(
                f"required check {name!r} is still {cr.status!r}"
            )
        if cr.conclusion != "success":
            raise PrMergeBlockedError(
                f"required check {name!r} concluded {cr.conclusion!r}, "
                "not 'success'"
            )

    try:
        result = pr.merge(merge_method=merge_method, sha=head_sha)
    except GithubException as e:
        # Covers the head-SHA race (409 — branch moved after our read), a
        # repo that disallows the chosen merge method (405), and any other
        # state change between read and merge. All map to a single 409
        # "not right now"; the real status is logged for diagnosis.
        data = e.data if isinstance(e.data, dict) else {}
        detail = data.get("message") or str(e)
        log.warning(
            "merge refused for PR #%s (github %s): %s",
            pr_number, e.status, detail,
        )
        raise PrMergeBlockedError(
            f"GitHub refused the merge: {detail}"
        ) from e

    if not result.merged:
        raise PrMergeBlockedError(
            f"merge was not completed: {result.message or 'unknown reason'}"
        )

    comment_posted = True
    comment_error: str | None = None
    try:
        pr.create_issue_comment(f"Merged by DriftScribe ({merge_method}).")
    except GithubException as e:
        comment_posted = False
        comment_error = str(e)
        log.warning("failed to comment on merged PR #%s: %s", pr.number, e)

    return {
        "dry_run": False,
        "merged": True,
        "already_merged": False,
        "url": pr.html_url,
        "number": pr.number,
        "sha": result.sha,
        "merge_method": merge_method,
        "comment_posted": comment_posted,
        "comment_error": comment_error,
    }
