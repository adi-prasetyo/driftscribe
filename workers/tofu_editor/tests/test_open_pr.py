"""Happy-path + auth/schema tests for the tofu-editor ``/open-pr`` handler
(Phase D1-3).

Complements ``test_path_allowlist.py`` (the fail-closed deny suite). Here we
prove the ACCEPT path: a valid multi-file request calls
``ds_github.open_iac_pr`` exactly once with the expected kwargs (branch pinned
``base="main"``, title, body, the file dicts) and the handler returns 200 with
``pr_number`` / ``pr_url`` / ``branch``. Plus the two boundary cases that do
NOT belong in the policy suite: an auth failure (caller-verify raises) → 403,
and an unknown body field (``extra="forbid"``) → 422.
"""
import os

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

# Env MUST be set before importing workers.tofu_editor.main (boot-env reads at
# import time).
os.environ.setdefault("IAC_EDITOR_TARGET_REPO", "adi-prasetyo/driftscribe")
os.environ.setdefault("GITHUB_TOKEN", "test-token")
os.environ.setdefault("OWN_URL", "https://tofu-editor.example.com")
os.environ.setdefault(
    "ALLOWED_CALLERS",
    "driftscribe-agent@test-proj.iam.gserviceaccount.com",
)

from workers.tofu_editor import main as tofu_editor_main  # noqa: E402
from workers.tofu_editor.main import _verify_caller_dep, app  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    """TestClient with the GitHub write seam captured and auth bypassed."""
    captured: list = []

    def fake_open_iac_pr(repo, **kwargs):
        captured.append({"repo": repo, **kwargs})
        return {
            "url": "https://github.com/adi-prasetyo/driftscribe/pull/123",
            "number": 123,
            "branch": kwargs.get("branch"),
            "labeled": True,
            "label_error": None,
            "reused": False,
        }

    sentinel_repo = object()
    monkeypatch.setattr(
        tofu_editor_main.ds_github, "open_iac_pr", fake_open_iac_pr
    )
    monkeypatch.setattr(tofu_editor_main, "_get_repo", lambda: sentinel_repo)

    app.dependency_overrides[_verify_caller_dep] = (
        lambda: "driftscribe-agent@test-proj.iam.gserviceaccount.com"
    )
    yield TestClient(app), captured, sentinel_repo
    app.dependency_overrides.clear()


def _valid_two_file_body() -> dict:
    return {
        "target_repo": "adi-prasetyo/driftscribe",
        "branch": "infra/add-two-files",
        "base": "main",
        "title": "feat(iac): add bucket + readme",
        "body": "Adds a bucket and a README under iac/.",
        "files": [
            {
                "path": "iac/bucket.tf",
                "content": 'resource "google_storage_bucket" "b" {}\n',
            },
            {
                "path": "iac/README.md",
                "content": "# iac\n\nDocs for the new bucket.\n",
            },
        ],
    }


def test_open_pr_happy_multi_file_returns_200(client) -> None:
    tc, captured, sentinel_repo = client
    body = _valid_two_file_body()
    r = tc.post("/open-pr", json=body)
    assert r.status_code == 200, r.text

    # open_iac_pr called exactly once.
    assert len(captured) == 1
    call = captured[0]

    # repo is the seam's sentinel (no real GitHub client constructed).
    assert call["repo"] is sentinel_repo
    # base is PINNED to "main" by the handler (belt-and-suspenders).
    assert call["base"] == "main"
    assert call["branch"] == "infra/add-two-files"
    assert call["title"] == "feat(iac): add bucket + readme"
    assert call["body"] == "Adds a bucket and a README under iac/."
    # files passed through as plain dicts (model_dump), both of them.
    assert call["files"] == [
        {
            "path": "iac/bucket.tf",
            "content": 'resource "google_storage_bucket" "b" {}\n',
        },
        {
            "path": "iac/README.md",
            "content": "# iac\n\nDocs for the new bucket.\n",
        },
    ]

    # Handler response shape.
    data = r.json()
    assert data == {
        "status": "opened",
        "pr_number": 123,
        "pr_url": "https://github.com/adi-prasetyo/driftscribe/pull/123",
        "branch": "infra/add-two-files",
        "plan_builder_dispatched": False,
    }


def test_caller_verify_failure_returns_403(client) -> None:
    tc, captured, _sentinel = client

    def _raise_403():
        raise HTTPException(status_code=403, detail="forbidden caller")

    app.dependency_overrides[_verify_caller_dep] = _raise_403
    r = tc.post("/open-pr", json=_valid_two_file_body())
    assert r.status_code == 403, r.text
    # Auth runs before any GitHub side effect.
    assert captured == []


def test_unknown_field_returns_422(client) -> None:
    tc, captured, _sentinel = client
    body = _valid_two_file_body() | {"surprise": "extra"}
    r = tc.post("/open-pr", json=body)
    assert r.status_code == 422, r.text
    assert captured == []
