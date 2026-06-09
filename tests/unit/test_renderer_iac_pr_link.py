"""Serve-time PR-link derivation for iac_apply decision rows.

``attach_iac_pr_link(decision, repo)`` is the read-path sibling of
``scrub_decision_rationale``: for an ``iac_apply`` decision it derives a
``github.url`` pointing at the GitHub PR from the TRUSTED config repo +
the persisted ``pr_number``. The URL is fully derivable, so it is never
persisted — it is attached at /decisions serve time and works for every
row (including pre-existing docs) with no Firestore migration.

Conventions mirror ``scrub_decision_rationale``: copy-on-change,
identity-when-nothing-to-do, never mutates the input, never raises,
accepts ``object`` and returns non-dict inputs as-is.
"""

import pytest

from agent.renderer import attach_iac_pr_link

REPO = "adi-prasetyo/driftscribe"


def _iac(pr_number=68, **extra):
    d = {"action": "iac_apply", "decision_id": "d1", "pr_number": pr_number,
         "head_sha": "0496b305deadbeef", "apply_status": "applied"}
    d.update(extra)
    return d


# --- the happy path -------------------------------------------------------- #

def test_attaches_pr_url_for_iac_apply_row():
    out = attach_iac_pr_link(_iac(pr_number=68), REPO)
    assert out["github"] == {"url": "https://github.com/adi-prasetyo/driftscribe/pull/68"}


def test_returns_new_dict_and_does_not_mutate_input():
    doc = _iac(pr_number=66)
    out = attach_iac_pr_link(doc, REPO)
    assert out is not doc
    assert "github" not in doc          # original untouched (list_decisions hands back live dicts)
    assert out["github"]["url"].endswith("/pull/66")


# --- gates: leave the row untouched (by identity) -------------------------- #

def test_non_iac_action_is_identity():
    doc = {"action": "drift_issue", "pr_number": 5}
    assert attach_iac_pr_link(doc, REPO) is doc


def test_existing_github_is_never_clobbered():
    doc = _iac(pr_number=68, github={"url": "https://github.com/x/y/pull/1"})
    assert attach_iac_pr_link(doc, REPO) is doc


def test_non_dict_input_is_returned_as_is():
    assert attach_iac_pr_link(None, REPO) is None
    assert attach_iac_pr_link("nope", REPO) == "nope"


@pytest.mark.parametrize("bad_pr", [None, 0, -1, "68", 68.0, True])
def test_invalid_pr_number_is_identity(bad_pr):
    # True must NOT pass as 1 (type(True) is bool, not int); strings/floats rejected.
    doc = _iac(pr_number=bad_pr)
    assert attach_iac_pr_link(doc, REPO) is doc


def test_missing_pr_number_is_identity():
    doc = {"action": "iac_apply", "decision_id": "d1"}
    assert attach_iac_pr_link(doc, REPO) is doc


@pytest.mark.parametrize("bad_repo", ["", "noslash", "a/b/c", "/leadingslash",
                                      "trailing/", "has space/repo", None])
def test_bad_repo_shape_is_identity(bad_repo):
    doc = _iac(pr_number=68)
    assert attach_iac_pr_link(doc, bad_repo) is doc
