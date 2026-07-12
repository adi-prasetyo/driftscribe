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

# NOTE: the module-level /infra/graph inventory cache is reset around every
# integration test by the autouse fixture in conftest.py (alongside the trace /
# state / workload resets), so the default 60s TTL can't leak a cached success
# from one case into the next.


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
# Version-skew canary: a pre-#193 worker omits not_in_iac_control_plane, so the
# actionable-drift badge silently over-reports. The route logs a WARNING when a
# fresh inventory shows that skew, so the half-deploy is visible server-side.
# --------------------------------------------------------------------------- #


def test_missing_control_plane_count_on_adoptable_drift_logs_warning(monkeypatch, caplog):
    # _inventory()'s Cloud Run entry has not_in_iac=1 but NO
    # not_in_iac_control_plane key — the exact pre-#193 worker shape.
    _mock_call(monkeypatch, returns=_inventory())
    client = TestClient(app)
    with caplog.at_level("WARNING"):
        assert client.get("/infra/graph").status_code == 200
    recs = [r for r in caplog.records
            if r.msg == "infra_graph_inventory_missing_control_plane_count"]
    assert len(recs) == 1
    assert RUN_TYPE in recs[0].stale_asset_types


def test_control_plane_count_present_logs_no_warning(monkeypatch, caplog):
    inv = _inventory()
    # A current worker emits the key (0 = "no control-plane drift", legitimate).
    inv["by_type"][RUN_TYPE]["not_in_iac_control_plane"] = 0
    _mock_call(monkeypatch, returns=inv)
    client = TestClient(app)
    with caplog.at_level("WARNING"):
        assert client.get("/infra/graph").status_code == 200
    assert not [r for r in caplog.records
                if r.msg == "infra_graph_inventory_missing_control_plane_count"]


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
# In-process TTL cache (perf: a live CAI enumeration takes ~25-35s, and the
# panel re-fetches on every page load — without a cache every load spins for
# half a minute). Successful inventories are cached for INFRA_GRAPH_CACHE_TTL_S.
# --------------------------------------------------------------------------- #


def _counting_call(monkeypatch, *, returns=None, raises=None) -> dict:
    """Like _mock_call but records how many times the worker was invoked, so a
    cache hit (worker NOT called) is observable. Returns the shared counter."""
    state = {"n": 0}

    def fake_call(worker: str, payload: dict, **kwargs):
        assert worker == "infra_reader"
        assert payload == {}
        state["n"] += 1
        if raises is not None:
            raise raises
        return returns

    monkeypatch.setattr("agent.main.worker_client.call", fake_call)
    return state


def _set_ttl(monkeypatch, value: str) -> None:
    monkeypatch.setenv("INFRA_GRAPH_CACHE_TTL_S", value)
    get_settings.cache_clear()


def test_successful_inventory_cached_within_ttl(monkeypatch):
    """A second request inside the TTL is served from cache — the expensive
    worker call happens exactly once, and the response advertises the cache."""
    calls = _counting_call(monkeypatch, returns=_inventory())
    client = TestClient(app)
    r1 = client.get("/infra/graph")
    r2 = client.get("/infra/graph")
    assert r1.status_code == 200 and r2.status_code == 200
    assert calls["n"] == 1, "second request must be served from cache"
    assert r1.headers.get("x-infra-graph-cache") == "miss"
    assert r2.headers.get("x-infra-graph-cache") == "hit"
    # The cached body is byte-for-byte the freshly-built DTO.
    assert r1.json() == r2.json()
    assert r2.headers.get("cache-control") == "no-store"


def test_cache_served_at_ttl_boundary(monkeypatch):
    """At exactly age == ttl the entry is still fresh (``age <= ttl``)."""
    calls = _counting_call(monkeypatch, returns=_inventory())
    clock = {"t": 500.0}
    monkeypatch.setattr("agent.main.time.monotonic", lambda: clock["t"])
    _set_ttl(monkeypatch, "30")
    client = TestClient(app)
    client.get("/infra/graph")
    clock["t"] = 500.0 + 30.0  # exactly on the boundary
    r = client.get("/infra/graph")
    assert calls["n"] == 1
    assert r.headers.get("x-infra-graph-cache") == "hit"


def test_cache_expires_just_past_ttl(monkeypatch):
    """Just past the TTL the inventory is re-fetched (no stale serving)."""
    calls = _counting_call(monkeypatch, returns=_inventory())
    clock = {"t": 1000.0}
    monkeypatch.setattr("agent.main.time.monotonic", lambda: clock["t"])
    _set_ttl(monkeypatch, "30")
    client = TestClient(app)
    client.get("/infra/graph")  # cached at t=1000
    clock["t"] = 1000.0 + 30.0 + 0.1  # just past the TTL
    r = client.get("/infra/graph")
    assert calls["n"] == 2
    assert r.headers.get("x-infra-graph-cache") == "miss"


def test_degraded_inventory_not_cached(monkeypatch):
    """A CAI soft-fail (``error`` inventory) is never cached — the next request
    retries the worker instead of being pinned to the degraded result."""
    calls = _counting_call(
        monkeypatch,
        returns={"error": "cloud_asset_unavailable", "detail": "x", "project": "p"},
    )
    client = TestClient(app)
    r1 = client.get("/infra/graph")
    r2 = client.get("/infra/graph")
    assert calls["n"] == 2
    assert r1.json()["degraded"] is True
    assert r1.headers.get("x-infra-graph-cache") == "miss"
    assert r2.headers.get("x-infra-graph-cache") == "miss"


def test_worker_error_not_cached(monkeypatch):
    """A ``WorkerClientError`` likewise isn't cached — the next request retries."""
    calls = _counting_call(
        monkeypatch,
        raises=worker_client.WorkerClientError(503, "down", "infra_reader"),
    )
    client = TestClient(app)
    client.get("/infra/graph")
    client.get("/infra/graph")
    assert calls["n"] == 2


def test_ttl_zero_disables_cache(monkeypatch):
    """``INFRA_GRAPH_CACHE_TTL_S <= 0`` disables caching entirely (read + write)."""
    calls = _counting_call(monkeypatch, returns=_inventory())
    _set_ttl(monkeypatch, "0")
    client = TestClient(app)
    r1 = client.get("/infra/graph")
    r2 = client.get("/infra/graph")
    assert calls["n"] == 2
    assert r1.headers.get("x-infra-graph-cache") == "disabled"
    assert r2.headers.get("x-infra-graph-cache") == "disabled"


def test_non_finite_ttl_falls_back_to_default(monkeypatch):
    """A nan TTL would poison the expiry comparison (``age > nan`` is always
    False → the cache would never expire); the validator coerces it back to the
    60s default so the cache stays bounded."""
    _set_ttl(monkeypatch, "nan")
    assert get_settings().infra_graph_cache_ttl_s == 60.0


def test_l2_cache_ttl_default_is_900s(monkeypatch):
    """L2 (Firestore) default is longer than L1 so a freshly-recycled instance
    serves a warm map for the typical idle/cold-start window. (The autouse
    fixture pins the env var to 0 for the tier-1 tests, so observe the field's
    intrinsic default by removing it.)"""
    monkeypatch.delenv("INFRA_GRAPH_L2_CACHE_TTL_S", raising=False)
    get_settings.cache_clear()
    assert get_settings().infra_graph_l2_cache_ttl_s == 900.0


def test_non_finite_l2_ttl_falls_back_to_default(monkeypatch):
    """Same non-finite footgun as L1: a nan L2 TTL would never expire (``age >
    nan`` is always False), pinning a stale Firestore doc forever. The validator
    coerces it back to the 900s default."""
    monkeypatch.setenv("INFRA_GRAPH_L2_CACHE_TTL_S", "inf")
    get_settings.cache_clear()
    assert get_settings().infra_graph_l2_cache_ttl_s == 900.0


# --------------------------------------------------------------------------- #
# L2 (Firestore) cache layer. The coordinator runs --min-instances=0, so the
# in-process L1 dies on every scale-to-zero recycle; L2 persists the inventory
# to Firestore so a fresh instance serves a warm map. The read chain is
# L1 (monotonic) -> L2 (wall-clock) -> live. L2 is DISABLED by default in the
# autouse conftest fixture (INFRA_GRAPH_L2_CACHE_TTL_S=0) so the tier-1 tests
# above are unaffected; these opt in via _set_l2_ttl + an injected store.
# --------------------------------------------------------------------------- #

from agent.infra_graph_cache_store import (  # noqa: E402
    FirestoreInfraGraphCacheStore,
    InMemoryInfraGraphCacheStore,
)
from agent.main import (  # noqa: E402
    _INFRA_GRAPH_L2_FORMAT_VERSION,
    _set_infra_graph_cache_store_for_tests,
)


def _set_l2_ttl(monkeypatch, value: str) -> None:
    monkeypatch.setenv("INFRA_GRAPH_L2_CACHE_TTL_S", value)
    get_settings.cache_clear()


def _inject_l2(store=None):
    """Plug an in-process L2 store via the module injection seam so tests never
    construct a real Firestore client. Returns the store for assertions."""
    store = store or InMemoryInfraGraphCacheStore()
    _set_infra_graph_cache_store_for_tests(store)
    return store


def test_l2_hit_when_l1_disabled(monkeypatch):
    """With L1 off and L2 on, the second request is served from the Firestore
    layer — worker called once, ``hit-l2`` header, age advertised."""
    calls = _counting_call(monkeypatch, returns=_inventory())
    _set_ttl(monkeypatch, "0")  # L1 disabled
    _set_l2_ttl(monkeypatch, "900")
    _inject_l2()
    client = TestClient(app)
    r1 = client.get("/infra/graph")
    r2 = client.get("/infra/graph")
    assert calls["n"] == 1, "second request must be served from L2"
    assert r1.headers.get("x-infra-graph-cache") == "miss"
    assert r2.headers.get("x-infra-graph-cache") == "hit-l2"
    assert r2.headers.get("x-infra-graph-cache-age-s") is not None
    assert r1.json() == r2.json()
    assert r2.headers.get("cache-control") == "no-store"


def test_l1_takes_precedence_over_l2(monkeypatch):
    """When both layers are warm, L1 wins (no Firestore round-trip) — the header
    reads ``hit``, not ``hit-l2``."""
    calls = _counting_call(monkeypatch, returns=_inventory())
    _set_l2_ttl(monkeypatch, "900")  # L1 stays at its 60s default
    store = _inject_l2()
    client = TestClient(app)
    client.get("/infra/graph")
    r2 = client.get("/infra/graph")
    assert calls["n"] == 1
    assert r2.headers.get("x-infra-graph-cache") == "hit"
    # The live fetch must have written L2 too (not just L1) — otherwise a later
    # cold instance couldn't serve from Firestore.
    assert store.get() is not None


def test_l2_served_at_ttl_boundary(monkeypatch):
    calls = _counting_call(monkeypatch, returns=_inventory())
    clock = {"t": 500.0}
    monkeypatch.setattr("agent.main.time.time", lambda: clock["t"])
    _set_ttl(monkeypatch, "0")  # L1 off so L2 is exercised
    _set_l2_ttl(monkeypatch, "30")
    _inject_l2()
    client = TestClient(app)
    client.get("/infra/graph")  # written_at = 500
    clock["t"] = 500.0 + 30.0  # exactly on the boundary (age == ttl → fresh)
    r = client.get("/infra/graph")
    assert calls["n"] == 1
    assert r.headers.get("x-infra-graph-cache") == "hit-l2"


def test_l2_expires_just_past_ttl(monkeypatch):
    calls = _counting_call(monkeypatch, returns=_inventory())
    clock = {"t": 1000.0}
    monkeypatch.setattr("agent.main.time.time", lambda: clock["t"])
    _set_ttl(monkeypatch, "0")
    _set_l2_ttl(monkeypatch, "30")
    _inject_l2()
    client = TestClient(app)
    client.get("/infra/graph")  # written_at = 1000
    clock["t"] = 1000.0 + 30.0 + 0.1  # just past the TTL
    r = client.get("/infra/graph")
    assert calls["n"] == 2
    assert r.headers.get("x-infra-graph-cache") == "miss"


def test_l2_future_written_at_treated_as_miss(monkeypatch):
    """A doc with a written_at far in the future (clock skew / hand-edit) must
    not be served as a stale hit forever — it's distrusted as a miss."""
    clock = {"t": 1000.0}
    monkeypatch.setattr("agent.main.time.time", lambda: clock["t"])
    _set_ttl(monkeypatch, "0")
    _set_l2_ttl(monkeypatch, "900")
    store = _inject_l2()
    # Plant a record stamped 10 minutes in the future.
    store.set({"format_version": _INFRA_GRAPH_L2_FORMAT_VERSION, "written_at": 1600.0, "payload": _inventory()})
    calls = _counting_call(monkeypatch, returns=_inventory())
    client = TestClient(app)
    r = client.get("/infra/graph")
    assert calls["n"] == 1, "future-stamped doc must be refetched, not served"
    assert r.headers.get("x-infra-graph-cache") == "miss"


def test_l2_format_version_mismatch_is_miss(monkeypatch):
    """A doc written by an older deploy with a different payload contract is
    ignored (format_version gate), so a deploy can't serve a stale-shaped doc."""
    monkeypatch.setattr("agent.main.time.time", lambda: 1000.0)
    _set_ttl(monkeypatch, "0")
    _set_l2_ttl(monkeypatch, "900")
    store = _inject_l2()
    store.set({"format_version": 999, "written_at": 1000.0, "payload": _inventory()})
    calls = _counting_call(monkeypatch, returns=_inventory())
    client = TestClient(app)
    r = client.get("/infra/graph")
    assert calls["n"] == 1
    assert r.headers.get("x-infra-graph-cache") == "miss"


def test_l2_error_payload_treated_as_miss(monkeypatch):
    """A record whose payload carries an ``error`` key is rejected on read
    (defense against a regression that persisted a degraded inventory) — the
    request re-fetches rather than serving the error."""
    monkeypatch.setattr("agent.main.time.time", lambda: 1000.0)
    _set_ttl(monkeypatch, "0")
    _set_l2_ttl(monkeypatch, "900")
    store = _inject_l2()
    store.set(
        {"format_version": _INFRA_GRAPH_L2_FORMAT_VERSION, "written_at": 1000.0,
         "payload": {"error": "cloud_asset_unavailable"}}
    )
    calls = _counting_call(monkeypatch, returns=_inventory())
    client = TestClient(app)
    r = client.get("/infra/graph")
    assert calls["n"] == 1
    assert r.headers.get("x-infra-graph-cache") == "miss"


def test_l2_hit_promotes_into_l1(monkeypatch):
    """Read-through: an L2 hit warms L1 so the next request serves from memory
    (header flips hit-l2 → hit) without re-reading Firestore."""
    monkeypatch.setattr("agent.main.time.time", lambda: 1000.0)
    _set_l2_ttl(monkeypatch, "900")  # L1 stays at its 60s default (enabled)
    store = _inject_l2()
    store.set({"format_version": _INFRA_GRAPH_L2_FORMAT_VERSION, "written_at": 1000.0, "payload": _inventory()})
    calls = _counting_call(monkeypatch, returns=_inventory())
    client = TestClient(app)
    r1 = client.get("/infra/graph")
    r2 = client.get("/infra/graph")
    assert r1.headers.get("x-infra-graph-cache") == "hit-l2"
    assert r2.headers.get("x-infra-graph-cache") == "hit"  # promoted into L1
    assert calls["n"] == 0  # never touched the live worker


def test_l2_store_read_error_falls_through_to_live(monkeypatch):
    """A store whose get() RAISES must not 500 the endpoint — _read_l2_cache
    treats it as a miss and the request falls through to a live fetch
    (request-handler fail-soft holds for ANY store impl)."""

    class _BoomStore:
        def get(self):
            raise RuntimeError("firestore down")

        def set(self, record):
            return False

    _set_ttl(monkeypatch, "0")
    _set_l2_ttl(monkeypatch, "900")
    _inject_l2(_BoomStore())
    calls = _counting_call(monkeypatch, returns=_inventory())
    client = TestClient(app)
    r = client.get("/infra/graph")
    assert r.status_code == 200
    assert calls["n"] == 1
    assert r.headers.get("x-infra-graph-cache") == "miss"
    assert r.json()["degraded"] is False


def test_degraded_inventory_not_written_to_l2(monkeypatch):
    calls = _counting_call(
        monkeypatch,
        returns={"error": "cloud_asset_unavailable", "detail": "x", "project": "p"},
    )
    _set_ttl(monkeypatch, "0")
    _set_l2_ttl(monkeypatch, "900")
    store = _inject_l2()
    client = TestClient(app)
    client.get("/infra/graph")
    client.get("/infra/graph")
    assert calls["n"] == 2, "a degraded inventory must never be cached in L2"
    assert store.get() is None


def test_worker_error_not_written_to_l2(monkeypatch):
    calls = _counting_call(
        monkeypatch,
        raises=worker_client.WorkerClientError(503, "down", "infra_reader"),
    )
    _set_ttl(monkeypatch, "0")
    _set_l2_ttl(monkeypatch, "900")
    store = _inject_l2()
    client = TestClient(app)
    client.get("/infra/graph")
    client.get("/infra/graph")
    assert calls["n"] == 2
    assert store.get() is None


def test_both_layers_disabled_emits_disabled(monkeypatch):
    calls = _counting_call(monkeypatch, returns=_inventory())
    _set_ttl(monkeypatch, "0")
    _set_l2_ttl(monkeypatch, "0")
    _inject_l2()
    client = TestClient(app)
    r1 = client.get("/infra/graph")
    r2 = client.get("/infra/graph")
    assert calls["n"] == 2
    assert r1.headers.get("x-infra-graph-cache") == "disabled"
    assert r2.headers.get("x-infra-graph-cache") == "disabled"


def test_l2_retains_safe_unmatched_projection_but_drops_raw_paths(monkeypatch):
    """The persisted L2 record DROPS raw ``declared_not_found`` (full canonical
    paths + sensitive identities) but DELIBERATELY RETAINS the bounded,
    redaction-safe ``unmatched_iac`` projection that feeds the operator's
    unmatched-declarations band.

    This intentionally REVISES the earlier privacy stance for NON-sensitive stale
    resource NAMES (the old test asserted the name must never reach the cache):
    the panel already exposes live names of these same asset types, and this
    feature requires exposing the unmatched short name. Full canonical paths and
    every sensitive type remain excluded. See plan
    2026-07-11-unmatched-iac-declarations.
    """
    import json
    bucket_type = "storage.googleapis.com/Bucket"
    inv = _inventory()
    # Raw diagnostic: a non-sensitive bucket (full canonical path) + a redacted
    # secret. Only the bucket, SHORT-named, may survive into the cache.
    inv["declared_not_found"] = [
        {"address": "google_storage_bucket.bucket_a",
         "identity": "projects/p/buckets/super-secret-internal-bucket",
         "asset_type": bucket_type, "source": "iac", "confidence": "high",
         "possible_causes": ["cai_lag", "not_yet_applied", "format_mismatch"]},
        {"address": "google_secret_manager_secret.api_key",
         "asset_type": SECRET_TYPE, "source": "iac", "confidence": "high",
         "identity_redacted": True,
         "possible_causes": ["cai_lag", "not_yet_applied", "format_mismatch"]},
    ]
    # The worker's safe projection (short name only, no canonical path).
    inv["unmatched_iac"] = {
        "count": 1,
        "entries": [{"asset_type": bucket_type,
                     "name": "super-secret-internal-bucket",
                     "address": "google_storage_bucket.bucket_a"}],
        "truncated": 0,
    }
    calls = _counting_call(monkeypatch, returns=inv)
    _set_ttl(monkeypatch, "0")
    _set_l2_ttl(monkeypatch, "900")
    store = _inject_l2()
    client = TestClient(app)
    r1 = client.get("/infra/graph")
    record = store.get()
    assert record is not None
    blob = json.dumps(record)

    # 1. Raw declared_not_found and the full canonical path are gone from the cache.
    assert "declared_not_found" not in record["payload"]
    assert "projects/p/buckets/super-secret-internal-bucket" not in blob
    # 2. The safe projection survives — asset_type + short name + address only.
    proj = record["payload"]["unmatched_iac"]
    assert proj == {
        "count": 1,
        "entries": [{"asset_type": bucket_type,
                     "name": "super-secret-internal-bucket",
                     "address": "google_storage_bucket.bucket_a"}],
        "truncated": 0,
    }
    assert set(proj["entries"][0]) == {"asset_type", "name", "address"}
    # 3. No secret-type raw identity in the cache OR the served DTO.
    assert "api_key" not in blob and "api-key" not in blob
    assert "api_key" not in r1.text and "api-key" not in r1.text
    # The served band shows the SHORT name but NEVER the canonical path.
    assert "super-secret-internal-bucket" in r1.text
    assert "projects/p/buckets/" not in r1.text
    # Positive: legitimate non-sensitive samples MUST survive stripping.
    assert record["payload"]["by_type"][RUN_TYPE]["sample"][0]["name"] == "payment-demo"
    # 4. First (live) response and the L2-hit response are byte-identical.
    r2 = client.get("/infra/graph")
    assert calls["n"] == 1
    assert r1.json() == r2.json()


def test_l2_firestore_store_path_at_default_ttl(monkeypatch):
    """Codex review #8: exercise the REAL FirestoreInfraGraphCacheStore class
    (with an injected fake client) at the default 900s TTL, so the production
    store path — not just the in-memory double — round-trips through the endpoint."""

    class _FakeSnap:
        def __init__(self, d):
            self._d = d

        @property
        def exists(self):
            return self._d is not None

        def to_dict(self):
            return dict(self._d) if self._d is not None else None

    class _FakeDoc:
        def __init__(self, store, key):
            self._store, self._key = store, key

        def get(self):
            return _FakeSnap(self._store.get(self._key))

        def set(self, data):
            self._store[self._key] = dict(data)

    class _FakeColl:
        def __init__(self, store):
            self._store = store

        def document(self, key):
            return _FakeDoc(self._store, key)

    class _FakeClient:
        def __init__(self):
            self._docs = {}

        def collection(self, name):
            return _FakeColl(self._docs)

    calls = _counting_call(monkeypatch, returns=_inventory())
    _set_ttl(monkeypatch, "0")  # force L2
    _set_l2_ttl(monkeypatch, "900")  # default prod TTL
    _inject_l2(FirestoreInfraGraphCacheStore(project="test-proj", client=_FakeClient()))
    client = TestClient(app)
    r1 = client.get("/infra/graph")
    r2 = client.get("/infra/graph")
    assert calls["n"] == 1, "second request served from the Firestore-backed store"
    assert r1.headers.get("x-infra-graph-cache") == "miss"
    assert r2.headers.get("x-infra-graph-cache") == "hit-l2"


# --------------------------------------------------------------------------- #
# Pre-warm endpoint: POST /internal/infra-graph/refresh (Cloud Scheduler target)
# --------------------------------------------------------------------------- #

REFRESH_PATH = "/internal/infra-graph/refresh"
_PREWARM_AUD = "https://coord.test/internal/infra-graph/refresh"


def _set_prewarm_audience(monkeypatch, value: str) -> None:
    monkeypatch.setenv("INFRA_PREWARM_AUDIENCE", value)
    get_settings.cache_clear()


def _accept_oidc(monkeypatch, email: str) -> None:
    """Make verify_oauth2_token (used by verify_oidc_caller) accept any token and
    return a claims dict with the given email — so the endpoint's auth is
    exercised without real Google-signed tokens."""
    monkeypatch.setattr(
        "driftscribe_lib.auth.id_token.verify_oauth2_token",
        lambda token, transport, audience: {"email": email},
    )


def test_refresh_503_when_audience_unset(monkeypatch):
    # INFRA_PREWARM_AUDIENCE defaults empty → fail-closed, endpoint dormant.
    _mock_call(monkeypatch, returns=_inventory())
    client = TestClient(app)
    resp = client.post(REFRESH_PATH, headers={"Authorization": "Bearer x"})
    assert resp.status_code == 503


def test_refresh_401_without_token(monkeypatch):
    _set_prewarm_audience(monkeypatch, _PREWARM_AUD)
    _mock_call(monkeypatch, returns=_inventory())
    client = TestClient(app)
    assert client.post(REFRESH_PATH).status_code == 401


def test_refresh_403_wrong_service_account(monkeypatch):
    _set_prewarm_audience(monkeypatch, _PREWARM_AUD)
    _accept_oidc(monkeypatch, "intruder@evil.example")
    _mock_call(monkeypatch, returns=_inventory())
    client = TestClient(app)
    resp = client.post(REFRESH_PATH, headers={"Authorization": "Bearer x"})
    assert resp.status_code == 403


def test_refresh_verify_serialized_by_transport_lock(monkeypatch):
    """The prewarm verify must hold ``_GOOGLE_AUTH_TRANSPORT_LOCK``.

    It shares ``_GOOGLE_AUTH_TRANSPORT`` (a single ``requests.Session``)
    with /eventarc, whose verifies run on ``asyncio.to_thread`` worker
    threads — while THIS sync route runs on a threadpool thread. Every use
    of the shared transport must hold the lock so the Session is never
    driven from two threads at once (Codex review follow-up to the
    2026-07-07 backend audit, finding 1).
    """
    _set_prewarm_audience(monkeypatch, _PREWARM_AUD)
    _mock_call(monkeypatch, returns=_inventory())
    lock_held: dict[str, bool] = {}

    def _fake_verify(token, transport, audience):
        from agent import main as agent_main

        lock_held["value"] = agent_main._GOOGLE_AUTH_TRANSPORT_LOCK.locked()
        # Wrong SA → 403 right after verification (auth path fully
        # exercised, downstream warm skipped).
        return {"email": "intruder@evil.example"}

    monkeypatch.setattr(
        "driftscribe_lib.auth.id_token.verify_oauth2_token", _fake_verify
    )
    client = TestClient(app)
    resp = client.post(REFRESH_PATH, headers={"Authorization": "Bearer x"})
    assert resp.status_code == 403
    assert lock_held["value"] is True, (
        "verify_oidc_caller used the shared transport without "
        "_GOOGLE_AUTH_TRANSPORT_LOCK held"
    )


def test_refresh_200_warms_l2(monkeypatch):
    _set_prewarm_audience(monkeypatch, _PREWARM_AUD)
    # gcp_project is "test-proj" (conftest) → expected SA email:
    _accept_oidc(monkeypatch, "infra-prewarm-sa@test-proj.iam.gserviceaccount.com")
    _set_ttl(monkeypatch, "0")  # L1 off so the follow-up GET proves L2 specifically
    _set_l2_ttl(monkeypatch, "900")
    store = _inject_l2()
    calls = _counting_call(monkeypatch, returns=_inventory())
    client = TestClient(app)
    resp = client.post(REFRESH_PATH, headers={"Authorization": "Bearer x"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["cached"] is True
    assert body["resource_count"] == 2
    assert calls["n"] == 1
    assert store.get() is not None
    # End-to-end: the warmed L2 doc actually serves the next GET (no second
    # worker call) — proving the pre-warm achieves its purpose, not just that
    # *some* record was written.
    r = client.get("/infra/graph")
    assert r.headers.get("x-infra-graph-cache") == "hit-l2"
    assert calls["n"] == 1


def test_refresh_runs_skew_canary_on_stale_worker(monkeypatch, caplog):
    """Pre-warm must also flag a stale-worker inventory, else a scheduled warm
    caches a field-less inventory into L2 and the canary never fires on GET."""
    _set_prewarm_audience(monkeypatch, _PREWARM_AUD)
    _accept_oidc(monkeypatch, "infra-prewarm-sa@test-proj.iam.gserviceaccount.com")
    _set_l2_ttl(monkeypatch, "900")
    _inject_l2()
    # _inventory()'s Cloud Run entry lacks not_in_iac_control_plane (pre-#193).
    _counting_call(monkeypatch, returns=_inventory())
    client = TestClient(app)
    with caplog.at_level("WARNING"):
        assert client.post(REFRESH_PATH, headers={"Authorization": "Bearer x"}).status_code == 200
    recs = [r for r in caplog.records
            if r.msg == "infra_graph_inventory_missing_control_plane_count"]
    assert len(recs) == 1
    assert RUN_TYPE in recs[0].stale_asset_types


def test_refresh_soft_200_on_worker_error(monkeypatch):
    _set_prewarm_audience(monkeypatch, _PREWARM_AUD)
    _accept_oidc(monkeypatch, "infra-prewarm-sa@test-proj.iam.gserviceaccount.com")
    _set_l2_ttl(monkeypatch, "900")
    store = _inject_l2()
    _counting_call(
        monkeypatch,
        raises=worker_client.WorkerClientError(503, "down", "infra_reader"),
    )
    client = TestClient(app)
    resp = client.post(REFRESH_PATH, headers={"Authorization": "Bearer x"})
    # Soft-fail 200 so Cloud Scheduler doesn't retry-storm a transient worker blip.
    assert resp.status_code == 200
    assert resp.json()["cached"] is False
    assert store.get() is None


def test_refresh_soft_200_on_cai_error(monkeypatch):
    """The worker can soft-fail CAI to a 200 with an ``error`` key (not a raised
    WorkerClientError). Pre-warm must treat that as a non-persist soft-200 too,
    not cache the degraded inventory."""
    _set_prewarm_audience(monkeypatch, _PREWARM_AUD)
    _accept_oidc(monkeypatch, "infra-prewarm-sa@test-proj.iam.gserviceaccount.com")
    _set_l2_ttl(monkeypatch, "900")
    store = _inject_l2()
    _counting_call(
        monkeypatch,
        returns={"error": "cloud_asset_unavailable", "detail": "x", "project": "p"},
    )
    client = TestClient(app)
    resp = client.post(REFRESH_PATH, headers={"Authorization": "Bearer x"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["cached"] is False
    assert body["reason"] == "inventory_error"
    assert store.get() is None


def test_refresh_reports_l2_disabled_when_only_l1(monkeypatch):
    """``cached`` must mean the PERSISTENT layer was warmed. With L2 off (even
    though L1 is on), the pre-warm reports cached=false / reason=l2_disabled
    rather than a misleading success (Codex completed-work review #2)."""
    _set_prewarm_audience(monkeypatch, _PREWARM_AUD)
    _accept_oidc(monkeypatch, "infra-prewarm-sa@test-proj.iam.gserviceaccount.com")
    _set_l2_ttl(monkeypatch, "0")  # L2 disabled; L1 keeps its 60s default
    _inject_l2()
    _counting_call(monkeypatch, returns=_inventory())
    client = TestClient(app)
    body = client.post(REFRESH_PATH, headers={"Authorization": "Bearer x"}).json()
    assert body["cached"] is False
    assert body["reason"] == "l2_disabled"


def test_refresh_reports_l2_write_failed_when_store_write_fails(monkeypatch):
    """A swallowed Firestore write (set→False) must surface as cached=false /
    reason=l2_write_failed, not a false success (Codex completed-work review #1)."""

    class _FailingStore:
        def get(self):
            return None

        def set(self, record):
            return False  # simulate a swallowed IAM/network write failure

    _set_prewarm_audience(monkeypatch, _PREWARM_AUD)
    _accept_oidc(monkeypatch, "infra-prewarm-sa@test-proj.iam.gserviceaccount.com")
    _set_l2_ttl(monkeypatch, "900")
    _inject_l2(_FailingStore())
    _counting_call(monkeypatch, returns=_inventory())
    client = TestClient(app)
    body = client.post(REFRESH_PATH, headers={"Authorization": "Bearer x"}).json()
    assert body["cached"] is False
    assert body["reason"] == "l2_write_failed"


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
