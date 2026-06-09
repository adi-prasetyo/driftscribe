"""Write-time PR-title capture for iac_apply decision rows.

The PR title is external, mutable, non-derivable content, so (unlike the PR URL)
it is captured ONCE at apply time and persisted on the decision doc as
``pr_title``. The rail renders it as the row subtitle.

* ``_fetch_pr_title(repo, pr_number)`` — fail-soft GitHub read; normalizes
  whitespace (anti-spoof: the title renders on one ellipsised line), caps length,
  returns ``None`` on any error or empty title — a cosmetic field must never break
  an apply.
* ``_record_iac_decision(..., pr_title=...)`` — persists the title when a non-empty
  string is supplied; omits the field otherwise.
"""

import pytest

from agent.main import _fetch_pr_title, _record_iac_decision
from agent.state_store import InMemoryStateStore


# --------------------------------------------------------------------------- #
# _fetch_pr_title — fail-soft, normalized, capped
# --------------------------------------------------------------------------- #


class _FakePR:
    def __init__(self, title):
        self.title = title


class _FakeRepo:
    def __init__(self, *, title=None, raises=False):
        self._title = title
        self._raises = raises
        self.seen = None

    def get_pull(self, pr_number):
        self.seen = pr_number
        if self._raises:
            raise RuntimeError("github down")
        return _FakePR(self._title)


def test_fetch_pr_title_returns_the_title():
    assert _fetch_pr_title(_FakeRepo(title="infra(checkout): storefront"), 68) \
        == "infra(checkout): storefront"


def test_fetch_pr_title_collapses_internal_whitespace_and_strips():
    # newlines/tabs/runs of spaces collapse to single spaces; outer ws stripped.
    assert _fetch_pr_title(_FakeRepo(title="  feat:\n  add\tbucket   now "), 1) \
        == "feat: add bucket now"


def test_fetch_pr_title_caps_length_to_200():
    out = _fetch_pr_title(_FakeRepo(title="x" * 500), 1)
    assert out is not None and len(out) == 200


def test_fetch_pr_title_none_on_github_exception():
    # Cosmetic field — a GitHub error must degrade to None, never propagate.
    assert _fetch_pr_title(_FakeRepo(raises=True), 1) is None


@pytest.mark.parametrize("blank", ["", "   ", "\n\t ", None])
def test_fetch_pr_title_none_on_empty_title(blank):
    assert _fetch_pr_title(_FakeRepo(title=blank), 1) is None


# --------------------------------------------------------------------------- #
# _record_iac_decision — persists pr_title when given
# --------------------------------------------------------------------------- #


def _record(pr_title):
    state = InMemoryStateStore()
    ek = "iac-apply-68-deadbeef"
    state.record_event(ek, {"pr_number": 68})
    return _record_iac_decision(
        state, ek, apply_status="applied", merge_state="merged",
        head_sha="0496b305", pr_number=68, approver="op@example.com",
        pr_title=pr_title,
    )


def test_record_iac_decision_stores_pr_title():
    d = _record("infra(checkout): storefront + orders-worker")
    assert d["pr_title"] == "infra(checkout): storefront + orders-worker"


@pytest.mark.parametrize("absent", [None, ""])
def test_record_iac_decision_omits_pr_title_when_absent(absent):
    d = _record(absent)
    assert "pr_title" not in d


def test_record_iac_decision_pr_title_defaults_to_absent():
    # The param is optional — callers that don't pass it record no pr_title.
    state = InMemoryStateStore()
    ek = "iac-apply-1-x"
    state.record_event(ek, {})
    d = _record_iac_decision(
        state, ek, apply_status="failed", merge_state="n/a",
        head_sha="abc", pr_number=1, approver="op@example.com",
    )
    assert "pr_title" not in d
