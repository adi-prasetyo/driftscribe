"""Unit tests for ``agent.worker_client.call_open_infra_pr`` (Phase D2-1).

The coordinator → tofu-editor seam. The tofu-editor worker is the
agent-authoring path: it commits validated, ``iac/``-only file writes onto an
``infra/`` branch and opens ONE PR. ``/open-pr`` is its sole canonical endpoint.

These tests mirror the existing ``test_worker_client`` idiom exactly: a fixed
fake :func:`mint_id_token` that records the audience it was called with, and
``respx`` for the httpx layer so we can assert URL / headers / body without
standing up a server. They pin the three properties that matter for this
wrapper:

- **Path locking** — ``call_open_infra_pr`` MUST hit ``/open-pr`` (the editor's
  only canonical endpoint), reached via the default path, never a caller-picked
  one.
- **Audience binding** — the minted token's ``aud`` is the worker ROOT url,
  never the ``/open-pr`` path (Cloud Run validates ``aud`` against the receiving
  service's URL; a path-suffixed audience is a latent custom-domain bug).
- **Payload passthrough** — the body is exactly the worker's ``OpenIacPrRequest``
  shape with ``base`` pinned to ``"main"`` in code (the LLM never supplies base /
  label / endpoint), and a multi-file ``files`` list survives the round trip.
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from agent import worker_client
from agent.worker_client import WorkerClientError


TOFU_EDITOR_URL = "https://tofu-editor.example.com"


@pytest.fixture(autouse=True)
def _stub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seed ``TOFU_EDITOR_URL`` into env. ``worker_client`` reads env lazily
    (Codex review of 11.7 plan) so a per-test monkeypatch is the correct way to
    vary it."""
    monkeypatch.setenv("TOFU_EDITOR_URL", TOFU_EDITOR_URL)


@pytest.fixture(autouse=True)
def _stub_mint_id_token(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the real ADC-backed token mint with a fake that records the
    audience it was called with. Returning a list lets each test assert on the
    captured audience without unwrapping a Mock."""
    captured: list[str] = []

    def fake_mint(audience: str) -> str:
        captured.append(audience)
        return "fake-id-token"

    monkeypatch.setattr(worker_client, "mint_id_token", fake_mint)
    return captured


def _multi_file_body() -> list[dict]:
    """A representative multi-file write — the editor's whole point is one PR
    spanning multiple ``iac/`` files in one commit."""
    return [
        {"path": "iac/buckets.tf", "content": 'resource "google_storage_bucket" "b" {}\n'},
        {"path": "iac/outputs.tf", "content": "output \"name\" { value = \"b\" }\n"},
    ]


# --------------------------------------------------------------------------- #
# Wiring: TOFU_EDITOR_URL resolves; /open-pr is the canonical endpoint.
# --------------------------------------------------------------------------- #


def test_tofu_editor_url_resolves_from_env() -> None:
    """The tofu_editor base URL resolves from ``TOFU_EDITOR_URL``."""
    assert worker_client._worker_url("tofu_editor") == TOFU_EDITOR_URL


def test_tofu_editor_canonical_endpoint_is_open_pr() -> None:
    """``/open-pr`` is the editor's sole canonical (default) endpoint."""
    assert worker_client.WORKER_ENDPOINTS["tofu_editor"] == "/open-pr"


# --------------------------------------------------------------------------- #
# call_open_infra_pr: POSTs to /open-pr with the exact payload; returns JSON.
# --------------------------------------------------------------------------- #


@respx.mock
def test_call_open_infra_pr_posts_to_open_pr_endpoint() -> None:
    """``call_open_infra_pr`` routes to the editor's /open-pr endpoint via the
    default path (no ``endpoint=`` override) and returns the worker's parsed
    JSON dict."""
    route = respx.post(f"{TOFU_EDITOR_URL}/open-pr").respond(
        200,
        json={
            "status": "opened",
            "pr_number": 7,
            "pr_url": "https://github.com/o/r/pull/7",
            "branch": "infra/add-bucket",
        },
    )
    out = worker_client.call_open_infra_pr(
        "owner/repo",
        "infra/add-bucket",
        "Add a bucket",
        "Body of the PR.",
        _multi_file_body(),
    )
    assert out == {
        "status": "opened",
        "pr_number": 7,
        "pr_url": "https://github.com/o/r/pull/7",
        "branch": "infra/add-bucket",
    }
    assert route.called
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer fake-id-token"
    assert req.headers["Content-Type"] == "application/json"


@respx.mock
def test_call_open_infra_pr_payload_passthrough_base_pinned_main() -> None:
    """The body is exactly the worker's ``OpenIacPrRequest`` shape: the five
    caller fields pass through verbatim, ``base`` is pinned to ``"main"`` in
    code (never caller-supplied), and the multi-file ``files`` list survives the
    round trip unchanged."""
    route = respx.post(f"{TOFU_EDITOR_URL}/open-pr").respond(
        200, json={"status": "opened", "pr_number": 1}
    )
    files = _multi_file_body()
    worker_client.call_open_infra_pr(
        "owner/repo",
        "infra/add-bucket",
        "Add a bucket",
        "Body of the PR.",
        files,
    )
    sent = json.loads(route.calls.last.request.content)
    assert sent == {
        "target_repo": "owner/repo",
        "branch": "infra/add-bucket",
        "base": "main",
        "title": "Add a bucket",
        "body": "Body of the PR.",
        "files": files,
    }
    # The multi-file list is preserved (two distinct iac/ writes in one PR).
    assert sent["base"] == "main"
    assert len(sent["files"]) == 2


@respx.mock
def test_call_open_infra_pr_audience_is_root_url(_stub_mint_id_token) -> None:
    """Audience binding holds for /open-pr — the minted token's ``aud`` is the
    worker ROOT url, never the /open-pr path. ``endswith`` (not substring) since
    ``tofu-editor`` naturally contains no path collision, matching the existing
    wrapper audience tests."""
    respx.post(f"{TOFU_EDITOR_URL}/open-pr").respond(200, json={"status": "opened"})
    worker_client.call_open_infra_pr(
        "owner/repo", "infra/x", "t", "b", _multi_file_body()
    )
    assert _stub_mint_id_token == [TOFU_EDITOR_URL]
    assert all(not aud.endswith("/open-pr") for aud in _stub_mint_id_token)


# --------------------------------------------------------------------------- #
# Error mapping: worker non-2xx / transport failures surface as
# WorkerClientError (mirrors the rest of the worker_client suite).
# --------------------------------------------------------------------------- #


@respx.mock
def test_call_open_infra_pr_surfaces_403_with_status_preserved() -> None:
    """A policy rejection from the editor (e.g. path outside iac/, target_repo
    mismatch) → ``WorkerClientError`` with the 403 status preserved and the
    worker name attached."""
    respx.post(f"{TOFU_EDITOR_URL}/open-pr").respond(
        403, json={"detail": "path outside iac/"}
    )
    with pytest.raises(WorkerClientError) as exc:
        worker_client.call_open_infra_pr(
            "owner/repo", "infra/x", "t", "b", _multi_file_body()
        )
    assert exc.value.status_code == 403
    assert exc.value.worker == "tofu_editor"
    assert "path outside iac/" in exc.value.body


@respx.mock
def test_call_open_infra_pr_maps_transport_error_to_503() -> None:
    """A transport failure (ConnectError) → synthetic 503 so the caller can
    distinguish "editor unreachable" from a real worker 5xx."""
    respx.post(f"{TOFU_EDITOR_URL}/open-pr").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    with pytest.raises(WorkerClientError) as exc:
        worker_client.call_open_infra_pr(
            "owner/repo", "infra/x", "t", "b", _multi_file_body()
        )
    assert exc.value.status_code == 503
    assert "unreachable" in str(exc.value).lower()


def test_call_open_infra_pr_raises_503_when_url_unset(monkeypatch) -> None:
    """Missing config at runtime is a deploy bug (the D3-1/D4 redeploy sets
    ``TOFU_EDITOR_URL``). Fail closed at 503 until then."""
    monkeypatch.delenv("TOFU_EDITOR_URL", raising=False)
    with pytest.raises(WorkerClientError) as exc:
        worker_client.call_open_infra_pr(
            "owner/repo", "infra/x", "t", "b", _multi_file_body()
        )
    assert exc.value.status_code == 503
    assert "not configured" in str(exc.value).lower()
