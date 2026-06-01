"""Layer 2 fail-closed policy tests for the tofu-editor worker (Phase D1-3).

Mirrors ``workers/upgrade_docs/tests/test_path_allowlist.py`` — env seeded
before import, auth dependency overridden, the GitHub write seam
monkey-patched with a capture. These tests pin the policy surface that
``/open-pr`` MUST enforce BEFORE any GitHub call: ``target_repo``, file-write
path allowlist (``iac/``-only, ``.tf``/``.md`` suffix, foundation guard,
traversal), ``branch``, ``base``, empty/oversize bounds.

For EVERY rejected input the capture list MUST stay empty (proving policy
short-circuited before any GitHub side effect) AND the status code is 403
(policy) or 422 (schema-shaped). This is a NEW WRITE SURFACE; fail-closed
correctness is the whole point of the worker, so this is the canonical suite.
"""
import os
import subprocess
import sys
import textwrap

import pytest
from fastapi.testclient import TestClient

# Env MUST be set before importing workers.tofu_editor.main — the module reads
# IAC_EDITOR_TARGET_REPO / GITHUB_TOKEN / OWN_URL / ALLOWED_CALLERS at import
# time and KeyErrors if any are missing.
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
    """TestClient with the GitHub write seam stubbed and auth bypassed.

    ``captured`` is a list the fake ``open_iac_pr`` appends its kwargs to —
    in the path-allowlist tests we assert ``captured == []`` to prove policy
    short-circuited before any GitHub side effect. ``_get_repo`` is also
    stubbed to a sentinel so no real GitHub client is ever constructed.
    """
    captured: list = []

    def fake_open_iac_pr(repo, **kwargs):
        captured.append({"repo": repo, **kwargs})
        return {
            "url": "https://github.com/adi-prasetyo/driftscribe/pull/77",
            "number": 77,
            "branch": kwargs.get("branch"),
            "labeled": True,
            "label_error": None,
            "reused": False,
        }

    monkeypatch.setattr(
        tofu_editor_main.ds_github, "open_iac_pr", fake_open_iac_pr
    )
    monkeypatch.setattr(tofu_editor_main, "_get_repo", lambda: object())

    app.dependency_overrides[_verify_caller_dep] = (
        lambda: "driftscribe-agent@test-proj.iam.gserviceaccount.com"
    )
    yield TestClient(app), captured
    app.dependency_overrides.clear()


def _valid_body() -> dict:
    return {
        "target_repo": "adi-prasetyo/driftscribe",
        "branch": "infra/add-bucket",
        "base": "main",
        "title": "feat(iac): add a storage bucket",
        "body": "Adds one google_storage_bucket under iac/.",
        "files": [
            {
                "path": "iac/bucket.tf",
                "content": 'resource "google_storage_bucket" "b" {}\n',
            }
        ],
    }


# path allowlist tests --------------------------------------------------- #


def test_path_outside_iac_returns_403(client) -> None:
    tc, captured = client
    body = _valid_body()
    body["files"] = [{"path": "agent/x.py", "content": "print('x')\n"}]
    r = tc.post("/open-pr", json=body)
    assert r.status_code == 403, r.text
    assert captured == []


def test_path_bad_suffix_returns_403(client) -> None:
    tc, captured = client
    body = _valid_body()
    body["files"] = [{"path": "iac/x.sh", "content": "echo hi\n"}]
    r = tc.post("/open-pr", json=body)
    assert r.status_code == 403, r.text
    assert captured == []


def test_path_foundation_file_returns_403(client) -> None:
    # iac/versions.tf is in PROTECTED_FOUNDATION (operator-only).
    tc, captured = client
    body = _valid_body()
    body["files"] = [
        {"path": "iac/versions.tf", "content": 'terraform {}\n'}
    ]
    r = tc.post("/open-pr", json=body)
    assert r.status_code == 403, r.text
    assert captured == []


def test_path_traversal_dotdot_returns_403(client) -> None:
    tc, captured = client
    body = _valid_body()
    body["files"] = [{"path": "iac/../x.tf", "content": 'x = 1\n'}]
    r = tc.post("/open-pr", json=body)
    assert r.status_code == 403, r.text
    assert captured == []


# base allowlist test ---------------------------------------------------- #


def test_base_not_main_returns_403(client) -> None:
    tc, captured = client
    body = _valid_body() | {"base": "dev"}
    r = tc.post("/open-pr", json=body)
    assert r.status_code == 403, r.text
    assert captured == []


# branch allowlist tests ------------------------------------------------- #


def test_branch_wrong_prefix_returns_403(client) -> None:
    tc, captured = client
    body = _valid_body() | {"branch": "upgrade/x"}
    r = tc.post("/open-pr", json=body)
    assert r.status_code == 403, r.text
    assert captured == []


def test_branch_with_double_dot_returns_403(client) -> None:
    tc, captured = client
    body = _valid_body() | {"branch": "infra/.."}
    r = tc.post("/open-pr", json=body)
    assert r.status_code == 403, r.text
    assert captured == []


# target_repo allowlist test --------------------------------------------- #


def test_foreign_target_repo_returns_403(client) -> None:
    tc, captured = client
    body = _valid_body() | {"target_repo": "attacker/repo"}
    r = tc.post("/open-pr", json=body)
    assert r.status_code == 403, r.text
    assert "target_repo" in r.json()["detail"]
    assert captured == []


# empty / oversize bounds ------------------------------------------------ #


def test_empty_files_returns_422(client) -> None:
    tc, captured = client
    body = _valid_body() | {"files": []}
    r = tc.post("/open-pr", json=body)
    assert r.status_code == 422, r.text
    assert captured == []


def test_oversize_content_returns_422(client) -> None:
    # One file > MAX_FILE_BYTES (200_000) → schema-shaped reject (422).
    tc, captured = client
    body = _valid_body()
    body["files"] = [
        {"path": "iac/big.tf", "content": "x" * 200_001}
    ]
    r = tc.post("/open-pr", json=body)
    assert r.status_code == 422, r.text
    assert captured == []


def test_oversize_title_returns_422(client) -> None:
    # title > MAX_TITLE (200) → 422.
    tc, captured = client
    body = _valid_body() | {"title": "T" * 201}
    r = tc.post("/open-pr", json=body)
    assert r.status_code == 422, r.text
    assert captured == []


def test_oversize_body_returns_422(client) -> None:
    # body > MAX_BODY (20_000) → 422.
    tc, captured = client
    body = _valid_body() | {"body": "B" * 20_001}
    r = tc.post("/open-pr", json=body)
    assert r.status_code == 422, r.text
    assert captured == []


def test_non_ascii_path_returns_403(client) -> None:
    # Fullwidth 'x' carries a .tf suffix and survives normpath but is a
    # confusable — the ASCII path-char allowlist rejects it (403).
    tc, captured = client
    body = _valid_body()
    body["files"] = [{"path": "iac/ｘ.tf", "content": 'x = 1\n'}]
    r = tc.post("/open-pr", json=body)
    assert r.status_code == 403, r.text
    assert captured == []


# Boot-time guard -------------------------------------------------------- #


def test_empty_allowed_callers_fails_to_boot() -> None:
    """An empty ``ALLOWED_CALLERS`` must fail the revision at import time —
    refusing to ship a worker that can never authenticate any caller. Spawn a
    clean subprocess so the assertion is on the worker's own boot, not this
    test process (which already imported the module with a valid value)."""
    script = textwrap.dedent(
        """
        import os
        os.environ["IAC_EDITOR_TARGET_REPO"] = "adi-prasetyo/driftscribe"
        os.environ["GITHUB_TOKEN"] = "test-token"
        os.environ["OWN_URL"] = "https://tofu-editor.example.com"
        os.environ["ALLOWED_CALLERS"] = ""   # empty → must raise at boot
        import workers.tofu_editor.main  # noqa: F401
        """
    ).strip()
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        ))),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, (
        "tofu-editor worker booted with an empty ALLOWED_CALLERS set — it "
        f"must fail-closed at import time.\nstdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    assert "ALLOWED_CALLERS" in result.stderr, result.stderr
