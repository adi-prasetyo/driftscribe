"""Layer 2 policy tests for the Upgrade Docs worker (Phase 17.C.3).

Mirrors ``workers/docs/tests/test_patch.py`` and
``workers/upgrade_reader/tests/test_read.py`` — env seeded before import,
auth dependency overridden, GitHub side effects monkey-patched. These
tests pin the policy surface: ``target_repo``, ``lockfile_path``,
``branch``, ``base``. Each one MUST short-circuit before any GitHub call
runs.

Status-code convention: this is a write-side worker; all policy
violations return 403 (matching ``workers/docs/main.py``). 422 is
reserved for pydantic schema violations (covered in ``test_patch.py``).
"""
import os

import pytest
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
    """TestClient with GitHub patching stubbed and auth bypassed.

    ``captured`` is a dict the fake ``open_docs_pr`` fills with kwargs —
    in the path-allowlist tests we assert ``captured == {}`` to prove
    policy short-circuited before any GitHub side effect.
    """
    captured: dict = {}

    def fake_open_docs_pr(**kwargs):
        captured.update(kwargs)
        return {
            "dry_run": False,
            "url": "https://github.com/adi-prasetyo/driftscribe/pull/99",
            "number": 99,
            "labeled": True,
            "label_error": None,
        }

    monkeypatch.setattr(
        upgrade_docs_main.ds_github, "open_docs_pr", fake_open_docs_pr
    )
    monkeypatch.setattr(upgrade_docs_main, "_get_repo", lambda: object())
    # Default _read_lockfile so we don't reach github.com if a malformed
    # request slipped through to the read step (it shouldn't — policy is
    # checked first — but a defensive stub keeps test failures clean).
    monkeypatch.setattr(
        upgrade_docs_main,
        "_read_lockfile",
        lambda _repo, _path: (
            {
                "name": "phase17-upgrade-demo",
                "version": "1.0.0",
                "dependencies": {"lodash": "4.17.20"},
            },
            "",
        ),
    )

    app.dependency_overrides[_verify_caller_dep] = (
        lambda: "coordinator@test-proj.iam.gserviceaccount.com"
    )
    yield TestClient(app), captured
    app.dependency_overrides.clear()


def _valid_body() -> dict:
    return {
        "target_repo": "adi-prasetyo/driftscribe",
        "lockfile_path": "demo/upgrade-target/package.json",
        "package_name": "lodash",
        "target_version": "4.17.21",
        "advisory_url": "https://github.com/advisories/GHSA-35jh-r3h4-6jhm",
        "branch": "upgrade/lodash-4-17-21",
        "base": "main",
        "title": "upgrade(lodash): 4.17.20 -> 4.17.21",
        "body": "Bumps lodash to address GHSA-35jh-r3h4-6jhm.",
    }


# lockfile_path tests ----------------------------------------------------- #


def test_lockfile_path_outside_allowlist_returns_403(client) -> None:
    tc, captured = client
    body = _valid_body() | {"lockfile_path": "infra/cloudbuild.yaml"}
    r = tc.post("/patch", json=body)
    assert r.status_code == 403, r.text
    assert captured == {}


def test_lockfile_path_package_lock_rejected_returns_403(client) -> None:
    # `package-lock.json` is a structurally similar npm file but is NOT in
    # the Phase 17 allowlist (single ecosystem, single file).
    tc, captured = client
    body = _valid_body() | {
        "lockfile_path": "demo/upgrade-target/package-lock.json"
    }
    r = tc.post("/patch", json=body)
    assert r.status_code == 403
    assert captured == {}


def test_lockfile_path_traversal_dotdot_returns_403(client) -> None:
    tc, captured = client
    body = _valid_body() | {
        "lockfile_path": "demo/upgrade-target/../infra/cloudbuild.yaml"
    }
    r = tc.post("/patch", json=body)
    assert r.status_code == 403
    assert captured == {}


def test_lockfile_path_traversal_normpath_mismatch_returns_403(client) -> None:
    # `os.path.normpath("demo/upgrade-target/./package.json")` strips the
    # `./` segment, so the normalized form differs from the literal input.
    tc, captured = client
    body = _valid_body() | {
        "lockfile_path": "demo/upgrade-target/./package.json"
    }
    r = tc.post("/patch", json=body)
    assert r.status_code == 403
    assert captured == {}


def test_lockfile_path_double_slash_returns_403(client) -> None:
    # `normpath` collapses `//` to `/`, so the normalized form differs
    # from the literal input → reject.
    tc, captured = client
    body = _valid_body() | {
        "lockfile_path": "demo//upgrade-target/package.json"
    }
    r = tc.post("/patch", json=body)
    assert r.status_code == 403
    assert captured == {}


def test_lockfile_path_absolute_returns_403(client) -> None:
    tc, captured = client
    body = _valid_body() | {"lockfile_path": "/etc/passwd"}
    r = tc.post("/patch", json=body)
    assert r.status_code == 403
    assert captured == {}


def test_lockfile_path_empty_returns_403(client) -> None:
    tc, captured = client
    body = _valid_body() | {"lockfile_path": ""}
    r = tc.post("/patch", json=body)
    assert r.status_code == 403
    assert captured == {}


# target_repo allowlist test --------------------------------------------- #


def test_target_repo_mismatch_returns_403(client) -> None:
    tc, captured = client
    body = _valid_body() | {"target_repo": "attacker/repo"}
    r = tc.post("/patch", json=body)
    assert r.status_code == 403
    assert "target_repo" in r.json()["detail"]
    assert captured == {}


# branch allowlist tests -------------------------------------------------- #


def test_branch_must_start_with_upgrade_prefix_403(client) -> None:
    tc, captured = client
    body = _valid_body() | {"branch": "feature/foo"}
    r = tc.post("/patch", json=body)
    assert r.status_code == 403
    assert captured == {}


def test_branch_with_double_dot_returns_403(client) -> None:
    tc, captured = client
    body = _valid_body() | {"branch": "upgrade/foo..bar"}
    r = tc.post("/patch", json=body)
    assert r.status_code == 403
    assert captured == {}


def test_branch_with_whitespace_returns_403(client) -> None:
    tc, captured = client
    body = _valid_body() | {"branch": "upgrade/foo bar"}
    r = tc.post("/patch", json=body)
    assert r.status_code == 403
    assert captured == {}


def test_branch_empty_suffix_returns_403(client) -> None:
    tc, captured = client
    body = _valid_body() | {"branch": "upgrade/"}
    r = tc.post("/patch", json=body)
    assert r.status_code == 403
    assert captured == {}


# base allowlist test ----------------------------------------------------- #


def test_base_must_be_main_returns_403(client) -> None:
    tc, captured = client
    body = _valid_body() | {"base": "production"}
    r = tc.post("/patch", json=body)
    assert r.status_code == 403
    assert captured == {}
