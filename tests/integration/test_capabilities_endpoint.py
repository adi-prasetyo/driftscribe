"""Integration tests for ``GET /capabilities`` (Task 3 — safety-cage DTO).

The route serializes the agent's safety cage from ``agent.capabilities``
(the same constants the enforcement code imports). Pins:

* token guard — same semantics as ``/infra/graph`` / ``/decisions``;
* ``Cache-Control: no-store`` on success;
* DTO shape: version 1, four workload names;
* method-bearing gate pin — the IaC-apply gate is the POST (not the GET
  form page), and the POST carries the ``require_cf_operator`` guard.

Test conventions mirror ``tests/integration/test_infra_graph_endpoint.py``
exactly: ``@pytest.mark.no_auth_override`` for real-auth tests,
``monkeypatch.setenv("DRIFTSCRIBE_TOKEN", …)`` + ``get_settings.cache_clear()``,
default-client uses the autouse ``dependency_overrides[verify_token]`` bypass.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent.config import get_settings
from agent.main import app


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_capabilities_ok():
    """Default auth-overridden client → 200 + no-store + DTO shape."""
    client = TestClient(app)
    r = client.get("/capabilities")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-store"
    dto = r.json()
    assert dto["version"] == 1
    assert {w["name"] for w in dto["workloads"]} == {
        "drift", "upgrade", "explore", "provision"
    }


# --------------------------------------------------------------------------- #
# Token guard (real verify_token via no_auth_override)
# --------------------------------------------------------------------------- #


@pytest.mark.no_auth_override
class TestCapabilitiesTokenGuard:
    def _set_token(self, monkeypatch, value: str) -> None:
        monkeypatch.setenv("DRIFTSCRIBE_TOKEN", value)
        get_settings.cache_clear()

    def test_without_token_returns_401(self, monkeypatch):
        self._set_token(monkeypatch, "tok-capabilities-123")
        client = TestClient(app)
        assert client.get("/capabilities").status_code == 401

    def test_wrong_token_returns_403(self, monkeypatch):
        self._set_token(monkeypatch, "tok-capabilities-123")
        client = TestClient(app)
        resp = client.get(
            "/capabilities",
            headers={"X-DriftScribe-Token": "wrong-token"},
        )
        assert resp.status_code == 403

    def test_correct_token_succeeds(self, monkeypatch):
        self._set_token(monkeypatch, "tok-capabilities-123")
        client = TestClient(app)
        resp = client.get(
            "/capabilities",
            headers={"X-DriftScribe-Token": "tok-capabilities-123"},
        )
        assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Anti-drift: gate routes exist with the right method and guard
# --------------------------------------------------------------------------- #


def test_gate_routes_exist_with_method_and_guard():
    """Method-bearing gate pin (Codex must-fix #4).

    A path-only pin would pass even if the gated POST disappeared and only
    the unauthenticated GET form page remained. We assert on (path, method)
    pairs and additionally verify that the IaC-apply POST carries the
    ``require_cf_operator`` guard (parameter-level Depends — FastAPI
    populates ``dependant.dependencies`` for those too, as verified during
    implementation).
    """
    from fastapi.routing import APIRoute

    from agent.capabilities import HUMAN_GATES

    routes = {
        (r.path, m): r
        for r in app.routes
        if isinstance(r, APIRoute)
        for m in r.methods
    }
    for gate in HUMAN_GATES:
        assert (gate["route"], gate["method"]) in routes, (
            f"Gate '{gate['id']}' route {gate['method']} {gate['route']} "
            f"not found in app routes"
        )

    # The IaC-apply POST must carry the operator-identity guard:
    iac_post = routes[("/iac-approvals/{pr_number}", "POST")]
    dep_names = {d.call.__name__ for d in iac_post.dependant.dependencies}
    assert "require_cf_operator" in dep_names, (
        f"require_cf_operator not found in POST /iac-approvals/{{pr_number}} "
        f"dependencies; found: {dep_names}"
    )
