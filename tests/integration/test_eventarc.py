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

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from google.auth import exceptions as google_auth_exceptions

from agent.config import get_settings
from agent.main import app
from driftscribe_lib import logging as ds_logging


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


def test_eventarc_email_check_uses_hmac_compare_digest(monkeypatch):
    """Phase 15.3: pin that the email-claim check goes through
    ``hmac.compare_digest`` (Codex carry-over from Phase 14). Without
    this test the implementation could silently regress to ``==`` and
    only the (mild) timing-attack property would be lost — no other
    test would catch it.

    Same pattern as ``test_token_guard.py::test_constant_time_compare_is_used``.
    """
    _set_audience(monkeypatch)
    with (
        patch("agent.main.verify_oauth2_token") as m_verify,
        patch("agent.main.hmac.compare_digest", return_value=False) as cmp_,
    ):
        m_verify.return_value = {"email": _EXPECTED_EMAIL, "aud": _VALID_AUDIENCE}
        client = TestClient(app)
        r = client.post(
            "/eventarc",
            json=_audit_log_body(),
            headers={"Authorization": "Bearer fake-token"},
        )
    # compare_digest returned False (forced) → 403 even though the email
    # actually matched. That's the proof the comparison routes through
    # compare_digest and not ``==``.
    assert r.status_code == 403
    assert cmp_.called, "email check must use hmac.compare_digest, not =="
    args, _kwargs = cmp_.call_args
    assert all(isinstance(a, str) for a in args), (
        "compare_digest args must be str+str (None must be coerced)"
    )


def test_eventarc_rejects_missing_email_claim(monkeypatch):
    """Token verifies but the ``email`` claim is absent → 403.

    Phase 15.3 hardening: the comparison uses ``hmac.compare_digest`` for
    constant-time semantics, which requires str+str inputs. A claims dict
    without ``email`` must not crash; the principal check must still
    surface the same 403 as any other principal mismatch.

    Phase 15.4 update: the absent-key case is now caught by the
    ``isinstance(presented_email, str)`` short-circuit (None is not a
    str) BEFORE compare_digest. The previous code coerced None to ``""``
    via ``or ""`` and compared against the expected email — which also
    yielded False/403 — but the new path is type-safe against non-str
    non-None values (123, [], {}) too. Either way the externally
    observable behavior pinned here is the same: 403, no crash.
    """
    _set_audience(monkeypatch)
    with patch("agent.main.verify_oauth2_token") as m_verify:
        # No 'email' key in claims — verifier returned a degenerate dict.
        m_verify.return_value = {"aud": _VALID_AUDIENCE}
        client = TestClient(app)
        r = client.post(
            "/eventarc",
            json=_audit_log_body(),
            headers={"Authorization": "Bearer fake-token"},
        )
    assert r.status_code == 403
    detail = r.json()["detail"].lower()
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
    # Phase 17.A.3 (Codex blocker): /eventarc hardcodes workload="drift"
    # server-side. Pin the full call so a regression that drops the
    # kwarg — or, worse, lets the payload smuggle a different workload
    # in — would fail this assertion.
    mock_recheck.assert_awaited_once_with("eventarc", workload="drift")
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


# --------------------------------------------------------------------------- #
# Phase 15.4: Codex review cleanup — non-string claim/label hardening.
#
# Two carry-over bugs from the Phase 15 phase-level Codex review:
#
# - Bug A: ``claims.get("email")`` could (off-spec) return a non-str like
#   an int or list. The previous ``... or ""`` only coerced None — not
#   123 — so ``hmac.compare_digest(123, "...")`` raised ``TypeError``
#   inside the handler and FastAPI surfaced 500. Correct outcome is 403
#   (same as wrong-principal): "this verified token's email claim isn't
#   acceptable". Fix: ``isinstance(presented_email, str)`` short-circuit
#   BEFORE compare_digest.
#
# - Bug B: ``labels.get("service_name")`` could be a non-string. The
#   truthy non-string cases — ``["payment-demo"]``, ``{"name": "x"}`` —
#   would slip past the ``if not service`` existence check and flow into
#   the ``non-target-service`` response, where they'd be echoed back in
#   the body. That partially defeats the "fixed short reason, no payload
#   echo" intent of the 15.3 ignored-200 hardening. The falsy non-string
#   cases (``[]``, ``{}``) happened to be caught by the existing
#   ``not service`` truthiness check and routed to ``malformed-payload``
#   — but only by accident of Python truthiness, not by type contract.
#   Fix: ``isinstance(..., str)`` up front in the same combined check.
#   That pins the type contract (so a future refactor to ``is None``
#   doesn't silently break the falsy-non-string case) AND closes the
#   echo-leak from truthy non-strings.
# --------------------------------------------------------------------------- #


def test_eventarc_non_string_email_claim_returns_403_not_500(monkeypatch):
    """Phase 15.4 (Bug A): an off-spec ``email`` claim that isn't a
    string must surface as 403, NOT 500.

    Background: ``hmac.compare_digest`` requires str+str (or bytes+bytes)
    and raises ``TypeError`` on a type mismatch. The previous code did
    ``claims.get("email") or ""`` which coerces None but not 123 or
    ``["x"]`` — so a malformed verified token could crash the handler
    into a 500. The right code is the same as any other wrong-principal
    case: 403. We pin this with an int because it's the smallest example
    of a non-str-but-truthy value (so the ``or ""`` fallback wouldn't
    even fire).

    Note: OIDC spec says ``email`` is a string. A verified Google id_token
    SHOULD never present a non-string email. But over a long enough
    horizon, "should never" is not "cannot", and the cost of being wrong
    here is 500-pages in production. The isinstance guard is cheap.
    """
    _set_audience(monkeypatch)
    with patch("agent.main.verify_oauth2_token") as m_verify:
        # Non-string email claim — would have crashed compare_digest.
        m_verify.return_value = {"email": 123, "aud": _VALID_AUDIENCE}
        client = TestClient(app)
        r = client.post(
            "/eventarc",
            json=_audit_log_body(),
            headers={"Authorization": "Bearer fake-token"},
        )
    # Must be 403 (the unified principal-mismatch outcome), NOT 500.
    assert r.status_code == 403, (
        f"non-string email must surface as 403, got {r.status_code}: {r.text}"
    )
    detail = r.json()["detail"].lower()
    assert "service account" in detail or "principal" in detail
    # And of course the offending int must not appear in the detail
    # (defense-in-depth against a future refactor that decides to echo).
    assert "123" not in detail


@pytest.mark.parametrize(
    "service_name, location",
    [
        ([], "asia-northeast1"),         # service_name is list
        ({}, "asia-northeast1"),         # service_name is dict
        (["payment-demo"], "asia-northeast1"),  # truthy non-string list
        ("payment-demo", []),            # location is list
        ("payment-demo", {}),            # location is dict
        ({"name": "x"}, "asia-northeast1"),  # truthy non-string dict
    ],
)
def test_eventarc_non_string_labels_return_malformed_payload(
    monkeypatch, service_name, location
):
    """Phase 15.4 (Bug B): non-string ``service_name`` / ``location``
    labels must route to ``malformed-payload`` (the fixed-reason ignored
    200), NOT ``non-target-service`` (which echoes the offending value
    back in the response body).

    Both truthy (``["payment-demo"]``, ``{"name": "x"}``) and falsy
    (``[]``, ``{}``) non-string values are tested:

    - Falsy non-strings would previously be caught by ``if not service``
      and routed to ``malformed-payload`` — but only by accident of
      Python's truthiness rules, not by type. A future refactor that
      changed the check to ``if service is None`` would silently break
      that. The isinstance check pins the type contract.
    - Truthy non-strings (``["payment-demo"]``) would previously slip
      past the existence check and reach the whitelist comparison, where
      they'd be echoed in the ``non-target-service`` response body. That
      defeats the "fixed short reason, no payload echo" intent of the
      15.3 hardening.

    Both shapes share the same ``reason: missing_service_or_region``
    because they fail the same contract ("we can't safely whitelist-check
    this label"). Operators reading the response don't need a finer-
    grained tag; the log lines have the structured detail.
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
                        "service_name": service_name,
                        "location": location,
                    }
                }
            },
            headers={"Authorization": "Bearer fake-token"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "ignored": "malformed-payload",
        "reason": "missing_service_or_region",
    }, (
        f"non-string labels must route to malformed-payload, not "
        f"non-target-service (which would echo the value): {body!r}"
    )
    # And in particular, the non-target-service code path MUST NOT have
    # been taken — the offending value must not be echoed back.
    assert "service" not in body, body
    assert "region" not in body, body
    mock_recheck.assert_not_awaited()


def test_eventarc_ignored_path_preserves_inbound_trace_id_and_resets_contextvar(
    monkeypatch,
):
    """Phase 15.4 (cross-task integration): an ignored 200 path on
    ``/eventarc`` must (a) preserve a well-formed inbound ``X-Trace-Id``,
    (b) NOT call ``_do_recheck``, and (c) reset the ContextVar after the
    response so the binding doesn't leak across requests.

    This is the seam between the Phase 14.2 ignored-200 hardening and
    the Phase 15.2 trace-id middleware. If a future refactor added an
    early-return without going through the middleware's finally — or if
    the middleware were ever removed from this app — the assertion on
    ``get_trace_id() == ""`` would catch it.

    Inbound trace id is 32-char lowercase hex (the format the middleware
    adopts unmodified per ``test_trace_propagation.py``).
    """
    _set_audience(monkeypatch)
    inbound_trace = "9" * 32  # well-formed 32-char hex
    mock_recheck = AsyncMock()
    # Use a malformed-payload trigger (resource.labels missing) — exercises
    # the same early-return shape as non-target-service but is also
    # representative of the broader "ignored" path family.
    with (
        patch("agent.main.verify_oauth2_token") as m_verify,
        patch("agent.main._do_recheck", mock_recheck),
    ):
        m_verify.return_value = {"email": _EXPECTED_EMAIL, "aud": _VALID_AUDIENCE}
        client = TestClient(app)
        r = client.post(
            "/eventarc",
            json={"protoPayload": {"methodName": "x"}},  # no resource.labels
            headers={
                "Authorization": "Bearer fake-token",
                "X-Trace-Id": inbound_trace,
            },
        )

    # (a) Ignored path returned 200.
    assert r.status_code == 200
    body = r.json()
    assert body["ignored"] == "malformed-payload"

    # (b) Inbound trace id was adopted unchanged by the middleware.
    assert r.headers.get("X-Trace-Id") == inbound_trace, (
        f"middleware must preserve well-formed inbound trace id, "
        f"got {r.headers.get('X-Trace-Id')!r}"
    )

    # (c) _do_recheck was NOT called on this ignored path.
    mock_recheck.assert_not_awaited()

    # (d) ContextVar was reset by the middleware's finally — the binding
    # must not outlive the request.
    assert ds_logging.get_trace_id() == "", (
        "trace-id ContextVar leaked past the request boundary"
    )


def test_eventarc_non_target_service_path_also_preserves_trace_id(monkeypatch):
    """Companion to the previous test: the OTHER ignored path
    (non-target-service, which still echoes service+region for operator
    diagnostics — that part is intentional and unchanged in 15.4) must
    also preserve the inbound trace id and reset the ContextVar.

    Pins that BOTH families of ignored-200 returns go through the
    middleware's response cycle.
    """
    _set_audience(monkeypatch)
    inbound_trace = "8" * 32
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
            headers={
                "Authorization": "Bearer fake-token",
                "X-Trace-Id": inbound_trace,
            },
        )
    assert r.status_code == 200
    assert r.json()["ignored"] == "non-target-service"
    assert r.headers.get("X-Trace-Id") == inbound_trace
    mock_recheck.assert_not_awaited()
    assert ds_logging.get_trace_id() == ""
