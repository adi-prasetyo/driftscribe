import datetime as dt
from unittest.mock import MagicMock

import pytest
from github import GithubException, UnknownObjectException

from agent.github_actions import open_docs_pr, open_drift_issue, open_escalation_issue
from driftscribe_lib.github import (
    PrMergeBlockedError,
    PrNotEligibleError,
    close_pr,
    merge_pr,
)


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


# close_pr -------------------------------------------------------------- #


def _label(name: str) -> MagicMock:
    # MagicMock(name=...) sets the mock's *repr* name, not a .name attr —
    # set it explicitly so {lbl.name for lbl in ...} works.
    m = MagicMock()
    m.name = name
    return m


def _eligible_pr(state: str = "open") -> MagicMock:
    """An open PR that passes the default upgrade-workload gate:
    driftscribe label + upgrade/ head + main base."""
    pr = MagicMock()
    pr.get_labels.return_value = [_label("driftscribe"), _label("docs")]
    pr.head.ref = "upgrade/lodash-4-17-21"
    pr.base.ref = "main"
    pr.state = state
    pr.html_url = "https://github.com/owner/repo/pull/1"
    pr.number = 1
    return pr


def _close_kwargs(**overrides):
    base = dict(
        pr_number=1,
        reason="superseded by manual bump",
        dry_run=False,
        required_label="driftscribe",
        required_head_prefix="upgrade/",
        required_base="main",
    )
    base.update(overrides)
    return base


def test_close_pr_dry_run_returns_preview_without_api_calls():
    repo = MagicMock()
    res = close_pr(repo, **_close_kwargs(dry_run=True))
    repo.get_pull.assert_not_called()
    assert res == {"dry_run": True, "number": 1, "would_close": True}


def test_close_pr_closes_eligible_open_pr_and_comments():
    repo = MagicMock()
    pr = _eligible_pr()
    repo.get_pull.return_value = pr

    res = close_pr(repo, **_close_kwargs())

    pr.edit.assert_called_once_with(state="closed")
    pr.create_issue_comment.assert_called_once()
    assert "superseded by manual bump" in pr.create_issue_comment.call_args.args[0]
    assert res["closed"] is True
    assert res["already_closed"] is False
    assert res["comment_posted"] is True
    assert res["url"].endswith("pull/1")
    assert res["number"] == 1


def test_close_pr_refuses_pr_missing_required_label():
    repo = MagicMock()
    pr = _eligible_pr()
    pr.get_labels.return_value = [_label("docs")]  # no driftscribe label
    repo.get_pull.return_value = pr

    with pytest.raises(PrNotEligibleError) as exc:
        close_pr(repo, **_close_kwargs())
    assert exc.value.status_code == 403
    pr.edit.assert_not_called()


def test_close_pr_refuses_wrong_head_prefix():
    repo = MagicMock()
    pr = _eligible_pr()
    pr.head.ref = "feature/random"  # not an upgrade/ branch
    repo.get_pull.return_value = pr

    with pytest.raises(PrNotEligibleError) as exc:
        close_pr(repo, **_close_kwargs())
    assert exc.value.status_code == 403
    pr.edit.assert_not_called()


def test_close_pr_refuses_wrong_base():
    repo = MagicMock()
    pr = _eligible_pr()
    pr.base.ref = "production"  # not main
    repo.get_pull.return_value = pr

    with pytest.raises(PrNotEligibleError) as exc:
        close_pr(repo, **_close_kwargs())
    assert exc.value.status_code == 403
    pr.edit.assert_not_called()


def test_close_pr_raises_404_when_pr_not_found():
    repo = MagicMock()
    repo.get_pull.side_effect = UnknownObjectException(404, "not found", {})

    with pytest.raises(PrNotEligibleError) as exc:
        close_pr(repo, **_close_kwargs(pr_number=999))
    assert exc.value.status_code == 404


def test_close_pr_idempotent_when_already_closed():
    repo = MagicMock()
    pr = _eligible_pr(state="closed")
    repo.get_pull.return_value = pr

    res = close_pr(repo, **_close_kwargs())

    pr.edit.assert_not_called()
    pr.create_issue_comment.assert_not_called()
    assert res["closed"] is True
    assert res["already_closed"] is True


def test_close_pr_comment_failure_is_best_effort():
    repo = MagicMock()
    pr = _eligible_pr()
    pr.create_issue_comment.side_effect = GithubException(403, "no perms", {})
    repo.get_pull.return_value = pr

    res = close_pr(repo, **_close_kwargs())

    # The close itself still succeeded; only the audit comment failed.
    pr.edit.assert_called_once_with(state="closed")
    assert res["closed"] is True
    assert res["comment_posted"] is False
    assert res["comment_error"]


# merge_pr -------------------------------------------------------------- #


def _check_run(name, *, status="completed", conclusion="success", minute=0):
    cr = MagicMock()
    cr.name = name
    cr.status = status
    cr.conclusion = conclusion
    cr.completed_at = dt.datetime(2026, 5, 25, 12, minute, tzinfo=dt.timezone.utc)
    cr.started_at = cr.completed_at
    return cr


def _merge_pr_obj(
    *,
    merged=False,
    state="open",
    draft=False,
    mergeable=True,
    mergeable_state="clean",
):
    """A PR that, by default, passes the full merge gate.

    MagicMock auto-creates truthy attributes, so the merge-relevant flags
    (``merged`` / ``draft`` / ``mergeable`` / ``mergeable_state``) MUST be
    set explicitly or every gate would misfire.
    """
    pr = MagicMock()
    pr.get_labels.return_value = [_label("driftscribe"), _label("docs")]
    pr.head.ref = "upgrade/lodash-4-17-21"
    pr.head.sha = "headsha1234567"
    pr.base.ref = "main"
    pr.merged = merged
    pr.state = state
    pr.draft = draft
    pr.mergeable = mergeable
    pr.mergeable_state = mergeable_state
    pr.html_url = "https://github.com/owner/repo/pull/1"
    pr.number = 1
    result = MagicMock()
    result.merged = True
    result.sha = "mergedsha999"
    result.message = "Pull Request successfully merged"
    pr.merge.return_value = result
    return pr


def _repo_with(pr, check_runs=None):
    repo = MagicMock()
    repo.get_pull.return_value = pr
    commit = MagicMock()
    commit.get_check_runs.return_value = (
        check_runs if check_runs is not None else [_check_run("lint-test")]
    )
    repo.get_commit.return_value = commit
    return repo


def _merge_kwargs(**overrides):
    base = dict(
        pr_number=1,
        dry_run=False,
        merge_method="squash",
        required_checks={"lint-test"},
        required_label="driftscribe",
        required_head_prefix="upgrade/",
        required_base="main",
    )
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # The mergeability retry sleeps 1.5s × 3 — neutralize it so the
    # "unknown after retries" test doesn't add ~4.5s to the suite.
    monkeypatch.setattr("driftscribe_lib.github.time.sleep", lambda _s: None)


def test_merge_pr_dry_run_returns_preview_without_api_calls():
    repo = MagicMock()
    res = merge_pr(repo, **_merge_kwargs(dry_run=True))
    repo.get_pull.assert_not_called()
    assert res == {"dry_run": True, "number": 1, "would_merge": True}


def test_merge_pr_merges_eligible_green_pr_with_head_sha_and_squash():
    pr = _merge_pr_obj()
    repo = _repo_with(pr)

    res = merge_pr(repo, **_merge_kwargs())

    pr.merge.assert_called_once_with(merge_method="squash", sha="headsha1234567")
    repo.get_commit.assert_called_once_with("headsha1234567")
    pr.create_issue_comment.assert_called_once()
    assert res["merged"] is True
    assert res["already_merged"] is False
    assert res["sha"] == "mergedsha999"
    assert res["merge_method"] == "squash"
    assert res["comment_posted"] is True


def test_merge_pr_refuses_pr_missing_required_label():
    pr = _merge_pr_obj()
    pr.get_labels.return_value = [_label("docs")]
    repo = _repo_with(pr)
    with pytest.raises(PrNotEligibleError) as exc:
        merge_pr(repo, **_merge_kwargs())
    assert exc.value.status_code == 403
    pr.merge.assert_not_called()


def test_merge_pr_refuses_wrong_head_prefix():
    pr = _merge_pr_obj()
    pr.head.ref = "feature/random"
    repo = _repo_with(pr)
    with pytest.raises(PrNotEligibleError) as exc:
        merge_pr(repo, **_merge_kwargs())
    assert exc.value.status_code == 403
    pr.merge.assert_not_called()


def test_merge_pr_refuses_wrong_base():
    pr = _merge_pr_obj()
    pr.base.ref = "production"
    repo = _repo_with(pr)
    with pytest.raises(PrNotEligibleError) as exc:
        merge_pr(repo, **_merge_kwargs())
    assert exc.value.status_code == 403
    pr.merge.assert_not_called()


def test_merge_pr_raises_404_when_pr_not_found():
    repo = MagicMock()
    repo.get_pull.side_effect = UnknownObjectException(404, "not found", {})
    with pytest.raises(PrNotEligibleError) as exc:
        merge_pr(repo, **_merge_kwargs(pr_number=999))
    assert exc.value.status_code == 404


def test_merge_pr_idempotent_when_already_merged():
    pr = _merge_pr_obj(merged=True, state="closed")
    repo = _repo_with(pr)

    res = merge_pr(repo, **_merge_kwargs())

    pr.merge.assert_not_called()
    pr.create_issue_comment.assert_not_called()
    assert res["merged"] is True
    assert res["already_merged"] is True


def test_merge_pr_blocks_closed_unmerged_pr():
    pr = _merge_pr_obj(merged=False, state="closed")
    repo = _repo_with(pr)
    with pytest.raises(PrMergeBlockedError) as exc:
        merge_pr(repo, **_merge_kwargs())
    assert exc.value.status_code == 409
    pr.merge.assert_not_called()


def test_merge_pr_blocks_draft_pr():
    pr = _merge_pr_obj(draft=True)
    repo = _repo_with(pr)
    with pytest.raises(PrMergeBlockedError) as exc:
        merge_pr(repo, **_merge_kwargs())
    assert exc.value.status_code == 409
    pr.merge.assert_not_called()


def test_merge_pr_blocks_when_mergeability_unknown_after_retries():
    pr = _merge_pr_obj(mergeable=None)
    repo = _repo_with(pr)  # get_pull always returns the same None-mergeable PR
    with pytest.raises(PrMergeBlockedError) as exc:
        merge_pr(repo, **_merge_kwargs())
    assert exc.value.status_code == 409
    assert "computing" in exc.value.reason
    pr.merge.assert_not_called()


def test_merge_pr_blocks_when_not_mergeable():
    pr = _merge_pr_obj(mergeable=False, mergeable_state="dirty")
    repo = _repo_with(pr)
    with pytest.raises(PrMergeBlockedError) as exc:
        merge_pr(repo, **_merge_kwargs())
    assert exc.value.status_code == 409
    pr.merge.assert_not_called()


def test_merge_pr_reauthorizes_after_retry_blocks_retargeted_base():
    # The PR was eligible at first read (mergeable still computing → retry),
    # but during our wait it was retargeted off main. The post-retry
    # re-authorization must catch the base change — sha= alone wouldn't.
    stale = _merge_pr_obj(mergeable=None)  # forces the retry path
    retargeted = _merge_pr_obj(mergeable=True)
    retargeted.base.ref = "production"
    repo = MagicMock()
    repo.get_pull.side_effect = [stale, retargeted]
    commit = MagicMock()
    commit.get_check_runs.return_value = [_check_run("lint-test")]
    repo.get_commit.return_value = commit

    with pytest.raises(PrNotEligibleError) as exc:
        merge_pr(repo, **_merge_kwargs())
    assert exc.value.status_code == 403
    retargeted.merge.assert_not_called()


def test_merge_pr_reauthorizes_after_retry_returns_already_merged():
    # Someone merged the PR during our mergeability wait — the post-retry
    # re-check returns the idempotent already-merged result, not a re-merge.
    stale = _merge_pr_obj(mergeable=None)
    just_merged = _merge_pr_obj(merged=True, state="closed", mergeable=True)
    repo = MagicMock()
    repo.get_pull.side_effect = [stale, just_merged]

    res = merge_pr(repo, **_merge_kwargs())

    assert res["already_merged"] is True
    just_merged.merge.assert_not_called()


@pytest.mark.parametrize(
    "state",
    # The first four are GitHub states we explicitly never merge; the last
    # two pin the fail-closed allowlist — an unrecognized/future state and
    # a None state must be refused, not merged blind into main.
    ["dirty", "behind", "blocked", "unknown", "has_hooks", None],
)
def test_merge_pr_blocks_states_outside_allowlist(state):
    pr = _merge_pr_obj(mergeable=True, mergeable_state=state)
    repo = _repo_with(pr)
    with pytest.raises(PrMergeBlockedError) as exc:
        merge_pr(repo, **_merge_kwargs())
    assert exc.value.status_code == 409
    # `blocked` (branch protection) is PERMANENT — a plain retry can't clear it;
    # every other refused state is transient (rebase/wait/rerun could fix it).
    assert exc.value.permanent is (state == "blocked")
    pr.merge.assert_not_called()


def test_merge_pr_allows_unstable_state_when_required_checks_green():
    # ``unstable`` = mergeable but a NON-required check is non-green. Our
    # explicit required-check allowlist governs this — a green ``lint-test``
    # must still merge despite the unstable rollup. This is the whole point
    # of the allowlist vs. requiring ``mergeable_state == "clean"``.
    pr = _merge_pr_obj(mergeable=True, mergeable_state="unstable")
    repo = _repo_with(pr, check_runs=[_check_run("lint-test")])

    res = merge_pr(repo, **_merge_kwargs())

    pr.merge.assert_called_once()
    assert res["merged"] is True


def test_merge_pr_blocks_when_required_check_pending():
    pr = _merge_pr_obj()
    repo = _repo_with(
        pr, check_runs=[_check_run("lint-test", status="in_progress", conclusion=None)]
    )
    with pytest.raises(PrMergeBlockedError) as exc:
        merge_pr(repo, **_merge_kwargs())
    assert exc.value.status_code == 409
    pr.merge.assert_not_called()


def test_merge_pr_blocks_when_required_check_failed():
    pr = _merge_pr_obj()
    repo = _repo_with(
        pr, check_runs=[_check_run("lint-test", conclusion="failure")]
    )
    with pytest.raises(PrMergeBlockedError) as exc:
        merge_pr(repo, **_merge_kwargs())
    assert exc.value.status_code == 409
    pr.merge.assert_not_called()


def test_merge_pr_blocks_when_required_check_missing():
    pr = _merge_pr_obj()
    repo = _repo_with(pr, check_runs=[_check_run("some-other-check")])
    with pytest.raises(PrMergeBlockedError) as exc:
        merge_pr(repo, **_merge_kwargs())
    assert exc.value.status_code == 409
    assert "has not reported" in exc.value.reason
    pr.merge.assert_not_called()


def test_merge_pr_uses_latest_check_run_per_name():
    # A re-run flips the result: the OLDER run passed, the NEWER failed.
    # We must honor the latest (failure) and block, not pick "any success".
    pr = _merge_pr_obj()
    repo = _repo_with(
        pr,
        check_runs=[
            _check_run("lint-test", conclusion="success", minute=0),
            _check_run("lint-test", conclusion="failure", minute=5),
        ],
    )
    with pytest.raises(PrMergeBlockedError) as exc:
        merge_pr(repo, **_merge_kwargs())
    assert exc.value.status_code == 409
    pr.merge.assert_not_called()


def test_merge_pr_blocks_when_no_required_checks_configured():
    pr = _merge_pr_obj()
    repo = _repo_with(pr)
    with pytest.raises(PrMergeBlockedError) as exc:
        merge_pr(repo, **_merge_kwargs(required_checks=set()))
    assert exc.value.status_code == 409
    assert "no required checks" in exc.value.reason
    pr.merge.assert_not_called()


def test_merge_pr_maps_github_merge_exception_to_409():
    # Head-SHA race / disallowed method / state change at merge time.
    pr = _merge_pr_obj()
    pr.merge.side_effect = GithubException(
        409, {"message": "Head branch was modified. Review and try the merge again."}, {}
    )
    repo = _repo_with(pr)
    with pytest.raises(PrMergeBlockedError) as exc:
        merge_pr(repo, **_merge_kwargs())
    assert exc.value.status_code == 409


def test_merge_pr_blocks_when_api_reports_not_merged():
    pr = _merge_pr_obj()
    result = MagicMock()
    result.merged = False
    result.message = "Base branch was modified"
    pr.merge.return_value = result
    repo = _repo_with(pr)
    with pytest.raises(PrMergeBlockedError) as exc:
        merge_pr(repo, **_merge_kwargs())
    assert exc.value.status_code == 409


def test_merge_pr_comment_failure_is_best_effort():
    pr = _merge_pr_obj()
    pr.create_issue_comment.side_effect = GithubException(403, "no perms", {})
    repo = _repo_with(pr)

    res = merge_pr(repo, **_merge_kwargs())

    # The merge itself still succeeded; only the audit comment failed.
    pr.merge.assert_called_once()
    assert res["merged"] is True
    assert res["comment_posted"] is False
    assert res["comment_error"]
