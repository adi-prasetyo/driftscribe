"""Integration tests for ``GET /ui/transparency`` (Svelte SPA shell).

Post-refresh the route serves a THIN shell (a ``#app`` mount + the Vite
manifest-resolved JS/CSS); the Svelte app renders the DOM client-side. So this
file pins the SHELL contract (200, html, no-store, no token, the bundle
reference, the legacy fallback, and the manifest-present/absent resolution).

The behavioral/structural guards that the old single-file template inlined have
been RE-HOMED (not dropped) — each is now asserted on the pure function or the
runtime DOM:

  - token + 401/403 try-then-prompt  → frontend vitest api.test.ts
  - SSE consume + frame parsing       → frontend vitest sse.test.ts
  - event→group binning (incl mcp)    → frontend vitest timeline.test.ts
  - worker/MCP friendly labels        → frontend vitest labels.test.ts
  - same-origin approval-CTA guard    → frontend vitest approval.test.ts
                                         + mock smoke (off-origin → no link)
  - workload <select> option values   → frontend vitest workloads.test.ts
                                         + tests/unit/test_transparency_template_testids.py
  - testids / three groups / DOM flow → frontend/tests/smoke + tests/e2e/ui
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import agent.main as main
from agent.main import app


client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_manifest_cache():
    """Each test starts from a clean manifest cache (it caches on success)."""
    main._VITE_MANIFEST_CACHE = None
    yield
    main._VITE_MANIFEST_CACHE = None


def test_ui_transparency_route_returns_html():
    resp = client.get("/ui/transparency")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers.get("Cache-Control") == "no-store"
    body = resp.text
    assert "DriftScribe" in body
    assert "reasoning timeline" in body.lower()


def test_ui_transparency_route_does_not_require_token():
    # The shell itself is unauthenticated; the SPA's API calls carry the token.
    resp = client.get("/ui/transparency")  # NO X-DriftScribe-Token
    assert resp.status_code == 200


def test_ui_transparency_shell_mounts_spa_bundle():
    """The shell must carry the #app mount point and reference the built ES
    module + stylesheet under /static (resolved from the Vite manifest)."""
    body = client.get("/ui/transparency").text
    assert 'id="app"' in body
    assert "/static/" in body
    assert 'type="module"' in body
    assert '<link rel="stylesheet"' in body


def test_shell_uses_hashed_assets_when_manifest_present(tmp_path, monkeypatch):
    """Manifest present → the shell points at the hashed entry + its CSS."""
    static = tmp_path / "static"
    (static / ".vite").mkdir(parents=True)
    (static / ".vite" / "manifest.json").write_text(
        json.dumps(
            {
                "src/main.ts": {
                    "file": "transparency-DEADBEEF.js",
                    "isEntry": True,
                    "css": ["driftscribe-CAFEBABE.css"],
                }
            }
        )
    )
    monkeypatch.setattr(main, "_STATIC_DIR", static)
    main._VITE_MANIFEST_CACHE = None

    body = client.get("/ui/transparency").text
    assert "/static/transparency-DEADBEEF.js" in body
    assert "/static/driftscribe-CAFEBABE.css" in body


def test_shell_falls_back_when_manifest_absent(tmp_path, monkeypatch):
    """Manifest absent (pure-Python CI / pre-build) → 200 with the dev fallback
    asset names, so the route never 500s without a `vite build`."""
    empty = tmp_path / "no-static"
    empty.mkdir()
    monkeypatch.setattr(main, "_STATIC_DIR", empty)
    main._VITE_MANIFEST_CACHE = None

    resp = client.get("/ui/transparency")
    assert resp.status_code == 200
    body = resp.text
    assert 'id="app"' in body
    assert "/static/transparency.js" in body
    assert "/static/driftscribe.css" in body


def test_legacy_ui_still_reachable():
    """The pre-refresh single-file UI is preserved one release as a safety net."""
    resp = client.get("/ui/transparency-legacy")
    assert resp.status_code == 200
    assert resp.headers.get("Cache-Control") == "no-store"
    body = resp.text
    # Legacy landmarks (the inline-JS single-file page) are intact here.
    assert 'id="group-coordinator"' in body
    assert "decisions-rail" in body
