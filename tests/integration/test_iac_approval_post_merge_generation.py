"""ds-2wy — GET /iac-approvals/{n} must not offer a form whose POST is doomed.

The reachable sequence (all premises verified against the code):

1. ``.github/workflows/iac.yml`` asserts the PR is OPEN at PLAN time and, before
   this fix, never rechecked before posting the C2 marker comment. GitHub allows
   comments on merged PRs.
2. ``agent.iac_artifacts.find_latest_c2_comment`` is NEWEST-WINS.
3. So a plan-builder run for generation B that finishes AFTER generation A merged
   the PR posts the newest C2 comment, and this page binds to B.
4. B has no decision (``_iac_event_key`` hashes ``generation_metadata``, so A's
   decision cannot match), every gate rung passes, and a CSRF form is minted.
5. The POST then takes its fresh-apply branch, calls ``assert_pr_ready_at_sha``,
   and 409s on ``PR is already merged``.

Backend safety was never in question — nothing unauthorized applies. The defect
is a page that ASKS for an action it already knows cannot succeed, while the desk
card describes the merged generation and says something different.

The two tests that matter most here are the ones that prove the fix is not
OVER-broad: ``test_waiting_for_rebake_on_merged_pr_keeps_the_resume_form`` (the
C6 resume legitimately lives on a merged PR) and
``test_decision_lookup_failure_keeps_the_form_even_on_a_merged_pr`` (a Firestore
outage must not be read as "there is no decision" — found by Codex reviewing an
earlier draft of this fix, which gated on ``existing is None`` alone and would
have suppressed a real resume during an outage).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent.config import get_settings
from agent.main import app, get_state
from driftscribe_lib import github as gh_lib

from tests.integration.test_iac_approval_get import (  # reuse the C2/view fixtures
    _HEAD,
    _configured,
    _inmemory,
    _patch_resolve,
    _ref,
    _seed_decision,
    _view,
)

__all__ = ["_configured", "_inmemory"]  # keep the imported fixtures referenced


class _FakePull:
    def __init__(self, *, merged=False, state="open", draft=False, head_sha=_HEAD):
        self.merged = merged
        self.state = state
        self.draft = draft
        self.head = type("_H", (), {"sha": head_sha})()


class _FakeRepo:
    """Minimal PyGithub-shaped repo. ``pull`` may be an Exception to raise."""

    def __init__(self, pull):
        self._pull = pull
        self.calls = 0

    def get_pull(self, pr_number):
        self.calls += 1
        if isinstance(self._pull, Exception):
            raise self._pull
        return self._pull


def _patch_repo(monkeypatch, pull) -> _FakeRepo:
    repo = _FakeRepo(pull)
    monkeypatch.setattr("agent.main.get_repo", lambda token, name: repo)
    return repo


def _seed(apply_status: str, merge_state: str) -> None:
    """Reuse the sibling suite's seeder verbatim.

    Rolling a local copy got this wrong once already: it omitted the
    ``record_event`` claim, so ``find_decision_for_event`` never linked the
    decision and the "resume survives" test failed for a reason that had nothing
    to do with the gate under test.
    """
    _seed_decision(apply_status=apply_status, merge_state=merge_state)


def _get(monkeypatch, pull, *, lang: str = "") -> str:
    _patch_resolve(monkeypatch, ref=_ref(), view=_view())
    _patch_repo(monkeypatch, pull)
    url = "/iac-approvals/42" + (f"?lang={lang}" if lang else "")
    resp = TestClient(app).get(url)
    assert resp.status_code == 200, "the GET must stay probe-safe 200 on every path"
    return resp.text


def _has_form(body: str) -> bool:
    return 'data-testid="approve-button"' in body and 'name="form_token"' in body


# --------------------------------------------------------------------------- #
# The defect itself.
# --------------------------------------------------------------------------- #


def test_merged_pr_with_no_decision_suppresses_the_form(
    _configured, _inmemory, monkeypatch
):
    body = _get(monkeypatch, _FakePull(merged=True))
    assert not _has_form(body), (
        "a merged PR with no decision for this generation can only 409 at the "
        "POST — the page must not mint a token for it"
    )
    assert "already merged" in body


def test_closed_unmerged_pr_suppresses_the_form(_configured, _inmemory, monkeypatch):
    body = _get(monkeypatch, _FakePull(state="closed"))
    assert not _has_form(body)
    assert "closed" in body


def test_draft_pr_suppresses_the_form(_configured, _inmemory, monkeypatch):
    body = _get(monkeypatch, _FakePull(draft=True))
    assert not _has_form(body)
    assert "draft" in body


def test_head_moved_off_the_artifact_suppresses_the_form(
    _configured, _inmemory, monkeypatch
):
    body = _get(monkeypatch, _FakePull(head_sha="f" * 40))
    assert not _has_form(body)
    assert "moved past" in body


def test_merged_outranks_head_moved_in_the_reported_reason(
    _configured, _inmemory, monkeypatch
):
    """A merged AND force-pushed PR reads as merged — the durable fact wins."""
    body = _get(monkeypatch, _FakePull(merged=True, head_sha="f" * 40))
    assert "already merged" in body
    assert "moved past" not in body


# --------------------------------------------------------------------------- #
# Not over-broad. These are the tests that keep the fix honest.
# --------------------------------------------------------------------------- #


def test_open_pr_with_no_decision_still_mints_the_form(
    _configured, _inmemory, monkeypatch
):
    """The hot path is unchanged — and passes for the RIGHT reason.

    Asserting on the probe CALL matters: with a bare ``object()`` repo (the
    default in the sibling suite) this test would also pass, but only because the
    probe raised AttributeError and fell soft. Then the gate could be broken in
    either direction and this test would never notice.
    """
    _patch_resolve(monkeypatch, ref=_ref(), view=_view())
    repo = _patch_repo(monkeypatch, _FakePull())
    body = TestClient(app).get("/iac-approvals/42").text
    assert _has_form(body)
    assert repo.calls >= 1, "the open-PR verdict must come from a real probe"


def test_waiting_for_rebake_on_merged_pr_keeps_the_resume_form(
    _configured, _inmemory, monkeypatch
):
    """The C6 resume LIVES on a merged PR — the whole point of that state.

    ``_iac_create_merge_first`` records ``waiting_for_rebake`` and then merges, so
    the resume click always arrives at a merged PR. A gate that keyed on "merged"
    without first checking for a decision would delete this flow outright.
    """
    _seed("waiting_for_rebake", "merged")
    body = _get(monkeypatch, _FakePull(merged=True))
    assert _has_form(body), "the post-rebake Apply must survive the ds-2wy gate"


def test_applied_failed_on_open_pr_keeps_the_merge_only_form(
    _configured, _inmemory, monkeypatch
):
    _seed("applied", "failed")
    body = _get(monkeypatch, _FakePull())
    assert _has_form(body)


# --------------------------------------------------------------------------- #
# Fail-soft. Two INDEPENDENT failure modes; neither may cost the operator a form.
# --------------------------------------------------------------------------- #


def test_anonymous_viewer_keeps_operator_only_copy_and_costs_no_probe(
    _configured, _inmemory, monkeypatch
):
    """The gate is scoped to ``can_approve``, and that scoping is load-bearing.

    Two reasons, both pinned here. (1) COST: during an open demo window every
    anonymous render would otherwise buy a GitHub round-trip, on a path where no
    form is minted anyway. (2) COPY: the operator-only rung is deliberately
    ordered ahead of the operator-state rungs (see the gate ladder) so a visitor
    reads "operator-only", not a state note about a dial they cannot reach.
    Without this test, dropping ``can_approve`` from the gate condition changes
    both and nothing fails.
    """
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "adp-app.cloudflareaccess.com")
    monkeypatch.setenv("CF_ACCESS_AUD_TAG", "a" * 64)
    get_settings.cache_clear()
    _patch_resolve(monkeypatch, ref=_ref(), view=_view())
    _patch_repo(monkeypatch, _FakePull(merged=True))

    # Spy on the ds-2wy probe specifically. A raw get_pull counter would not do:
    # the "view source" fetch on this same render calls get_pull too, so a count
    # there could never distinguish "the gate probed" from "the page rendered".
    probes: list[int] = []
    real = gh_lib.fresh_apply_blocker
    monkeypatch.setattr(
        "agent.main.github.fresh_apply_blocker",
        lambda repo, pr, head: (probes.append(pr), real(repo, pr, head))[1],
    )

    body = TestClient(app).get("/iac-approvals/42").text  # no Cf-Access header

    assert not _has_form(body)
    assert 'data-testid="approve-operator-only"' in body
    assert "operator-only" in body
    assert "already merged" not in body
    assert probes == [], "no ds-2wy probe on a path that mints no form"


def test_probe_failure_leaves_the_form_alone(_configured, _inmemory, monkeypatch):
    """GitHub hiccup ⇒ page unchanged. The POST is still the real boundary."""
    body = _get(monkeypatch, RuntimeError("github 502"))
    assert _has_form(body)


def test_decision_lookup_failure_keeps_the_form_even_on_a_merged_pr(
    _configured, _inmemory, monkeypatch
):
    """A store outage is NOT "there is no decision".

    This is the Codex finding. The GET reports both as ``existing = None``, so a
    gate written as ``if existing is None and pr_merged: suppress`` would, during
    a Firestore outage, hide the resume form for a real ``waiting_for_rebake``
    decision on a (correctly) merged PR — converting a transient read failure
    into a lost operator action. The gate must require a definitive "no decision"
    answer, which is what ``_lookup_answered`` carries.
    """
    _seed("waiting_for_rebake", "merged")

    def _boom(event_key):
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(get_state(), "find_decision_for_event", _boom)
    body = _get(monkeypatch, _FakePull(merged=True))
    assert _has_form(body), (
        "an unanswered decision lookup must fall back to today's behavior, not "
        "to the merged-PR suppression"
    )


# --------------------------------------------------------------------------- #
# Copy + contract.
# --------------------------------------------------------------------------- #


def test_blocker_reason_localizes_to_japanese(_configured, _inmemory, monkeypatch):
    body = _get(monkeypatch, _FakePull(merged=True), lang="ja")
    assert "マージされている" in body
    assert "already merged" not in body


@pytest.mark.parametrize(
    "blocker",
    [
        gh_lib.FRESH_APPLY_MERGED,
        gh_lib.FRESH_APPLY_CLOSED,
        gh_lib.FRESH_APPLY_DRAFT,
        gh_lib.FRESH_APPLY_HEAD_MOVED,
    ],
)
def test_every_blocker_has_operator_copy(blocker):
    """A blocker with no copy would silently degrade to "no blocker" at runtime.

    ``_FRESH_APPLY_REASON_KEY.get()`` is deliberately forgiving so an unmapped
    key cannot 500 the always-200 GET; this test is what makes that forgiveness
    safe by failing CI instead.
    """
    from agent import approval_i18n
    from agent.main import _FRESH_APPLY_REASON_KEY

    key = _FRESH_APPLY_REASON_KEY.get(blocker)
    assert key is not None, f"{blocker} has no REASON_EN key"
    assert key in approval_i18n.REASON_EN
    assert approval_i18n.REASON_EN[key] in approval_i18n.REASON_JA, (
        f"{key} renders English under ?lang=ja"
    )
