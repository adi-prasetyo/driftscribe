"""Tests for the standalone payment-demo FastAPI app under demo/.

Import strategy: `demo/` is a sibling Cloud Run service, not part of the
`agent`/`checker` packages declared in the root pyproject.toml's
`[tool.setuptools].packages`. Adding `"demo"` to that list would couple the
agent's install surface to a service it does not depend on. Manipulating
`sys.path` works but pollutes module resolution for the rest of the suite.

We therefore load `demo/main.py` via `importlib.util` from its absolute
filesystem location. Verbose, but zero global state leaks.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_MAIN = REPO_ROOT / "demo" / "main.py"


def _load_demo_main():
    """Fresh-load demo.main so module-level state (logger handlers etc.) is clean."""
    spec = importlib.util.spec_from_file_location("payment_demo_main", DEMO_MAIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["payment_demo_main"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def demo_app(monkeypatch):
    """Provide a fresh TestClient against demo.main:app with a clean env baseline.

    Entering TestClient as a context manager triggers the ASGI lifespan, so the
    boot-time secret-name guard runs in every test (rather than only when
    uvicorn boots the app for real). Without the `with` block the lifespan is
    skipped and the guard becomes dead code in CI.
    """
    for key in ("PAYMENT_MODE", "FEATURE_NEW_CHECKOUT", "FEATURE_BETA_UI", "NEW_THING", "K_REVISION"):
        monkeypatch.delenv(key, raising=False)
    module = _load_demo_main()
    with TestClient(module.app) as client:
        yield module, client


def test_root_returns_ok(demo_app):
    _, client = demo_app
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json() == {"service": "payment-demo", "ok": True}


def test_debug_config_only_exposes_safe_keys(demo_app, monkeypatch):
    """/debug/config must NOT leak NEW_THING even when it's set in the env.

    This is the asymmetry that powers Beat C of the demo: NEW_THING is
    visible via Cloud Run Admin API but invisible via /debug/config.
    """
    monkeypatch.setenv("PAYMENT_MODE", "mock")
    monkeypatch.setenv("FEATURE_NEW_CHECKOUT", "true")
    monkeypatch.setenv("FEATURE_BETA_UI", "false")
    monkeypatch.setenv("NEW_THING", "should-not-appear")
    _, client = demo_app
    resp = client.get("/debug/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "payment-demo"
    assert body["config"] == {
        "PAYMENT_MODE": "mock",
        "FEATURE_NEW_CHECKOUT": "true",
        "FEATURE_BETA_UI": "false",
    }
    assert "NEW_THING" not in body["config"]


def test_debug_config_uses_unset_for_missing_keys(demo_app, monkeypatch):
    monkeypatch.setenv("PAYMENT_MODE", "mock")
    monkeypatch.setenv("FEATURE_NEW_CHECKOUT", "false")
    # FEATURE_BETA_UI deliberately left unset.
    _, client = demo_app
    resp = client.get("/debug/config")
    assert resp.status_code == 200
    assert resp.json()["config"]["FEATURE_BETA_UI"] == "<unset>"


def test_debug_config_includes_revision(demo_app, monkeypatch):
    monkeypatch.setenv("K_REVISION", "demo-rev-1")
    _, client = demo_app
    resp = client.get("/debug/config")
    assert resp.status_code == 200
    assert resp.json()["revision"] == "demo-rev-1"


def test_debug_config_revision_falls_back_to_local(demo_app):
    _, client = demo_app
    resp = client.get("/debug/config")
    assert resp.status_code == 200
    assert resp.json()["revision"] == "local"


def test_boot_guard_refuses_secret_named_safe_key(monkeypatch):
    """Sabotage SAFE_KEYS to include a secret-named entry; entering the ASGI
    lifespan must raise so the app refuses to serve. This is the test that
    actually justifies the inline-regex duplication — without it, the guard
    is dead code.
    """
    module = _load_demo_main()
    module.SAFE_KEYS = {"PAYMENT_MODE", "STRIPE_API_KEY"}
    with pytest.raises(RuntimeError, match="secret-named entries"):
        with TestClient(module.app):
            pass


def test_demo_secret_regex_matches_agent_canonical():
    """Pin literal equivalence between the demo's inline copy of the secret-
    name regex and the canonical one in agent.secret_guard. If the canonical
    regex changes and someone forgets to update the demo, this test breaks
    loudly — the runtime-time decoupling is preserved, the test-time check
    catches drift.
    """
    from agent.secret_guard import SECRET_NAME_PATTERN

    module = _load_demo_main()
    assert module._SECRET_NAME_PATTERN.pattern == SECRET_NAME_PATTERN.pattern
    assert module._SECRET_NAME_PATTERN.flags == SECRET_NAME_PATTERN.flags
