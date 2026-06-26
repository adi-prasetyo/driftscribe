"""Unit tests for the two new read-only github helpers backing the
open-trace follow-up:

* ``get_pr_body`` — the agent-authored PR description, capped, for the
  open-trace "What this change did" disclosure.
* ``is_pr_merged_at_head`` — the merge-status reconcile probe: True ONLY when
  the PR is merged AND its head still matches the as-applied head_sha (mirrors
  ``merge_pr_at_sha``'s head-first invariant so a force-push-then-merge at a
  different head is never a false positive).

PyGithub-mock style, no network.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from driftscribe_lib import github as gh


def _repo_with_pull(*, body=None, merged=False, head_sha="a" * 40):
    repo = MagicMock()
    pull = SimpleNamespace(body=body, merged=merged, head=SimpleNamespace(sha=head_sha))
    repo.get_pull.return_value = pull
    return repo


# --- get_pr_body -----------------------------------------------------------

def test_get_pr_body_returns_body_untruncated():
    repo = _repo_with_pull(body="## What this does\n\nRepoints the SA.\n")
    out = gh.get_pr_body(repo, 32)
    assert out == {"body": "## What this does\n\nRepoints the SA.\n", "truncated": False}


def test_get_pr_body_none_when_pr_has_no_description():
    repo = _repo_with_pull(body=None)
    out = gh.get_pr_body(repo, 32)
    assert out == {"body": None, "truncated": False}


def test_get_pr_body_truncates_oversize_and_flags():
    big = "x" * 20000
    repo = _repo_with_pull(body=big)
    out = gh.get_pr_body(repo, 32, max_chars=16384)
    assert out["truncated"] is True
    assert len(out["body"]) == 16384
    assert out["body"] == big[:16384]


def test_get_pr_body_empty_string_is_none_like():
    repo = _repo_with_pull(body="")
    out = gh.get_pr_body(repo, 32)
    # An empty body carries no explanation — normalise to None so the UI omits it.
    assert out == {"body": None, "truncated": False}


# --- is_pr_merged_at_head --------------------------------------------------

def test_is_pr_merged_at_head_true_when_merged_and_head_matches():
    repo = _repo_with_pull(merged=True, head_sha="c" * 40)
    assert gh.is_pr_merged_at_head(repo, 32, "c" * 40) is True


def test_is_pr_merged_at_head_false_when_head_moved():
    # Force-push-then-merge at a different head: the applied artifact is NOT what
    # merged, so this must NOT reconcile (mirrors merge_pr_at_sha's guard).
    repo = _repo_with_pull(merged=True, head_sha="d" * 40)
    assert gh.is_pr_merged_at_head(repo, 32, "c" * 40) is False


def test_is_pr_merged_at_head_false_when_not_merged():
    repo = _repo_with_pull(merged=False, head_sha="c" * 40)
    assert gh.is_pr_merged_at_head(repo, 32, "c" * 40) is False
