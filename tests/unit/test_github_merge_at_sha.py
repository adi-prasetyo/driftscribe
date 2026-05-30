"""Unit tests for the head-SHA-bound merge/readiness helpers (Phase C5e-3).

Covers ``driftscribe_lib.github``:

- ``assert_pr_ready_at_sha`` — read-only pre-propose gate (Codex r2: readiness
  BEFORE any mint). Empty checks refuse; head must match the pinned sha; base must
  be ``main``; PR must be open/non-draft; every required check must be green on the
  pinned sha.
- ``merge_pr_at_sha`` — the mutating, head-SHA-bound merge. Ordering matters:
  base+head are asserted FIRST so a manually-merged NEWER head is NOT mistaken for
  an idempotent reconcile of an OLDER artifact. Empty checks refuse; a non-green
  required check refuses; dry_run previews.
- ``_assert_pr_eligible(required_label=None)`` — skips the label-membership check
  while still enforcing head-prefix / base (Codex blocker #5).
- ``get_pr_head_sha`` — the cheap step-5b re-check.

Reuses the fake-PyGithub doubles idiom from ``test_github_actions.py``.
"""
import datetime as dt
from unittest.mock import MagicMock

import pytest
from github import GithubException, UnknownObjectException

from driftscribe_lib.github import (
    PrMergeBlockedError,
    PrNotEligibleError,
    _assert_pr_eligible,
    assert_pr_ready_at_sha,
    get_pr_head_sha,
    merge_pr_at_sha,
)

_HEAD = "a" * 40


def _label(name: str) -> MagicMock:
    m = MagicMock()
    m.name = name
    return m


def _check_run(name, *, status="completed", conclusion="success", minute=0):
    cr = MagicMock()
    cr.name = name
    cr.status = status
    cr.conclusion = conclusion
    cr.completed_at = dt.datetime(2026, 5, 25, 12, minute, tzinfo=dt.timezone.utc)
    cr.started_at = cr.completed_at
    return cr


def _pr_obj(
    *,
    merged=False,
    state="open",
    draft=False,
    mergeable=True,
    mergeable_state="clean",
    head_sha=_HEAD,
    base_ref="main",
):
    pr = MagicMock()
    pr.get_labels.return_value = [_label("driftscribe")]
    pr.head.ref = "iac/some-change"
    pr.head.sha = head_sha
    pr.base.ref = base_ref
    pr.merged = merged
    pr.state = state
    pr.draft = draft
    pr.mergeable = mergeable
    pr.mergeable_state = mergeable_state
    pr.html_url = "https://github.com/owner/repo/pull/5"
    pr.number = 5
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
        check_runs if check_runs is not None else [_check_run("tofu")]
    )
    repo.get_commit.return_value = commit
    return repo


def _ready_kwargs(**overrides):
    base = dict(
        pr_number=5,
        expected_head_sha=_HEAD,
        required_checks={"tofu"},
    )
    base.update(overrides)
    return base


def _merge_kwargs(**overrides):
    base = dict(
        pr_number=5,
        expected_head_sha=_HEAD,
        required_checks={"tofu"},
        merge_method="squash",
        dry_run=False,
    )
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("driftscribe_lib.github.time.sleep", lambda _s: None)


# --------------------------------------------------------------------------- #
# _assert_pr_eligible(required_label=None)
# --------------------------------------------------------------------------- #


def test_assert_pr_eligible_none_label_skips_label_check():
    # PR has NO matching label, but required_label=None ⇒ no label check.
    pr = _pr_obj()
    pr.get_labels.return_value = [_label("unrelated")]
    pr.head.ref = "iac/x"
    pr.base.ref = "main"
    # Should NOT raise even though the label is absent.
    _assert_pr_eligible(
        pr,
        required_label=None,
        required_head_prefix="iac/",
        required_base="main",
    )


def test_assert_pr_eligible_none_label_still_enforces_base_and_prefix():
    pr = _pr_obj()
    pr.get_labels.return_value = []
    pr.head.ref = "feature/random"  # wrong prefix
    pr.base.ref = "main"
    with pytest.raises(PrNotEligibleError):
        _assert_pr_eligible(
            pr,
            required_label=None,
            required_head_prefix="iac/",
            required_base="main",
        )
    pr.head.ref = "iac/x"
    pr.base.ref = "production"  # wrong base
    with pytest.raises(PrNotEligibleError):
        _assert_pr_eligible(
            pr,
            required_label=None,
            required_head_prefix="iac/",
            required_base="main",
        )


def test_assert_pr_eligible_existing_label_still_required_when_given():
    pr = _pr_obj()
    pr.get_labels.return_value = [_label("docs")]
    with pytest.raises(PrNotEligibleError):
        _assert_pr_eligible(
            pr,
            required_label="driftscribe",
            required_head_prefix=None,
            required_base=None,
        )


# --------------------------------------------------------------------------- #
# assert_pr_ready_at_sha
# --------------------------------------------------------------------------- #


def test_ready_ok_returns_head_sha():
    pr = _pr_obj()
    repo = _repo_with(pr)
    out = assert_pr_ready_at_sha(repo, **_ready_kwargs())
    assert out == _HEAD
    pr.merge.assert_not_called()  # read-only


def test_ready_empty_checks_refuse():
    pr = _pr_obj()
    repo = _repo_with(pr)
    with pytest.raises(PrMergeBlockedError) as exc:
        assert_pr_ready_at_sha(repo, **_ready_kwargs(required_checks=set()))
    assert "merge disabled" in str(exc.value)


def test_ready_pr_not_found_404():
    repo = MagicMock()
    repo.get_pull.side_effect = UnknownObjectException(404, "nope", {})
    with pytest.raises(PrNotEligibleError) as exc:
        assert_pr_ready_at_sha(repo, **_ready_kwargs(pr_number=999))
    assert exc.value.status_code == 404


def test_ready_base_not_main_rejected():
    pr = _pr_obj(base_ref="production")
    repo = _repo_with(pr)
    with pytest.raises(PrNotEligibleError):
        assert_pr_ready_at_sha(repo, **_ready_kwargs())


def test_ready_head_mismatch_blocked():
    pr = _pr_obj(head_sha="b" * 40)
    repo = _repo_with(pr)
    with pytest.raises(PrMergeBlockedError) as exc:
        assert_pr_ready_at_sha(repo, **_ready_kwargs())
    assert "head moved" in str(exc.value)


def test_ready_draft_blocked():
    pr = _pr_obj(draft=True)
    repo = _repo_with(pr)
    with pytest.raises(PrMergeBlockedError):
        assert_pr_ready_at_sha(repo, **_ready_kwargs())


def test_ready_closed_unmerged_blocked():
    pr = _pr_obj(state="closed", merged=False)
    repo = _repo_with(pr)
    with pytest.raises(PrMergeBlockedError):
        assert_pr_ready_at_sha(repo, **_ready_kwargs())


def test_ready_merged_blocked():
    # A merged PR cannot be "ready to propose+apply" — fail closed.
    pr = _pr_obj(merged=True, state="closed")
    repo = _repo_with(pr)
    with pytest.raises(PrMergeBlockedError):
        assert_pr_ready_at_sha(repo, **_ready_kwargs())


def test_ready_check_red_blocked():
    pr = _pr_obj()
    repo = _repo_with(pr, check_runs=[_check_run("tofu", conclusion="failure")])
    with pytest.raises(PrMergeBlockedError):
        assert_pr_ready_at_sha(repo, **_ready_kwargs())


def test_ready_check_missing_blocked():
    pr = _pr_obj()
    repo = _repo_with(pr, check_runs=[_check_run("static-gate")])
    with pytest.raises(PrMergeBlockedError):
        assert_pr_ready_at_sha(repo, **_ready_kwargs(required_checks={"tofu"}))


def test_ready_check_pending_blocked():
    pr = _pr_obj()
    repo = _repo_with(pr, check_runs=[_check_run("tofu", status="in_progress")])
    with pytest.raises(PrMergeBlockedError):
        assert_pr_ready_at_sha(repo, **_ready_kwargs())


def test_ready_checks_read_on_expected_sha():
    pr = _pr_obj()
    repo = _repo_with(pr)
    assert_pr_ready_at_sha(repo, **_ready_kwargs())
    repo.get_commit.assert_called_once_with(_HEAD)


# --------------------------------------------------------------------------- #
# merge_pr_at_sha
# --------------------------------------------------------------------------- #


def test_merge_at_sha_dry_run_no_api():
    repo = MagicMock()
    res = merge_pr_at_sha(repo, **_merge_kwargs(dry_run=True))
    repo.get_pull.assert_not_called()
    assert res == {"dry_run": True, "number": 5, "would_merge": True}


def test_merge_at_sha_happy_merge_uses_expected_sha():
    pr = _pr_obj()
    repo = _repo_with(pr)
    res = merge_pr_at_sha(repo, **_merge_kwargs())
    pr.merge.assert_called_once_with(merge_method="squash", sha=_HEAD)
    repo.get_commit.assert_called_once_with(_HEAD)
    assert res["merged"] is True
    assert res["already_merged"] is False
    assert res["number"] == 5
    pr.create_issue_comment.assert_called_once()


def test_merge_at_sha_empty_checks_refuse():
    pr = _pr_obj()
    repo = _repo_with(pr)
    with pytest.raises(PrMergeBlockedError) as exc:
        merge_pr_at_sha(repo, **_merge_kwargs(required_checks=set()))
    assert "merge disabled" in str(exc.value)


def test_merge_at_sha_pr_not_found_404():
    repo = MagicMock()
    repo.get_pull.side_effect = UnknownObjectException(404, "nope", {})
    with pytest.raises(PrNotEligibleError) as exc:
        merge_pr_at_sha(repo, **_merge_kwargs(pr_number=999))
    assert exc.value.status_code == 404


def test_merge_at_sha_base_not_main_rejected():
    pr = _pr_obj(base_ref="production")
    repo = _repo_with(pr)
    with pytest.raises(PrMergeBlockedError) as exc:
        merge_pr_at_sha(repo, **_merge_kwargs())
    assert exc.value.status_code == 409
    pr.merge.assert_not_called()


def test_merge_at_sha_head_mismatch_blocks_before_already_merged():
    # CRITICAL ORDERING: a PR with merged=True but head!=expected must NOT be
    # reported as already_merged — the head check fires FIRST (409 stale).
    pr = _pr_obj(merged=True, state="closed", head_sha="b" * 40)
    repo = _repo_with(pr)
    with pytest.raises(PrMergeBlockedError) as exc:
        merge_pr_at_sha(repo, **_merge_kwargs())
    assert exc.value.status_code == 409
    pr.merge.assert_not_called()


def test_merge_at_sha_already_merged_matching_head_idempotent():
    pr = _pr_obj(merged=True, state="closed", head_sha=_HEAD)
    repo = _repo_with(pr)
    res = merge_pr_at_sha(repo, **_merge_kwargs())
    assert res == {
        "merged": True,
        "already_merged": True,
        "number": 5,
        "url": pr.html_url,
    }
    pr.merge.assert_not_called()


def test_merge_at_sha_check_red_blocked():
    pr = _pr_obj()
    repo = _repo_with(pr, check_runs=[_check_run("tofu", conclusion="failure")])
    with pytest.raises(PrMergeBlockedError):
        merge_pr_at_sha(repo, **_merge_kwargs())
    pr.merge.assert_not_called()


def test_merge_at_sha_check_missing_blocked():
    pr = _pr_obj()
    repo = _repo_with(pr, check_runs=[_check_run("static-gate")])
    with pytest.raises(PrMergeBlockedError):
        merge_pr_at_sha(repo, **_merge_kwargs(required_checks={"tofu"}))
    pr.merge.assert_not_called()


def test_merge_at_sha_draft_blocked():
    pr = _pr_obj(draft=True)
    repo = _repo_with(pr)
    with pytest.raises(PrMergeBlockedError):
        merge_pr_at_sha(repo, **_merge_kwargs())


def test_merge_at_sha_not_mergeable_blocked():
    pr = _pr_obj(mergeable=False, mergeable_state="dirty")
    repo = _repo_with(pr)
    with pytest.raises(PrMergeBlockedError):
        merge_pr_at_sha(repo, **_merge_kwargs())


def test_merge_at_sha_mergeability_unknown_after_retries_blocked():
    pr = _pr_obj(mergeable=None)
    repo = _repo_with(pr)
    with pytest.raises(PrMergeBlockedError):
        merge_pr_at_sha(repo, **_merge_kwargs())


def test_merge_at_sha_reasserts_head_after_retry():
    # mergeability None at first read forces a re-fetch; the re-fetched PR has a
    # moved head → must block (409 stale), not merge.
    stale = _pr_obj(mergeable=None)
    moved = _pr_obj(mergeable=True, head_sha="b" * 40)
    repo = MagicMock()
    repo.get_pull.side_effect = [stale, moved, moved]
    commit = MagicMock()
    commit.get_check_runs.return_value = [_check_run("tofu")]
    repo.get_commit.return_value = commit
    with pytest.raises(PrMergeBlockedError):
        merge_pr_at_sha(repo, **_merge_kwargs())
    moved.merge.assert_not_called()


def test_merge_at_sha_github_exception_maps_to_409():
    pr = _pr_obj()
    pr.merge.side_effect = GithubException(409, {"message": "head changed"}, {})
    repo = _repo_with(pr)
    with pytest.raises(PrMergeBlockedError) as exc:
        merge_pr_at_sha(repo, **_merge_kwargs())
    assert exc.value.status_code == 409


def test_merge_at_sha_api_reports_not_merged():
    pr = _pr_obj()
    result = MagicMock()
    result.merged = False
    result.message = "base branch was modified"
    pr.merge.return_value = result
    repo = _repo_with(pr)
    with pytest.raises(PrMergeBlockedError):
        merge_pr_at_sha(repo, **_merge_kwargs())


def test_merge_at_sha_comment_failure_best_effort():
    pr = _pr_obj()
    pr.create_issue_comment.side_effect = GithubException(403, "no perms", {})
    repo = _repo_with(pr)
    res = merge_pr_at_sha(repo, **_merge_kwargs())
    assert res["merged"] is True
    assert res["already_merged"] is False


# --------------------------------------------------------------------------- #
# get_pr_head_sha
# --------------------------------------------------------------------------- #


def test_get_pr_head_sha():
    pr = _pr_obj(head_sha="c" * 40)
    repo = _repo_with(pr)
    assert get_pr_head_sha(repo, 5) == "c" * 40
    repo.get_pull.assert_called_once_with(5)
