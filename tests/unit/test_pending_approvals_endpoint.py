"""HTTP surface for GET /infra/pending-approvals (open infra PRs for the panel).

Fakes the GitHub repo (no network). The lister uses a server-side label filter
(``get_issues(state="open", labels=[driftscribe-infra])``) and a ``.pull_request``
test, so the fake returns already-labeled SimpleNamespace items as GitHub would.
"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import agent.main as main
from agent.auth import verify_token


@pytest.fixture(autouse=True)
def _reset_cache():
    # The endpoint memoizes in a module global; isolate every test so a prior
    # test's cached list can't mask a later degrade/relist.
    main._PENDING_APPROVALS_CACHE = None
    yield
    main._PENDING_APPROVALS_CACHE = None


@pytest.fixture
def client_with_token():
    main.app.dependency_overrides[verify_token] = lambda: None
    yield TestClient(main.app)
    main.app.dependency_overrides.pop(verify_token, None)


@pytest.fixture
def client_no_token(monkeypatch):
    # Real verify_token: a token IS configured, the request sends none → 401.
    main.app.dependency_overrides.pop(verify_token, None)
    monkeypatch.setenv("DRIFTSCRIBE_TOKEN", "test-secret")
    main.get_settings.cache_clear()
    yield TestClient(main.app)
    main.get_settings.cache_clear()


def _issue(number, title, body, *, is_pr, html_url="https://gh/x"):
    return SimpleNamespace(
        number=number,
        title=title,
        body=body,
        html_url=html_url,
        pull_request=SimpleNamespace() if is_pr else None,
    )


def test_lists_open_infra_adoption_prs(monkeypatch, client_with_token):
    # As GitHub would return for labels=[driftscribe-infra], state=open:
    issues = [
        _issue(168, "Adopt topic", "**Import id:** `projects/p/topics/adopt-probe-topic`", is_pr=True),
        _issue(169, "Tracking issue", "not a PR", is_pr=False),  # issue, not a PR → excluded
        _issue(171, "Add alerting", "freehand body", is_pr=True),  # infra PR, no resource
    ]
    fake_repo = SimpleNamespace(get_issues=lambda **kw: issues)
    monkeypatch.setattr(main, "get_repo", lambda *a, **k: fake_repo)

    r = client_with_token.get("/infra/pending-approvals")
    assert r.status_code == 200
    body = r.json()
    nums = {a["pr_number"] for a in body["approvals"]}
    assert nums == {168, 171}
    a168 = next(a for a in body["approvals"] if a["pr_number"] == 168)
    assert a168["asset_type"] == "pubsub.googleapis.com/Topic"
    assert a168["resource_name"] == "adopt-probe-topic"
    assert body.get("degraded") in (False, None)


def test_lists_request_newest_first_explicitly(monkeypatch, client_with_token):
    # The docstring promises "newest first"; enforce it via explicit sort params
    # rather than relying on GitHub's implicit default (adversarial review).
    captured: dict = {}

    def fake_get_issues(**kw):
        captured.update(kw)
        return []

    fake_repo = SimpleNamespace(get_issues=fake_get_issues)
    monkeypatch.setattr(main, "get_repo", lambda *a, **k: fake_repo)

    r = client_with_token.get("/infra/pending-approvals")
    assert r.status_code == 200
    assert captured.get("state") == "open"
    assert captured.get("labels") == ["driftscribe-infra"]
    assert captured.get("sort") == "created"
    assert captured.get("direction") == "desc"


def test_github_failure_degrades_soft(monkeypatch, client_with_token):
    def boom(*a, **k):
        raise RuntimeError("github down")

    monkeypatch.setattr(main, "get_repo", boom)
    r = client_with_token.get("/infra/pending-approvals")
    assert r.status_code == 200
    assert r.json() == {"approvals": [], "degraded": True}


def test_requires_token(client_no_token):
    assert client_no_token.get("/infra/pending-approvals").status_code in (401, 403)


# --------------------------------------------------------------------------- #
# ds-qua — the authoring reasoning trace rides the DTO
# --------------------------------------------------------------------------- #
@pytest.fixture
def trace_store():
    from agent.iac_pr_trace_store import (
        InMemoryIacPrTraceStore,
        _reset_iac_pr_trace_store_for_tests,
        _set_iac_pr_trace_store_for_tests,
    )

    store = InMemoryIacPrTraceStore()
    _set_iac_pr_trace_store_for_tests(store)
    yield store
    _reset_iac_pr_trace_store_for_tests()


def _one_pr(monkeypatch, number=168):
    issues = [_issue(number, "Adopt topic", "freehand body", is_pr=True)]
    monkeypatch.setattr(
        main, "get_repo", lambda *a, **k: SimpleNamespace(get_issues=lambda **kw: issues)
    )


@pytest.fixture
def listing_repo(monkeypatch):
    """The repo the listing is built from. Real value required: the store refuses a
    blank repo key, since an unscoped record is exactly the cross-repo over-claim
    this feature must not have."""
    monkeypatch.setenv("GITHUB_REPO", "adi-prasetyo/driftscribe")
    main.get_settings.cache_clear()
    yield "adi-prasetyo/driftscribe"
    main.get_settings.cache_clear()


def test_a_recorded_authoring_trace_reaches_the_dto(
    monkeypatch, client_with_token, trace_store, listing_repo
):
    trace = "f" * 32
    assert trace_store.set_if_absent(listing_repo, 168, trace) is True
    _one_pr(monkeypatch)

    r = client_with_token.get("/infra/pending-approvals")
    assert r.status_code == 200
    (a,) = r.json()["approvals"]
    assert a["authoring_trace_id"] == trace


def test_an_unrecorded_pr_reports_a_blank_trace_never_a_guess(
    monkeypatch, client_with_token, trace_store
):
    """A PR opened before this record existed must produce NO link. The key is
    always present so the SPA never distinguishes 'absent' from 'unknown'."""
    _one_pr(monkeypatch)
    r = client_with_token.get("/infra/pending-approvals")
    (a,) = r.json()["approvals"]
    assert a["authoring_trace_id"] == ""


def test_a_trace_recorded_under_another_repo_never_leaks_across(
    monkeypatch, client_with_token, trace_store, listing_repo
):
    """THE repo-scoping finding. PR numbers are repository-local, and the authoring
    side can target a different repo via IAC_EDITOR_TARGET_REPO_OVERRIDE. Repo B's
    PR #168 must not supply evidence for repo A's PR #168."""
    trace_store.set_if_absent("owner/some-other-repo", 168, "f" * 32)
    _one_pr(monkeypatch)

    r = client_with_token.get("/infra/pending-approvals")
    (a,) = r.json()["approvals"]
    assert a["authoring_trace_id"] == ""


def test_a_malformed_stored_trace_degrades_to_blank(
    monkeypatch, client_with_token, trace_store
):
    """Not replayable through ?reasoning= → no link, rather than a link that opens
    once and then fails to restore when shared."""

    class _Junk:
        def get(self, repo, pr_number):
            return "not-a-trace-id"

        def set_if_absent(self, repo, pr_number, trace_id):
            return True

    from agent.iac_pr_trace_store import _set_iac_pr_trace_store_for_tests

    _set_iac_pr_trace_store_for_tests(_Junk())
    _one_pr(monkeypatch)

    r = client_with_token.get("/infra/pending-approvals")
    (a,) = r.json()["approvals"]
    assert a["authoring_trace_id"] == ""


def test_a_trace_store_outage_does_not_degrade_the_listing(
    monkeypatch, client_with_token, trace_store
):
    """The real FirestoreIacPrTraceStore catches its own outages, so this fixture is
    not a Firestore outage — it models a BROKEN store implementation or a bad test
    override. That is exactly what the endpoint-side guard is for: the row is
    actionable approval data and must not depend on optional evidence surviving."""

    class _Exploding:
        def get(self, repo, pr_number):
            raise RuntimeError("firestore is down")

        def set_if_absent(self, repo, pr_number, trace_id):
            raise RuntimeError("firestore is down")

    from agent.iac_pr_trace_store import _set_iac_pr_trace_store_for_tests

    _set_iac_pr_trace_store_for_tests(_Exploding())
    _one_pr(monkeypatch)

    r = client_with_token.get("/infra/pending-approvals")
    assert r.status_code == 200
    body = r.json()
    # The row MUST survive. An earlier version of this test accepted
    # `len(...) == 1 or degraded is True`, which permitted the exact failure it was
    # named for: an evidence lookup blanking the whole panel and hiding an approval
    # the operator can act on. Optional evidence costs the link, never the row.
    assert body.get("degraded") in (False, None)
    (a,) = body["approvals"]
    assert a["pr_number"] == 168
    assert a["authoring_trace_id"] == ""


def test_a_blank_listing_repo_yields_no_link_rather_than_an_unscoped_guess(
    monkeypatch, client_with_token, trace_store
):
    """No `listing_repo` fixture: GITHUB_REPO is unset. An unscoped key would be the
    cross-repo over-claim itself, so the store refuses it in both directions."""
    assert trace_store.set_if_absent("", 168, "f" * 32) is False
    _one_pr(monkeypatch)
    r = client_with_token.get("/infra/pending-approvals")
    (a,) = r.json()["approvals"]
    assert a["authoring_trace_id"] == ""
