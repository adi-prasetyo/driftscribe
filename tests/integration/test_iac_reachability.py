"""Integration tests for the read-only GET /iac-apply/reachability (Phase C5c).

The reachability endpoint is the GO/NO-GO gate for the coordinator's Direct VPC
egress cutover. It fans :func:`worker_client.probe_worker_health` out across
EVERY configured worker (the 7 siblings + the tofu-apply worker) and returns a
single ``go`` verdict:

* ``worker_healthy`` — tofu_apply ``app_reached`` (its app answered 405/403, not
  a pre-app 404) — proves the internal-ingress mutator is reachable via the VPC.
* ``all_siblings_reachable`` — every non-tofu_apply worker ``app_reached`` (no
  regression from the run.app DNS rewrite).
* ``go = worker_healthy AND all_siblings_reachable``.

The probe GETs each worker's canonical POST path (``/healthz`` is GFE-reserved →
pre-app 404, useless for an internal service); the app's 405 is the proof.

Status codes: 200 when ``go``; 502 otherwise; 503 when ``TOFU_APPLY_URL`` is
unset (the new path cannot exist yet). Token-guarded like ``/recheck`` /
``/decisions`` — header only, never a query param. ``Cache-Control: no-store``.

This module keeps the REAL ``verify_token`` dependency wired (``no_auth_override``
marker), so the token-guard cases exercise the production auth path — every
OTHER integration module gets the conftest's autouse stub. The fan-out itself is
mocked at the ``probe_worker_health`` seam (``agent.main.worker_client``) so the
tests assert the endpoint's gate logic without standing up eight HTTP stubs.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent import worker_client
from agent.config import get_settings
from agent.main import app

pytestmark = pytest.mark.no_auth_override

_TOKEN = "static-server-token-c5c"


def _set_token(monkeypatch, value: str = _TOKEN) -> None:
    """Set DRIFTSCRIBE_TOKEN and bust the Settings cache (mirrors
    test_token_guard's helper — the autouse conftest does not touch it)."""
    monkeypatch.setenv("DRIFTSCRIBE_TOKEN", value)
    get_settings.cache_clear()


def _probe_result(
    worker: str,
    *,
    reachable: bool,
    status_code: int | None,
    error: str | None = None,
    target: str = "https://worker.example.com",
) -> dict:
    """Build a probe_worker_health-shaped result dict.

    ``app_reached`` mirrors the probe: reachable with a status NOT in
    {401,403,404} (404 = GFE/ingress pre-app reject; 401/403 = auth/IAM reject a
    real call would also hit; 405/200 = the app router answered)."""
    return {
        "worker": worker,
        "target": None if error == "url_unset" else target,
        "probed_path": worker_client.WORKER_ENDPOINTS.get(worker, "/x"),
        "reachable": reachable,
        "app_reached": bool(
            reachable
            and status_code is not None
            and status_code not in (401, 403, 404)
        ),
        "status_code": status_code,
        "latency_ms": None if not reachable else 12,
        "error": error,
    }


def _install_probe(monkeypatch, results_by_worker: dict[str, dict]) -> None:
    """Patch the fan-out seam so each worker key returns a canned result.

    Patches the name the route actually calls (``agent.main.worker_client``).
    Any worker not present in ``results_by_worker`` falls back to a healthy
    405 (GET on a POST canonical → app_reached) — so a test only has to specify
    the workers it wants to vary.
    """

    def fake_probe(worker: str, **kwargs) -> dict:
        if worker in results_by_worker:
            return results_by_worker[worker]
        return _probe_result(worker, reachable=True, status_code=405)

    monkeypatch.setattr(
        "agent.main.worker_client.probe_worker_health", fake_probe
    )


# --------------------------------------------------------------------------- #
# Auth: header-only token guard (real verify_token via no_auth_override).
# --------------------------------------------------------------------------- #


def test_missing_token_returns_401(monkeypatch):
    """No credential → 401 (the real verify_token's missing-header path)."""
    _set_token(monkeypatch)
    _install_probe(monkeypatch, {})
    client = TestClient(app)
    resp = client.get("/iac-apply/reachability")
    assert resp.status_code == 401
    assert "X-DriftScribe-Token" in resp.json()["detail"]


def test_wrong_token_returns_403(monkeypatch):
    """A present-but-wrong token → 403."""
    _set_token(monkeypatch)
    _install_probe(monkeypatch, {})
    client = TestClient(app)
    resp = client.get(
        "/iac-apply/reachability", headers={"X-DriftScribe-Token": "nope"}
    )
    assert resp.status_code == 403


def test_token_in_query_param_is_rejected(monkeypatch):
    """The token is accepted via header ONLY — a query-param token is ignored,
    so the request is treated as unauthenticated (401)."""
    _set_token(monkeypatch)
    _install_probe(monkeypatch, {})
    client = TestClient(app)
    resp = client.get(f"/iac-apply/reachability?x_driftscribe_token={_TOKEN}")
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# Gate logic (valid token).
# --------------------------------------------------------------------------- #


def test_all_workers_app_reached_returns_go_true(monkeypatch):
    """Valid token + every worker app_reached (405) → 200 with go=true,
    worker_healthy=true, and a per-worker results list covering all 8."""
    _set_token(monkeypatch)
    _install_probe(monkeypatch, {})  # all default to app_reached 405
    client = TestClient(app)
    resp = client.get(
        "/iac-apply/reachability", headers={"X-DriftScribe-Token": _TOKEN}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["go"] is True
    assert body["worker_healthy"] is True
    assert body["all_siblings_reachable"] is True
    workers = {r["worker"] for r in body["results"]}
    assert workers == set(worker_client._WORKER_URL_ENV)
    assert "tofu_apply" in workers


def test_one_sibling_unreachable_returns_502_go_false(monkeypatch):
    """One sibling with a transport error → all_siblings_reachable false →
    go=false → 502. worker_healthy can still be true (tofu_apply is fine)."""
    _set_token(monkeypatch)
    _install_probe(
        monkeypatch,
        {
            "reader": _probe_result(
                "reader",
                reachable=False,
                status_code=None,
                error="ConnectError: connection refused",
            )
        },
    )
    client = TestClient(app)
    resp = client.get(
        "/iac-apply/reachability", headers={"X-DriftScribe-Token": _TOKEN}
    )
    assert resp.status_code == 502
    body = resp.json()
    assert body["go"] is False
    assert body["all_siblings_reachable"] is False
    # The new path itself is healthy — the regression is in a sibling.
    assert body["worker_healthy"] is True


def test_tofu_apply_404_returns_502_worker_unhealthy(monkeypatch):
    """tofu_apply reachable but 404 (pre-app: GFE-reserved path OR an ingress
    rejection — the request never reached the worker process) → app_reached
    false → worker_healthy false → go=false → 502. The siblings are all fine,
    proving the 404 is what fails the gate, not a sibling. (A 403 would mean the
    request DID reach past ingress to IAM, so 404 — not 403 — is 'not reached'.)"""
    _set_token(monkeypatch)
    _install_probe(
        monkeypatch,
        {
            "tofu_apply": _probe_result(
                "tofu_apply", reachable=True, status_code=404
            )
        },
    )
    client = TestClient(app)
    resp = client.get(
        "/iac-apply/reachability", headers={"X-DriftScribe-Token": _TOKEN}
    )
    assert resp.status_code == 502
    body = resp.json()
    assert body["go"] is False
    assert body["worker_healthy"] is False


def test_tofu_apply_403_returns_502_worker_unhealthy(monkeypatch):
    """tofu_apply reachable but 403 (auth/IAM reject — Cloud Run IAM before the
    container OR the app's verify_caller). A real /propose|/apply would hit the
    SAME rejection, so 403 is NOT a green cutover signal: app_reached false →
    worker_healthy false → go=false → 502. 405 — not merely non-404 — is the bar
    (Codex C5c review)."""
    _set_token(monkeypatch)
    _install_probe(
        monkeypatch,
        {
            "tofu_apply": _probe_result(
                "tofu_apply", reachable=True, status_code=403
            )
        },
    )
    client = TestClient(app)
    resp = client.get(
        "/iac-apply/reachability", headers={"X-DriftScribe-Token": _TOKEN}
    )
    assert resp.status_code == 502
    body = resp.json()
    assert body["go"] is False
    assert body["worker_healthy"] is False


def test_tofu_apply_url_unset_returns_503_with_results(monkeypatch):
    """tofu_apply URL unset (probe returns error 'url_unset') → 503, body
    carries the detail AND the full results list for diagnosis."""
    _set_token(monkeypatch)
    _install_probe(
        monkeypatch,
        {
            "tofu_apply": _probe_result(
                "tofu_apply",
                reachable=False,
                status_code=None,
                error="url_unset",
            )
        },
    )
    client = TestClient(app)
    resp = client.get(
        "/iac-apply/reachability", headers={"X-DriftScribe-Token": _TOKEN}
    )
    assert resp.status_code == 503
    body = resp.json()
    assert body["go"] is False
    assert "TOFU_APPLY_URL not configured" in body["detail"]
    # results MUST be present so the operator can diagnose the rest in one call.
    assert "results" in body
    workers = {r["worker"] for r in body["results"]}
    assert workers == set(worker_client._WORKER_URL_ENV)


def test_cache_control_no_store_on_all_paths(monkeypatch):
    """Every response (200 go, 502 no-go, 503 unset) carries Cache-Control:
    no-store — a stale cached verdict during a cutover would mislead."""
    _set_token(monkeypatch)

    # 200 (go)
    _install_probe(monkeypatch, {})
    client = TestClient(app)
    ok = client.get(
        "/iac-apply/reachability", headers={"X-DriftScribe-Token": _TOKEN}
    )
    assert ok.status_code == 200
    assert ok.headers.get("Cache-Control") == "no-store"

    # 502 (no-go)
    _install_probe(
        monkeypatch,
        {"reader": _probe_result("reader", reachable=False, status_code=None,
                                 error="ConnectError: x")},
    )
    nogo = client.get(
        "/iac-apply/reachability", headers={"X-DriftScribe-Token": _TOKEN}
    )
    assert nogo.status_code == 502
    assert nogo.headers.get("Cache-Control") == "no-store"

    # 503 (tofu_apply url unset)
    _install_probe(
        monkeypatch,
        {"tofu_apply": _probe_result("tofu_apply", reachable=False,
                                     status_code=None, error="url_unset")},
    )
    unset = client.get(
        "/iac-apply/reachability", headers={"X-DriftScribe-Token": _TOKEN}
    )
    assert unset.status_code == 503
    assert unset.headers.get("Cache-Control") == "no-store"


def test_endpoint_probes_every_configured_worker(monkeypatch):
    """The endpoint iterates the source-of-truth worker set (no hardcoded
    second copy): probe_worker_health is called once per key in
    _WORKER_URL_ENV, including tofu_apply."""
    _set_token(monkeypatch)
    called: list[str] = []

    def recording_probe(worker: str, **kwargs) -> dict:
        called.append(worker)
        return _probe_result(worker, reachable=True, status_code=200)

    monkeypatch.setattr(
        "agent.main.worker_client.probe_worker_health", recording_probe
    )
    client = TestClient(app)
    resp = client.get(
        "/iac-apply/reachability", headers={"X-DriftScribe-Token": _TOKEN}
    )
    assert resp.status_code == 200
    assert set(called) == set(worker_client._WORKER_URL_ENV)
    assert len(called) == len(worker_client._WORKER_URL_ENV)
