"""Integration tests for ``GET /ui/transparency`` (Phase 19.B.1).

Scaffolding-only coverage: the route must serve the template, set
``Cache-Control: no-store`` (operator surface, mutable state), and be
reachable WITHOUT the ``X-DriftScribe-Token`` header — the HTML itself
is unauthenticated and every API call the page makes (in later 19.B
tasks) carries the token from ``sessionStorage``.

Landmarks pinned here are intentionally minimal — later UI tasks
(19.B.2-19.B.4) add their own tests that lock down the IDs they wire
up. The landmarks below are the ones a judge or a future refactor
would expect to find before any JS runs.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from agent.main import app


client = TestClient(app)


def test_ui_transparency_route_returns_html():
    resp = client.get("/ui/transparency")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers.get("Cache-Control") == "no-store"
    body = resp.text
    # Pin key landmarks so later tasks (and judges) can rely on them.
    assert "DriftScribe" in body
    assert "transparency" in body.lower() or "reasoning timeline" in body.lower()


def test_ui_transparency_route_does_not_require_token():
    # The HTML itself is unauthenticated; subsequent API calls carry the token.
    resp = client.get("/ui/transparency")  # NO X-DriftScribe-Token
    assert resp.status_code == 200


def test_ui_transparency_route_has_hook_landmarks():
    """Pin the structural IDs that 19.B.2-19.B.4 will attach to. If
    any of these disappear in a future refactor, the corresponding
    follow-up task's JS would silently no-op — better to fail here."""
    resp = client.get("/ui/transparency")
    body = resp.text
    # Token + decisions rail (19.B.2 / 19.B.3)
    assert 'id="token-status"' in body
    assert 'id="decisions-rail"' in body
    # Chat form pieces (19.B.3)
    assert 'id="prompt-input"' in body
    assert 'id="workload-select"' in body
    assert 'id="send-btn"' in body
    # Trace badge + final-response card (19.B.3)
    assert 'id="trace-badge"' in body
    assert 'id="final-response-card"' in body
    # Timeline groups (19.B.4)
    assert 'id="group-coordinator"' in body
    assert 'id="group-tools"' in body
    assert 'id="group-mcp"' in body


def test_ui_transparency_contains_token_prompt_helpers():
    # Pin the API shape that 19.B.3 will rely on.
    resp = client.get("/ui/transparency")
    body = resp.text
    assert "getToken" in body
    assert "api(" in body or "function api" in body
    assert "X-DriftScribe-Token" in body
    assert "driftscribe_token" in body  # sessionStorage key
    assert "401" in body and "403" in body  # cleared on auth failures


def test_ui_transparency_contains_chat_polling_logic():
    resp = client.get("/ui/transparency")
    body = resp.text
    # Pin the public JS contract that 19.B.4 will rely on.
    assert "/chat" in body
    assert "/trace/" in body  # polling endpoint
    assert "X-Trace-Id" in body  # header read on /chat response
    assert "final-response-card" in body  # element written to
    assert "2000" in body or "2_000" in body  # 2-second poll cadence
    # Status pill states — at least 'complete' must be referenced.
    assert "complete" in body.lower()


def test_ui_transparency_contains_three_group_renderer():
    """19.B.4: three-group timeline render (coordinator / tools / MCP).

    Pin the renderer's public surface so subsequent refactors don't quietly
    regress the worker-friendly labels (Codex v3 MINOR) or drop one of the
    three group anchors that the polling glue writes into.
    """
    body = client.get("/ui/transparency").text
    assert "_WORKER_LABELS" in body or "WORKER_LABELS" in body
    assert "Reader (drift)" in body
    assert "Developer Knowledge MCP" in body
    assert "group-coordinator" in body
    assert "group-tools" in body
    assert "group-mcp" in body


def test_ui_transparency_contains_approval_cta_renderer():
    """19.B.5: inline HITL approval CTA.

    Pin: the renderer exists and only fires for ``propose_rollback_tool``
    with a same-origin ``/approvals/`` URL. If a future refactor drops the
    helper or relaxes the URL guard, this test fails before judges (or an
    attacker probing for an open redirect) ever see the regression.
    """
    body = client.get("/ui/transparency").text
    # Pin: the renderer exists and only fires for propose_rollback_tool.
    assert "propose_rollback_tool" in body
    assert "approval_url" in body
    assert "Approve" in body  # button text
    assert "approval-cta" in body or "approval-btn" in body
    # Same-origin guard — must NOT have been relaxed to accept arbitrary URLs.
    assert '"/approvals/"' in body or "'/approvals/'" in body


def test_ui_transparency_contains_decisions_pane():
    """19.B.6: past-decisions pane + historical-trace navigation.

    Pin the rail's contract: it must reference /decisions, render an
    "open trace" affordance backed by a /trace/{id} fetch, dim the form
    when a historical trace is active ("← new chat" to return), and
    surface expired approval URLs (expires_at timestamp comparison) so a
    judge clicking a stale rollback gets a strikethrough + badge instead
    of a dead "Approve →".
    """
    body = client.get("/ui/transparency").text
    assert "decisions-rail" in body
    assert "/decisions" in body
    assert "/trace/" in body  # historical fetch
    assert "open trace" in body.lower() or "open-trace" in body.lower()
    assert "← new chat" in body or "new chat" in body.lower()
    # expired badge / past-decision approval CTA
    assert "expires_at" in body or "expired" in body
