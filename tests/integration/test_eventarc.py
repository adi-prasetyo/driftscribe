"""Integration tests for the ``/eventarc`` auto-trigger endpoint (Phase 14.2).

The handler accepts CloudEvent-wrapped Cloud Run audit-log entries, verifies a
Google-signed ID token minted for the ``eventarc-trigger-sa`` service account,
filters by ``(service, region)`` whitelist, and dispatches the same recheck
path as ``/recheck`` under the ``trigger="eventarc"`` label.

Mock surface:

- ``agent.main.verify_oauth2_token`` — the Google id_token verifier. We never
  hand it a real signed token; tests configure its return value or side
  effects to exercise each auth branch.
- ``agent.main._do_recheck`` — patched as ``AsyncMock`` so we can pin that
  the trigger label is exactly ``"eventarc"`` without exercising the whole
  classify/render/perform pipeline (already covered elsewhere).

Settings: tests set ``EVENTARC_AUDIENCE`` and rely on the autouse conftest's
``GCP_PROJECT=test-proj``. ``get_settings.cache_clear()`` is called after each
monkeypatch.setenv so the new value is observed.
"""

from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from google.auth import exceptions as google_auth_exceptions

from agent.config import get_settings
from agent.main import app


_VALID_AUDIENCE = "https://driftscribe-agent-xyz.a.run.app"
_EXPECTED_EMAIL = "eventarc-trigger-sa@test-proj.iam.gserviceaccount.com"


def _audit_log_body(
    service_name: str = "payment-demo",
    location: str = "asia-northeast1",
    method_name: str = "google.cloud.run.v2.Services.UpdateService",
) -> dict:
    """Shape a minimal Cloud Run audit-log entry.

    Only ``resource.labels.{service_name,location}`` are read by the handler
    today; ``protoPayload`` is included for parity with the real payload
    documented in ``docs/architecture/eventarc-payload.md`` so a future test
    that asserts on it doesn't silently pass against a thinner stub.
    """
    return {
        "protoPayload": {
            "@type": "type.googleapis.com/google.cloud.audit.AuditLog",
            "methodName": method_name,
            "resourceName": (
                f"projects/test-proj/locations/{location}/services/{service_name}"
            ),
            "serviceName": "run.googleapis.com",
        },
        "resource": {
            "type": "cloud_run_revision",
            "labels": {
                "service_name": service_name,
                "location": location,
                "project_id": "test-proj",
            },
        },
    }


def _set_audience(monkeypatch, value: str = _VALID_AUDIENCE) -> None:
    """Configure EVENTARC_AUDIENCE and bust the Settings cache."""
    monkeypatch.setenv("EVENTARC_AUDIENCE", value)
    get_settings.cache_clear()


def test_eventarc_rejects_missing_authorization(monkeypatch):
    """No Authorization header → 401 before any token verification."""
    _set_audience(monkeypatch)
    client = TestClient(app)
    r = client.post("/eventarc", json=_audit_log_body())
    assert r.status_code == 401
    assert "authorization" in r.json()["detail"].lower()


def test_eventarc_rejects_non_bearer_authorization(monkeypatch):
    """``Authorization: Basic <...>`` → 401. Only Bearer is accepted."""
    _set_audience(monkeypatch)
    client = TestClient(app)
    r = client.post(
        "/eventarc",
        json=_audit_log_body(),
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert r.status_code == 401
    assert "bearer" in r.json()["detail"].lower()


def test_eventarc_rejects_invalid_token(monkeypatch):
    """``verify_oauth2_token`` raising ValueError → 401 (bad signature/aud/exp)."""
    _set_audience(monkeypatch)
    with patch("agent.main.verify_oauth2_token") as m_verify:
        m_verify.side_effect = ValueError("Token expired")
        client = TestClient(app)
        r = client.post(
            "/eventarc",
            json=_audit_log_body(),
            headers={"Authorization": "Bearer fake-token"},
        )
    assert r.status_code == 401
    assert "token" in r.json()["detail"].lower()


def test_eventarc_rejects_wrong_issuer(monkeypatch):
    """``verify_oauth2_token`` raising GoogleAuthError → 401 (wrong issuer).

    Per the verifier's docstring, ``GoogleAuthError`` surfaces when the ``iss``
    claim isn't one of Google's. Same collapse-to-401 policy as ValueError:
    a probe should not distinguish "wrong issuer" from "expired token".
    """
    _set_audience(monkeypatch)
    with patch("agent.main.verify_oauth2_token") as m_verify:
        m_verify.side_effect = google_auth_exceptions.GoogleAuthError(
            "Wrong issuer. 'iss' should be one of the following: ..."
        )
        client = TestClient(app)
        r = client.post(
            "/eventarc",
            json=_audit_log_body(),
            headers={"Authorization": "Bearer fake-token"},
        )
    assert r.status_code == 401
    # Verifier's internal message must NOT leak through.
    assert "iss" not in r.json()["detail"].lower()


def test_eventarc_rejects_transport_error(monkeypatch):
    """JWKS fetch failure (``TransportError``) → 401, NOT 500.

    Strictly this is an upstream-availability condition, but we collapse to
    401 so (a) the auth-failure response is uniform (a probe cannot tell
    "your token is bad" from "our cert cache is cold"), and (b) Eventarc's
    retry on 401 hits a warm cache on the next attempt.

    ``TransportError`` is a subclass of ``GoogleAuthError``, so the same
    except-clause handles it. Test explicitly to pin the behavior against a
    future refactor that splits the catch.
    """
    _set_audience(monkeypatch)
    with patch("agent.main.verify_oauth2_token") as m_verify:
        m_verify.side_effect = google_auth_exceptions.TransportError(
            "Could not fetch certificates at https://www.googleapis.com/..."
        )
        client = TestClient(app)
        r = client.post(
            "/eventarc",
            json=_audit_log_body(),
            headers={"Authorization": "Bearer fake-token"},
        )
    assert r.status_code == 401


def test_eventarc_rejects_wrong_email_claim(monkeypatch):
    """Token verifies but ``email`` is not the eventarc trigger SA → 403.

    Belt-and-suspenders next to the IAM ``run.invoker`` binding: even if a
    misconfigured deploy granted the role too broadly, only the dedicated
    trigger SA's ID tokens are honored by this handler.
    """
    _set_audience(monkeypatch)
    with patch("agent.main.verify_oauth2_token") as m_verify:
        m_verify.return_value = {
            "email": "some-other-sa@test-proj.iam.gserviceaccount.com",
            "aud": _VALID_AUDIENCE,
        }
        client = TestClient(app)
        r = client.post(
            "/eventarc",
            json=_audit_log_body(),
            headers={"Authorization": "Bearer fake-token"},
        )
    assert r.status_code == 403
    # Don't echo the actual presented email — that's an info leak.
    detail = r.json()["detail"].lower()
    assert "some-other-sa" not in detail
    assert "service account" in detail or "principal" in detail


def test_eventarc_accepts_correct_email_and_dispatches_recheck(monkeypatch):
    """Full valid path: bearer verifies, email matches, recheck dispatched.

    Pins that ``_do_recheck`` is called with ``trigger="eventarc"`` so the
    decision document records the auto-trigger source (which the smoke test
    polls Firestore for).
    """
    _set_audience(monkeypatch)
    recheck_result = {
        "decision_id": "test-dec-123",
        "event_key": "eventarc-payment-demo-deadbeef",
        "action": "no_op",
        "trigger": "eventarc",
    }
    mock_recheck = AsyncMock(return_value=recheck_result)
    with (
        patch("agent.main.verify_oauth2_token") as m_verify,
        patch("agent.main._do_recheck", mock_recheck),
    ):
        m_verify.return_value = {"email": _EXPECTED_EMAIL, "aud": _VALID_AUDIENCE}
        client = TestClient(app)
        r = client.post(
            "/eventarc",
            json=_audit_log_body(),
            headers={"Authorization": "Bearer fake-token"},
        )
    assert r.status_code == 200
    assert r.json() == recheck_result
    mock_recheck.assert_awaited_once_with("eventarc")
    # The verifier was called with the configured audience — pin that the
    # handler uses settings.eventarc_audience, not a hardcoded value.
    args, kwargs = m_verify.call_args
    assert kwargs.get("audience") == _VALID_AUDIENCE or _VALID_AUDIENCE in args


def test_eventarc_ignores_non_target_service(monkeypatch):
    """Body with ``service_name`` != target → 200 ignored, NO recheck.

    Eventarc retries on non-2xx; ignoring with 200 prevents the trigger from
    looping on the same off-target event forever.
    """
    _set_audience(monkeypatch)
    mock_recheck = AsyncMock()
    with (
        patch("agent.main.verify_oauth2_token") as m_verify,
        patch("agent.main._do_recheck", mock_recheck),
    ):
        m_verify.return_value = {"email": _EXPECTED_EMAIL, "aud": _VALID_AUDIENCE}
        client = TestClient(app)
        r = client.post(
            "/eventarc",
            json=_audit_log_body(service_name="other-service"),
            headers={"Authorization": "Bearer fake-token"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body.get("ignored") == "non-target-service"
    assert body.get("service") == "other-service"
    mock_recheck.assert_not_awaited()


def test_eventarc_ignores_non_target_region(monkeypatch):
    """Body with ``location`` != target region → 200 ignored, NO recheck."""
    _set_audience(monkeypatch)
    mock_recheck = AsyncMock()
    with (
        patch("agent.main.verify_oauth2_token") as m_verify,
        patch("agent.main._do_recheck", mock_recheck),
    ):
        m_verify.return_value = {"email": _EXPECTED_EMAIL, "aud": _VALID_AUDIENCE}
        client = TestClient(app)
        r = client.post(
            "/eventarc",
            json=_audit_log_body(location="us-central1"),
            headers={"Authorization": "Bearer fake-token"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body.get("ignored") == "non-target-service"
    assert body.get("region") == "us-central1"
    mock_recheck.assert_not_awaited()


def test_eventarc_returns_200_ignored_on_missing_resource_labels(monkeypatch):
    """Body without ``resource.labels`` → 200 ignored, NO recheck.

    Phase 15.3 (Codex carry-over from Phase 14): we previously returned 400
    here, but Eventarc retries on 4xx in some paths, and a future audit-log
    schema change could trigger a retry storm. Acknowledge delivery with
    200 + ``{"ignored": "malformed-payload"}`` instead.
    """
    _set_audience(monkeypatch)
    mock_recheck = AsyncMock()
    with (
        patch("agent.main.verify_oauth2_token") as m_verify,
        patch("agent.main._do_recheck", mock_recheck),
    ):
        m_verify.return_value = {"email": _EXPECTED_EMAIL, "aud": _VALID_AUDIENCE}
        client = TestClient(app)
        r = client.post(
            "/eventarc",
            json={"protoPayload": {"methodName": "x"}},  # no resource.labels
            headers={"Authorization": "Bearer fake-token"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ignored"] == "malformed-payload"
    assert body["reason"] == "missing_resource"
    mock_recheck.assert_not_awaited()


def test_eventarc_returns_200_ignored_on_invalid_json(monkeypatch):
    """Body that isn't valid JSON → 200 ignored, NO recheck."""
    _set_audience(monkeypatch)
    mock_recheck = AsyncMock()
    with (
        patch("agent.main.verify_oauth2_token") as m_verify,
        patch("agent.main._do_recheck", mock_recheck),
    ):
        m_verify.return_value = {"email": _EXPECTED_EMAIL, "aud": _VALID_AUDIENCE}
        client = TestClient(app)
        r = client.post(
            "/eventarc",
            content=b"not json at all { ] :",
            headers={
                "Authorization": "Bearer fake-token",
                "Content-Type": "application/json",
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ignored"] == "malformed-payload"
    assert body["reason"] == "invalid_json"
    mock_recheck.assert_not_awaited()


def test_eventarc_invalid_json_response_does_not_echo_payload(monkeypatch):
    """Phase 15.3 hardening: the invalid_json 200 response MUST NOT echo
    raw bytes from the request body. The previous 400 detail string
    embedded ``str(e)`` from the JSON parser, which contains a fragment of
    the offending input — a small info-leak / response-inflation vector
    against an unauthenticated-from-the-internet endpoint (the auth gate
    runs first, but defense-in-depth).
    """
    _set_audience(monkeypatch)
    # An attacker-controlled marker we'll search for in the response body
    # to confirm it isn't being reflected back.
    leak_marker = "SECRET_CANARY_zzz_should_not_appear"
    mock_recheck = AsyncMock()
    with (
        patch("agent.main.verify_oauth2_token") as m_verify,
        patch("agent.main._do_recheck", mock_recheck),
    ):
        m_verify.return_value = {"email": _EXPECTED_EMAIL, "aud": _VALID_AUDIENCE}
        client = TestClient(app)
        r = client.post(
            "/eventarc",
            content=f'{{"oops": "{leak_marker}'.encode(),  # missing closing brace+quote
            headers={
                "Authorization": "Bearer fake-token",
                "Content-Type": "application/json",
            },
        )
    assert r.status_code == 200
    assert leak_marker not in r.text
    mock_recheck.assert_not_awaited()


def test_eventarc_returns_200_ignored_on_empty_service_label(monkeypatch):
    """``resource.labels`` present but ``service_name`` empty → 200 ignored.

    The shape check needs to catch ``{"resource": {"labels": {}}}`` (and
    similar partial structures) — otherwise the whitelist comparison
    would silently route every event off-target with empty ``service``.
    """
    _set_audience(monkeypatch)
    mock_recheck = AsyncMock()
    with (
        patch("agent.main.verify_oauth2_token") as m_verify,
        patch("agent.main._do_recheck", mock_recheck),
    ):
        m_verify.return_value = {"email": _EXPECTED_EMAIL, "aud": _VALID_AUDIENCE}
        client = TestClient(app)
        r = client.post(
            "/eventarc",
            json={
                "resource": {
                    "labels": {
                        "service_name": "",  # empty
                        "location": "asia-northeast1",
                    }
                }
            },
            headers={"Authorization": "Bearer fake-token"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ignored"] == "malformed-payload"
    assert body["reason"] == "missing_service_or_region"
    mock_recheck.assert_not_awaited()


def test_eventarc_returns_200_ignored_when_body_is_not_object(monkeypatch):
    """Body parses as JSON but is a list, not a dict → 200 ignored."""
    _set_audience(monkeypatch)
    mock_recheck = AsyncMock()
    with (
        patch("agent.main.verify_oauth2_token") as m_verify,
        patch("agent.main._do_recheck", mock_recheck),
    ):
        m_verify.return_value = {"email": _EXPECTED_EMAIL, "aud": _VALID_AUDIENCE}
        client = TestClient(app)
        r = client.post(
            "/eventarc",
            json=["not", "an", "object"],
            headers={"Authorization": "Bearer fake-token"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ignored"] == "malformed-payload"
    assert body["reason"] == "body_not_object"
    mock_recheck.assert_not_awaited()


def test_eventarc_returns_200_ignored_when_labels_not_object(monkeypatch):
    """``resource`` present but ``resource.labels`` missing → 200 ignored."""
    _set_audience(monkeypatch)
    mock_recheck = AsyncMock()
    with (
        patch("agent.main.verify_oauth2_token") as m_verify,
        patch("agent.main._do_recheck", mock_recheck),
    ):
        m_verify.return_value = {"email": _EXPECTED_EMAIL, "aud": _VALID_AUDIENCE}
        client = TestClient(app)
        r = client.post(
            "/eventarc",
            json={"resource": {"type": "cloud_run_revision"}},  # no labels
            headers={"Authorization": "Bearer fake-token"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ignored"] == "malformed-payload"
    assert body["reason"] == "missing_labels"
    mock_recheck.assert_not_awaited()


def test_eventarc_returns_503_when_audience_unset(monkeypatch):
    """Empty EVENTARC_AUDIENCE → 503 before any token verify (fail-closed)."""
    monkeypatch.setenv("EVENTARC_AUDIENCE", "")
    get_settings.cache_clear()
    with patch("agent.main.verify_oauth2_token") as m_verify:
        client = TestClient(app)
        r = client.post(
            "/eventarc",
            json=_audit_log_body(),
            headers={"Authorization": "Bearer fake-token"},
        )
    assert r.status_code == 503
    assert "eventarc_audience" in r.json()["detail"].lower()
    m_verify.assert_not_called()


def test_eventarc_returns_503_when_gcp_project_unset(monkeypatch):
    """Empty GCP_PROJECT → 503: the expected-email can't be built."""
    _set_audience(monkeypatch)
    monkeypatch.setenv("GCP_PROJECT", "")
    get_settings.cache_clear()
    client = TestClient(app)
    r = client.post(
        "/eventarc",
        json=_audit_log_body(),
        headers={"Authorization": "Bearer fake-token"},
    )
    assert r.status_code == 503
    assert "gcp_project" in r.json()["detail"].lower()


def test_eventarc_propagates_recheck_502(monkeypatch):
    """If ``_do_recheck`` raises HTTPException(502), the handler does NOT
    swallow it — the worker-failure status surfaces unchanged to the caller.
    """
    _set_audience(monkeypatch)
    mock_recheck = AsyncMock(
        side_effect=HTTPException(status_code=502, detail="cloud run read failed")
    )
    with (
        patch("agent.main.verify_oauth2_token") as m_verify,
        patch("agent.main._do_recheck", mock_recheck),
    ):
        m_verify.return_value = {"email": _EXPECTED_EMAIL, "aud": _VALID_AUDIENCE}
        client = TestClient(app)
        r = client.post(
            "/eventarc",
            json=_audit_log_body(),
            headers={"Authorization": "Bearer fake-token"},
        )
    assert r.status_code == 502
    assert "cloud run read failed" in r.json()["detail"]
