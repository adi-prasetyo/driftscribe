"""Unit tests for ``agent.worker_client`` (Phase 11.7).

The coordinator's outbound HTTP layer. These tests pin three properties:

- **Audience binding** — :func:`mint_id_token` MUST be called with the
  worker's ROOT URL (no endpoint path). Cloud Run's audience check
  silently tolerates path-suffixed audiences today, but custom domains
  break that fallback; getting this wrong now ships a latent bug.
- **Error mapping** — worker 4xx/5xx, transport failures, missing
  config, and non-JSON bodies must all surface as
  :class:`WorkerClientError` with status codes that disambiguate the
  failure class in logs.
- **Endpoint locking** — :func:`call_execute` MUST hit ``/execute``,
  not ``/propose``. The two endpoints are the difference between
  "ask permission" and "do the thing", and the LLM-facing tools never
  get to pick.

Mocking strategy: monkeypatch :func:`agent.worker_client.mint_id_token`
to return a fixed token (avoids hitting the metadata server), and use
``respx`` for the httpx layer so we can assert on URL / headers / body
without standing up a server.
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from agent import worker_client
from agent.worker_client import WorkerClientError


READER_URL = "https://reader.example.com"
DOCS_URL = "https://docs.example.com"
ROLLBACK_URL = "https://rollback.example.com"
NOTIFIER_URL = "https://notifier.example.com"
UPGRADE_DOCS_URL = "https://upgrade-docs.example.com"


@pytest.fixture(autouse=True)
def _stub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seed the four worker URLs into env. ``worker_client`` reads env
    lazily (Codex review of 11.7 plan) so a per-test monkeypatch is the
    correct way to vary them."""
    monkeypatch.setenv("READER_URL", READER_URL)
    monkeypatch.setenv("DOCS_URL", DOCS_URL)
    monkeypatch.setenv("ROLLBACK_URL", ROLLBACK_URL)
    monkeypatch.setenv("NOTIFIER_URL", NOTIFIER_URL)
    monkeypatch.setenv("UPGRADE_DOCS_URL", UPGRADE_DOCS_URL)


@pytest.fixture(autouse=True)
def _stub_mint_id_token(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the real ADC-backed token mint with a fake that records
    the audience it was called with. Returning a list lets each test
    assert on the captured audience without unwrapping a Mock."""
    captured: list[str] = []

    def fake_mint(audience: str) -> str:
        captured.append(audience)
        return "fake-id-token"

    monkeypatch.setattr(worker_client, "mint_id_token", fake_mint)
    return captured


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


@respx.mock
def test_call_reader_posts_to_read_endpoint_with_bearer(_stub_mint_id_token) -> None:
    route = respx.post(f"{READER_URL}/read").respond(
        200, json={"env": {"X": "1"}, "revision": "rev-1"}
    )
    out = worker_client.call("reader", {})
    assert out == {"env": {"X": "1"}, "revision": "rev-1"}
    assert route.called
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer fake-id-token"
    assert req.headers["Content-Type"] == "application/json"
    # Payload survives the round trip.
    assert req.content == b"{}"


@respx.mock
def test_call_audience_is_root_url_not_endpoint(_stub_mint_id_token) -> None:
    """The ID token's ``aud`` claim MUST be the worker's ROOT URL.

    Cloud Run validates the audience against the receiving service's
    URL (which has no path). Pinning this here so a refactor that
    accidentally passes the endpoint URL doesn't silently regress.
    """
    respx.post(f"{READER_URL}/read").respond(200, json={"ok": True})
    worker_client.call("reader", {})
    assert _stub_mint_id_token == [READER_URL]
    # Critically: audience MUST NOT end with the endpoint path. Using
    # endswith here (not "/read in aud") because "reader" naturally
    # contains the substring "read".
    assert all(not aud.endswith("/read") for aud in _stub_mint_id_token)


@respx.mock
def test_call_docs_uses_patch_endpoint() -> None:
    route = respx.post(f"{DOCS_URL}/patch").respond(200, json={"url": "u"})
    out = worker_client.call(
        "docs",
        {
            "file_path": "demo/docs/runbook.md",
            "new_content": "x",
            "branch": "driftscribe/x",
            "base": "main",
            "title": "t",
            "body": "b",
        },
    )
    assert out == {"url": "u"}
    assert route.called


@respx.mock
def test_call_notifier_uses_notify_endpoint() -> None:
    route = respx.post(f"{NOTIFIER_URL}/notify").respond(
        200, json={"status": "sent"}
    )
    out = worker_client.call(
        "notifier",
        {"channel": "alert", "severity": "high", "body": "drift detected"},
    )
    assert out == {"status": "sent"}
    assert route.called


@respx.mock
def test_call_rollback_default_endpoint_is_propose() -> None:
    """Without an explicit ``endpoint=`` arg, the rollback worker is
    reached at /propose. /execute is gated behind :func:`call_execute`."""
    route_propose = respx.post(f"{ROLLBACK_URL}/propose").respond(
        200, json={"approval_id": "id1"}
    )
    route_execute = respx.post(f"{ROLLBACK_URL}/execute").respond(
        200, json={"status": "executed"}
    )
    out = worker_client.call(
        "rollback",
        {"target_revision": "payment-demo-00002-bbb", "reason": "rb"},
    )
    assert out == {"approval_id": "id1"}
    assert route_propose.called
    assert not route_execute.called


# --------------------------------------------------------------------------- #
# call_execute: must hit /execute, not /propose
# --------------------------------------------------------------------------- #


@respx.mock
def test_call_execute_hits_execute_not_propose() -> None:
    route_propose = respx.post(f"{ROLLBACK_URL}/propose").respond(
        200, json={"approval_id": "wrong"}
    )
    route_execute = respx.post(f"{ROLLBACK_URL}/execute").respond(
        200, json={"status": "executed", "operation_name": "op/1"}
    )
    out = worker_client.call_execute(
        "00000000-0000-0000-0000-000000000000", "fake-token-43-chars-aaaaaaaaaaaaaaaaaaaaaaa"
    )
    assert out["status"] == "executed"
    assert route_execute.called
    assert not route_propose.called


@respx.mock
def test_call_execute_payload_shape_exact() -> None:
    """The /execute payload MUST be exactly ``{approval_id, approval_token}``.
    The rollback worker's ExecuteRequest schema has ``extra="forbid"`` and
    will 422 on any spelling drift; the test pins the wire contract."""
    import json

    route = respx.post(f"{ROLLBACK_URL}/execute").respond(
        200, json={"status": "executed"}
    )
    worker_client.call_execute("aid", "atok")
    body = json.loads(route.calls.last.request.content)
    assert body == {"approval_id": "aid", "approval_token": "atok"}


# --------------------------------------------------------------------------- #
# Error mapping
# --------------------------------------------------------------------------- #


@respx.mock
def test_call_surfaces_4xx_with_status_code_preserved() -> None:
    respx.post(f"{READER_URL}/read").respond(422, json={"detail": "bad field"})
    with pytest.raises(WorkerClientError) as exc:
        worker_client.call("reader", {"bad": "field"})
    assert exc.value.status_code == 422
    assert exc.value.worker == "reader"
    assert "bad field" in exc.value.body


@respx.mock
def test_call_surfaces_5xx_with_status_code_preserved() -> None:
    respx.post(f"{READER_URL}/read").respond(500, text="server boom")
    with pytest.raises(WorkerClientError) as exc:
        worker_client.call("reader", {})
    assert exc.value.status_code == 500


@respx.mock
def test_call_maps_transport_error_to_503() -> None:
    """httpx.RequestError covers DNS / connection / timeout failures —
    things that prevented us from getting *any* response. We synthesize
    a 503 so the caller's error path can distinguish "worker unreachable"
    from "worker returned a real 5xx"."""
    respx.post(f"{READER_URL}/read").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    with pytest.raises(WorkerClientError) as exc:
        worker_client.call("reader", {})
    assert exc.value.status_code == 503
    assert "unreachable" in str(exc.value).lower()


@respx.mock
def test_call_maps_timeout_to_503() -> None:
    respx.post(f"{READER_URL}/read").mock(
        side_effect=httpx.ReadTimeout("slow")
    )
    with pytest.raises(WorkerClientError) as exc:
        worker_client.call("reader", {})
    assert exc.value.status_code == 503


def test_call_raises_503_when_worker_url_unset(monkeypatch) -> None:
    """Missing config at runtime is a deploy bug. Fail closed at 503."""
    monkeypatch.delenv("READER_URL", raising=False)
    with pytest.raises(WorkerClientError) as exc:
        worker_client.call("reader", {})
    assert exc.value.status_code == 503
    assert "not configured" in str(exc.value).lower()


def test_call_raises_503_when_worker_url_empty(monkeypatch) -> None:
    monkeypatch.setenv("READER_URL", "")
    with pytest.raises(WorkerClientError) as exc:
        worker_client.call("reader", {})
    assert exc.value.status_code == 503


def test_call_raises_for_unknown_worker_name() -> None:
    """Defense in depth: a typo in adk_tools.py shouldn't quietly hit
    a random URL — refuse the call entirely at this layer."""
    with pytest.raises(WorkerClientError) as exc:
        worker_client.call("ghost", {})
    assert exc.value.status_code == 503
    assert "ghost" in str(exc.value).lower()


@respx.mock
def test_call_maps_non_json_body_to_502() -> None:
    """A misconfigured proxy or future cache layer could send HTML on
    2xx. Surface a 502 so the chat handler doesn't crash trying to
    .json() the response."""
    respx.post(f"{READER_URL}/read").respond(
        200, text="<html>not json</html>", headers={"Content-Type": "text/html"}
    )
    with pytest.raises(WorkerClientError) as exc:
        worker_client.call("reader", {})
    assert exc.value.status_code == 502


@respx.mock
def test_call_error_body_is_truncated() -> None:
    """Worker error bodies could be arbitrarily large (stack traces,
    HTML pages). Truncate so the chat reply doesn't echo 50KB of detail."""
    big = "X" * 5000
    respx.post(f"{READER_URL}/read").respond(500, text=big)
    with pytest.raises(WorkerClientError) as exc:
        worker_client.call("reader", {})
    assert len(exc.value.body) <= 500


# --------------------------------------------------------------------------- #
# Trailing slash handling
# --------------------------------------------------------------------------- #


@respx.mock
def test_call_strips_trailing_slash_from_worker_url(monkeypatch) -> None:
    """Operators may accidentally include a trailing slash in the env
    var. The client must normalize so ``f"{base}{path}"`` doesn't
    produce a ``//`` request URL."""
    monkeypatch.setenv("READER_URL", f"{READER_URL}/")
    route = respx.post(f"{READER_URL}/read").respond(200, json={"ok": True})
    worker_client.call("reader", {})
    assert route.called


# --------------------------------------------------------------------------- #
# call_close_pr: must hit upgrade_docs /close, not /patch
# --------------------------------------------------------------------------- #


@respx.mock
def test_call_close_pr_hits_close_endpoint_with_exact_payload() -> None:
    """``call_close_pr`` routes to the upgrade_docs worker's /close
    endpoint (not its canonical /patch) and sends exactly the three
    fields the worker's ClosePrRequest schema expects."""
    route_patch = respx.post(f"{UPGRADE_DOCS_URL}/patch").respond(
        200, json={"should": "not be called"}
    )
    route_close = respx.post(f"{UPGRADE_DOCS_URL}/close").respond(
        200, json={"closed": True, "number": 1}
    )
    out = worker_client.call_close_pr("owner/repo", 1, "superseded")
    assert out == {"closed": True, "number": 1}
    assert route_close.called
    assert not route_patch.called
    sent = json.loads(route_close.calls.last.request.content)
    assert sent == {
        "target_repo": "owner/repo",
        "pr_number": 1,
        "reason": "superseded",
    }


@respx.mock
def test_call_close_pr_audience_is_root_url(_stub_mint_id_token) -> None:
    """Audience binding holds for the /close wrapper too — the minted
    token's ``aud`` is the worker ROOT url, never the /close path."""
    respx.post(f"{UPGRADE_DOCS_URL}/close").respond(200, json={"closed": True})
    worker_client.call_close_pr("owner/repo", 1, "r")
    assert _stub_mint_id_token == [UPGRADE_DOCS_URL]
    assert all(not aud.endswith("/close") for aud in _stub_mint_id_token)


# --------------------------------------------------------------------------- #
# call_merge_pr: must hit upgrade_docs /merge, not /patch
# --------------------------------------------------------------------------- #


@respx.mock
def test_call_merge_pr_hits_merge_endpoint_with_exact_payload() -> None:
    """``call_merge_pr`` routes to the upgrade_docs worker's /merge
    endpoint (not its canonical /patch) and sends exactly the two fields
    the worker's MergePrRequest schema expects — no merge method, no
    check list (both are deploy policy, never client-supplied)."""
    route_patch = respx.post(f"{UPGRADE_DOCS_URL}/patch").respond(
        200, json={"should": "not be called"}
    )
    route_merge = respx.post(f"{UPGRADE_DOCS_URL}/merge").respond(
        200, json={"merged": True, "number": 1}
    )
    out = worker_client.call_merge_pr("owner/repo", 1)
    assert out == {"merged": True, "number": 1}
    assert route_merge.called
    assert not route_patch.called
    sent = json.loads(route_merge.calls.last.request.content)
    assert sent == {"target_repo": "owner/repo", "pr_number": 1}


@respx.mock
def test_call_merge_pr_audience_is_root_url(_stub_mint_id_token) -> None:
    """Audience binding holds for the /merge wrapper too — the minted
    token's ``aud`` is the worker ROOT url, never the /merge path."""
    respx.post(f"{UPGRADE_DOCS_URL}/merge").respond(200, json={"merged": True})
    worker_client.call_merge_pr("owner/repo", 1)
    assert _stub_mint_id_token == [UPGRADE_DOCS_URL]
    assert all(not aud.endswith("/merge") for aud in _stub_mint_id_token)
