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
