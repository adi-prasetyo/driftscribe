"""Integration tests for the "view source" affordance + cache on the read-only
GET /iac-approvals/{pr_number}, and the operator-gated POST .../refresh-source.

The approval page renders the actual ``.tf`` a PR adds/changes (read-through
cached on the verified head_sha). Source is visible to everyone (demo posture);
the manual refresh is operator-gated. Mirrors the resolution-seam mocking of
test_iac_approval_get.py and the CF-operator/Origin handling of
test_iac_approval_post.py.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import agent.main as main_mod
from agent.auth import require_cf_operator
from agent.config import get_settings
from agent.iac_artifacts import C2CommentRef, IacPlanView
from agent.iac_pr_source_cache import InMemoryIacPrSourceCacheStore
from agent.main import (
    _IAC_PR_SOURCE_FORMAT_VERSION,
    _set_iac_pr_source_cache_store_for_tests,
    app,
)

_HEAD = "a" * 40
_PLAN_SHA = "b" * 64
_PLAN_JSON_SHA = "c" * 64
_BUCKET = "test-proj-tofu-artifacts"
_PREFIX = f"gs://{_BUCKET}/pr-42/{_HEAD}/run-7-1/"
_META_URI = _PREFIX + "metadata.json"
_ORIGIN = "https://driftscribe.example"
_OPERATOR = "op@example.com"

_FILE = {"path": "iac/adopt_bucket_demo.tf", "content": 'resource "google_storage_bucket" "demo" {}\n'}


def _metadata() -> dict:
    return {
        "schema_version": "c2.v1",
        "repo": "theghostsquad00/driftscribe",
        "pr_number": 42,
        "head_sha": _HEAD,
        "base_sha": "d" * 40,
        "workflow_run_id": "7700000001",
        "workflow_run_attempt": "1",
        "artifact_uri_plan": _PREFIX + "plan.tfplan",
        "artifact_uri_json": _PREFIX + "plan.json",
        "generation_plan": "1700000000000001",
        "generation_json": "1700000000000002",
        "plan_sha256": _PLAN_SHA,
        "plan_json_sha256": _PLAN_JSON_SHA,
        "opentofu_version": "1.12.0",
        "provider_lockfile_sha256": "e" * 64,
    }


def _ref() -> C2CommentRef:
    return C2CommentRef(
        head_sha=_HEAD,
        plan_sha256=_PLAN_SHA,
        plan_json_sha256=_PLAN_JSON_SHA,
        generation_plan="1700000000000001",
        generation_json="1700000000000002",
        generation_metadata="1700000000000003",
        artifact_uri_plan=_PREFIX + "plan.tfplan",
        artifact_uri_json=_PREFIX + "plan.json",
        artifact_uri_metadata=_META_URI,
        opentofu_version="1.12.0",
        comment_id=556677,
        tofu_show_text="# import — 0 to add, 0 to change, 0 to destroy",
    )


def _view(**overrides) -> IacPlanView:
    base = dict(
        metadata=_metadata(),
        tofu_show_text=_ref().tofu_show_text,
        integrity_ok=True,
        denylist_violations=[],
        unverifiable=False,
        _artifact_uri_metadata=_META_URI,
        _generation_metadata="1700000000000003",
    )
    base.update(overrides)
    return IacPlanView(**base)


@pytest.fixture
def _configured(monkeypatch):
    monkeypatch.setenv("GITHUB_REPO", "theghostsquad00/driftscribe")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")
    monkeypatch.setenv("DRIFTSCRIBE_TOKEN", "static-server-token")
    monkeypatch.setenv("TOFU_ARTIFACTS_BUCKET", _BUCKET)
    monkeypatch.setenv("COORDINATOR_ORIGIN", _ORIGIN)
    monkeypatch.setenv("DRY_RUN", "false")
    # GCP_PROJECT="" keeps get_state() on the InMemory store; we still inject the
    # source store explicitly so the test owns it.
    monkeypatch.setenv("GCP_PROJECT", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def _src_store():
    store = InMemoryIacPrSourceCacheStore()
    _set_iac_pr_source_cache_store_for_tests(store)
    return store


def _patch_resolve(monkeypatch, *, ref, view):
    monkeypatch.setattr("agent.main.get_repo", lambda token, repo: object())
    monkeypatch.setattr(
        main_mod.iac_artifacts, "find_latest_c2_comment", lambda repo, pr: ref
    )
    monkeypatch.setattr(
        main_mod.iac_artifacts,
        "load_plan_view",
        lambda r, *, bucket_name, client=None, expected_repo=None: view,
    )


def _patch_source_fetch(monkeypatch, *, files, truncated=False, calls=None, raises=None):
    def fake(repo, pr_number, head_sha, **kw):
        if calls is not None:
            calls.append((pr_number, head_sha))
        if raises is not None:
            raise raises
        return {"files": list(files), "truncated": truncated}

    monkeypatch.setattr(main_mod.github, "list_pr_iac_tf_files", fake)


# --------------------------------------------------------------------------- #
# GET — view source
# --------------------------------------------------------------------------- #


def test_source_block_rendered_on_cache_miss(_configured, _src_store, monkeypatch):
    _patch_resolve(monkeypatch, ref=_ref(), view=_view())
    calls = []
    _patch_source_fetch(monkeypatch, files=[_FILE], calls=calls)

    resp = TestClient(app).get("/iac-approvals/42")
    assert resp.status_code == 200
    body = resp.text
    assert 'data-testid="iac-source"' in body
    assert _FILE["path"] in body
    assert "google_storage_bucket" in body  # content rendered
    assert 'data-testid="iac-source-demo-note"' in body
    # Fetched once and persisted under the verified head_sha.
    assert calls == [(42, _HEAD)]
    rec = _src_store.get(42)
    assert rec["head_sha"] == _HEAD
    assert rec["files"] == [_FILE]
    assert rec["format_version"] == _IAC_PR_SOURCE_FORMAT_VERSION


def test_cache_hit_does_not_call_github(_configured, _src_store, monkeypatch):
    import time as _t

    _src_store.set(
        42,
        {
            "format_version": _IAC_PR_SOURCE_FORMAT_VERSION,
            "written_at": _t.time(),
            "head_sha": _HEAD,
            "files": [_FILE],
            "truncated": False,
        },
    )
    _patch_resolve(monkeypatch, ref=_ref(), view=_view())
    calls = []
    _patch_source_fetch(monkeypatch, files=[_FILE], calls=calls)

    resp = TestClient(app).get("/iac-approvals/42")
    assert resp.status_code == 200
    assert _FILE["path"] in resp.text
    assert calls == [], "a warm cache hit must not call the GitHub API"


def test_head_sha_mismatch_refetches(_configured, _src_store, monkeypatch):
    import time as _t

    _src_store.set(
        42,
        {
            "format_version": _IAC_PR_SOURCE_FORMAT_VERSION,
            "written_at": _t.time(),
            "head_sha": "f" * 40,  # stale — PR has since moved to _HEAD
            "files": [{"path": "iac/old.tf", "content": "old\n"}],
            "truncated": False,
        },
    )
    _patch_resolve(monkeypatch, ref=_ref(), view=_view())
    calls = []
    _patch_source_fetch(monkeypatch, files=[_FILE], calls=calls)

    resp = TestClient(app).get("/iac-approvals/42")
    assert resp.status_code == 200
    assert calls == [(42, _HEAD)], "stale head_sha must trigger a refetch"
    assert _src_store.get(42)["head_sha"] == _HEAD  # overwritten


def test_corrupt_cached_entry_is_ignored_and_refetched(
    _configured, _src_store, monkeypatch
):
    import time as _t

    # Same head_sha + format_version, but a malformed entry (path not iac/**.tf):
    # a tampered/old-shaped doc must be treated as a miss and refetched, never
    # rendered as-is.
    _src_store.set(
        42,
        {
            "format_version": _IAC_PR_SOURCE_FORMAT_VERSION,
            "written_at": _t.time(),
            "head_sha": _HEAD,
            "files": [{"path": "../etc/passwd", "content": "root:x:0:0"}],
            "truncated": False,
        },
    )
    _patch_resolve(monkeypatch, ref=_ref(), view=_view())
    calls = []
    _patch_source_fetch(monkeypatch, files=[_FILE], calls=calls)

    resp = TestClient(app).get("/iac-approvals/42")
    assert resp.status_code == 200
    assert "../etc/passwd" not in resp.text  # the bad entry is never rendered
    assert calls == [(42, _HEAD)], "a corrupt cached doc must trigger a refetch"
    assert _src_store.get(42)["files"] == [_FILE]  # overwritten with valid data


def test_fetch_failure_is_fail_soft(_configured, _src_store, monkeypatch):
    _patch_resolve(monkeypatch, ref=_ref(), view=_view())
    _patch_source_fetch(monkeypatch, files=[], raises=RuntimeError("github down"))

    resp = TestClient(app).get("/iac-approvals/42")
    assert resp.status_code == 200  # always-200 GET, never a 5xx
    assert 'data-testid="iac-source"' not in resp.text


def test_source_not_rendered_for_unverifiable_view(_configured, _src_store, monkeypatch):
    _patch_resolve(monkeypatch, ref=_ref(), view=_view(unverifiable=True))
    calls = []
    _patch_source_fetch(monkeypatch, files=[_FILE], calls=calls)

    resp = TestClient(app).get("/iac-approvals/42")
    assert resp.status_code == 200
    assert 'data-testid="iac-source"' not in resp.text
    assert calls == [], "no source fetch for an untrustworthy artifact"


def test_refresh_button_shown_for_operator_present(_configured, _src_store, monkeypatch):
    # CF Access configured + a JWT header present (presence-checked here; the POST
    # crypto-verifies) ⇒ refresh control shown.
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "team.cloudflareaccess.com")
    monkeypatch.setenv("CF_ACCESS_AUD_TAG", "aud-tag")
    get_settings.cache_clear()
    _patch_resolve(monkeypatch, ref=_ref(), view=_view())
    _patch_source_fetch(monkeypatch, files=[_FILE])
    resp = TestClient(app).get(
        "/iac-approvals/42", headers={"Cf-Access-Jwt-Assertion": "dummy.jwt.token"}
    )
    assert 'data-testid="iac-source"' in resp.text
    assert 'data-testid="refresh-source"' in resp.text


def test_refresh_button_hidden_when_cf_unconfigured(_configured, _src_store, monkeypatch):
    # CF Access unconfigured (local/dev): the POST would 503, so hide the control.
    _patch_resolve(monkeypatch, ref=_ref(), view=_view())
    _patch_source_fetch(monkeypatch, files=[_FILE])
    resp = TestClient(app).get("/iac-approvals/42")
    assert 'data-testid="iac-source"' in resp.text  # source still shown
    assert 'data-testid="refresh-source"' not in resp.text


def test_refresh_button_hidden_for_anonymous_demo_viewer(
    _configured, _src_store, monkeypatch
):
    # CF Access configured + NO Cf-Access-Jwt-Assertion header ⇒ anonymous demo
    # viewer: source visible, refresh control hidden.
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "team.cloudflareaccess.com")
    monkeypatch.setenv("CF_ACCESS_AUD_TAG", "aud-tag")
    get_settings.cache_clear()
    _patch_resolve(monkeypatch, ref=_ref(), view=_view())
    _patch_source_fetch(monkeypatch, files=[_FILE])
    resp = TestClient(app).get("/iac-approvals/42")
    assert 'data-testid="iac-source"' in resp.text  # source still shown
    assert 'data-testid="refresh-source"' not in resp.text


# --------------------------------------------------------------------------- #
# POST .../refresh-source
# --------------------------------------------------------------------------- #


def test_refresh_post_forces_refetch_and_redirects(_configured, _src_store, monkeypatch):
    import time as _t

    # Cache already warm for the current head — refresh must STILL refetch (force).
    _src_store.set(
        42,
        {
            "format_version": _IAC_PR_SOURCE_FORMAT_VERSION,
            "written_at": _t.time(),
            "head_sha": _HEAD,
            "files": [_FILE],
            "truncated": False,
        },
    )
    _patch_resolve(monkeypatch, ref=_ref(), view=_view())
    calls = []
    fresh = {"path": "iac/adopt_bucket_demo.tf", "content": "refreshed\n"}
    _patch_source_fetch(monkeypatch, files=[fresh], calls=calls)
    app.dependency_overrides[require_cf_operator] = lambda: _OPERATOR
    try:
        resp = TestClient(app).post(
            "/iac-approvals/42/refresh-source",
            headers={"Origin": _ORIGIN},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.pop(require_cf_operator, None)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/iac-approvals/42"
    assert calls == [(42, _HEAD)], "refresh must force a refetch even on a warm cache"
    assert _src_store.get(42)["files"] == [fresh]  # resaved


def test_refresh_post_rejects_cross_site(_configured, _src_store, monkeypatch):
    _patch_resolve(monkeypatch, ref=_ref(), view=_view())
    calls = []
    _patch_source_fetch(monkeypatch, files=[_FILE], calls=calls)
    app.dependency_overrides[require_cf_operator] = lambda: _OPERATOR
    try:
        resp = TestClient(app).post(
            "/iac-approvals/42/refresh-source",
            headers={"Origin": "https://evil.example.com"},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.pop(require_cf_operator, None)
    assert resp.status_code == 403
    assert calls == [], "a cross-site refresh must not fetch"


def test_refresh_post_requires_operator(_configured, _src_store, monkeypatch):
    # CF Access NOT configured ⇒ require_cf_operator fail-closes 503 before the body.
    _patch_resolve(monkeypatch, ref=_ref(), view=_view())
    resp = TestClient(app).post(
        "/iac-approvals/42/refresh-source",
        headers={"Origin": _ORIGIN},
        follow_redirects=False,
    )
    assert resp.status_code == 503
