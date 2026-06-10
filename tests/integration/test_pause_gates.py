"""Integration tests for the five mutation gates + two approval-page displays
that honor the operator pause flag (Task 2).

Every test toggles pause through the REAL ``POST /pause`` endpoint — the
in-memory StateStore singleton makes that an honest end-to-end path (the
mutation gates read the same singleton via ``get_state()``). The not-paused
behavior of these routes is proven untouched by the existing recheck / eventarc
/ chat / approval suites; here we only assert the paused-and-fail-closed deltas.

Per-route contract under pause (see docs/plans/2026-06-10-pause-button.md §4):

- POST /recheck            → 423 PAUSED_DETAIL (force=true does NOT bypass).
- POST /eventarc           → 200 {"ignored":"paused", service, region} for an
                             in-scope event; off-target events NEVER read the
                             flag (whitelist precedes it — pinned hard).
- POST /chat               → 200 calm reply + paused=true (JSON + single-`done`
                             SSE frame); no LLM call.
- POST /approvals/{id}     → approve 423; reject ALLOWED (safety-direction).
- POST /iac-approvals/{n}  → approve 423; reject ALLOWED (audit no-op).
- GET  /iac-approvals/{n}  → calm approve-pending note, no token/approve button.
- GET  /approvals/{id}     → paused-note + disabled Approve (Reject stays live).
"""
from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import agent.main as main_mod
from agent import approvals as approval_helpers
from agent import iac_csrf, worker_client
from agent.auth import require_cf_operator
from agent.config import get_settings
from agent.iac_artifacts import C2CommentRef, IacPlanView
from agent.main import app, get_state
from agent.pause import PAUSED_DETAIL
from driftscribe_lib.approvals import Approval


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _pause(client: TestClient, *, reason: str | None = "kill-switch test") -> None:
    """Toggle pause ON via the real POST /pause endpoint (in-memory store)."""
    r = client.post("/pause", json={"paused": True, "reason": reason})
    assert r.status_code == 200, r.text
    assert r.json()["paused"] is True


def _parse_sse(text: str):
    """Return list of (event_name|None, data_dict|None) from an SSE body.

    Mirrors the parser in tests/unit/test_chat_sse.py so the frame-shape
    assertions read the same way.
    """
    frames = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block or block.startswith(":"):
            continue  # blank or heartbeat comment
        ev = None
        data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                ev = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = json.loads(line[len("data:"):].strip())
        frames.append((ev, data))
    return frames


# --------------------------------------------------------------------------- #
# POST /recheck
# --------------------------------------------------------------------------- #


def test_recheck_paused_returns_423_and_does_not_dispatch():
    """Paused → 423 PAUSED_DETAIL, and the recheck pipeline never runs."""
    mock_recheck = AsyncMock()
    client = TestClient(app)
    _pause(client)
    with patch("agent.main._do_recheck", mock_recheck):
        r = client.post("/recheck")
    assert r.status_code == 423
    assert r.json()["detail"] == PAUSED_DETAIL
    mock_recheck.assert_not_awaited()


def test_recheck_force_true_does_not_bypass_pause():
    """``?force=true`` outranks nothing — pause still wins with 423."""
    mock_recheck = AsyncMock()
    client = TestClient(app)
    _pause(client)
    with patch("agent.main._do_recheck", mock_recheck):
        r = client.post("/recheck?force=true")
    assert r.status_code == 423
    assert r.json()["detail"] == PAUSED_DETAIL
    mock_recheck.assert_not_awaited()


def test_recheck_fail_closed_when_pause_read_raises():
    """A store read failure fails closed → /recheck refuses with 423."""
    state = get_state()
    mock_recheck = AsyncMock()
    with patch.object(state, "get_pause", side_effect=RuntimeError("Firestore down")), \
         patch("agent.main._do_recheck", mock_recheck):
        client = TestClient(app)
        r = client.post("/recheck")
    assert r.status_code == 423
    mock_recheck.assert_not_awaited()


# --------------------------------------------------------------------------- #
# POST /eventarc
# --------------------------------------------------------------------------- #

_VALID_AUDIENCE = "https://driftscribe-agent-xyz.a.run.app/eventarc"
_EXPECTED_EMAIL = "eventarc-trigger-sa@test-proj.iam.gserviceaccount.com"


def _audit_log_body(service_name: str = "payment-demo",
                    location: str = "asia-northeast1") -> dict:
    return {
        "resource": {
            "type": "cloud_run_revision",
            "labels": {
                "service_name": service_name,
                "location": location,
                "project_id": "test-proj",
            },
        },
    }


def _set_audience(monkeypatch) -> None:
    monkeypatch.setenv("EVENTARC_AUDIENCE", _VALID_AUDIENCE)
    get_settings.cache_clear()


def test_eventarc_in_scope_event_paused_returns_200_ignored(monkeypatch):
    """In-scope event while paused → 200 {"ignored":"paused", service, region};
    no recheck. 200 (not 423) so Eventarc does NOT retry — the event is
    acknowledged and dropped (same retry-storm-safe shape as non-target-service).
    """
    _set_audience(monkeypatch)
    mock_recheck = AsyncMock()
    client = TestClient(app)
    _pause(client)
    with patch("agent.main.verify_oauth2_token") as m_verify, \
         patch("agent.main._do_recheck", mock_recheck):
        m_verify.return_value = {"email": _EXPECTED_EMAIL, "aud": _VALID_AUDIENCE}
        r = client.post(
            "/eventarc",
            json=_audit_log_body(),
            headers={"Authorization": "Bearer fake-token"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "ignored": "paused",
        "service": "payment-demo",
        "region": "asia-northeast1",
    }
    mock_recheck.assert_not_awaited()


def test_eventarc_off_target_still_ignored_while_paused(monkeypatch):
    """An off-target event while paused returns non-target-service (the
    whitelist still wins for off-target shapes — the paused branch only
    follows the whitelist for in-scope events)."""
    _set_audience(monkeypatch)
    mock_recheck = AsyncMock()
    client = TestClient(app)
    _pause(client)
    with patch("agent.main.verify_oauth2_token") as m_verify, \
         patch("agent.main._do_recheck", mock_recheck):
        m_verify.return_value = {"email": _EXPECTED_EMAIL, "aud": _VALID_AUDIENCE}
        r = client.post(
            "/eventarc",
            json=_audit_log_body(service_name="other-service"),
            headers={"Authorization": "Bearer fake-token"},
        )
    assert r.status_code == 200
    assert r.json()["ignored"] == "non-target-service"
    mock_recheck.assert_not_awaited()


def test_eventarc_off_target_never_reads_the_pause_flag(monkeypatch):
    """HARD ordering pin: the service/region whitelist precedes the pause read,
    so an off-target event must NOT consult the flag. Patch get_pause to RAISE
    and send an off-target event — it still returns non-target-service, proving
    the flag was never read on that path (a read would have fail-closed)."""
    _set_audience(monkeypatch)
    state = get_state()
    mock_recheck = AsyncMock()
    with patch("agent.main.verify_oauth2_token") as m_verify, \
         patch.object(state, "get_pause", side_effect=RuntimeError("must not be read")), \
         patch("agent.main._do_recheck", mock_recheck):
        m_verify.return_value = {"email": _EXPECTED_EMAIL, "aud": _VALID_AUDIENCE}
        client = TestClient(app)
        r = client.post(
            "/eventarc",
            json=_audit_log_body(service_name="other-service"),
            headers={"Authorization": "Bearer fake-token"},
        )
    assert r.status_code == 200
    assert r.json()["ignored"] == "non-target-service"
    mock_recheck.assert_not_awaited()


# --------------------------------------------------------------------------- #
# POST /chat
# --------------------------------------------------------------------------- #


def test_chat_paused_json_returns_calm_reply(monkeypatch):
    """Paused JSON path → 200 calm reply, tool_calls==[], paused is True;
    the LLM (run_chat) is never invoked."""
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    fake = AsyncMock(return_value={"reply": "x", "tool_calls": [], "session_id": "s"})
    client = TestClient(app)
    _pause(client)
    with patch("agent.adk_agent.run_chat", fake):
        r = client.post("/chat", json={"prompt": "do a thing", "session_id": "sess-1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["paused"] is True
    assert body["tool_calls"] == []
    assert body["session_id"] == "sess-1"
    assert "paused" in body["reply"].lower()
    fake.assert_not_awaited()


def test_chat_paused_sse_emits_single_done_frame(monkeypatch):
    """Paused SSE path → exactly one `done` frame carrying the calm reply +
    paused=true, and NO `meta` frame (SPA tolerates a null trace id)."""
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    fake_stream = AsyncMock()
    client = TestClient(app)
    _pause(client)
    with patch("agent.adk_agent.run_chat_stream", fake_stream):
        r = client.post(
            "/chat",
            json={"prompt": "do a thing"},
            headers={"Accept": "text/event-stream"},
        )
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    frames = _parse_sse(r.text)
    # No meta frame — there is no trace for a refused turn.
    assert all(ev != "meta" for ev, _ in frames)
    done = [d for ev, d in frames if ev == "done"]
    assert len(done) == 1
    assert done[0]["paused"] is True
    assert done[0]["tool_calls"] == []
    assert "paused" in done[0]["reply"].lower()
    fake_stream.assert_not_called()


def test_chat_fail_closed_reply_when_pause_read_raises(monkeypatch):
    """A store read failure fails /chat closed too — 200 with paused=True and a
    reply that says the pause state could not be read (the read_error variant of
    the calm copy, not the operator-chose-this one); no LLM call."""
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    state = get_state()
    fake = AsyncMock(return_value={"reply": "x", "tool_calls": [], "session_id": "s"})
    with patch.object(state, "get_pause", side_effect=RuntimeError("Firestore down")), \
         patch("agent.adk_agent.run_chat", fake):
        client = TestClient(app)
        r = client.post("/chat", json={"prompt": "do a thing"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["paused"] is True
    assert body["tool_calls"] == []
    reply = body["reply"].lower()
    assert "could not be read" in reply
    assert "fails closed" in reply
    fake.assert_not_awaited()


# --------------------------------------------------------------------------- #
# POST /approvals/{id} (rollback HITL)
# --------------------------------------------------------------------------- #


class _FakeApprovalStore:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}

    def create_pending(self, *, target_revision: str, reason: str) -> Approval:
        approval_id = str(uuid.uuid4())
        now = dt.datetime.now(dt.timezone.utc)
        data = {
            "status": "pending",
            "target_revision": target_revision,
            "reason": reason,
            "token_hmac": "fake-hmac",
            "expires_at": now + dt.timedelta(minutes=15),
            "created_at": now,
            "created_by": "coordinator@test",
        }
        self.docs[approval_id] = data
        return Approval(approval_id=approval_id, **data)

    def get(self, approval_id: str) -> Approval | None:
        if approval_id not in self.docs:
            return None
        return Approval(approval_id=approval_id, **self.docs[approval_id])

    def claim_denied(self, approval_id: str) -> Approval | None:
        data = self.docs.get(approval_id)
        if not data or data.get("status") != "pending":
            return None
        data["status"] = "denied"
        return Approval(approval_id=approval_id, **data)


@pytest.fixture
def _rollback_store(monkeypatch):
    s = _FakeApprovalStore()
    monkeypatch.setattr(approval_helpers, "get_approval_store", lambda: s)
    return s


def test_approvals_approve_paused_returns_423(_rollback_store, monkeypatch):
    """Approve while paused → 423; worker_client.call_execute is never called."""
    calls: list = []
    monkeypatch.setattr(
        worker_client, "call_execute",
        lambda aid, tok: calls.append((aid, tok)) or {"status": "executed"},
    )
    approval = _rollback_store.create_pending(
        target_revision="payment-demo-00002-bbb", reason="r"
    )
    client = TestClient(app)
    _pause(client)
    r = client.post(
        f"/approvals/{approval.approval_id}",
        data={"t": "raw-token-abc", "decision": "approve"},
    )
    assert r.status_code == 423
    assert r.json()["detail"] == PAUSED_DETAIL
    assert calls == []


def test_approvals_reject_allowed_while_paused(_rollback_store, monkeypatch):
    """Reject while paused IS allowed — denying a pending rollback is the
    safety-direction (it prevents action). call_deny IS invoked and the page
    re-renders 200."""
    deny_calls: list = []

    def fake_deny(aid, tok):
        deny_calls.append((aid, tok))
        _rollback_store.claim_denied(aid)
        return {"approval_id": aid, "status": "denied"}

    monkeypatch.setattr(worker_client, "call_deny", fake_deny)
    approval = _rollback_store.create_pending(
        target_revision="payment-demo-00002-bbb", reason="r"
    )
    client = TestClient(app)
    _pause(client)
    r = client.post(
        f"/approvals/{approval.approval_id}",
        data={"t": "raw-token-abc", "decision": "reject"},
    )
    assert r.status_code == 200, r.text
    assert deny_calls == [(approval.approval_id, "raw-token-abc")]
    assert _rollback_store.docs[approval.approval_id]["status"] == "denied"


# --------------------------------------------------------------------------- #
# GET /approvals/{id} display
# --------------------------------------------------------------------------- #


def test_approvals_get_paused_disables_approve_only(_rollback_store):
    """Paused GET of a pending approval → calm paused-note above the form, the
    Approve button is disabled, the Reject button stays active (mirrors the POST
    asymmetry: approve is gated, reject is allowed)."""
    approval = _rollback_store.create_pending(
        target_revision="payment-demo-00002-bbb", reason="r"
    )
    client = TestClient(app)
    _pause(client)
    r = client.get(f"/approvals/{approval.approval_id}?t=tok")
    assert r.status_code == 200
    body = r.text
    assert 'data-testid="paused-note"' in body
    # The Approve button is disabled; Reject is not.
    approve = re.search(r'data-testid="approve-button"[^>]*>', body)
    reject = re.search(r'data-testid="reject-button"[^>]*>', body)
    assert approve and "disabled" in approve.group(0)
    assert reject and "disabled" not in reject.group(0)


def test_approvals_get_not_paused_approve_enabled(_rollback_store):
    """Sanity: when NOT paused, the Approve button has no disabled attr and the
    paused-note is absent."""
    approval = _rollback_store.create_pending(
        target_revision="payment-demo-00002-bbb", reason="r"
    )
    client = TestClient(app)
    r = client.get(f"/approvals/{approval.approval_id}?t=tok")
    assert r.status_code == 200
    body = r.text
    assert 'data-testid="paused-note"' not in body
    approve = re.search(r'data-testid="approve-button"[^>]*>', body)
    assert approve and "disabled" not in approve.group(0)


# --------------------------------------------------------------------------- #
# POST + GET /iac-approvals/{pr_number}
# --------------------------------------------------------------------------- #

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


def _iac_metadata() -> dict:
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


def _iac_ref() -> C2CommentRef:
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


def _iac_view(**overrides) -> IacPlanView:
    base = dict(
        metadata=_iac_metadata(),
        tofu_show_text=_iac_ref().tofu_show_text,
        integrity_ok=True,
        denylist_violations=[],
        unverifiable=False,
        _artifact_uri_metadata=_META_URI,
        _generation_metadata=_GEN_META,
        _plan_json={
            "resource_changes": [
                {"address": "google_cloud_run_v2_service.x",
                 "type": "google_cloud_run_v2_service",
                 "change": {"actions": ["update"]}}
            ]
        },
    )
    base.update(overrides)
    return IacPlanView(**base)


@pytest.fixture
def _iac_configured(monkeypatch):
    monkeypatch.setenv("GITHUB_REPO", "theghostsquad00/driftscribe")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")
    monkeypatch.setenv("DRIFTSCRIBE_TOKEN", "static-server-token")
    monkeypatch.setenv("TOFU_ARTIFACTS_BUCKET", _BUCKET)
    monkeypatch.setenv("COORDINATOR_ORIGIN", _ORIGIN)
    monkeypatch.setenv("IAC_REQUIRED_CHECKS", "tofu,static-gate")
    monkeypatch.setenv("IAC_MERGE_METHOD", "squash")
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "team.cloudflareaccess.com")
    monkeypatch.setenv("CF_ACCESS_AUD_TAG", "aud-tag")
    # Not dry-run + empty GCP_PROJECT keeps get_state() on the InMemory store so
    # POST /pause and the gate share one singleton.
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("GCP_PROJECT", "")
    get_settings.cache_clear()
    app.dependency_overrides[require_cf_operator] = lambda: _OPERATOR
    yield
    app.dependency_overrides.pop(require_cf_operator, None)
    get_settings.cache_clear()


def _iac_patch_resolve(monkeypatch):
    ref, view = _iac_ref(), _iac_view()
    monkeypatch.setattr(main_mod, "_resolve_iac_plan", lambda s, pr: (ref, view))


def _iac_patch_get_resolve(monkeypatch):
    """Patch the GET's three resolution seams to the approvable happy path."""
    monkeypatch.setattr("agent.main.get_repo", lambda token, repo: object())
    monkeypatch.setattr(
        main_mod.iac_artifacts, "find_latest_c2_comment", lambda repo, pr: _iac_ref()
    )
    monkeypatch.setattr(
        main_mod.iac_artifacts,
        "load_plan_view",
        lambda r, *, bucket_name, client=None: _iac_view(),
    )


def _iac_mint():
    return iac_csrf.mint_form_token(
        get_settings(),
        pr_number=42,
        head_sha=_HEAD,
        artifact_uri_metadata=_META_URI,
        generation_metadata=_GEN_META,
        plan_sha256=_PLAN_SHA,
        plan_json_sha256=_PLAN_JSON_SHA,
        comment_id=556677,
    )


def test_iac_approve_paused_returns_423(_iac_configured, monkeypatch):
    """Approve while paused → 423 in the dry-run-precedent slot (after Origin +
    CSRF, before _resolve_iac_plan). No propose/apply worker calls."""
    _iac_patch_resolve(monkeypatch)
    propose_calls: list = []
    monkeypatch.setattr(
        main_mod.worker_client, "call_propose",
        lambda *a, **k: propose_calls.append(a) or {"approval_id": "x", "approval_token": "y"},
    )
    token = _iac_mint()
    client = TestClient(app)
    _pause(client)
    r = client.post(
        "/iac-approvals/42",
        data={"form_token": token, "decision": "approve"},
        headers={"Origin": _ORIGIN, "Cf-Access-Jwt-Assertion": _JWT},
    )
    assert r.status_code == 423
    assert r.json()["detail"] == PAUSED_DETAIL
    assert propose_calls == []


def test_iac_approve_paused_bad_origin_still_403(_iac_configured, monkeypatch):
    """Pause is checked AFTER Origin/CSRF, so a cross-site probe still gets 403
    (not a pause hint) even while paused."""
    _iac_patch_resolve(monkeypatch)
    token = _iac_mint()
    client = TestClient(app)
    _pause(client)
    r = client.post(
        "/iac-approvals/42",
        data={"form_token": token, "decision": "approve"},
        headers={"Origin": "https://evil.example.com", "Cf-Access-Jwt-Assertion": _JWT},
    )
    assert r.status_code == 403


def test_iac_reject_allowed_while_paused(_iac_configured, monkeypatch):
    """Reject while paused stays a 200 audit no-op (mutates nothing)."""
    _iac_patch_resolve(monkeypatch)
    client = TestClient(app)
    _pause(client)
    r = client.post(
        "/iac-approvals/42",
        data={"form_token": _iac_mint(), "decision": "reject"},
        headers={"Origin": _ORIGIN, "Cf-Access-Jwt-Assertion": _JWT},
    )
    assert r.status_code == 200
    assert "reject" in r.text.lower()


def test_iac_get_paused_shows_calm_note_no_form(_iac_configured, monkeypatch):
    """GET while paused → calm approve-pending note containing "paused", and
    NO token-field / approve-button (no CSRF token minted while paused)."""
    _iac_patch_get_resolve(monkeypatch)
    client = TestClient(app)
    _pause(client)
    r = client.get("/iac-approvals/42")
    assert r.status_code == 200
    body = r.text
    assert 'data-testid="approve-pending"' in body
    assert "paused" in body.lower()
    assert 'data-testid="token-field"' not in body
    assert 'data-testid="approve-button"' not in body
    assert 'name="form_token"' not in body


# --------------------------------------------------------------------------- #
# Read-only routes are UNAFFECTED while paused
# --------------------------------------------------------------------------- #


def test_decisions_and_capabilities_unaffected_while_paused():
    """Read-only routes never read the flag — they stay 200 while paused."""
    client = TestClient(app)
    _pause(client)
    assert client.get("/decisions").status_code == 200
    assert client.get("/capabilities").status_code == 200
