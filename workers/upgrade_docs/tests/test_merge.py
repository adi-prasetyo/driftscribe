"""Tests for the Upgrade Docs ``/merge`` endpoint.

Mirrors ``test_close.py``: the fixture stubs the ``ds_github.merge_pr``
seam (so we never touch github.com) and bypasses auth via
``dependency_overrides``. Tests assert the worker:

- forwards ``pr_number`` and the deploy-pinned policy (driftscribe label
  + ``upgrade/`` head + ``main`` base + squash method + ``lint-test``
  required check) to the shared lib;
- re-validates ``target_repo`` against the deploy allowlist (403);
- maps :class:`driftscribe_lib.github.PrNotEligibleError` to its carried
  status code (403 provenance / 404 not found) and
  :class:`driftscribe_lib.github.PrMergeBlockedError` to 409;
- enforces the closed-schema (extra/missing field, ``pr_number<=0``, and
  in particular rejects a smuggled ``merge_method`` / ``reason``) and the
  shared auth contract.
"""
import os

import pytest
from fastapi import HTTPException, status
from fastapi.testclient import TestClient

# Env MUST be set before importing workers.upgrade_docs.main — the module
# reads UPGRADE_TARGET_REPO / GITHUB_TOKEN / OWN_URL / ALLOWED_CALLERS at
# import time and KeyErrors if any are missing.
os.environ.setdefault("UPGRADE_TARGET_REPO", "adi-prasetyo/driftscribe")
os.environ.setdefault("GITHUB_TOKEN", "test-token")
os.environ.setdefault("OWN_URL", "https://upgrade-docs.example.com")
os.environ.setdefault(
    "ALLOWED_CALLERS",
    "coordinator@test-proj.iam.gserviceaccount.com",
)

from workers.upgrade_docs import main as upgrade_docs_main  # noqa: E402
from workers.upgrade_docs.main import _verify_caller_dep, app  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    """TestClient with the merge seam stubbed and auth bypassed.

    ``captured`` collects the kwargs forwarded to ``ds_github.merge_pr``
    so tests can assert the worker pins the merge policy server-side.
    """
    captured: dict = {}

    def fake_merge_pr(repo, **kwargs):
        captured.update(kwargs)
        return {
            "dry_run": False,
            "merged": True,
            "already_merged": False,
            "url": "https://github.com/adi-prasetyo/driftscribe/pull/1",
            "number": 1,
            "sha": "mergedsha",
            "merge_method": kwargs.get("merge_method"),
            "comment_posted": True,
            "comment_error": None,
        }

    monkeypatch.setattr(upgrade_docs_main.ds_github, "merge_pr", fake_merge_pr)
    monkeypatch.setattr(upgrade_docs_main, "_get_repo", lambda: object())

    app.dependency_overrides[_verify_caller_dep] = (
        lambda: "coordinator@test-proj.iam.gserviceaccount.com"
    )
    yield TestClient(app), captured
    app.dependency_overrides.clear()


def _valid_body() -> dict:
    return {"target_repo": "adi-prasetyo/driftscribe", "pr_number": 1}


def test_merge_happy_path_forwards_policy_and_returns_result(client) -> None:
    tc, captured = client
    r = tc.post("/merge", json=_valid_body())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["merged"] is True
    assert body["number"] == 1

    # The worker pins the merge policy server-side — the caller never gets
    # to relax the gate or pick the merge method.
    assert captured["pr_number"] == 1
    assert captured["required_label"] == "driftscribe"
    assert captured["required_head_prefix"] == "upgrade/"
    assert captured["required_base"] == "main"
    assert captured["merge_method"] == upgrade_docs_main.MERGE_METHOD == "squash"
    assert set(captured["required_checks"]) == set(upgrade_docs_main.REQUIRED_CHECKS)
    assert "lint-test" in captured["required_checks"]
    assert captured["dry_run"] is False


def test_merge_target_repo_mismatch_returns_403(client) -> None:
    tc, captured = client
    body = _valid_body() | {"target_repo": "attacker/evil"}
    r = tc.post("/merge", json=body)
    assert r.status_code == 403
    # Policy short-circuit before any GitHub seam call.
    assert captured == {}


def test_merge_maps_pr_not_eligible_403(client, monkeypatch) -> None:
    tc, _ = client

    def refuse(repo, **kwargs):
        raise upgrade_docs_main.ds_github.PrNotEligibleError(
            "PR #1 is not a DriftScribe PR (missing 'driftscribe' label)"
        )

    monkeypatch.setattr(upgrade_docs_main.ds_github, "merge_pr", refuse)
    r = tc.post("/merge", json=_valid_body())
    assert r.status_code == 403
    assert "driftscribe" in r.json()["detail"]


def test_merge_maps_pr_not_found_404(client, monkeypatch) -> None:
    tc, _ = client

    def not_found(repo, **kwargs):
        raise upgrade_docs_main.ds_github.PrNotEligibleError(
            "PR #999 not found", status_code=404
        )

    monkeypatch.setattr(upgrade_docs_main.ds_github, "merge_pr", not_found)
    body = _valid_body() | {"pr_number": 999}
    r = tc.post("/merge", json=body)
    assert r.status_code == 404


def test_merge_maps_merge_blocked_409(client, monkeypatch) -> None:
    tc, _ = client

    def blocked(repo, **kwargs):
        raise upgrade_docs_main.ds_github.PrMergeBlockedError(
            "required check 'lint-test' concluded 'failure', not 'success'"
        )

    monkeypatch.setattr(upgrade_docs_main.ds_github, "merge_pr", blocked)
    r = tc.post("/merge", json=_valid_body())
    assert r.status_code == 409
    assert "lint-test" in r.json()["detail"]


def test_merge_zero_pr_number_returns_422(client) -> None:
    tc, _ = client
    body = _valid_body() | {"pr_number": 0}
    r = tc.post("/merge", json=body)
    assert r.status_code == 422


def test_merge_negative_pr_number_returns_422(client) -> None:
    tc, _ = client
    body = _valid_body() | {"pr_number": -3}
    r = tc.post("/merge", json=body)
    assert r.status_code == 422


def test_merge_rejects_smuggled_merge_method(client) -> None:
    # extra="forbid" — a caller cannot pick the merge strategy.
    tc, captured = client
    body = _valid_body() | {"merge_method": "merge"}
    r = tc.post("/merge", json=body)
    assert r.status_code == 422
    assert captured == {}


def test_merge_rejects_smuggled_required_checks(client) -> None:
    tc, captured = client
    body = _valid_body() | {"required_checks": []}
    r = tc.post("/merge", json=body)
    assert r.status_code == 422
    assert captured == {}


def test_merge_extra_field_rejected(client) -> None:
    tc, _ = client
    body = _valid_body() | {"sneaky_field": "evil"}
    r = tc.post("/merge", json=body)
    assert r.status_code == 422


def test_merge_missing_target_repo_rejected(client) -> None:
    tc, _ = client
    body = _valid_body()
    del body["target_repo"]
    r = tc.post("/merge", json=body)
    assert r.status_code == 422


# Auth (Layer 3) --------------------------------------------------------- #


def test_merge_missing_bearer_returns_401(client) -> None:
    tc, _ = client

    def deny_401():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
        )

    app.dependency_overrides[_verify_caller_dep] = deny_401
    r = tc.post("/merge", json=_valid_body())
    assert r.status_code == 401


def test_merge_caller_not_in_allowlist_returns_403(client) -> None:
    tc, _ = client

    def deny_caller():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="caller service account not allowed",
        )

    app.dependency_overrides[_verify_caller_dep] = deny_caller
    r = tc.post("/merge", json=_valid_body())
    assert r.status_code == 403
