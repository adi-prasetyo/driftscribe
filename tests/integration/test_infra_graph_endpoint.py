"""Integration tests for ``GET /infra/graph`` (Phase 1 — Infrastructure panel).

Backs the operator UI's resource-map panel. The route proxies the read-only
``infra_reader`` worker and reshapes its inventory via ``build_graph``. Pins:

* token guard (header-only) — same as ``/decisions`` / ``/trace``;
* ``Cache-Control: no-store`` on every path;
* the worker's CAI soft-fail (``cloud_asset_unavailable`` at 200) → degraded DTO;
* a real worker transport/config failure (``WorkerClientError``) → soft-failed to
  a 200 degraded DTO (the panel never hard-errors);
* secret redaction — a planted secret name never reaches the payload.

The worker call is mocked at the ``agent.main.worker_client.call`` seam (mirrors
test_iac_reachability's probe mock) so these assert the route's transform + error
mapping without standing up the infra_reader HTTP stub.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent import worker_client
from agent.config import get_settings
from agent.main import app

RUN_TYPE = "run.googleapis.com/Service"
SECRET_TYPE = "secretmanager.googleapis.com/Secret"


def _inventory() -> dict:
    return {
        "project": "test-proj",
        "generated_at": "2026-06-03T00:00:00+00:00",
        "freshness_caveat": "CAI is eventually consistent…",
        "iac_snapshot_sha": "cafef00d",
        "total_resources": 2,
        "declared_in_iac": 1,
        "not_in_iac": 1,
        "by_type": {
            RUN_TYPE: {
                "count": 2, "declared_in_iac": 1, "not_in_iac": 1, "sensitive": False,
                "sample": [
                    {"name": "payment-demo", "location": "asia-northeast1", "iac": True, "match_confidence": "high"},
                    {"name": "storefront", "location": "asia-northeast1", "iac": False, "match_confidence": None},
                ],
            },
            SECRET_TYPE: {
                "count": 1, "declared_in_iac": 0, "not_in_iac": 1, "sensitive": True,
            },
        },
        "declared_not_found": [],
        "truncated": {"per_type_sample": 10},
    }


def _mock_call(monkeypatch, *, returns=None, raises=None) -> None:
    def fake_call(worker: str, payload: dict, **kwargs):
        assert worker == "infra_reader"
        assert payload == {}
        if raises is not None:
            raise raises
        return returns

    monkeypatch.setattr("agent.main.worker_client.call", fake_call)


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_returns_graph_dto_with_no_store(monkeypatch):
    _mock_call(monkeypatch, returns=_inventory())
    client = TestClient(app)
    resp = client.get("/infra/graph")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"
    body = resp.json()
    assert body["degraded"] is False
    assert body["edges"] == []
    assert body["totals"] == {"resources": 2, "managed": 1, "drift": 1}
    atypes = {g["asset_type"] for g in body["groups"]}
    assert atypes == {RUN_TYPE, SECRET_TYPE}


def test_secret_group_counts_only_no_names(monkeypatch):
    _mock_call(monkeypatch, returns=_inventory())
    client = TestClient(app)
    body = client.get("/infra/graph").json()
    secret = next(g for g in body["groups"] if g["asset_type"] == SECRET_TYPE)
    assert secret["sensitive"] is True
    assert secret["nodes"] == []
    assert secret["count"] == 1


def test_planted_secret_name_never_reaches_payload(monkeypatch):
    """Mirrors decision.test.ts's safety test: even if the worker (wrongly)
    sampled a secret, the route must not surface its name."""
    inv = _inventory()
    inv["by_type"][SECRET_TYPE] = {
        "count": 1, "declared_in_iac": 0, "not_in_iac": 1,
        # planted name + missing sensitive flag — build_graph must still drop it
        "sample": [{"name": "ghp_PLANTED_secret", "location": "g", "iac": False, "match_confidence": None}],
    }
    _mock_call(monkeypatch, returns=inv)
    client = TestClient(app)
    resp = client.get("/infra/graph")
    assert "ghp_PLANTED_secret" not in resp.text


# --------------------------------------------------------------------------- #
# Degradation (always soft-fail to 200)
# --------------------------------------------------------------------------- #


def test_cloud_asset_unavailable_soft_fails_to_degraded_200(monkeypatch):
    _mock_call(
        monkeypatch,
        returns={"error": "cloud_asset_unavailable", "detail": "perm denied", "project": "test-proj"},
    )
    client = TestClient(app)
    resp = client.get("/infra/graph")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"
    body = resp.json()
    assert body["degraded"] is True
    assert body["degraded_reason"] == "cloud_asset_unavailable"
    assert body["groups"] == []


def test_worker_unreachable_soft_fails_to_degraded_200(monkeypatch):
    _mock_call(
        monkeypatch,
        raises=worker_client.WorkerClientError(503, "infra_reader unreachable", "infra_reader"),
    )
    client = TestClient(app)
    resp = client.get("/infra/graph")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"
    body = resp.json()
    assert body["degraded"] is True
    assert body["degraded_reason"] == "infra_reader_unavailable"
    assert "503" in (body.get("detail") or "")


# --------------------------------------------------------------------------- #
# Token guard (real verify_token via no_auth_override)
# --------------------------------------------------------------------------- #


@pytest.mark.no_auth_override
class TestInfraGraphTokenGuard:
    def _set_token(self, monkeypatch, value: str) -> None:
        monkeypatch.setenv("DRIFTSCRIBE_TOKEN", value)
        get_settings.cache_clear()

    def test_without_token_returns_401(self, monkeypatch):
        self._set_token(monkeypatch, "tok-123")
        _mock_call(monkeypatch, returns=_inventory())
        client = TestClient(app)
        assert client.get("/infra/graph").status_code == 401

    def test_wrong_token_returns_403(self, monkeypatch):
        self._set_token(monkeypatch, "tok-123")
        _mock_call(monkeypatch, returns=_inventory())
        client = TestClient(app)
        resp = client.get("/infra/graph", headers={"X-DriftScribe-Token": "nope"})
        assert resp.status_code == 403

    def test_correct_token_succeeds(self, monkeypatch):
        self._set_token(monkeypatch, "tok-123")
        _mock_call(monkeypatch, returns=_inventory())
        client = TestClient(app)
        resp = client.get("/infra/graph", headers={"X-DriftScribe-Token": "tok-123"})
        assert resp.status_code == 200
