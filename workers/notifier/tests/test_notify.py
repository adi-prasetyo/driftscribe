"""Tests for the Notifier Agent worker (Phase 11.6).

The Notifier is the simplest of the four workers: ``POST /notify`` takes
``{channel, severity, body}``, builds a normalized payload, and posts it to
the *single* webhook URL configured at boot via Secret Manager. The caller
cannot supply or override the URL — Layer 2 enforcement is that ``url`` is
not a field on the schema, and ``extra="forbid"`` rejects any attempt to
sneak it in (a textbook confused-deputy attempt).

Coverage:

- Happy path: well-formed body → 200, outbound POST goes to the
  env-configured URL with the expected normalized payload.
- Confused-deputy: caller supplies a ``url`` field → 422, no outbound POST.
- Invalid channel / severity / oversized body / empty body → 422.
- Webhook 5xx → 502; webhook connection error → 502.
- Missing bearer / caller not in allowlist → 401 / 403 (delegated to
  ``verify_caller``).
- ``/healthz`` is unauthenticated.
- Real ``_verify_caller_dep`` is wired with OWN_URL + ALLOWED_CALLERS read
  from env at boot (mirror of reader/docs/rollback Layer 3 integration
  check — same Codex review #4 rationale).
"""
import os

import pytest
from fastapi import HTTPException, status
from fastapi.testclient import TestClient

# Env MUST be set before importing workers.notifier.main — the module reads
# OWN_URL / ALLOWED_CALLERS / GCP_PROJECT / NOTIFY_WEBHOOK_URL at import
# time and raises if any is missing. This mirrors the production fail-fast
# behavior.
os.environ.setdefault("GCP_PROJECT", "test-proj")
os.environ.setdefault("OWN_URL", "https://notifier.example.com")
os.environ.setdefault(
    "ALLOWED_CALLERS",
    "coordinator@test-proj.iam.gserviceaccount.com",
)
os.environ.setdefault("NOTIFY_WEBHOOK_URL", "https://webhook.example.com/test")

from workers.notifier import main as notifier_main  # noqa: E402
from workers.notifier.main import _verify_caller_dep, app  # noqa: E402


# --------------------------------------------------------------------------- #
# httpx stubs
# --------------------------------------------------------------------------- #


class _FakeResp:
    """Minimal httpx.Response stand-in for the happy path."""

    status_code = 200
    text = "ok"


class _FakeClient:
    """Captures the outbound POST so tests can assert on URL + payload.

    Stored as a class attribute (``_FakeClient.last``) because the worker
    constructs its own ``httpx.Client()`` instance inside ``/notify`` —
    we can't reach in and read the instance state from the test. A class
    attribute is the simplest channel; pytest's monkeypatch resets between
    tests so this doesn't leak across test cases (each test gets a fresh
    monkeypatch and our fixture re-patches).
    """

    last: dict = {}

    def __init__(self, *args, **kwargs):
        # Reset on each instantiation so a test that creates a client but
        # never POSTs doesn't observe a stale capture from a prior test.
        _FakeClient.last = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, json):
        _FakeClient.last = {"url": url, "json": json}
        return _FakeResp()


@pytest.fixture
def client(monkeypatch):
    """Build a TestClient with httpx stubbed and auth bypassed.

    Patches ``httpx.Client`` as it's bound inside ``workers.notifier.main``
    — not the source in the ``httpx`` package — for the same reason the
    reader patches ``workers.reader.main.read_live_state``: the worker
    imported ``httpx`` and now has its own binding (``httpx.Client``
    resolves through the module's ``httpx`` name).
    """
    _FakeClient.last = {}
    monkeypatch.setattr(notifier_main.httpx, "Client", _FakeClient)
    app.dependency_overrides[_verify_caller_dep] = (
        lambda: "coordinator@test-proj.iam.gserviceaccount.com"
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Happy path + Layer 2 (payload-intent policy) tests
# --------------------------------------------------------------------------- #


def test_notify_happy_path(client):
    """Well-formed body posts to the env-configured URL and returns 200."""
    r = client.post(
        "/notify",
        json={"channel": "info", "severity": "low", "body": "hello"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "sent"
    assert body["channel"] == "info"
    assert body["severity"] == "low"
    assert body["downstream_status"] == 200
    # The outbound URL is the env-configured one — NOT the caller's input.
    assert _FakeClient.last["url"] == "https://webhook.example.com/test"
    # The outbound payload is the worker's normalized dict, not the
    # caller's raw body verbatim.
    payload = _FakeClient.last["json"]
    assert payload["service"] == "DriftScribe"
    assert payload["channel"] == "info"
    assert payload["severity"] == "low"
    assert "hello" in payload["text"]


def test_caller_url_field_rejected(client):
    """Confused-deputy attempt: caller tries to supply a ``url`` field.

    ``extra="forbid"`` on the request schema makes pydantic raise
    ``ValidationError`` before the handler runs, which FastAPI surfaces
    as 422. The outbound httpx call must NOT have happened — the
    captured outbound dict stays empty.
    """
    r = client.post(
        "/notify",
        json={
            "channel": "info",
            "severity": "low",
            "body": "hello",
            "url": "https://attacker.example.com/exfil",
        },
    )
    assert r.status_code == 422
    assert _FakeClient.last == {}, (
        "outbound POST must not happen when schema rejects the request"
    )


def test_caller_extra_arbitrary_field_rejected(client):
    """Schema closure check: any unexpected field is refused, not just url."""
    r = client.post(
        "/notify",
        json={
            "channel": "info",
            "severity": "low",
            "body": "hello",
            "priority": "max",  # not in schema
        },
    )
    assert r.status_code == 422
    assert _FakeClient.last == {}


def test_invalid_channel_rejected(client):
    """Channel constrained to info|alert|approval via ``Literal``."""
    r = client.post(
        "/notify",
        json={"channel": "private-attack-channel", "severity": "low", "body": "x"},
    )
    assert r.status_code == 422
    assert _FakeClient.last == {}


def test_valid_channels_all_accepted(client):
    """Sanity-check that the three allowed channels do work."""
    for ch in ("info", "alert", "approval"):
        r = client.post(
            "/notify",
            json={"channel": ch, "severity": "low", "body": "x"},
        )
        assert r.status_code == 200, f"channel={ch} should be accepted"


def test_invalid_severity_rejected(client):
    """Severity constrained to low|medium|high|critical via ``Literal``."""
    r = client.post(
        "/notify",
        json={"channel": "info", "severity": "lol", "body": "x"},
    )
    assert r.status_code == 422
    assert _FakeClient.last == {}


def test_valid_severities_all_accepted(client):
    for sev in ("low", "medium", "high", "critical"):
        r = client.post(
            "/notify",
            json={"channel": "info", "severity": sev, "body": "x"},
        )
        assert r.status_code == 200, f"severity={sev} should be accepted"


def test_oversize_body_rejected(client):
    """Body length cap is 10000; anything larger is refused at the schema."""
    r = client.post(
        "/notify",
        json={"channel": "info", "severity": "low", "body": "x" * 20000},
    )
    assert r.status_code == 422
    assert _FakeClient.last == {}


def test_at_limit_body_accepted(client):
    """Exactly 10000 chars is at the cap and must be accepted."""
    r = client.post(
        "/notify",
        json={"channel": "info", "severity": "low", "body": "x" * 10000},
    )
    assert r.status_code == 200


def test_empty_body_rejected(client):
    """``min_length=1`` keeps the payload meaningful."""
    r = client.post(
        "/notify",
        json={"channel": "info", "severity": "low", "body": ""},
    )
    assert r.status_code == 422


def test_missing_channel_rejected(client):
    """All three fields are required."""
    r = client.post(
        "/notify",
        json={"severity": "low", "body": "x"},
    )
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Downstream-webhook failure-mode tests
# --------------------------------------------------------------------------- #


def test_webhook_5xx_returns_502(client, monkeypatch):
    """Non-2xx from the downstream webhook → 502 with the status surfaced."""

    class _FailResp:
        status_code = 500
        text = "downstream broken"

    class _FailClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json):
            return _FailResp()

    monkeypatch.setattr(notifier_main.httpx, "Client", _FailClient)
    r = client.post(
        "/notify",
        json={"channel": "info", "severity": "low", "body": "x"},
    )
    assert r.status_code == 502
    assert "500" in r.json()["detail"]


def test_webhook_4xx_returns_502(client, monkeypatch):
    """4xx from the downstream is also "this notification didn't land" → 502."""

    class _FailResp:
        status_code = 404
        text = "no such hook"

    class _FailClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json):
            return _FailResp()

    monkeypatch.setattr(notifier_main.httpx, "Client", _FailClient)
    r = client.post(
        "/notify",
        json={"channel": "info", "severity": "low", "body": "x"},
    )
    assert r.status_code == 502


def test_webhook_connect_error_returns_502(client, monkeypatch):
    """``httpx.RequestError`` (incl. ConnectError / TimeoutException) → 502."""
    import httpx

    class _TimeoutClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json):
            raise httpx.ConnectError("nope")

    monkeypatch.setattr(notifier_main.httpx, "Client", _TimeoutClient)
    r = client.post(
        "/notify",
        json={"channel": "info", "severity": "low", "body": "x"},
    )
    assert r.status_code == 502
    assert "unavailable" in r.json()["detail"].lower()


# --------------------------------------------------------------------------- #
# Layer 3 (inter-service auth) tests
# --------------------------------------------------------------------------- #


def test_missing_bearer_returns_401(client):
    def deny_401():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
        )

    app.dependency_overrides[_verify_caller_dep] = deny_401
    r = client.post(
        "/notify",
        json={"channel": "info", "severity": "low", "body": "x"},
    )
    assert r.status_code == 401


def test_caller_not_in_allowlist_returns_403(client):
    def deny_caller():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="caller 'nope@example.com' not in allowed_callers",
        )

    app.dependency_overrides[_verify_caller_dep] = deny_caller
    r = client.post(
        "/notify",
        json={"channel": "info", "severity": "low", "body": "x"},
    )
    assert r.status_code == 403


def test_healthz_does_not_require_auth(client):
    """``/healthz`` has no Depends, so even a denying override doesn't fire."""

    def boom():
        raise HTTPException(status_code=401, detail="should not be called")

    app.dependency_overrides[_verify_caller_dep] = boom
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_real_verify_caller_dep_wired_with_env(monkeypatch):
    """Layer 3 integration check (mirror of reader/docs/rollback).

    Without ``dependency_overrides`` the real ``_verify_caller_dep`` must
    call ``verify_caller`` with OWN_URL + ALLOWED_CALLERS read from env
    at boot. We monkeypatch the module-level constants rather than relying
    on the import-time env read because, in a unified pytest run, another
    worker's test module may have populated ``OWN_URL`` before this module
    was imported (Python caches the import; ``os.environ.setdefault`` at
    the top of this file would then be a no-op).
    """
    seen = {}

    def fake_verify(request, *, own_url, allowed_callers):
        seen["own_url"] = own_url
        seen["allowed_callers"] = set(allowed_callers)
        return "coordinator@test-proj.iam.gserviceaccount.com"

    monkeypatch.setattr(notifier_main, "verify_caller", fake_verify)
    monkeypatch.setattr(notifier_main, "OWN_URL", "https://notifier.example.com")
    monkeypatch.setattr(
        notifier_main,
        "ALLOWED_CALLERS",
        frozenset({"coordinator@test-proj.iam.gserviceaccount.com"}),
    )
    # Stub httpx too so the request doesn't try to hit the network.
    monkeypatch.setattr(notifier_main.httpx, "Client", _FakeClient)

    # No dependency_overrides — exercise the real _verify_caller_dep.
    notifier_main.app.dependency_overrides.clear()
    c = TestClient(app)
    r = c.post(
        "/notify",
        json={"channel": "info", "severity": "low", "body": "hello"},
        headers={"Authorization": "Bearer faketoken"},
    )
    assert r.status_code == 200, r.text
    assert seen["own_url"] == "https://notifier.example.com"
    assert seen["allowed_callers"] == {
        "coordinator@test-proj.iam.gserviceaccount.com",
    }
