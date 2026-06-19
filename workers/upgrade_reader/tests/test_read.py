"""Tests for the Upgrade Reader worker (Phase 17.C.2).

Mirrors ``workers/reader/tests/test_read.py``: env is seeded before import,
the FastAPI app's dependencies are swapped via ``app.dependency_overrides``,
and the GitHub side-effects (``_get_repo`` / ``_read_lockfile`` /
``_lookup_advisories``) are monkey-patched on ``workers.upgrade_reader.main``
so we never touch github.com.

Covers the contract laid out in the plan:

- Empty body / extra field / missing field → 4xx (Layer 2 payload-intent).
- ``target_repo`` mismatch against env allowlist → 400.
- ``lockfile_path`` outside the regex / containing ``..`` / non-normalized
  → 400.
- Missing bearer / wrong audience → 401; wrong caller email → 403.
- ``/healthz`` is unauthenticated.
- Coordinator-isolation invariant: importing the worker module MUST NOT
  pull any ``agent.*`` module into ``sys.modules``.
"""
import os
import subprocess
import sys
import textwrap

import pytest
from fastapi import HTTPException, status
from fastapi.testclient import TestClient

# Env MUST be set before importing workers.upgrade_reader.main — the module
# reads UPGRADE_TARGET_REPO / GITHUB_TOKEN / GCP_PROJECT / OWN_URL /
# ALLOWED_CALLERS at import time and KeyErrors if any are missing. This
# mirrors the production fail-fast behavior.
os.environ.setdefault("UPGRADE_TARGET_REPO", "adi-prasetyo/driftscribe")
os.environ.setdefault("GITHUB_TOKEN", "test-token")
os.environ.setdefault("GCP_PROJECT", "test-proj")
os.environ.setdefault("OWN_URL", "https://upgrade-reader.example.com")
os.environ.setdefault(
    "ALLOWED_CALLERS",
    "coordinator@test-proj.iam.gserviceaccount.com",
)

from workers.upgrade_reader import main as upgrade_reader_main  # noqa: E402
from workers.upgrade_reader.main import _verify_caller_dep, app  # noqa: E402


# Stub objects shared by tests --------------------------------------------- #


class _StubRepo:
    """Sentinel — the patched ``_read_lockfile`` ignores the repo arg."""


def _stub_lockfile(_repo, _path):
    return {
        "name": "phase17-upgrade-demo",
        "version": "1.0.0",
        "dependencies": {"lodash": "4.17.20"},
    }


def _stub_no_advisories(_name, _version):
    return []


def _stub_one_advisory(_name, _version):
    return [
        {
            "ghsa_id": "GHSA-35jh-r3h4-6jhm",
            "severity": "medium",
            "url": "https://github.com/advisories/GHSA-35jh-r3h4-6jhm",
            "summary": "Command Injection in lodash",
        }
    ]


@pytest.fixture
def client(monkeypatch):
    """TestClient with GitHub/HTTP patching stubbed and auth bypassed.

    Tests override individual seams (e.g. ``_lookup_advisories``) on top of
    this baseline when they need different behavior.
    """
    monkeypatch.setattr(upgrade_reader_main, "_get_repo", lambda: _StubRepo())
    monkeypatch.setattr(upgrade_reader_main, "_read_lockfile", _stub_lockfile)
    monkeypatch.setattr(
        upgrade_reader_main, "_lookup_advisories", _stub_one_advisory
    )

    # Default to "auth passed" — failure tests override this again.
    app.dependency_overrides[_verify_caller_dep] = (
        lambda: "coordinator@test-proj.iam.gserviceaccount.com"
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def _valid_body() -> dict:
    return {
        "target_repo": "adi-prasetyo/driftscribe",
        "lockfile_path": "demo/upgrade-target/package.json",
    }


# Happy path + healthz ----------------------------------------------------- #


def test_read_happy_path(client) -> None:
    r = client.post("/read", json=_valid_body())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["target_repo"] == "adi-prasetyo/driftscribe"
    assert body["lockfile_path"] == "demo/upgrade-target/package.json"
    assert isinstance(body["dependencies"], list)
    assert len(body["dependencies"]) == 1
    dep = body["dependencies"][0]
    assert dep["name"] == "lodash"
    assert dep["version"] == "4.17.20"
    assert dep["advisories"][0]["ghsa_id"] == "GHSA-35jh-r3h4-6jhm"


def test_healthz_does_not_require_auth(client) -> None:
    # Even if the dependency would deny, /healthz has no Depends on it.
    def boom():
        raise HTTPException(status_code=401, detail="should not be called")

    app.dependency_overrides[_verify_caller_dep] = boom
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


# Auth tests (Layer 3) ----------------------------------------------------- #


def test_missing_bearer_returns_401(client) -> None:
    def deny_401():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
        )

    app.dependency_overrides[_verify_caller_dep] = deny_401
    r = client.post("/read", json=_valid_body())
    assert r.status_code == 401


def test_wrong_audience_returns_401(client) -> None:
    def deny_audience():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
        )

    app.dependency_overrides[_verify_caller_dep] = deny_audience
    r = client.post("/read", json=_valid_body())
    assert r.status_code == 401


def test_caller_not_in_allowlist_returns_403(client) -> None:
    def deny_caller():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="caller service account not allowed",
        )

    app.dependency_overrides[_verify_caller_dep] = deny_caller
    r = client.post("/read", json=_valid_body())
    assert r.status_code == 403


def test_real_verify_caller_dep_wired_with_env(monkeypatch) -> None:
    """Layer 3 integration check (Codex review pattern): without
    dependency_overrides, the real ``_verify_caller_dep`` must call
    ``verify_caller`` with the ``OWN_URL`` and ``ALLOWED_CALLERS`` read from
    env at boot.

    We monkeypatch the module-level constants rather than relying on the
    import-time env read because, in a unified pytest run, another worker's
    test module may have populated ``OWN_URL`` before this module was
    imported.
    """
    seen: dict = {}

    def fake_verify(request, *, own_url, allowed_callers):
        seen["own_url"] = own_url
        seen["allowed_callers"] = set(allowed_callers)
        return "coordinator@test-proj.iam.gserviceaccount.com"

    monkeypatch.setattr(upgrade_reader_main, "verify_caller", fake_verify)
    monkeypatch.setattr(upgrade_reader_main, "_get_repo", lambda: _StubRepo())
    monkeypatch.setattr(upgrade_reader_main, "_read_lockfile", _stub_lockfile)
    monkeypatch.setattr(
        upgrade_reader_main, "_lookup_advisories", _stub_no_advisories
    )
    monkeypatch.setattr(
        upgrade_reader_main, "OWN_URL", "https://upgrade-reader.example.com"
    )
    monkeypatch.setattr(
        upgrade_reader_main,
        "ALLOWED_CALLERS",
        frozenset({"coordinator@test-proj.iam.gserviceaccount.com"}),
    )
    # Also pin TARGET_REPO so a cross-test mutation can't desync the body.
    monkeypatch.setattr(
        upgrade_reader_main, "TARGET_REPO", "adi-prasetyo/driftscribe"
    )

    # No dependency_overrides — exercise the real _verify_caller_dep.
    c = TestClient(app)
    r = c.post(
        "/read",
        json=_valid_body(),
        headers={"Authorization": "Bearer faketoken"},
    )
    assert r.status_code == 200
    assert seen["own_url"] == "https://upgrade-reader.example.com"
    assert seen["allowed_callers"] == {
        "coordinator@test-proj.iam.gserviceaccount.com",
    }


# Schema tests (Layer 2 — pydantic forbid) --------------------------------- #


def test_extra_field_in_body_rejected(client) -> None:
    body = _valid_body() | {"advisory_source": "github"}
    r = client.post("/read", json=body)
    assert 400 <= r.status_code < 500, (
        f"expected 4xx for extra field, got {r.status_code}: {r.text}"
    )


def test_missing_target_repo_rejected(client) -> None:
    body = {"lockfile_path": "demo/upgrade-target/package.json"}
    r = client.post("/read", json=body)
    assert r.status_code == 422


def test_missing_lockfile_path_rejected(client) -> None:
    body = {"target_repo": "adi-prasetyo/driftscribe"}
    r = client.post("/read", json=body)
    assert r.status_code == 422


# target_repo allowlist tests --------------------------------------------- #


def test_target_repo_mismatch_returns_400(client) -> None:
    body = _valid_body() | {"target_repo": "attacker/repo"}
    r = client.post("/read", json=body)
    assert r.status_code == 400
    assert "target_repo" in r.json()["detail"]


def test_target_repo_match_succeeds(client) -> None:
    # Sanity check that the same env-pinned value the other tests rely on
    # is what the happy-path body uses.
    r = client.post("/read", json=_valid_body())
    assert r.status_code == 200


# lockfile_path allowlist tests (Layer 2 — path policy) ------------------- #


def test_lockfile_path_outside_allowlist_returns_400(client) -> None:
    body = _valid_body() | {"lockfile_path": "infra/cloudbuild.yaml"}
    r = client.post("/read", json=body)
    assert r.status_code == 400


def test_lockfile_path_traversal_dotdot_returns_400(client) -> None:
    body = _valid_body() | {
        "lockfile_path": "demo/upgrade-target/../infra/cloudbuild.yaml"
    }
    r = client.post("/read", json=body)
    assert r.status_code == 400


def test_lockfile_path_traversal_normpath_mismatch_returns_400(client) -> None:
    # `os.path.normpath("demo/upgrade-target/./package.json")` strips the
    # `./` segment, so the normalized form differs from the literal input.
    body = _valid_body() | {
        "lockfile_path": "demo/upgrade-target/./package.json"
    }
    r = client.post("/read", json=body)
    assert r.status_code == 400


def test_lockfile_path_double_slash_returns_400(client) -> None:
    # `normpath` collapses `//` to `/`, so the normalized form differs
    # from the literal input → reject.
    body = _valid_body() | {
        "lockfile_path": "demo//upgrade-target/package.json"
    }
    r = client.post("/read", json=body)
    assert r.status_code == 400


def test_lockfile_path_package_lock_rejected_returns_400(client) -> None:
    # `package-lock.json` is a structurally similar npm file but is NOT in
    # the Phase 17 allowlist (single ecosystem, single file). The regex
    # rejects it cleanly.
    body = _valid_body() | {
        "lockfile_path": "demo/upgrade-target/package-lock.json"
    }
    r = client.post("/read", json=body)
    assert r.status_code == 400


# Advisory mapping tests --------------------------------------------------- #


def test_dependency_with_matching_advisory_returns_advisory_block(
    client, monkeypatch
) -> None:
    monkeypatch.setattr(
        upgrade_reader_main, "_lookup_advisories", _stub_one_advisory
    )
    r = client.post("/read", json=_valid_body())
    assert r.status_code == 200
    deps = r.json()["dependencies"]
    assert len(deps) == 1
    advisories = deps[0]["advisories"]
    assert len(advisories) == 1
    assert advisories[0]["ghsa_id"] == "GHSA-35jh-r3h4-6jhm"
    assert advisories[0]["severity"] == "medium"
    assert advisories[0]["url"].startswith("https://github.com/advisories/")
    assert "lodash" in advisories[0]["summary"]


def test_dependency_without_advisory_returns_empty_advisories_list(
    client, monkeypatch
) -> None:
    monkeypatch.setattr(
        upgrade_reader_main, "_lookup_advisories", _stub_no_advisories
    )
    r = client.post("/read", json=_valid_body())
    assert r.status_code == 200
    deps = r.json()["dependencies"]
    assert len(deps) == 1
    assert deps[0]["advisories"] == []


# Coordinator-isolation invariant ----------------------------------------- #


def test_worker_does_not_import_coordinator_registry() -> None:
    """Spec-critical invariant: importing ``workers.upgrade_reader.main`` must
    NOT pull any ``agent.*`` module into ``sys.modules``.

    Why this matters: ``agent.workloads.registry`` (and the rest of the
    ``agent.*`` package) drags in coordinator-only deps via
    ``agent.adk_tools``. Workers stay isolated from coordinator authority
    code — they bundle only ``driftscribe_lib/`` + their worker source. See
    the long comment at ``agent/workloads/registry.py:429-440`` for the
    full rationale.

    Implementation note: a simple ``sys.modules`` check inside the running
    pytest session would be flaky — other test files (e.g. the agent
    tests) will have already imported ``agent.*`` modules earlier in the
    same Python process. We spawn a clean Python subprocess that imports
    ONLY the worker module and then inspects its own ``sys.modules``. If
    a future change adds ``from agent...`` anywhere in the worker's chain,
    this subprocess will surface it loudly.
    """
    script = textwrap.dedent(
        """
        import os
        import sys

        # Required env for boot-time module load — match the values seeded
        # by the test module so the worker imports cleanly in the subprocess.
        os.environ["UPGRADE_TARGET_REPO"] = "adi-prasetyo/driftscribe"
        os.environ["GITHUB_TOKEN"] = "test-token"
        os.environ["GCP_PROJECT"] = "test-proj"
        os.environ["OWN_URL"] = "https://upgrade-reader.example.com"
        os.environ["ALLOWED_CALLERS"] = "coordinator@test-proj.iam.gserviceaccount.com"

        import workers.upgrade_reader.main  # noqa: F401

        leaked = sorted(
            m for m in sys.modules if m == "agent" or m.startswith("agent.")
        )
        if leaked:
            sys.stderr.write("LEAKED: " + ",".join(leaked) + "\\n")
            sys.exit(1)
        sys.exit(0)
        """
    ).strip()
    # Use the same interpreter that's running pytest so imports resolve
    # against this venv. ``cwd`` = repo root so ``workers.upgrade_reader``
    # is importable. ``PYTHONPATH`` is inherited via the default env.
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        ))),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "upgrade-reader worker leaked coordinator imports — workers must "
        f"stay isolated from agent.* code.\nstderr: {result.stderr}\n"
        f"stdout: {result.stdout}"
    )
