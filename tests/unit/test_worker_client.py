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
TOFU_APPLY_URL = "https://tofu-apply.example.com"


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
    monkeypatch.setenv("TOFU_APPLY_URL", TOFU_APPLY_URL)


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


# --------------------------------------------------------------------------- #
# Phase C5a: tofu-apply worker wiring
#
# The tofu-apply worker is the sole infra mutator. /propose is its canonical
# (default) endpoint; /apply mutates; /deny is cleanup-only. None of the
# three wrappers is ever an ADK tool — they are server-side approval-handler
# calls only (the ADK-non-exposure pin lives at the bottom of this file).
# --------------------------------------------------------------------------- #


def test_tofu_apply_url_resolves_from_env() -> None:
    """The tofu_apply base URL resolves from ``TOFU_APPLY_URL``."""
    assert worker_client._worker_url("tofu_apply") == TOFU_APPLY_URL


def test_tofu_apply_raises_503_when_url_unset(monkeypatch) -> None:
    """Missing config at runtime is a deploy bug. Fail closed at 503."""
    monkeypatch.delenv("TOFU_APPLY_URL", raising=False)
    with pytest.raises(WorkerClientError) as exc:
        worker_client.call("tofu_apply", {})
    assert exc.value.status_code == 503
    assert "not configured" in str(exc.value).lower()


def test_tofu_apply_raises_503_when_url_empty(monkeypatch) -> None:
    monkeypatch.setenv("TOFU_APPLY_URL", "")
    with pytest.raises(WorkerClientError) as exc:
        worker_client.call("tofu_apply", {})
    assert exc.value.status_code == 503


# --------------------------------------------------------------------------- #
# call_propose: default endpoint is /propose; operator_jwt is conditional
# --------------------------------------------------------------------------- #


@respx.mock
def test_call_propose_posts_to_propose_default_endpoint() -> None:
    """``call_propose`` uses the worker's DEFAULT endpoint (/propose) — it
    must NOT pass an ``endpoint=`` override."""
    route = respx.post(f"{TOFU_APPLY_URL}/propose").respond(
        200, json={"approval_id": "id1", "status": "pending"}
    )
    out = worker_client.call_propose(
        "gs://bucket/plan.json", "42", "operator@example.com", None
    )
    assert out == {"approval_id": "id1", "status": "pending"}
    assert route.called


@respx.mock
def test_call_propose_includes_operator_jwt_when_present() -> None:
    """With ``operator_jwt="x"`` the body contains the ``operator_jwt`` key
    alongside the three canonical fields."""
    route = respx.post(f"{TOFU_APPLY_URL}/propose").respond(
        200, json={"approval_id": "id1"}
    )
    worker_client.call_propose(
        "gs://bucket/plan.json", "42", "operator@example.com", "x"
    )
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "artifact_uri_metadata": "gs://bucket/plan.json",
        "generation_metadata": "42",
        "approver": "operator@example.com",
        "operator_jwt": "x",
    }


@respx.mock
def test_call_propose_omits_operator_jwt_when_none() -> None:
    """With ``operator_jwt=None`` the body has NO ``operator_jwt`` key — the
    worker's ProposeRequest is ``extra="forbid"`` and the field does not
    exist until C5b, so omitting it keeps the wrapper wire-compatible."""
    route = respx.post(f"{TOFU_APPLY_URL}/propose").respond(
        200, json={"approval_id": "id1"}
    )
    worker_client.call_propose(
        "gs://bucket/plan.json", "42", "operator@example.com", None
    )
    body = json.loads(route.calls.last.request.content)
    assert "operator_jwt" not in body
    assert body == {
        "artifact_uri_metadata": "gs://bucket/plan.json",
        "generation_metadata": "42",
        "approver": "operator@example.com",
    }


@respx.mock
def test_call_propose_audience_is_root_url(_stub_mint_id_token) -> None:
    """Audience binding holds for /propose — the minted token's ``aud`` is
    the worker ROOT url, never the /propose path."""
    respx.post(f"{TOFU_APPLY_URL}/propose").respond(200, json={"ok": True})
    worker_client.call_propose("gs://b/p.json", "1", "op@e.com", None)
    assert _stub_mint_id_token == [TOFU_APPLY_URL]
    assert all(not aud.endswith("/propose") for aud in _stub_mint_id_token)


# --------------------------------------------------------------------------- #
# call_apply: must hit /apply; operator_jwt conditional; id+token always
# --------------------------------------------------------------------------- #


@respx.mock
def test_call_apply_hits_apply_not_propose() -> None:
    """``call_apply`` routes to /apply (the mutating path), never the
    default /propose."""
    route_propose = respx.post(f"{TOFU_APPLY_URL}/propose").respond(
        200, json={"should": "not be called"}
    )
    route_apply = respx.post(f"{TOFU_APPLY_URL}/apply").respond(
        200, json={"status": "applied"}
    )
    out = worker_client.call_apply("aid", "atok", None)
    assert out == {"status": "applied"}
    assert route_apply.called
    assert not route_propose.called


@respx.mock
def test_call_apply_includes_operator_jwt_when_present() -> None:
    route = respx.post(f"{TOFU_APPLY_URL}/apply").respond(
        200, json={"status": "applied"}
    )
    worker_client.call_apply("aid", "atok", "x")
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "approval_id": "aid",
        "approval_token": "atok",
        "operator_jwt": "x",
    }


@respx.mock
def test_call_apply_omits_operator_jwt_when_none() -> None:
    """``operator_jwt=None`` → no ``operator_jwt`` key; id+token always
    present (the worker's TokenRequest is ``extra="forbid"``)."""
    route = respx.post(f"{TOFU_APPLY_URL}/apply").respond(
        200, json={"status": "applied"}
    )
    worker_client.call_apply("aid", "atok", None)
    body = json.loads(route.calls.last.request.content)
    assert "operator_jwt" not in body
    assert body == {"approval_id": "aid", "approval_token": "atok"}


@respx.mock
def test_call_apply_audience_is_root_url(_stub_mint_id_token) -> None:
    respx.post(f"{TOFU_APPLY_URL}/apply").respond(200, json={"status": "applied"})
    worker_client.call_apply("aid", "atok", None)
    assert _stub_mint_id_token == [TOFU_APPLY_URL]
    assert all(not aud.endswith("/apply") for aud in _stub_mint_id_token)


# --------------------------------------------------------------------------- #
# call_plan_deny: must hit /deny; body is exactly id+token, never operator_jwt
# --------------------------------------------------------------------------- #


@respx.mock
def test_call_plan_deny_hits_deny_endpoint_with_exact_payload() -> None:
    """``call_plan_deny`` routes to /deny (cleanup-only) and sends exactly
    ``{approval_id, approval_token}`` — never an ``operator_jwt`` key, since
    cleanup carries no operator-identity binding."""
    route_propose = respx.post(f"{TOFU_APPLY_URL}/propose").respond(
        200, json={"should": "not be called"}
    )
    route_deny = respx.post(f"{TOFU_APPLY_URL}/deny").respond(
        200, json={"status": "denied"}
    )
    out = worker_client.call_plan_deny("aid", "atok")
    assert out == {"status": "denied"}
    assert route_deny.called
    assert not route_propose.called
    body = json.loads(route_deny.calls.last.request.content)
    assert body == {"approval_id": "aid", "approval_token": "atok"}
    assert "operator_jwt" not in body


@respx.mock
def test_call_plan_deny_audience_is_root_url(_stub_mint_id_token) -> None:
    respx.post(f"{TOFU_APPLY_URL}/deny").respond(200, json={"status": "denied"})
    worker_client.call_plan_deny("aid", "atok")
    assert _stub_mint_id_token == [TOFU_APPLY_URL]
    assert all(not aud.endswith("/deny") for aud in _stub_mint_id_token)


# --------------------------------------------------------------------------- #
# ADK-non-exposure: the three tofu-apply wrappers are NOT registered tools.
#
# Mirrors the Layer-0 guarantee for call_execute / call_deny: the operator's
# approval-handler is the only caller. ``COORDINATOR_TOOLS`` (agent.adk_agent)
# is the EXHAUSTIVE registry of ADK-exposed callables — these wrappers must
# not appear among the registered tool function names.
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Phase C5c: probe_worker_health — the read-only reachability primitive behind
# GET /iac-apply/reachability. The crux is the ``reachable`` semantics: ANY HTTP
# response (incl. 403/404 from the app) means the network route + TLS + ingress
# worked → reachable=True; only a transport error (DNS / route blackhole /
# connect timeout) means the path is broken → reachable=False. A URL-unset
# worker is a result ("url_unset"), never a raised exception. NEVER raises
# through — the endpoint fans this out and must not crash on one bad worker.
# --------------------------------------------------------------------------- #


@respx.mock
def test_probe_worker_health_405_is_app_reached(_stub_mint_id_token) -> None:
    """GET on the canonical POST path → the app answers 405 → reachable True AND
    app_reached True (status != 404). probed_path is the canonical endpoint, and
    the audience is the worker ROOT url (same binding rule as ``call``)."""
    respx.get(f"{TOFU_APPLY_URL}/propose").respond(405, text="method not allowed")
    out = worker_client.probe_worker_health("tofu_apply")
    assert out["worker"] == "tofu_apply"
    assert out["target"] == TOFU_APPLY_URL
    assert out["probed_path"] == "/propose"
    assert out["reachable"] is True
    assert out["app_reached"] is True
    assert out["status_code"] == 405
    assert out["error"] is None
    assert isinstance(out["latency_ms"], int)
    # Audience binding holds — aud is the ROOT url, never the /propose path.
    assert _stub_mint_id_token == [TOFU_APPLY_URL]
    assert all(not aud.endswith("/propose") for aud in _stub_mint_id_token)


@respx.mock
def test_probe_worker_health_200_is_app_reached() -> None:
    """Any non-404 app response counts as app_reached (e.g. a 200 if a worker
    ever answered GET on its canonical path)."""
    respx.get(f"{TOFU_APPLY_URL}/propose").respond(200, json={"ok": True})
    out = worker_client.probe_worker_health("tofu_apply")
    assert out["reachable"] is True
    assert out["app_reached"] is True
    assert out["status_code"] == 200


@respx.mock
def test_probe_worker_health_403_is_reachable_not_app_reached() -> None:
    """A 403 means the request reached the GFE (reachable True) but was rejected
    by ingress-IAM or app auth — a real /propose|/apply call would hit the SAME
    rejection, so it is NOT a green cutover signal: app_reached False (status in
    {401,403,404}). 405, not 403, is the 'reached' boundary."""
    respx.get(f"{TOFU_APPLY_URL}/propose").respond(403, text="forbidden")
    out = worker_client.probe_worker_health("tofu_apply")
    assert out["reachable"] is True
    assert out["app_reached"] is False
    assert out["status_code"] == 403
    assert out["error"] is None


@respx.mock
def test_probe_worker_health_404_is_reachable_not_app_reached() -> None:
    """A 404 is the pre-app reject — GFE-reserved path OR an internal-ingress
    rejection (the request never reached the worker process). reachable True
    (an HTTP status came back, not a blackhole) but app_reached False. This is
    the exact signal that fails the cutover gate for an unrouted internal call."""
    respx.get(f"{READER_URL}/read").respond(404, text="not found")
    out = worker_client.probe_worker_health("reader")
    assert out["worker"] == "reader"
    assert out["target"] == READER_URL
    assert out["probed_path"] == "/read"
    assert out["reachable"] is True
    assert out["app_reached"] is False
    assert out["status_code"] == 404
    assert out["error"] is None


@respx.mock
def test_probe_worker_health_transport_error_is_unreachable(_stub_mint_id_token) -> None:
    """A transport error (ConnectError) → reachable False, status_code None, and
    a non-None error string carrying the class name. NEVER raises through."""
    respx.get(f"{TOFU_APPLY_URL}/propose").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    out = worker_client.probe_worker_health("tofu_apply")
    assert out["worker"] == "tofu_apply"
    assert out["target"] == TOFU_APPLY_URL
    assert out["reachable"] is False
    assert out["status_code"] is None
    assert out["latency_ms"] is None
    assert "ConnectError" in out["error"]


@respx.mock
def test_probe_worker_health_timeout_is_unreachable() -> None:
    """A read/connect timeout is also a transport failure → reachable False with
    the timeout class in the error (httpx timeouts subclass httpx.HTTPError)."""
    respx.get(f"{TOFU_APPLY_URL}/propose").mock(
        side_effect=httpx.ConnectTimeout("timed out")
    )
    out = worker_client.probe_worker_health("tofu_apply")
    assert out["reachable"] is False
    assert out["status_code"] is None
    assert "ConnectTimeout" in out["error"]


def test_probe_worker_health_token_mint_failure_is_caught(monkeypatch) -> None:
    """If ``mint_id_token`` raises (metadata/auth failure), the probe CATCHES it
    and returns ``reachable=False, error="token_mint_failed: ..."`` rather than
    raising through — a diagnostic that 500s and loses every per-worker result
    would defeat its purpose. target is still the resolved URL."""
    monkeypatch.setenv("TOFU_APPLY_URL", TOFU_APPLY_URL)

    def boom(_base: str) -> str:
        raise RuntimeError("metadata server unreachable")

    monkeypatch.setattr(worker_client, "mint_id_token", boom)
    out = worker_client.probe_worker_health("tofu_apply")
    assert out["worker"] == "tofu_apply"
    assert out["target"] == TOFU_APPLY_URL
    assert out["reachable"] is False
    assert out["status_code"] is None
    assert out["latency_ms"] is None
    assert out["error"].startswith("token_mint_failed:")
    assert "RuntimeError" in out["error"]


def test_probe_worker_health_url_unset_returns_url_unset(monkeypatch) -> None:
    """When the worker's URL env is empty, ``_worker_url`` raises
    WorkerClientError — which the probe CATCHES and reports as a result
    (``error="url_unset"``, reachable False, target None), never propagating."""
    monkeypatch.setenv("TOFU_APPLY_URL", "")
    out = worker_client.probe_worker_health("tofu_apply")
    assert out["worker"] == "tofu_apply"
    assert out["target"] is None
    assert out["reachable"] is False
    assert out["status_code"] is None
    assert out["latency_ms"] is None
    assert out["error"] == "url_unset"


def test_probe_worker_health_url_unset_when_env_deleted(monkeypatch) -> None:
    """Same url_unset result when the env var is absent entirely (not just empty)."""
    monkeypatch.delenv("TOFU_APPLY_URL", raising=False)
    out = worker_client.probe_worker_health("tofu_apply")
    assert out["error"] == "url_unset"
    assert out["reachable"] is False


def test_probe_worker_health_is_not_an_adk_tool() -> None:
    """``probe_worker_health`` is an internal diagnostic (like ``call_apply``) —
    it must NEVER be exposed as an ADK tool. The LLM-facing tool registry
    (``COORDINATOR_TOOLS``) is exhaustive, so the proof is its absence there and
    on the adk_tools module."""
    from agent.adk_agent import COORDINATOR_TOOLS

    registered_names = {t.__name__ for t in COORDINATOR_TOOLS}
    assert "probe_worker_health" not in registered_names

    import agent.adk_tools as adk_tools

    assert not hasattr(adk_tools, "probe_worker_health")


# --------------------------------------------------------------------------- #
# Phase C5e-1: per-call timeout. /apply runs a real ``tofu apply`` (up to the
# worker's --timeout=900), so call_apply uses a long read timeout while every
# other call keeps the 30s default. A premature client timeout after the worker
# burned the approval + mutated infra would make the coordinator skip the merge
# → silent divergence; this is correctness, not latency.
# --------------------------------------------------------------------------- #


@pytest.fixture
def _capture_timeout(monkeypatch: pytest.MonkeyPatch) -> list:
    """Wrap ``httpx.Client`` so each constructed client records the ``timeout``
    it was built with, while still serving the respx-mocked transport."""
    captured: list = []
    real_client = httpx.Client

    class _RecordingClient(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            captured.append(kwargs.get("timeout"))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(worker_client.httpx, "Client", _RecordingClient)
    return captured


@respx.mock
def test_call_apply_uses_long_timeout(_capture_timeout) -> None:
    """``call_apply`` builds its httpx client with :data:`_APPLY_HTTPX_TIMEOUT`
    (920s read), not the 30s default."""
    respx.post(f"{TOFU_APPLY_URL}/apply").respond(200, json={"status": "applied"})
    worker_client.call_apply("aid", "atok", None)
    assert len(_capture_timeout) == 1
    timeout = _capture_timeout[0]
    assert timeout is worker_client._APPLY_HTTPX_TIMEOUT
    # The read budget must exceed the worker's Cloud Run --timeout=900.
    assert timeout.read > 900.0
    assert timeout.connect == 10.0


@respx.mock
def test_call_propose_uses_default_timeout(_capture_timeout) -> None:
    """``call_propose`` does NOT pass a timeout — it gets the 30s default
    (``call`` falls back to ``_HTTPX_TIMEOUT`` when ``timeout is None``, so the
    client is built with the 30s float, NOT the long apply timeout)."""
    respx.post(f"{TOFU_APPLY_URL}/propose").respond(200, json={"approval_id": "id1"})
    worker_client.call_propose("gs://b/p.json", "1", "op@e.com", None)
    assert _capture_timeout == [worker_client._HTTPX_TIMEOUT]
    assert _capture_timeout[0] is not worker_client._APPLY_HTTPX_TIMEOUT


@respx.mock
def test_call_plan_deny_uses_default_timeout(_capture_timeout) -> None:
    respx.post(f"{TOFU_APPLY_URL}/deny").respond(200, json={"status": "denied"})
    worker_client.call_plan_deny("aid", "atok")
    assert _capture_timeout == [worker_client._HTTPX_TIMEOUT]
    assert _capture_timeout[0] is not worker_client._APPLY_HTTPX_TIMEOUT


@respx.mock
def test_call_per_call_timeout_override_is_honored(_capture_timeout) -> None:
    """A caller-supplied ``timeout=`` overrides the default and is passed straight
    through to the httpx client."""
    respx.post(f"{READER_URL}/read").respond(200, json={"ok": True})
    custom = httpx.Timeout(connect=1.0, read=2.0, write=3.0, pool=4.0)
    worker_client.call("reader", {}, timeout=custom)
    assert _capture_timeout == [custom]


@respx.mock
def test_call_default_timeout_when_no_override(_capture_timeout) -> None:
    """Without ``timeout=``, ``call`` builds the client with the module 30s
    default — captured as the ``_HTTPX_TIMEOUT`` float since ``call`` passes it
    explicitly when ``timeout is None``."""
    respx.post(f"{READER_URL}/read").respond(200, json={"ok": True})
    worker_client.call("reader", {})
    assert _capture_timeout == [worker_client._HTTPX_TIMEOUT]


def test_tofu_apply_wrappers_are_not_adk_tools() -> None:
    """``call_propose`` / ``call_apply`` / ``call_plan_deny`` must NEVER be
    exposed as ADK tools — they are server-side approval-handler calls only.

    Same Layer-0 invariant the rollback worker's ``call_execute`` /
    ``call_deny`` enjoy: the LLM-facing tool registry
    (``agent.adk_agent.COORDINATOR_TOOLS``) is exhaustive, so the proof is
    that none of these three callables appears among the registered tool
    function names.
    """
    from agent.adk_agent import COORDINATOR_TOOLS

    registered_names = {t.__name__ for t in COORDINATOR_TOOLS}
    for name in ("call_propose", "call_apply", "call_plan_deny"):
        assert name not in registered_names, (
            f"{name} is a server-side mutation wrapper and must never be an "
            f"ADK tool, but it appears in COORDINATOR_TOOLS."
        )

    # Defense in depth: also confirm the coordinator's ADK-tool module does
    # not re-export these wrappers as module-level callables (a future PR
    # that imported them into agent.adk_tools could accidentally widen the
    # surface even before they hit COORDINATOR_TOOLS).
    import agent.adk_tools as adk_tools

    for name in ("call_propose", "call_apply", "call_plan_deny"):
        assert not hasattr(adk_tools, name), (
            f"agent.adk_tools must not expose {name} as a callable."
        )
