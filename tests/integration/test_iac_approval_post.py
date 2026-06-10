"""Integration tests for the POST /iac-approvals/{pr_number} state machine (C5e-3).

The POST performs propose-on-approve under a mandatory CF-Access operator
identity, acting on EXACTLY the artifact the GET page pinned into the signed form
token. It executes the §2 orchestration state machine: Origin + CSRF check →
re-resolve + pin assert → pre-propose readiness → idempotency claim →
``/propose`` → 5b head re-check → ``/apply`` (release matrix per the §2 table) →
merge (reconcile on merge-fail).

Mocking strategy: override ``require_cf_operator`` (so the CF dep returns a fixed
operator email without a real JWT), mint a valid ``form_token`` via
``iac_csrf.mint_form_token``, and monkeypatch the resolution + worker + github
seams on ``agent.main``. The ``Origin`` header is sent matching
``coordinator_origin``.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import agent.main as main_mod
from agent import iac_csrf, worker_client
from agent.auth import require_cf_operator
from agent.config import get_settings
from agent.iac_artifacts import C2CommentRef, IacPlanView
from agent.main import app, get_state
from driftscribe_lib.github import PrMergeBlockedError, PrNotEligibleError

_HEAD = "a" * 40
_PLAN_SHA = "b" * 64
_PLAN_JSON_SHA = "c" * 64
_BUCKET = "test-proj-tofu-artifacts"
_PREFIX = f"gs://{_BUCKET}/pr-42/{_HEAD}/run-7-1/"
_META_URI = _PREFIX + "metadata.json"
_GEN_META = "1700000000000003"
_ORIGIN = "https://driftscribe.adp-app.com"
_OPERATOR = "operator@example.com"
_JWT = "raw-cf-access-jwt-value"
_GEN_IAC_TREE = "1700000000000009"
_IAC_TREE_HASH = "f" * 64


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
        generation_metadata=_GEN_META,
        artifact_uri_plan=_PREFIX + "plan.tfplan",
        artifact_uri_json=_PREFIX + "plan.json",
        artifact_uri_metadata=_META_URI,
        opentofu_version="1.12.0",
        comment_id=556677,
        tofu_show_text="~ image = old -> new",
    )


# A non-create (in-place UPDATE) plan.json — the C5 default. has_create=False ⇒ the
# C5 apply-first path. Create-class tests override _plan_json with a create action.
_UPDATE_PLAN_JSON = {
    "resource_changes": [
        {"address": "google_cloud_run_v2_service.x", "type": "google_cloud_run_v2_service",
         "change": {"actions": ["update"]}}
    ]
}
_CREATE_PLAN_JSON = {
    "resource_changes": [
        {"address": "google_storage_bucket.new", "type": "google_storage_bucket",
         "change": {"actions": ["create"]}}
    ]
}


def _view(**overrides) -> IacPlanView:
    base = dict(
        metadata=_metadata(),
        tofu_show_text=_ref().tofu_show_text,
        integrity_ok=True,
        denylist_violations=[],
        unverifiable=False,
        _artifact_uri_metadata=_META_URI,
        _generation_metadata=_GEN_META,
        # Default to a non-create plan so existing C5 tests take the apply-first path.
        _plan_json=_UPDATE_PLAN_JSON,
    )
    base.update(overrides)
    return IacPlanView(**base)


def _create_view(**overrides) -> IacPlanView:
    """A create-class view (has_create=True) WITH a C6 sidecar (generation + hash)."""
    base = dict(
        _plan_json=_CREATE_PLAN_JSON,
        _generation_iac_tree=_GEN_IAC_TREE,
        _iac_tree_hash=_IAC_TREE_HASH,
    )
    base.update(overrides)
    return _view(**base)


@pytest.fixture
def _configured(monkeypatch):
    monkeypatch.setenv("GITHUB_REPO", "theghostsquad00/driftscribe")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")
    monkeypatch.setenv("DRIFTSCRIBE_TOKEN", "static-server-token")
    monkeypatch.setenv("TOFU_ARTIFACTS_BUCKET", _BUCKET)
    monkeypatch.setenv("COORDINATOR_ORIGIN", _ORIGIN)
    monkeypatch.setenv("IAC_REQUIRED_CHECKS", "tofu,static-gate")
    monkeypatch.setenv("IAC_MERGE_METHOD", "squash")
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "team.cloudflareaccess.com")
    monkeypatch.setenv("CF_ACCESS_AUD_TAG", "aud-tag")
    # The approve path fail-closes under coordinator dry-run; the conftest defaults
    # DRY_RUN=true. Flip it off for the approve tests (a dedicated test covers the
    # dry-run 503). GCP_PROJECT="" keeps get_state() on the InMemory store
    # (get_state uses Firestore only when NOT dry_run AND gcp_project is set).
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("GCP_PROJECT", "")
    get_settings.cache_clear()
    app.dependency_overrides[require_cf_operator] = lambda: _OPERATOR
    yield
    app.dependency_overrides.pop(require_cf_operator, None)
    get_settings.cache_clear()


def _mint(pr_number=42, **overrides):
    kw = dict(
        pr_number=pr_number,
        head_sha=_HEAD,
        artifact_uri_metadata=_META_URI,
        generation_metadata=_GEN_META,
        plan_sha256=_PLAN_SHA,
        plan_json_sha256=_PLAN_JSON_SHA,
        comment_id=556677,
    )
    kw.update(overrides)
    return iac_csrf.mint_form_token(get_settings(), **kw)


def _patch_resolve(monkeypatch, *, ref=None, view=None):
    if ref is None:
        ref = _ref()
    if view is None:
        view = _view()
    monkeypatch.setattr(main_mod, "_resolve_iac_plan", lambda s, pr: (ref, view))


def _patch_repo(monkeypatch):
    monkeypatch.setattr(main_mod, "get_repo", lambda token, repo: object())


def _patch_github(
    monkeypatch,
    *,
    ready=None,
    head_after_propose=_HEAD,
    merge=None,
):
    """Patch the github seams. ``ready``/``merge`` may be callables raising."""
    if ready is None:
        def ready(*a, **k):
            return _HEAD
    monkeypatch.setattr(main_mod.github, "assert_pr_ready_at_sha", ready)
    monkeypatch.setattr(
        main_mod.github, "get_pr_head_sha", lambda repo, pr: head_after_propose
    )
    if merge is None:
        def merge(*a, **k):
            return {
                "merged": True,
                "already_merged": False,
                "number": 42,
                "url": "u",
            }
    monkeypatch.setattr(main_mod.github, "merge_pr_at_sha", merge)


def _patch_workers(
    monkeypatch,
    *,
    propose=None,
    apply_=None,
    plan_deny=None,
    notifier=None,
    baked_hash=None,
):
    calls = {"propose": [], "apply": [], "plan_deny": [], "notify": []}

    def _default_propose(uri, gen, approver, jwt, generation_iac_tree=None):
        calls["propose"].append((uri, gen, approver, jwt, generation_iac_tree))
        return {"approval_id": "ap-1", "approval_token": "tok-1", "expires_at": 1}

    def _default_apply(aid, tok, jwt, generation_iac_tree=None):
        calls["apply"].append((aid, tok, jwt, generation_iac_tree))
        return {"approval_id": aid, "status": "applied", "apply_attempt_id": "att-1"}

    def _default_plan_deny(aid, tok):
        calls["plan_deny"].append((aid, tok))
        return {"status": "denied"}

    def _default_notify(worker, payload, **kw):
        calls["notify"].append((worker, payload))
        return {"ok": True}

    def _default_baked_hash():
        # C6c: by default the worker IS re-baked (baked hash == the create view's).
        return {"iac_tree_hash": _IAC_TREE_HASH}

    monkeypatch.setattr(
        main_mod.worker_client, "call_propose", propose or _default_propose
    )
    monkeypatch.setattr(
        main_mod.worker_client, "call_apply", apply_ or _default_apply
    )
    monkeypatch.setattr(
        main_mod.worker_client, "call_plan_deny", plan_deny or _default_plan_deny
    )
    monkeypatch.setattr(main_mod.worker_client, "call", notifier or _default_notify)
    monkeypatch.setattr(
        main_mod.worker_client, "get_baked_iac_hash", baked_hash or _default_baked_hash
    )
    return calls


def _post(client, *, token, decision="approve", origin=_ORIGIN, pr=42, sec_fetch_site=None):
    headers = {"Cf-Access-Jwt-Assertion": _JWT}
    if origin is not None:
        headers["Origin"] = origin
    if sec_fetch_site is not None:
        headers["Sec-Fetch-Site"] = sec_fetch_site
    return client.post(
        f"/iac-approvals/{pr}",
        data={"form_token": token, "decision": decision},
        headers=headers,
    )


# --------------------------------------------------------------------------- #
# Reject
# --------------------------------------------------------------------------- #


def test_reject_is_noop_no_worker_calls(_configured, monkeypatch):
    _patch_resolve(monkeypatch)
    calls = _patch_workers(monkeypatch)
    client = TestClient(app)
    resp = _post(client, token=_mint(), decision="reject")
    assert resp.status_code == 200
    assert "reject" in resp.text.lower()
    assert calls["propose"] == []
    assert calls["apply"] == []


# --------------------------------------------------------------------------- #
# Origin / CSRF / config
# --------------------------------------------------------------------------- #


def test_missing_origin_403(_configured, monkeypatch):
    _patch_resolve(monkeypatch)
    _patch_workers(monkeypatch)
    client = TestClient(app)
    resp = _post(client, token=_mint(), origin=None)
    assert resp.status_code == 403


def test_bad_origin_403(_configured, monkeypatch):
    _patch_resolve(monkeypatch)
    _patch_workers(monkeypatch)
    client = TestClient(app)
    resp = _post(client, token=_mint(), origin="https://evil.example.com")
    assert resp.status_code == 403


def test_malformed_origin_fails_closed_403_not_500(_configured, monkeypatch):
    # A non-numeric port makes urllib raise ValueError on .port — must be a
    # clean 403, never a 500 (Codex completed-work review).
    _patch_resolve(monkeypatch)
    _patch_workers(monkeypatch)
    client = TestClient(app)
    resp = _post(client, token=_mint(), origin="https://host:notaport")
    assert resp.status_code == 403


def test_null_origin_with_same_origin_fetch_site_ok(_configured, monkeypatch):
    # Chromium serializes the Origin of a no-referrer navigation (form) POST as
    # the opaque string "null" even for a genuine same-origin submit. The origin
    # gate must accept it on the strength of Sec-Fetch-Site: same-origin (a
    # Forbidden header the browser sets and a cross-site page cannot forge), so
    # the full approve path runs to completion. NOTE decision="approve" is
    # required: reject short-circuits BEFORE the origin gate (see handler), so a
    # reject would pass regardless of the gate and prove nothing.
    _patch_resolve(monkeypatch)
    _patch_repo(monkeypatch)
    _patch_github(monkeypatch)
    _patch_workers(monkeypatch)
    client = TestClient(app)
    resp = _post(client, token=_mint(), origin="null", sec_fetch_site="same-origin")
    assert resp.status_code == 200
    assert "applied and merged" in resp.text.lower()


def test_missing_origin_with_same_origin_fetch_site_ok(_configured, monkeypatch):
    # Same fallback when the engine omits Origin entirely on a same-origin POST.
    _patch_resolve(monkeypatch)
    _patch_repo(monkeypatch)
    _patch_github(monkeypatch)
    _patch_workers(monkeypatch)
    client = TestClient(app)
    resp = _post(client, token=_mint(), origin=None, sec_fetch_site="same-origin")
    assert resp.status_code == 200
    assert "applied and merged" in resp.text.lower()


def test_null_origin_cross_site_fetch_site_403(_configured, monkeypatch):
    # A cross-site attacker that suppresses its own Origin to "null" via
    # no-referrer still gets Sec-Fetch-Site: cross-site — must be rejected.
    _patch_resolve(monkeypatch)
    _patch_workers(monkeypatch)
    client = TestClient(app)
    resp = _post(
        client, token=_mint(), origin="null", sec_fetch_site="cross-site",
    )
    assert resp.status_code == 403


def test_null_origin_without_fetch_site_fails_closed_403(_configured, monkeypatch):
    # Opaque Origin + no Sec-Fetch-Site (older engines) → fail-closed.
    _patch_resolve(monkeypatch)
    _patch_workers(monkeypatch)
    client = TestClient(app)
    resp = _post(client, token=_mint(), origin="null")
    assert resp.status_code == 403


def test_unconfigured_origin_refuses_even_same_origin_403(_configured, monkeypatch):
    # Fail-closed invariant (Codex): an empty coordinator_origin must refuse ALL
    # POSTs — the Sec-Fetch-Site fallback must NOT bypass an unconfigured origin.
    monkeypatch.setenv("COORDINATOR_ORIGIN", "")
    get_settings.cache_clear()
    _patch_resolve(monkeypatch)
    _patch_workers(monkeypatch)
    client = TestClient(app)
    resp = _post(client, token=_mint(), origin="null", sec_fetch_site="same-origin")
    assert resp.status_code == 403


def test_forged_form_token_403(_configured, monkeypatch):
    _patch_resolve(monkeypatch)
    _patch_workers(monkeypatch)
    client = TestClient(app)
    resp = _post(client, token="not.a.valid.token")
    assert resp.status_code == 403


def test_expired_form_token_403(_configured, monkeypatch):
    _patch_resolve(monkeypatch)
    _patch_workers(monkeypatch)
    # ttl in the past relative to now.
    token = iac_csrf.mint_form_token(
        get_settings(),
        pr_number=42,
        head_sha=_HEAD,
        artifact_uri_metadata=_META_URI,
        generation_metadata=_GEN_META,
        plan_sha256=_PLAN_SHA,
        plan_json_sha256=_PLAN_JSON_SHA,
        comment_id=556677,
        ttl_seconds=-10,
    )
    client = TestClient(app)
    resp = _post(client, token=token)
    assert resp.status_code == 403


def test_driftscribe_token_unset_503(_configured, monkeypatch):
    # Mint while configured, then unset the server token → verify path 503s.
    token = _mint()
    monkeypatch.setenv("DRIFTSCRIBE_TOKEN", "")
    get_settings.cache_clear()
    _patch_resolve(monkeypatch)
    _patch_workers(monkeypatch)
    client = TestClient(app)
    resp = _post(client, token=token)
    assert resp.status_code == 503


def test_dry_run_refused_503(_configured, monkeypatch):
    # Coordinator dry-run: refuse BEFORE /propose — never apply live infra then
    # skip the merge (Codex C5e-3 completed-work review, BLOCKER).
    monkeypatch.setenv("DRY_RUN", "true")
    get_settings.cache_clear()
    token = _mint()
    _patch_resolve(monkeypatch)
    calls = _patch_workers(monkeypatch)
    client = TestClient(app)
    resp = _post(client, token=token)
    assert resp.status_code == 503
    assert calls["propose"] == []
    assert calls["apply"] == []


def test_comment_id_pin_mismatch_409(_configured, monkeypatch):
    # The signed token pins comment_id; a mismatch (all other fields equal) is a
    # changed-plan 409, not a silent accept (Codex C5e-3 review, IMPORTANT).
    _patch_resolve(monkeypatch)  # _ref() has comment_id=556677
    calls = _patch_workers(monkeypatch)
    token = _mint(comment_id=999999)
    client = TestClient(app)
    resp = _post(client, token=token)
    assert resp.status_code == 409
    assert calls["propose"] == []


# --------------------------------------------------------------------------- #
# Malformed worker responses (Codex C5e-3 completed-work review)
# --------------------------------------------------------------------------- #


def test_propose_malformed_response_502(_configured, monkeypatch):
    # A 2xx /propose missing approval_id/token must NOT flow None into /apply:
    # release the event, 502, never apply.
    _patch_resolve(monkeypatch)
    _patch_repo(monkeypatch)
    _patch_github(monkeypatch)

    def bad_propose(uri, gen, approver, jwt):
        return {"expires_at": 1}  # no approval_id / approval_token

    calls = _patch_workers(monkeypatch, propose=bad_propose)
    client = TestClient(app)
    resp = _post(client, token=_mint())
    assert resp.status_code == 502
    assert calls["apply"] == []


def test_propose_nondict_response_502(_configured, monkeypatch):
    # A 2xx /propose whose JSON is not even an object (list/str/null) must 502
    # cleanly, not raise on .get() and strand the claim (Codex C5e-3 r2).
    _patch_resolve(monkeypatch)
    _patch_repo(monkeypatch)
    _patch_github(monkeypatch)

    def nondict_propose(uri, gen, approver, jwt):
        return ["not", "a", "dict"]

    calls = _patch_workers(monkeypatch, propose=nondict_propose)
    client = TestClient(app)
    resp = _post(client, token=_mint())
    assert resp.status_code == 502
    assert calls["apply"] == []


def test_apply_malformed_200_is_ambiguous_504(_configured, monkeypatch):
    # A malformed /apply 2xx (worker only returns 200 after a real apply) is
    # treated as AMBIGUOUS: no merge, terminal decision, alert, 504.
    _patch_resolve(monkeypatch)
    _patch_repo(monkeypatch)
    _patch_github(monkeypatch)
    merge_called: list[int] = []

    def merge(*a, **k):
        merge_called.append(1)
        return {"merged": True, "already_merged": False, "number": 42, "url": "u"}

    monkeypatch.setattr(main_mod.github, "merge_pr_at_sha", merge)

    def bad_apply(aid, tok, jwt):
        return {"status": "applied"}  # missing approval_id + apply_attempt_id

    calls = _patch_workers(monkeypatch, apply_=bad_apply)
    client = TestClient(app)
    resp = _post(client, token=_mint())
    assert resp.status_code == 504
    assert merge_called == []  # never merged unapplied/uncertain config
    assert any(c[0] == "notifier" for c in calls["notify"])


# --------------------------------------------------------------------------- #
# Re-resolve + pin
# --------------------------------------------------------------------------- #


def test_artifact_swap_after_get_409(_configured, monkeypatch):
    # form_token pins artifact A; _resolve_iac_plan now returns artifact B.
    token = _mint()  # pins _HEAD
    swapped_md = _metadata()
    swapped_md["head_sha"] = "f" * 40
    swapped_view = _view(metadata=swapped_md)
    swapped_ref = C2CommentRef(
        head_sha="f" * 40,
        plan_sha256=_PLAN_SHA,
        plan_json_sha256=_PLAN_JSON_SHA,
        generation_plan="1700000000000001",
        generation_json="1700000000000002",
        generation_metadata=_GEN_META,
        artifact_uri_plan=_PREFIX + "plan.tfplan",
        artifact_uri_json=_PREFIX + "plan.json",
        artifact_uri_metadata=_META_URI,
        opentofu_version="1.12.0",
        comment_id=556677,
        tofu_show_text="",
    )
    # consistent() must pass for B (so we reach the pin assert, not consistency).
    swapped_md_consistent = _metadata()
    swapped_md_consistent["head_sha"] = "f" * 40
    swapped_view.metadata = swapped_md_consistent
    monkeypatch.setattr(
        main_mod, "_resolve_iac_plan", lambda s, pr: (swapped_ref, swapped_view)
    )
    calls = _patch_workers(monkeypatch)
    client = TestClient(app)
    resp = _post(client, token=token)
    assert resp.status_code == 409
    assert "changed" in resp.text.lower() or "changed" in str(resp.content).lower()
    assert calls["propose"] == []


def test_unverifiable_view_403(_configured, monkeypatch):
    monkeypatch.setattr(
        main_mod, "_resolve_iac_plan",
        lambda s, pr: (_ref(), _view(unverifiable=True)),
    )
    _patch_workers(monkeypatch)
    client = TestClient(app)
    resp = _post(client, token=_mint())
    assert resp.status_code == 403


def test_integrity_mismatch_403(_configured, monkeypatch):
    monkeypatch.setattr(
        main_mod, "_resolve_iac_plan",
        lambda s, pr: (_ref(), _view(integrity_ok=False)),
    )
    _patch_workers(monkeypatch)
    client = TestClient(app)
    resp = _post(client, token=_mint())
    assert resp.status_code == 403


def test_denylist_violation_403(_configured, monkeypatch):
    monkeypatch.setattr(
        main_mod, "_resolve_iac_plan",
        lambda s, pr: (_ref(), _view(denylist_violations=[("r", "d")])),
    )
    _patch_workers(monkeypatch)
    client = TestClient(app)
    resp = _post(client, token=_mint())
    assert resp.status_code == 403


def test_view_none_403(_configured, monkeypatch):
    monkeypatch.setattr(main_mod, "_resolve_iac_plan", lambda s, pr: (_ref(), None))
    _patch_workers(monkeypatch)
    client = TestClient(app)
    resp = _post(client, token=_mint())
    assert resp.status_code == 403


def test_inconsistent_view_403(_configured, monkeypatch):
    # _iac_artifact_consistent returns False (metadata pr_number mismatch).
    md = _metadata()
    md["pr_number"] = 99
    monkeypatch.setattr(
        main_mod, "_resolve_iac_plan", lambda s, pr: (_ref(), _view(metadata=md))
    )
    _patch_workers(monkeypatch)
    client = TestClient(app)
    resp = _post(client, token=_mint())
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Pre-propose readiness
# --------------------------------------------------------------------------- #


def test_readiness_fail_blocks_before_claim_and_propose(_configured, monkeypatch):
    _patch_resolve(monkeypatch)
    _patch_repo(monkeypatch)

    def _ready_raises(*a, **k):
        raise PrMergeBlockedError("PR head moved (expected aaaaaaa)")

    _patch_github(monkeypatch, ready=_ready_raises)
    calls = _patch_workers(monkeypatch)
    client = TestClient(app)
    resp = _post(client, token=_mint())
    assert resp.status_code == 409
    assert calls["propose"] == []
    # No event claimed → a subsequent (now-ready) approve must proceed.
    state = get_state()
    ek = main_mod._iac_event_key(
        "theghostsquad00/driftscribe", 42, _HEAD, _GEN_META
    )
    assert state.find_decision_for_event(ek) is None


def test_readiness_empty_checks_409(_configured, monkeypatch):
    monkeypatch.setenv("IAC_REQUIRED_CHECKS", "")
    get_settings.cache_clear()
    _patch_resolve(monkeypatch)
    _patch_repo(monkeypatch)

    def _ready_raises(*a, **k):
        raise PrMergeBlockedError("no required checks configured — merge disabled")

    _patch_github(monkeypatch, ready=_ready_raises)
    calls = _patch_workers(monkeypatch)
    client = TestClient(app)
    resp = _post(client, token=_mint())
    assert resp.status_code == 409
    assert calls["propose"] == []


def test_readiness_not_found_maps_status(_configured, monkeypatch):
    _patch_resolve(monkeypatch)
    _patch_repo(monkeypatch)

    def _ready_raises(*a, **k):
        raise PrNotEligibleError("PR #42 not found", status_code=404)

    _patch_github(monkeypatch, ready=_ready_raises)
    _patch_workers(monkeypatch)
    client = TestClient(app)
    resp = _post(client, token=_mint())
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_happy_propose_apply_merge(_configured, monkeypatch):
    _patch_resolve(monkeypatch)
    _patch_repo(monkeypatch)
    merge_calls = []

    def _merge(repo, **kw):
        merge_calls.append(kw)
        return {"merged": True, "already_merged": False, "number": 42, "url": "u"}

    _patch_github(monkeypatch, merge=_merge)
    calls = _patch_workers(monkeypatch)
    client = TestClient(app)
    resp = _post(client, token=_mint())
    assert resp.status_code == 200
    body = resp.text.lower()
    assert "applied and merged" in body
    # propose got the raw JWT + canonical operator email (C5 path: no sidecar gen).
    assert calls["propose"] == [(_META_URI, _GEN_META, _OPERATOR, _JWT, None)]
    # apply got the approval + raw JWT (C5 path: no sidecar gen).
    assert calls["apply"] == [("ap-1", "tok-1", _JWT, None)]
    # merge bound the exact applied head.
    assert merge_calls[0]["expected_head_sha"] == _HEAD
    assert merge_calls[0]["merge_method"] == "squash"


def test_happy_apply_captures_pr_title_on_decision(_configured, monkeypatch):
    """A successful apply records the as-applied GitHub PR title on the decision
    doc (rail subtitle). Captured once per request from the repo handle."""
    _patch_resolve(monkeypatch)

    class _PR:
        title = "infra(checkout): storefront + orders-worker Cloud Run services"

    class _Repo:
        def get_pull(self, pr_number):
            return _PR()

    monkeypatch.setattr(main_mod, "get_repo", lambda token, repo: _Repo())
    _patch_github(monkeypatch)
    _patch_workers(monkeypatch)
    resp = _post(TestClient(app), token=_mint())
    assert resp.status_code == 200
    ek = main_mod._iac_event_key("theghostsquad00/driftscribe", 42, _HEAD, _GEN_META)
    dec = get_state().find_decision_for_event(ek)
    assert dec["apply_status"] == "applied" and dec["merge_state"] == "merged"
    assert dec["pr_title"] == "infra(checkout): storefront + orders-worker Cloud Run services"


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #


def test_idempotency_concurrent_inflight_409(_configured, monkeypatch):
    _patch_resolve(monkeypatch)
    _patch_repo(monkeypatch)
    _patch_github(monkeypatch)
    _patch_workers(monkeypatch)
    # Pre-claim the event (simulate a concurrent in-flight apply) with no decision.
    state = get_state()
    ek = main_mod._iac_event_key(
        "theghostsquad00/driftscribe", 42, _HEAD, _GEN_META
    )
    assert state.record_event(ek, {"trigger": "iac_apply"}) is True
    client = TestClient(app)
    resp = _post(client, token=_mint())
    assert resp.status_code == 409
    assert "in progress" in resp.text.lower()


def test_idempotency_repost_after_merged_is_done(_configured, monkeypatch):
    _patch_resolve(monkeypatch)
    _patch_repo(monkeypatch)
    _patch_github(monkeypatch)
    calls = _patch_workers(monkeypatch)
    client = TestClient(app)
    r1 = _post(client, token=_mint())
    assert r1.status_code == 200
    assert "applied and merged" in r1.text.lower()
    # Re-POST: already merged → done, no re-apply.
    r2 = _post(client, token=_mint())
    assert r2.status_code == 200
    assert len(calls["apply"]) == 1  # not re-applied


# --------------------------------------------------------------------------- #
# 5b head moved after propose
# --------------------------------------------------------------------------- #


def test_head_moved_after_propose_denies_and_releases_409(_configured, monkeypatch):
    _patch_resolve(monkeypatch)
    _patch_repo(monkeypatch)
    _patch_github(monkeypatch, head_after_propose="f" * 40)  # moved
    calls = _patch_workers(monkeypatch)
    client = TestClient(app)
    resp = _post(client, token=_mint())
    assert resp.status_code == 409
    assert calls["apply"] == []  # never applied
    assert len(calls["plan_deny"]) == 1  # cleaned the pending
    # Event released → a subsequent approve can proceed.
    state = get_state()
    ek = main_mod._iac_event_key(
        "theghostsquad00/driftscribe", 42, _HEAD, _GEN_META
    )
    assert state.find_decision_for_event(ek) is None
    assert state.record_event(ek, {"x": 1}) is True  # not claimed


def test_head_read_failure_after_propose_cleans_up_409(_configured, monkeypatch):
    # A GitHub read error in the 5b head re-check must NOT strand the claim +
    # the pending approval we just minted (Codex completed-work review).
    _patch_resolve(monkeypatch)
    _patch_repo(monkeypatch)

    def _ready(*a, **k):
        return _HEAD

    monkeypatch.setattr(main_mod.github, "assert_pr_ready_at_sha", _ready)

    def _head_boom(repo, pr):
        raise RuntimeError("github read failed")

    monkeypatch.setattr(main_mod.github, "get_pr_head_sha", _head_boom)
    monkeypatch.setattr(
        main_mod.github,
        "merge_pr_at_sha",
        lambda *a, **k: {"merged": True, "already_merged": False, "number": 42, "url": "u"},
    )
    calls = _patch_workers(monkeypatch)
    client = TestClient(app)
    resp = _post(client, token=_mint())
    assert resp.status_code == 409
    assert calls["apply"] == []  # never applied
    assert len(calls["plan_deny"]) == 1  # cleaned the pending
    state = get_state()
    ek = main_mod._iac_event_key(
        "theghostsquad00/driftscribe", 42, _HEAD, _GEN_META
    )
    assert state.record_event(ek, {"x": 1}) is True  # released


# --------------------------------------------------------------------------- #
# Apply release matrix (§2 table)
# --------------------------------------------------------------------------- #


def test_apply_403_pre_claim_releases_and_denies(_configured, monkeypatch):
    _patch_resolve(monkeypatch)
    _patch_repo(monkeypatch)
    _patch_github(monkeypatch)

    def _apply_403(aid, tok, jwt):
        raise worker_client.WorkerClientError(403, "bad token", "tofu_apply")

    calls = _patch_workers(monkeypatch, apply_=_apply_403)
    client = TestClient(app)
    resp = _post(client, token=_mint())
    assert resp.status_code == 403
    assert len(calls["plan_deny"]) == 1
    state = get_state()
    ek = main_mod._iac_event_key(
        "theghostsquad00/driftscribe", 42, _HEAD, _GEN_META
    )
    assert state.record_event(ek, {"x": 1}) is True  # released


def test_apply_404_pre_claim_releases_and_denies(_configured, monkeypatch):
    _patch_resolve(monkeypatch)
    _patch_repo(monkeypatch)
    _patch_github(monkeypatch)

    def _apply_404(aid, tok, jwt):
        raise worker_client.WorkerClientError(404, "not found", "tofu_apply")

    calls = _patch_workers(monkeypatch, apply_=_apply_404)
    client = TestClient(app)
    resp = _post(client, token=_mint())
    assert resp.status_code == 403
    assert len(calls["plan_deny"]) == 1


@pytest.mark.parametrize(
    "worker_status,expected_status",
    [(423, 423), (409, 409), (422, 403)],
)
def test_apply_nonmutating_postclaim_releases(
    _configured, monkeypatch, worker_status, expected_status
):
    _patch_resolve(monkeypatch)
    _patch_repo(monkeypatch)
    _patch_github(monkeypatch)

    def _apply_err(aid, tok, jwt):
        raise worker_client.WorkerClientError(worker_status, "x", "tofu_apply")

    calls = _patch_workers(monkeypatch, apply_=_apply_err)
    client = TestClient(app)
    resp = _post(client, token=_mint())
    assert resp.status_code == expected_status
    # Post-claim non-mutating → NO plan_deny (the approval is already burned).
    assert calls["plan_deny"] == []
    # Event RELEASED → a subsequent approve can proceed (fresh mint).
    state = get_state()
    ek = main_mod._iac_event_key(
        "theghostsquad00/driftscribe", 42, _HEAD, _GEN_META
    )
    assert state.record_event(ek, {"x": 1}) is True


def test_apply_502_failed_no_release_terminal(_configured, monkeypatch):
    _patch_resolve(monkeypatch)
    _patch_repo(monkeypatch)
    _patch_github(monkeypatch)

    attempts = []

    def _apply_502(aid, tok, jwt):
        attempts.append((aid, tok, jwt))
        raise worker_client.WorkerClientError(502, "tofu apply failed", "tofu_apply")

    calls = _patch_workers(monkeypatch, apply_=_apply_502)
    client = TestClient(app)
    resp = _post(client, token=_mint())
    assert resp.status_code == 502
    # NOT released — a terminal decision was recorded; notifier alerted.
    assert len(calls["notify"]) == 1
    state = get_state()
    ek = main_mod._iac_event_key(
        "theghostsquad00/driftscribe", 42, _HEAD, _GEN_META
    )
    existing = state.find_decision_for_event(ek)
    assert existing is not None
    assert existing["apply_status"] == "failed"
    # Re-POST → terminal info, NO re-apply, NO merge.
    resp2 = _post(client, token=_mint())
    assert resp2.status_code == 200
    assert len(attempts) == 1  # apply attempted exactly once (no re-apply)


def test_apply_502_failed_state_suspect_terminal_and_reconcile_hint(_configured, monkeypatch):
    """C5g 1b: a worker 502 whose body carries the ``failed_state_suspect`` token
    → the coordinator records apply_status='failed_state_suspect' (not 'failed'),
    the alert + 502 detail point at the recovery runbook (state reconcile), the
    event is NOT released, and a re-POST is terminal (no re-apply) with a
    reconcile-pointing message."""
    _patch_resolve(monkeypatch)
    _patch_repo(monkeypatch)
    _patch_github(monkeypatch)

    attempts = []

    def _apply_suspect(aid, tok, jwt):
        attempts.append((aid, tok, jwt))
        raise worker_client.WorkerClientError(
            502,
            "tofu apply failed (exit 1) and state may be partially mutated "
            "(failed_state_suspect): a state reconcile is required before any retry",
            "tofu_apply",
        )

    calls = _patch_workers(monkeypatch, apply_=_apply_suspect)
    client = TestClient(app)
    resp = _post(client, token=_mint())
    assert resp.status_code == 502
    assert "failed_state_suspect" in resp.json()["detail"]
    assert "runbook" in resp.json()["detail"]
    # alert fired once and points at the recovery runbook (not the generic "verify").
    assert len(calls["notify"]) == 1
    assert "runbook" in calls["notify"][0][1]["body"]
    state = get_state()
    ek = main_mod._iac_event_key(
        "theghostsquad00/driftscribe", 42, _HEAD, _GEN_META
    )
    existing = state.find_decision_for_event(ek)
    assert existing is not None
    assert existing["apply_status"] == "failed_state_suspect"
    # Re-POST → terminal, NO re-apply, message points at the reconcile runbook.
    resp2 = _post(client, token=_mint())
    assert resp2.status_code == 200
    assert len(attempts) == 1
    assert "runbook" in resp2.text


def test_apply_synthetic_503_ambiguous_no_release_504(_configured, monkeypatch):
    _patch_resolve(monkeypatch)
    _patch_repo(monkeypatch)
    _patch_github(monkeypatch)

    attempts = []

    def _apply_503(aid, tok, jwt):
        attempts.append((aid, tok, jwt))
        raise worker_client.WorkerClientError(
            503, "tofu_apply unreachable: ReadTimeout", "tofu_apply"
        )

    calls = _patch_workers(monkeypatch, apply_=_apply_503)
    client = TestClient(app)
    resp = _post(client, token=_mint())
    assert resp.status_code == 504
    assert len(calls["notify"]) == 1
    state = get_state()
    ek = main_mod._iac_event_key(
        "theghostsquad00/driftscribe", 42, _HEAD, _GEN_META
    )
    existing = state.find_decision_for_event(ek)
    assert existing is not None
    assert existing["apply_status"] == "ambiguous"
    # Re-POST → terminal info, NO re-apply.
    resp2 = _post(client, token=_mint())
    assert resp2.status_code == 200
    assert len(attempts) == 1  # apply attempted exactly once (no re-apply)


# --------------------------------------------------------------------------- #
# applied → merge FAIL → reconcile
# --------------------------------------------------------------------------- #


def test_applied_merge_blocked_by_protection_is_permanent_message(_configured, monkeypatch):
    """C5g 4: a PERMANENT merge block (branch protection — a required review or
    status not yet satisfied) → apply is parked, but the banner + alert say
    resolve out-of-band / will NOT merge on re-submit, NOT the transient 'retry'
    wording."""
    _patch_resolve(monkeypatch)
    _patch_repo(monkeypatch)

    def _merge(repo, **kw):
        raise PrMergeBlockedError(
            "branch protection requires a review/status this account cannot satisfy",
            permanent=True,
        )

    _patch_github(monkeypatch, merge=_merge)
    calls = _patch_workers(monkeypatch)
    client = TestClient(app)
    r = _post(client, token=_mint())
    assert r.status_code == 200
    body = r.text.lower()
    assert "branch protection" in body
    assert "will not merge" in body
    assert "re-submit to retry" not in body  # the transient wording must NOT appear
    # alert names the branch-protection block + out-of-band resolution
    assert len(calls["notify"]) == 1
    assert "blocked by branch protection" in calls["notify"][0][1]["body"].lower()
    # still parked (applied + merge failed), event NOT released
    state = get_state()
    ek = main_mod._iac_event_key("theghostsquad00/driftscribe", 42, _HEAD, _GEN_META)
    existing = state.find_decision_for_event(ek)
    assert existing is not None
    assert existing["apply_status"] == "applied" and existing["merge_state"] == "failed"


def test_applied_merge_fail_parks_and_reconciles(_configured, monkeypatch):
    _patch_resolve(monkeypatch)
    _patch_repo(monkeypatch)
    merge_state = {"fail": True}

    def _merge(repo, **kw):
        if merge_state["fail"]:
            raise PrMergeBlockedError("mergeability still computing")
        return {"merged": True, "already_merged": False, "number": 42, "url": "u"}

    _patch_github(monkeypatch, merge=_merge)
    calls = _patch_workers(monkeypatch)
    client = TestClient(app)
    # First POST: apply OK, merge FAILS → parked, 200.
    r1 = _post(client, token=_mint())
    assert r1.status_code == 200
    assert "merge pending" in r1.text.lower() or "reconcile" in r1.text.lower()
    assert len(calls["notify"]) == 1
    state = get_state()
    ek = main_mod._iac_event_key(
        "theghostsquad00/driftscribe", 42, _HEAD, _GEN_META
    )
    existing = state.find_decision_for_event(ek)
    assert existing is not None
    assert existing["apply_status"] == "applied"
    assert existing["merge_state"] == "failed"
    # Second POST: merge now succeeds → merge-only reconcile (NO re-apply).
    merge_state["fail"] = False
    r2 = _post(client, token=_mint())
    assert r2.status_code == 200
    assert "applied and merged" in r2.text.lower()
    assert len(calls["apply"]) == 1  # NOT re-applied


# =========================================================================== #
# C6 — create-class merge-FIRST routing + resume
# =========================================================================== #


def _create_token():
    return _mint(generation_iac_tree=_GEN_IAC_TREE, iac_tree_hash=_IAC_TREE_HASH)


def test_create_class_merges_first_and_waits_for_rebake(_configured, monkeypatch):
    _patch_resolve(monkeypatch, view=_create_view())
    _patch_repo(monkeypatch)
    merge_calls = []

    def _merge(*a, **k):
        merge_calls.append(k)
        return {"merged": True, "already_merged": False, "number": 42, "url": "u"}

    _patch_github(monkeypatch, merge=_merge)
    calls = _patch_workers(monkeypatch)
    client = TestClient(app)
    resp = _post(client, token=_create_token())
    assert resp.status_code == 200, resp.text
    body = resp.text.lower()
    assert "merged to main" in body and "re-bake" in body
    assert len(merge_calls) == 1  # merged FIRST
    assert calls["propose"] == [] and calls["apply"] == []  # NOT applied yet
    state = get_state()
    ek = main_mod._iac_event_key("theghostsquad00/driftscribe", 42, _HEAD, _GEN_META)
    dec = state.find_decision_for_event(ek)
    assert dec["apply_status"] == "waiting_for_rebake"
    assert dec["merge_state"] == "merged"


def test_create_class_without_sidecar_409(_configured, monkeypatch):
    # A create plan whose C2 comment carries NO sidecar (empty generation/hash).
    _patch_resolve(monkeypatch, view=_view(_plan_json=_CREATE_PLAN_JSON))
    _patch_repo(monkeypatch)
    _patch_github(monkeypatch)
    calls = _patch_workers(monkeypatch)
    client = TestClient(app)
    resp = _post(client, token=_mint())  # token has empty sidecar fields → pin matches
    assert resp.status_code == 409
    assert "sidecar" in resp.text.lower()
    assert calls["propose"] == []


def test_create_class_resume_applies_without_re_merging(_configured, monkeypatch):
    _patch_resolve(monkeypatch, view=_create_view())
    _patch_repo(monkeypatch)
    merge_calls = []

    def _merge(*a, **k):
        merge_calls.append(k)
        return {"merged": True, "already_merged": False, "number": 42, "url": "u"}

    _patch_github(monkeypatch, merge=_merge)
    calls = _patch_workers(monkeypatch)
    client = TestClient(app)
    tok = _create_token()
    assert _post(client, token=tok).status_code == 200  # POST1: merge-first
    r2 = _post(client, token=tok)  # POST2: resume (operator re-baked)
    assert r2.status_code == 200, r2.text
    assert "applied" in r2.text.lower()
    assert len(merge_calls) == 1  # NOT merged again on resume
    # propose + apply each got the sidecar generation
    assert len(calls["propose"]) == 1 and calls["propose"][0][4] == _GEN_IAC_TREE
    assert len(calls["apply"]) == 1 and calls["apply"][0][3] == _GEN_IAC_TREE
    state = get_state()
    ek = main_mod._iac_event_key("theghostsquad00/driftscribe", 42, _HEAD, _GEN_META)
    dec = state.find_decision_for_event(ek)
    assert dec["apply_status"] == "applied" and dec["merge_state"] == "merged"


def test_resume_propose_refused_keeps_waiting(_configured, monkeypatch):
    _patch_resolve(monkeypatch, view=_create_view())
    _patch_repo(monkeypatch)
    _patch_github(monkeypatch)  # merge succeeds

    def _propose_422(uri, gen, approver, jwt, generation_iac_tree=None):
        raise worker_client.WorkerClientError(422, "iac-tree gate (re-bake required)", "tofu_apply")

    _patch_workers(monkeypatch, propose=_propose_422)
    client = TestClient(app)
    tok = _create_token()
    assert _post(client, token=tok).status_code == 200  # merge-first
    r2 = _post(client, token=tok)  # resume → propose refused (not re-baked)
    assert r2.status_code == 200
    assert "re-bake" in r2.text.lower()
    state = get_state()
    ek = main_mod._iac_event_key("theghostsquad00/driftscribe", 42, _HEAD, _GEN_META)
    assert state.find_decision_for_event(ek)["apply_status"] == "waiting_for_rebake"


def test_resume_apply_5xx_records_terminal_and_alerts(_configured, monkeypatch):
    _patch_resolve(monkeypatch, view=_create_view())
    _patch_repo(monkeypatch)
    _patch_github(monkeypatch)

    def _apply_502(aid, tok, jwt, generation_iac_tree=None):
        raise worker_client.WorkerClientError(502, "tofu apply failed (failed_state_suspect)", "tofu_apply")

    calls = _patch_workers(monkeypatch, apply_=_apply_502)
    client = TestClient(app)
    tok = _create_token()
    assert _post(client, token=tok).status_code == 200  # merge-first
    r2 = _post(client, token=tok)  # resume → apply 502
    assert r2.status_code == 502, r2.text
    assert "orphan" in r2.text.lower() or "runbook" in r2.text.lower()
    # an alert was sent + a terminal decision recorded
    assert any(n[0] == "notifier" for n in calls["notify"])
    state = get_state()
    ek = main_mod._iac_event_key("theghostsquad00/driftscribe", 42, _HEAD, _GEN_META)
    assert state.find_decision_for_event(ek)["apply_status"] == "failed_state_suspect"


def test_create_merge_failure_409_nothing_applied_keeps_pending_pointer(_configured, monkeypatch):
    """A create-class merge failure: 409, nothing applied — but the
    waiting_for_rebake+PENDING decision is KEPT as the recovery pointer (NOT released),
    so a re-submit re-tries the idempotent merge once the block is resolved."""
    _patch_resolve(monkeypatch, view=_create_view())
    _patch_repo(monkeypatch)

    def _merge_fail(*a, **k):
        raise PrMergeBlockedError("required check not green")

    _patch_github(monkeypatch, merge=_merge_fail)
    calls = _patch_workers(monkeypatch)
    client = TestClient(app)
    resp = _post(client, token=_create_token())
    assert resp.status_code == 409, resp.text
    assert calls["propose"] == [] and calls["apply"] == []
    state = get_state()
    ek = main_mod._iac_event_key("theghostsquad00/driftscribe", 42, _HEAD, _GEN_META)
    dec = state.find_decision_for_event(ek)
    assert dec["apply_status"] == "waiting_for_rebake" and dec["merge_state"] == "pending"


def test_create_class_crash_after_pending_recovers_by_re_merge(_configured, monkeypatch):
    """Crash window (Codex C6b-1 blocker): a waiting_for_rebake+PENDING decision exists
    (the merge may or may not have happened). A re-POST re-drives the IDEMPOTENT merge
    and promotes to merged — never strands a merged create-class PR with no pointer."""
    _patch_resolve(monkeypatch, view=_create_view())
    _patch_repo(monkeypatch)
    merge_calls = []

    def _merge(*a, **k):
        merge_calls.append(k)
        return {"merged": True, "already_merged": True, "number": 42, "url": "u"}

    _patch_github(monkeypatch, merge=_merge)
    _patch_workers(monkeypatch)
    # Pre-seed the crash state: claimed event + waiting_for_rebake + merge_state=pending.
    state = get_state()
    ek = main_mod._iac_event_key("theghostsquad00/driftscribe", 42, _HEAD, _GEN_META)
    state.record_event(ek, {"x": 1})
    main_mod._record_iac_decision(
        state, ek, apply_status="waiting_for_rebake", merge_state="pending",
        head_sha=_HEAD, pr_number=42, approver=_OPERATOR,
    )
    client = TestClient(app)
    resp = _post(client, token=_create_token())
    assert resp.status_code == 200, resp.text
    assert "re-bake" in resp.text.lower()
    assert len(merge_calls) == 1  # idempotent merge re-driven
    dec = state.find_decision_for_event(ek)
    assert dec["apply_status"] == "waiting_for_rebake" and dec["merge_state"] == "merged"


def test_resume_baked_hash_mismatch_short_circuits_before_propose(_configured, monkeypatch):
    """C6c: if the worker's baked iac_tree_hash != the approved hash, the resume
    short-circuits with a precise 'not re-baked' message BEFORE burning a propose."""
    _patch_resolve(monkeypatch, view=_create_view())
    _patch_repo(monkeypatch)
    _patch_github(monkeypatch)
    calls = _patch_workers(monkeypatch, baked_hash=lambda: {"iac_tree_hash": "0" * 64})
    client = TestClient(app)
    tok = _create_token()
    assert _post(client, token=tok).status_code == 200  # merge-first
    r2 = _post(client, token=tok)  # resume → baked-hash pre-check mismatch
    assert r2.status_code == 200
    body = r2.text.lower()
    assert "not re-baked" in body
    assert calls["propose"] == []  # short-circuited before propose


def test_resume_baked_hash_unreachable_falls_through_to_apply(_configured, monkeypatch):
    """C6c best-effort: a baked-hash GET failure (worker unreachable / older revision)
    falls through to propose→apply — the apply-time gate is the real guard."""
    _patch_resolve(monkeypatch, view=_create_view())
    _patch_repo(monkeypatch)
    _patch_github(monkeypatch)

    def _boom():
        raise worker_client.WorkerClientError(503, "unreachable", "tofu_apply")

    calls = _patch_workers(monkeypatch, baked_hash=_boom)
    client = TestClient(app)
    tok = _create_token()
    assert _post(client, token=tok).status_code == 200  # merge-first
    r2 = _post(client, token=tok)  # resume → GET fails → falls through → applies
    assert r2.status_code == 200 and "applied" in r2.text.lower()
    assert len(calls["apply"]) == 1


def test_resume_apply_clean_502_still_freezes_as_state_suspect(_configured, monkeypatch):
    """C6d (Codex blocker 4): a create-class resume 502 with NO failed_state_suspect
    token still freezes as failed_state_suspect — a failed CREATE can orphan a live
    resource the worker's 'clean' diagnosis can't disprove. NOT a retryable 'failed'."""
    _patch_resolve(monkeypatch, view=_create_view())
    _patch_repo(monkeypatch)
    _patch_github(monkeypatch)

    def _apply_502_clean(aid, tok, jwt, generation_iac_tree=None):
        raise worker_client.WorkerClientError(502, "tofu apply failed (exit 1)", "tofu_apply")

    _patch_workers(monkeypatch, apply_=_apply_502_clean)
    client = TestClient(app)
    tok = _create_token()
    assert _post(client, token=tok).status_code == 200  # merge-first
    r2 = _post(client, token=tok)  # resume → clean 502
    assert r2.status_code == 502, r2.text
    assert "orphan" in r2.text.lower()
    state = get_state()
    ek = main_mod._iac_event_key("theghostsquad00/driftscribe", 42, _HEAD, _GEN_META)
    assert state.find_decision_for_event(ek)["apply_status"] == "failed_state_suspect"


# --------------------------------------------------------------------------- #
# Blast-radius POST guard (ClickOps Wave 2 item 8)
#
# POST re-renders reuse iac_approval.html but do NOT set blast_phrase /
# cannot_touch_note (those are GET-only ctx keys). The | default("") guards in
# the template must prevent a TemplateNotFound / UndefinedError 500.
# --------------------------------------------------------------------------- #


def test_post_rerender_does_not_500_without_blast_phrase_keys(_configured, monkeypatch):
    """A reject POST re-renders iac_approval.html without blast_phrase / cannot_touch_note
    in ctx — the | default('') guards must prevent any Jinja UndefinedError."""
    _patch_resolve(monkeypatch)
    _patch_workers(monkeypatch)
    client = TestClient(app)
    # A "reject" POST takes the noop path and immediately re-renders the template
    # via _render_iac_approval_response, which does NOT set blast_phrase or
    # cannot_touch_note in ctx.  Without the default() guards this would 500.
    resp = _post(client, token=_mint(), decision="reject")
    assert resp.status_code == 200
