"""Tests for the Reader Agent worker (Phase 11.3).

Covers the contract laid out in the plan:

- Empty body → 200 with env + revision for the configured target.
- Extra fields in the body → 4xx (payload-intent policy, Layer 2).
- Missing bearer token → 401 (delegated to ``verify_caller``).
- Wrong-audience token → 401.
- Caller not in allowlist → 403.
- ``/healthz`` is unauthenticated.

The real ``verify_caller`` lives in ``driftscribe_lib.auth`` and is exercised
by the Phase 11.0 spike's tests; here we use FastAPI's
``app.dependency_overrides`` to swap the dependency, which is faster and
doesn't require a Google ID token at all.
"""
import os

import pytest
from fastapi import HTTPException, status
from fastapi.testclient import TestClient
from google.api_core import exceptions as gax

# Env MUST be set before importing workers.reader.main — the module reads
# OWN_URL / ALLOWED_CALLERS / GCP_PROJECT at import time and KeyErrors if
# missing. This mirrors the production fail-fast behavior.
os.environ.setdefault("GCP_PROJECT", "test-proj")
os.environ.setdefault("OWN_URL", "https://reader.example.com")
os.environ.setdefault(
    "ALLOWED_CALLERS",
    "coordinator@test-proj.iam.gserviceaccount.com",
)

from workers.reader import main as reader_main  # noqa: E402
from workers.reader.main import _verify_caller_dep, app  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    """Build a TestClient with Cloud Run reads stubbed and auth bypassed.

    The patch targets ``workers.reader.main.read_live_state`` (the symbol
    *as imported into the worker module*), not the source in
    ``driftscribe_lib.cloud_run`` — because ``main.py`` did
    ``from driftscribe_lib.cloud_run import read_live_state`` and now has
    its own binding. Individual tests can override ``_verify_caller_dep``
    again to simulate auth failure modes.
    """
    monkeypatch.setattr(
        reader_main,
        "read_live_state",
        lambda s, r, p, **_: {
            "env": {"PAYMENT_MODE": "mock", "FEATURE_NEW_CHECKOUT": "false"},
            "revision": "payment-demo-00001-abc",
        },
    )
    monkeypatch.setattr(
        reader_main,
        "list_previous_ready_revisions",
        lambda s, r, p, active, **_: ["payment-demo-00000-zzz"],
    )
    # Stub the client indirection (mirrors the rollback worker's tests) so no
    # real gRPC channel / ADC lookup is attempted — both consumers above are
    # stubbed and swallow the revisions_client kwarg via **_.
    monkeypatch.setattr(reader_main, "_get_revisions_client", lambda: object())
    # Pin the boot-time env-derived constants this test hard-asserts so the
    # asserted values can't be polluted by import order. In a unified pytest
    # run another worker's test module (e.g. infra_reader) may set GCP_PROJECT
    # via os.environ.setdefault *before* reader.main imports, turning this
    # module's own setdefault("GCP_PROJECT", "test-proj") into a no-op. Pinning
    # here mirrors how test_real_verify_caller_dep_wired_with_env pins
    # OWN_URL/ALLOWED_CALLERS, and keeps the test honest regardless of
    # collection order.
    monkeypatch.setattr(reader_main, "GCP_PROJECT", "test-proj")
    monkeypatch.setattr(reader_main, "TARGET_SERVICE", "payment-demo")
    monkeypatch.setattr(reader_main, "TARGET_REGION", "asia-northeast1")
    # Default to "auth passed" — failure tests override this again.
    app.dependency_overrides[_verify_caller_dep] = (
        lambda: "coordinator@test-proj.iam.gserviceaccount.com"
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_read_empty_body_returns_env_and_revision(client):
    r = client.post("/read", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "payment-demo"
    assert body["region"] == "asia-northeast1"
    assert body["project"] == "test-proj"
    assert body["env"] == {"PAYMENT_MODE": "mock", "FEATURE_NEW_CHECKOUT": "false"}
    assert body["revision"] == "payment-demo-00001-abc"
    assert body["previous_revisions"] == ["payment-demo-00000-zzz"]


def test_read_passes_active_revision_to_previous_revisions_lookup(client, monkeypatch):
    """The revision picked by ``read_live_state`` must be excluded from the
    rollback-candidate list — pass it through as ``active`` rather than
    re-deriving it, so the two can never disagree."""
    seen = {}

    def fake_list_previous(s, r, p, active, **_):
        seen["active"] = active
        return []

    monkeypatch.setattr(reader_main, "list_previous_ready_revisions", fake_list_previous)
    r = client.post("/read", json={})
    assert r.status_code == 200
    assert seen["active"] == "payment-demo-00001-abc"


def test_read_previous_revisions_empty_when_none_available(client, monkeypatch):
    """No-previous-revisions case: a service with only one (the active)
    revision returns an empty list, not an error or a missing key."""
    monkeypatch.setattr(
        reader_main, "list_previous_ready_revisions", lambda s, r, p, active, **_: []
    )
    r = client.post("/read", json={})
    assert r.status_code == 200
    assert r.json()["previous_revisions"] == []


def test_read_degrades_to_empty_previous_revisions_when_listing_fails(client, monkeypatch):
    """Fail-soft: previous_revisions is a best-effort supplement — a transient
    Cloud Run API failure on the listing must NOT 500 the whole /read when the
    core live-state read already succeeded (env/revision stay intact)."""
    def boom(s, r, p, active, **_):
        raise gax.ServiceUnavailable("transient backend blip")

    monkeypatch.setattr(reader_main, "list_previous_ready_revisions", boom)
    r = client.post("/read", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["previous_revisions"] == []
    assert body["env"] == {"PAYMENT_MODE": "mock", "FEATURE_NEW_CHECKOUT": "false"}
    assert body["revision"] == "payment-demo-00001-abc"


def test_extra_fields_rejected(client):
    # Layer 2: payload-intent policy. The caller cannot supply
    # service/region/project — those are hardcoded at boot.
    r = client.post("/read", json={"service": "other-service"})
    assert 400 <= r.status_code < 500, (
        f"expected 4xx for extra fields, got {r.status_code}: {r.text}"
    )


def test_extra_fields_region_rejected(client):
    r = client.post("/read", json={"region": "us-central1"})
    assert 400 <= r.status_code < 500


def test_missing_bearer_returns_401(client):
    def deny_401():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
        )

    app.dependency_overrides[_verify_caller_dep] = deny_401
    r = client.post("/read", json={})
    assert r.status_code == 401


def test_wrong_audience_returns_401(client):
    def deny_audience():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
        )

    app.dependency_overrides[_verify_caller_dep] = deny_audience
    r = client.post("/read", json={})
    assert r.status_code == 401


def test_caller_not_in_allowlist_returns_403(client):
    def deny_caller():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="caller service account not allowed",
        )

    app.dependency_overrides[_verify_caller_dep] = deny_caller
    r = client.post("/read", json={})
    assert r.status_code == 403


def test_healthz_does_not_require_auth(client):
    # Even if the dependency would deny, /healthz has no Depends on it.
    def boom():
        raise HTTPException(status_code=401, detail="should not be called")

    app.dependency_overrides[_verify_caller_dep] = boom
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_real_verify_caller_dep_wired_with_env(monkeypatch):
    """Layer 3 integration check (Codex review #4): without dependency_overrides
    the real ``_verify_caller_dep`` must call ``verify_caller`` with the
    OWN_URL and ALLOWED_CALLERS read from env at boot. Stub the lib function
    and capture its kwargs so we don't need a real Google ID token.

    We monkeypatch the module-level constants rather than relying on the
    import-time env read because, in a unified pytest run, another worker's
    test module may have populated ``OWN_URL`` before this module was
    imported (Python caches the import; ``os.environ.setdefault`` at the top
    of this file would then be a no-op and the constant would carry the
    other worker's value). Forcing the value here keeps the test honest no
    matter what order pytest collects worker test modules.
    """
    seen = {}

    def fake_verify(request, *, own_url, allowed_callers):
        seen["own_url"] = own_url
        seen["allowed_callers"] = set(allowed_callers)
        return "coordinator@test-proj.iam.gserviceaccount.com"

    # Patch the symbol at its import site in workers.reader.main (same
    # rationale as the read_live_state patch above).
    monkeypatch.setattr(reader_main, "verify_caller", fake_verify)
    monkeypatch.setattr(reader_main, "_get_revisions_client", lambda: object())
    monkeypatch.setattr(
        reader_main,
        "list_previous_ready_revisions",
        lambda s, r, p, active, **_: [],
    )
    monkeypatch.setattr(
        reader_main,
        "read_live_state",
        lambda s, r, p, **_: {"env": {}, "revision": "rev-x"},
    )
    monkeypatch.setattr(reader_main, "OWN_URL", "https://reader.example.com")
    monkeypatch.setattr(
        reader_main,
        "ALLOWED_CALLERS",
        frozenset({"coordinator@test-proj.iam.gserviceaccount.com"}),
    )
    # No dependency_overrides — exercise the real _verify_caller_dep.
    c = TestClient(app)
    r = c.post("/read", json={}, headers={"Authorization": "Bearer faketoken"})
    assert r.status_code == 200
    assert seen["own_url"] == "https://reader.example.com"
    assert seen["allowed_callers"] == {
        "coordinator@test-proj.iam.gserviceaccount.com",
    }
