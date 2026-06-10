"""Integration tests for the read-only GET /iac-approvals/{pr_number} (Phase C5e-2).

The GET route renders an infra-apply approval page from the C2 ``tofu plan``
artifact a plan-builder run already produced. It is read-only and ALWAYS returns
200 (probe-safe). It mints a signed, artifact-bound CSRF form token ONLY when the
artifact is approvable (integrity verified, no denylist violations, server token
configured).

Hard invariants this slice must NOT violate (tested below):

- never mints a plan approval, never calls the tofu-apply worker
  (``call_propose`` / ``call_apply``), never reads ``plan_approvals``.

Mocking strategy: monkeypatch the thin resolution seams on ``agent.main`` —
``get_repo`` (sentinel repo), ``iac_artifacts.find_latest_c2_comment`` (a
``C2CommentRef``), and ``iac_artifacts.load_plan_view`` (a constructed
``IacPlanView``). Settings are driven via env + ``get_settings.cache_clear()``
(the autouse conftest fixture resets the cache between tests).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent import worker_client
from agent.config import get_settings
from agent.iac_artifacts import C2CommentRef, IacPlanView
from agent.iac_csrf import verify_form_token
from agent.main import app, get_state, _iac_event_key, _record_iac_decision

_HEAD = "a" * 40
_PLAN_SHA = "b" * 64
_PLAN_JSON_SHA = "c" * 64
_BUCKET = "test-proj-tofu-artifacts"
_PREFIX = f"gs://{_BUCKET}/pr-42/{_HEAD}/run-7-1/"
_META_URI = _PREFIX + "metadata.json"


def _metadata() -> dict:
    """A realistic, full 15-field c2.v1 metadata dict."""
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
        tofu_show_text="# google_cloud_run_service.svc will be updated in-place\n  ~ image = \"old\" -> \"new\"",
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
    """Set settings so the route is fully configured + approvable by default."""
    monkeypatch.setenv("GITHUB_REPO", "theghostsquad00/driftscribe")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")
    monkeypatch.setenv("DRIFTSCRIBE_TOKEN", "static-server-token")
    monkeypatch.setenv("TOFU_ARTIFACTS_BUCKET", _BUCKET)
    # The POST fail-closes under coordinator dry-run, so the GET suppresses Approve
    # there too; the conftest defaults DRY_RUN=true, so flip it off to render the
    # approvable happy path (a dedicated test covers the dry-run suppression).
    monkeypatch.setenv("DRY_RUN", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _patch_resolve(monkeypatch, *, ref, view):
    """Patch the three module seams the route resolves through."""
    monkeypatch.setattr("agent.main.get_repo", lambda token, repo: object())
    import agent.main as main_mod

    monkeypatch.setattr(
        main_mod.iac_artifacts, "find_latest_c2_comment", lambda repo, pr: ref
    )
    monkeypatch.setattr(
        main_mod.iac_artifacts,
        "load_plan_view",
        lambda r, *, bucket_name, client=None: view,
    )


def test_happy_get_renders_fields_and_form(_configured, monkeypatch):
    _patch_resolve(monkeypatch, ref=_ref(), view=_view())
    client = TestClient(app)
    resp = client.get("/iac-approvals/42")
    assert resp.status_code == 200
    body = resp.text

    # Identity fields shown.
    assert _HEAD in body
    assert "theghostsquad00/driftscribe" in body
    assert _PLAN_JSON_SHA in body
    assert _PLAN_SHA in body
    assert "1.12.0" in body
    # tofu show text rendered.
    assert "will be updated in-place" in body
    # integrity verified, no denylist.
    assert "verified" in body
    # Approve button + hidden token field present.
    assert 'data-testid="approve-button"' in body
    assert 'data-testid="reject-button"' in body
    assert 'data-testid="token-field"' in body

    # Extract the form_token and verify it accepts for this PR + decodes to the
    # artifact identity (the artifact pin, Codex blocker #1).
    import re

    m = re.search(r'name="form_token"[^>]*value="([^"]+)"', body)
    assert m, "form_token not found in rendered form"
    token = m.group(1)
    s = get_settings()
    payload = verify_form_token(s, token, pr_number=42)
    assert payload is not None
    assert payload["head_sha"] == _HEAD
    assert payload["artifact_uri_metadata"] == _META_URI
    assert payload["generation_metadata"] == "1700000000000003"
    assert payload["plan_sha256"] == _PLAN_SHA
    assert payload["plan_json_sha256"] == _PLAN_JSON_SHA
    assert payload["comment_id"] == 556677


def test_no_plan_comment_renders_run_c2(_configured, monkeypatch):
    _patch_resolve(monkeypatch, ref=None, view=None)
    client = TestClient(app)
    resp = client.get("/iac-approvals/42")
    assert resp.status_code == 200
    body = resp.text
    assert "Run the C2 plan-builder" in body
    assert 'data-testid="approve-button"' not in body
    assert 'name="form_token"' not in body


def test_denylist_tripped_suppresses_approve(_configured, monkeypatch):
    view = _view(denylist_violations=[("protect-coordinator", "deletes driftscribe-agent")])
    _patch_resolve(monkeypatch, ref=_ref(), view=view)
    client = TestClient(app)
    resp = client.get("/iac-approvals/42")
    assert resp.status_code == 200
    body = resp.text
    assert "protect-coordinator" in body
    assert "deletes driftscribe-agent" in body
    assert 'data-testid="approve-button"' not in body
    assert 'name="form_token"' not in body
    assert "denylist" in body.lower()
    # A denylist trip is a genuine hard-stop → red "error" callout, not the
    # calm "not ready yet" note.
    assert 'data-testid="approve-blocked"' in body
    assert 'data-testid="approve-pending"' not in body


def test_integrity_mismatch_suppresses_approve(_configured, monkeypatch):
    view = _view(integrity_ok=False)
    _patch_resolve(monkeypatch, ref=_ref(), view=view)
    client = TestClient(app)
    resp = client.get("/iac-approvals/42")
    assert resp.status_code == 200
    body = resp.text
    assert "MISMATCH" in body
    assert 'data-testid="approve-button"' not in body
    assert 'name="form_token"' not in body
    assert 'data-testid="approve-blocked"' in body  # hard-stop → red


def test_unverifiable_suppresses_approve(_configured, monkeypatch):
    view = _view(unverifiable=True, integrity_ok=False, metadata={})
    _patch_resolve(monkeypatch, ref=_ref(), view=view)
    client = TestClient(app)
    resp = client.get("/iac-approvals/42")
    assert resp.status_code == 200
    body = resp.text
    assert "unverifiable" in body.lower()
    assert 'data-testid="approve-button"' not in body
    assert 'name="form_token"' not in body
    assert 'data-testid="approve-blocked"' in body  # hard-stop → red


def test_metadata_pr_mismatch_suppresses_approve(_configured, monkeypatch):
    # Metadata declares a DIFFERENT pr_number than the route → fail-closed
    # (Codex C5e-2 BLOCKER: never pin another PR's artifact to this page).
    md = _metadata()
    md["pr_number"] = 99
    view = _view(metadata=md)
    _patch_resolve(monkeypatch, ref=_ref(), view=view)
    client = TestClient(app)
    resp = client.get("/iac-approvals/42")
    assert resp.status_code == 200
    body = resp.text
    assert "does not match this PR" in body
    assert 'data-testid="approve-button"' not in body
    assert 'name="form_token"' not in body


def test_ref_metadata_inconsistency_suppresses_approve(_configured, monkeypatch):
    # The comment ref's head_sha disagrees with the fetched metadata's head_sha.
    bad_ref = C2CommentRef(
        head_sha="f" * 40,  # ref says one head, metadata says another
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
        tofu_show_text="",
    )
    _patch_resolve(monkeypatch, ref=bad_ref, view=_view())
    client = TestClient(app)
    resp = client.get("/iac-approvals/42")
    assert resp.status_code == 200
    assert "does not match this PR" in resp.text
    assert 'data-testid="approve-button"' not in resp.text


def test_resolver_unexpected_exception_stays_200(_configured, monkeypatch):
    # load_plan_view raising a NON-IacArtifactError must NOT escape as 500 — the
    # GET is probe-safe / always-200 (Codex C5e-2 IMPORTANT).
    monkeypatch.setattr("agent.main.get_repo", lambda token, repo: object())
    import agent.main as main_mod

    monkeypatch.setattr(
        main_mod.iac_artifacts, "find_latest_c2_comment", lambda repo, pr: _ref()
    )

    def _boom(*a, **k):
        raise RuntimeError("GCS permission denied")

    monkeypatch.setattr(main_mod.iac_artifacts, "load_plan_view", _boom)
    client = TestClient(app)
    resp = client.get("/iac-approvals/42")
    assert resp.status_code == 200
    assert 'data-testid="approve-button"' not in resp.text
    assert 'name="form_token"' not in resp.text


def test_token_unset_suppresses_approve_not_configured(_configured, monkeypatch):
    monkeypatch.setenv("DRIFTSCRIBE_TOKEN", "")
    get_settings.cache_clear()
    _patch_resolve(monkeypatch, ref=_ref(), view=_view())
    client = TestClient(app)
    resp = client.get("/iac-approvals/42")
    assert resp.status_code == 200
    body = resp.text
    assert "not configured" in body.lower()
    assert 'data-testid="approve-button"' not in body
    assert 'name="form_token"' not in body
    # "Not configured" is an environment gap, not a bad artifact → calm note.
    assert 'data-testid="approve-pending"' in body
    assert 'data-testid="approve-blocked"' not in body


def test_dry_run_suppresses_approve(_configured, monkeypatch):
    # Coordinator dry-run: the POST fail-closes, so the GET suppresses Approve too
    # (a fully-verifiable plan otherwise).
    monkeypatch.setenv("DRY_RUN", "true")
    get_settings.cache_clear()
    _patch_resolve(monkeypatch, ref=_ref(), view=_view())
    client = TestClient(app)
    resp = client.get("/iac-approvals/42")
    assert resp.status_code == 200
    body = resp.text
    assert "dry-run" in body.lower()
    assert 'data-testid="approve-button"' not in body
    assert 'name="form_token"' not in body
    # Dry-run is a "not ready yet" state, not a hard error → calm grey note.
    assert 'data-testid="approve-pending"' in body
    assert "Approval not available yet" in body
    assert 'data-testid="approve-blocked"' not in body


def test_approvals_not_configured_when_no_github(monkeypatch):
    # No github_token/github_repo → resolve returns (None, None) → run-C2 page.
    monkeypatch.setenv("GITHUB_REPO", "")
    monkeypatch.setenv("GITHUB_TOKEN", "")
    monkeypatch.setenv("DRIFTSCRIBE_TOKEN", "static-server-token")
    get_settings.cache_clear()
    client = TestClient(app)
    resp = client.get("/iac-approvals/42")
    assert resp.status_code == 200
    assert 'data-testid="approve-button"' not in resp.text
    get_settings.cache_clear()


def test_security_headers(_configured, monkeypatch):
    _patch_resolve(monkeypatch, ref=_ref(), view=_view())
    client = TestClient(app)
    resp = client.get("/iac-approvals/42")
    assert resp.headers.get("Cache-Control") == "no-store"
    assert resp.headers.get("Referrer-Policy") == "no-referrer"
    assert resp.headers.get("X-Frame-Options") == "DENY"
    csp = resp.headers.get("Content-Security-Policy")
    assert csp is not None
    assert "default-src 'none'" in csp
    # UI refresh: the page now links the same-origin built stylesheet instead of
    # shipping an inline <style>, so the CSP allows 'self' styles (NOT inline).
    assert "style-src 'self'" in csp
    assert "unsafe-inline" not in csp
    assert "form-action 'self'" in csp
    assert "base-uri 'none'" in csp
    assert "frame-ancestors 'none'" in csp


def test_get_never_calls_worker_or_reads_plan_approvals(_configured, monkeypatch):
    _patch_resolve(monkeypatch, ref=_ref(), view=_view())

    def _boom(*a, **k):
        raise AssertionError("GET must not call the tofu-apply worker")

    monkeypatch.setattr(worker_client, "call_propose", _boom)
    monkeypatch.setattr(worker_client, "call_apply", _boom)
    monkeypatch.setattr("agent.main.worker_client.call_propose", _boom)
    monkeypatch.setattr("agent.main.worker_client.call_apply", _boom)

    # A plan-approval store getter, if it exists on the module, must not be hit.
    import agent.main as main_mod

    for attr in ("get_plan_approval_store", "_plan_approval_store"):
        if hasattr(main_mod, attr):
            monkeypatch.setattr(
                main_mod,
                attr,
                lambda *a, **k: (_ for _ in ()).throw(
                    AssertionError(f"GET must not access {attr}")
                ),
            )

    client = TestClient(app)
    resp = client.get("/iac-approvals/42")
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Decision-state awareness — suppress the Approve form for a plan whose apply is
# already TERMINAL (applied+merged / failed); KEEP it for still-actionable
# states (waiting_for_rebake, applied+failed). Best-effort read of the
# decision/reconcile pointer (NOT plan_approvals); any failure falls back to the
# form (always-200). The generation_metadata used to compute the event key
# matches _view()/_ref() ("1700000000000003").
# --------------------------------------------------------------------------- #

_GEN_META = "1700000000000003"


def _seed_decision(*, apply_status: str, merge_state: str) -> None:
    """Record an iac_apply decision in the InMemory store under the key the GET
    will compute for PR 42 / _HEAD / _GEN_META. The event must be CLAIMED first
    (record_event) — find_decision_for_event only links a decision to a claimed
    event, mirroring the real POST's idempotency-claim-then-record flow."""
    s = get_settings()
    ek = _iac_event_key(s.github_repo, 42, _HEAD, _GEN_META)
    state = get_state()
    state.record_event(ek, {"pr_number": 42})
    _record_iac_decision(
        state, ek, apply_status=apply_status, merge_state=merge_state,
        head_sha=_HEAD, pr_number=42, approver="op@example.com",
    )


@pytest.fixture
def _inmemory(monkeypatch):
    """Pin get_state() to the InMemory store (Firestore only when gcp_project set)."""
    monkeypatch.setenv("GCP_PROJECT", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_already_applied_merged_suppresses_form(_configured, _inmemory, monkeypatch):
    _patch_resolve(monkeypatch, ref=_ref(), view=_view())
    _seed_decision(apply_status="applied", merge_state="merged")
    resp = TestClient(app).get("/iac-approvals/42")
    assert resp.status_code == 200
    body = resp.text
    # Form suppressed; no CSRF token minted for a resolved plan.
    assert 'data-testid="approve-button"' not in body
    assert 'name="form_token"' not in body
    # Green outcome banner, no misleading red/grey bottom callout.
    assert "Already applied and merged" in body
    assert 'data-testid="approve-blocked"' not in body
    assert 'data-testid="approve-pending"' not in body


def test_terminal_failure_suppresses_form_and_renders_red(_configured, _inmemory, monkeypatch):
    _patch_resolve(monkeypatch, ref=_ref(), view=_view())
    _seed_decision(apply_status="failed_state_suspect", merge_state="merged")
    resp = TestClient(app).get("/iac-approvals/42")
    assert resp.status_code == 200
    body = resp.text
    assert 'data-testid="approve-button"' not in body
    assert 'name="form_token"' not in body
    assert "Terminal state recorded" in body
    assert "failed_state_suspect" in body
    # A terminal failure must read as a red hard-stop, not green success.
    assert 'class="ds-blocked"' in body


def test_waiting_for_rebake_keeps_form(_configured, _inmemory, monkeypatch):
    # The create-class second click (post-rebake Apply) is still actionable.
    _patch_resolve(monkeypatch, ref=_ref(), view=_view())
    _seed_decision(apply_status="waiting_for_rebake", merge_state="merged")
    resp = TestClient(app).get("/iac-approvals/42")
    assert resp.status_code == 200
    body = resp.text
    assert 'data-testid="approve-button"' in body
    assert 'name="form_token"' in body


def test_applied_failed_keeps_form(_configured, _inmemory, monkeypatch):
    # applied + merge FAILED → the POST does a merge-only reconcile, so this is
    # still actionable: the form must stay (Codex review).
    _patch_resolve(monkeypatch, ref=_ref(), view=_view())
    _seed_decision(apply_status="applied", merge_state="failed")
    resp = TestClient(app).get("/iac-approvals/42")
    assert resp.status_code == 200
    body = resp.text
    assert 'data-testid="approve-button"' in body
    assert 'name="form_token"' in body


def test_decision_lookup_failure_falls_back_to_form(_configured, _inmemory, monkeypatch):
    # A decision-store read error must NOT break the always-200 GET — it falls
    # back to the artifact-only view (form shown; POST stays authoritative).
    _patch_resolve(monkeypatch, ref=_ref(), view=_view())

    def _boom():
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr("agent.main.get_state", _boom)
    resp = TestClient(app).get("/iac-approvals/42")
    assert resp.status_code == 200
    body = resp.text
    assert 'data-testid="approve-button"' in body
    assert 'name="form_token"' in body


# --------------------------------------------------------------------------- #
# Change-summary card — route-level regression pins (Task 8).
#
# The card is gated in the template by (show_summary AND integrity_ok AND no
# denylist AND not unverifiable). The route sets show_summary = True only when
# reason_severity != "error" AND not resolved_decision. These tests prove that
# gate from the outside — through the real GET route.
# --------------------------------------------------------------------------- #

_SUMMARY_PLAN = {
    "format_version": "1.2",
    "resource_changes": [
        {
            "address": "google_pubsub_topic.orders",
            "mode": "managed",
            "type": "google_pubsub_topic",
            "name": "orders",
            "change": {
                "actions": ["create"],
                "before": None,
                "after": {"name": "orders"},
                # required by schema; not exercised here
                "before_sensitive": False,
                "after_sensitive": False,
                "after_unknown": {"id": True},
            },
        },
    ],
}


def test_get_renders_change_summary_from_plan_json(_configured, monkeypatch):
    """Healthy, approvable view with a parsed plan renders the change-summary card."""
    view = _view(_plan_json=_SUMMARY_PLAN)
    _patch_resolve(monkeypatch, ref=_ref(), view=view)
    resp = TestClient(app).get("/iac-approvals/42")
    assert resp.status_code == 200
    body = resp.text
    assert 'data-testid="change-summary"' in body
    assert "Pub/Sub topic" in body
    assert 'data-testid="no-destroy-note"' in body


def test_get_still_200_with_summary_fallback(_configured, monkeypatch):
    """Healthy view whose plan JSON never parsed renders the summary-unavailable note."""
    view = _view()  # _plan_json=None (default)
    _patch_resolve(monkeypatch, ref=_ref(), view=view)
    resp = TestClient(app).get("/iac-approvals/42")
    assert resp.status_code == 200
    body = resp.text
    assert 'data-testid="summary-unavailable"' in body
    assert 'data-testid="change-summary"' not in body
    # Symmetry with the healthy test — catches a future unconditional render.
    assert 'data-testid="no-destroy-note"' not in body


def test_get_integrity_mismatch_renders_no_summary_card(_configured, monkeypatch):
    """Integrity MISMATCH (error-class) → show_summary=False → card absent even with plan JSON."""
    view = _view(integrity_ok=False, _plan_json=_SUMMARY_PLAN)
    _patch_resolve(monkeypatch, ref=_ref(), view=view)
    resp = TestClient(app).get("/iac-approvals/42")
    assert resp.status_code == 200
    body = resp.text
    # Trust gate fires: summary card must be absent.
    assert 'data-testid="change-summary"' not in body
    assert 'data-testid="no-destroy-note"' not in body
    # The page still renders its normal integrity-mismatch verdict (sanity guard).
    assert "MISMATCH" in body


def test_get_terminal_applied_merged_renders_no_summary_card(
    _configured, _inmemory, monkeypatch
):
    """Terminal decision applied+merged suppresses the change-summary card.

    Takes _inmemory (unlike the three tests above) because _seed_decision needs
    the InMemory store to install the terminal decision pointer the GET reads.
    """
    view = _view(_plan_json=_SUMMARY_PLAN)
    _patch_resolve(monkeypatch, ref=_ref(), view=view)
    _seed_decision(apply_status="applied", merge_state="merged")
    resp = TestClient(app).get("/iac-approvals/42")
    assert resp.status_code == 200
    body = resp.text
    assert 'data-testid="change-summary"' not in body
    # Sanity guard: green terminal banner is present.
    assert "Already applied and merged" in body


def test_get_terminal_failed_renders_no_summary_card(
    _configured, _inmemory, monkeypatch
):
    """Terminal decision failed_state_suspect+merged suppresses the change-summary card.

    Takes _inmemory (unlike the three tests above) because _seed_decision needs
    the InMemory store to install the terminal decision pointer the GET reads.
    """
    view = _view(_plan_json=_SUMMARY_PLAN)
    _patch_resolve(monkeypatch, ref=_ref(), view=view)
    _seed_decision(apply_status="failed_state_suspect", merge_state="merged")
    resp = TestClient(app).get("/iac-approvals/42")
    assert resp.status_code == 200
    body = resp.text
    assert 'data-testid="change-summary"' not in body
    # Sanity guard: red terminal banner is present.
    assert "Terminal state recorded" in body
