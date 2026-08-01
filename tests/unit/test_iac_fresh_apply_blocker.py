"""Unit tests for ``driftscribe_lib.github.fresh_apply_blocker`` (ds-2wy).

The helper answers ONE question: does the pull request's own lifecycle already
guarantee that a FRESH apply (no recorded decision) would be refused? It is a
deliberate SUBSET of ``assert_pr_ready_at_sha`` — required-check status is not
covered, because it needs a second API path and is transient. These tests pin
both the positive cases and that subset boundary, so nobody later mistakes a
``None`` for "ready to apply".
"""
from __future__ import annotations

import pytest

from driftscribe_lib import github as gh

_HEAD = "a" * 40


class _Pull:
    def __init__(self, *, merged=False, state="open", draft=False, head_sha=_HEAD):
        self.merged = merged
        self.state = state
        self.draft = draft
        self.head = type("_H", (), {"sha": head_sha})()


class _Repo:
    def __init__(self, pull):
        self._pull = pull
        self.calls = 0

    def get_pull(self, pr_number):
        self.calls += 1
        return self._pull


@pytest.mark.parametrize(
    ("pull", "expected"),
    [
        (_Pull(), None),
        (_Pull(merged=True), gh.FRESH_APPLY_MERGED),
        (_Pull(state="closed"), gh.FRESH_APPLY_CLOSED),
        (_Pull(draft=True), gh.FRESH_APPLY_DRAFT),
        (_Pull(head_sha="b" * 40), gh.FRESH_APPLY_HEAD_MOVED),
    ],
)
def test_lifecycle_verdicts(pull, expected):
    assert gh.fresh_apply_blocker(_Repo(pull), 42, _HEAD) == expected


def test_one_api_read_only():
    """The serve path renders this per approval-page view; keep it at one call."""
    repo = _Repo(_Pull())
    gh.fresh_apply_blocker(repo, 42, _HEAD)
    assert repo.calls == 1


def test_merged_outranks_every_other_blocker():
    """Most-permanent-first: a merged PR reads as merged, not head_moved/draft.

    A PR force-pushed and then merged satisfies BOTH predicates. Reporting
    "head moved" there would tell the operator to wait for a fresh plan that can
    never be applied, because the PR is finished.
    """
    pull = _Pull(merged=True, state="closed", draft=True, head_sha="c" * 40)
    assert gh.fresh_apply_blocker(_Repo(pull), 42, _HEAD) == gh.FRESH_APPLY_MERGED


def test_merged_pr_reporting_state_closed_is_still_merged():
    """GitHub reports a merged PR as ``state="closed"``; order must not flip it."""
    assert (
        gh.fresh_apply_blocker(_Repo(_Pull(merged=True, state="closed")), 42, _HEAD)
        == gh.FRESH_APPLY_MERGED
    )


def test_does_not_claim_readiness_it_cannot_see():
    """``None`` means "nothing HERE blocks it", never "ready to apply".

    An open, non-draft PR at the expected head with RED required checks still
    fails ``assert_pr_ready_at_sha``. This helper returns ``None`` for it by
    design — the POST owns that gate. Pinned so a future caller cannot quietly
    promote this to a readiness check.
    """
    assert gh.fresh_apply_blocker(_Repo(_Pull()), 42, _HEAD) is None


def test_errors_propagate_to_the_caller():
    """Fail-soft belongs to the caller, not here — the helper stays honest."""

    class _Boom:
        def get_pull(self, pr_number):
            raise RuntimeError("github 502")

    with pytest.raises(RuntimeError):
        gh.fresh_apply_blocker(_Boom(), 42, _HEAD)
