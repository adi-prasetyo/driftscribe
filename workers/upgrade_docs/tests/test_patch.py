"""End-to-end tests for the Upgrade Docs ``/patch`` endpoint (Phase 17.C.3).

Mirrors ``workers/docs/tests/test_patch.py`` and the
``workers/upgrade_reader`` test layout. The fixture stubs both the
``_read_lockfile`` seam (so we never call github.com to read the file)
and the ``open_docs_pr`` helper (so we never call github.com to write
the PR). Tests assert that the worker:

- bumps ONLY ``dependencies[package_name]`` to the requested version;
- preserves every other key in the lockfile byte-identically;
- cites the ``advisory_url`` in the PR body verbatim;
- 422s if ``package_name`` isn't already in the lockfile (minimal
  safety net — fuller semver / range checks land in 17.C.3a);
- enforces the auth + isolation contracts shared by all DriftScribe
  workers.
"""
import json
import os
import subprocess
import sys
import textwrap

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


# Lockfile contents used by the default fixture. Defined as a function so
# each test gets a fresh dict and mutation in one test never leaks into
# another via fixture re-use.
def _default_lockfile() -> dict:
    return {
        "name": "phase17-upgrade-demo",
        "version": "1.0.0",
        "scripts": {"start": "node index.js"},
        "dependencies": {"lodash": "4.17.20"},
    }


@pytest.fixture
def client(monkeypatch):
    """TestClient with both GitHub seams stubbed and auth bypassed.

    ``captured`` collects the kwargs the worker forwarded to
    ``open_docs_pr`` so tests can assert on the PR body, new_content,
    branch, base, title, file_path, dry_run, etc.
    """
    captured: dict = {}
    lockfile_holder = {"value": _default_lockfile()}

    def fake_open_docs_pr(**kwargs):
        captured.update(kwargs)
        return {
            "dry_run": False,
            "url": "https://github.com/adi-prasetyo/driftscribe/pull/77",
            "number": 77,
            "labeled": True,
            "label_error": None,
        }

    def fake_read_lockfile(_repo, _path):
        # Return a deep copy so the handler's mutation does not modify the
        # fixture's reference copy — tests that assert "other deps
        # untouched" should compare against a pristine baseline.
        return json.loads(json.dumps(lockfile_holder["value"]))

    monkeypatch.setattr(
        upgrade_docs_main.ds_github, "open_docs_pr", fake_open_docs_pr
    )
    monkeypatch.setattr(upgrade_docs_main, "_get_repo", lambda: object())
    monkeypatch.setattr(upgrade_docs_main, "_read_lockfile", fake_read_lockfile)

    app.dependency_overrides[_verify_caller_dep] = (
        lambda: "coordinator@test-proj.iam.gserviceaccount.com"
    )
    yield TestClient(app), captured, lockfile_holder
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


# Happy path + payload-forwarding ---------------------------------------- #


def test_patch_happy_path_bumps_version_and_cites_advisory(client) -> None:
    tc, captured, _ = client
    r = tc.post("/patch", json=_valid_body())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["number"] == 77
    assert body["labeled"] is True

    # new_content must parse as JSON and reflect the bump.
    parsed = json.loads(captured["new_content"])
    assert parsed["dependencies"]["lodash"] == "4.17.21"

    # PR body cites the advisory URL verbatim.
    assert (
        "https://github.com/advisories/GHSA-35jh-r3h4-6jhm"
        in captured["body"]
    )
    # Operator-supplied prelude is preserved.
    assert "Bumps lodash to address GHSA-35jh-r3h4-6jhm." in captured["body"]

    # Title / branch / base / file_path passed through unchanged.
    assert captured["title"] == "upgrade(lodash): 4.17.20 -> 4.17.21"
    assert captured["branch"] == "upgrade/lodash-4-17-21"
    assert captured["base"] == "main"
    assert captured["file_path"] == "demo/upgrade-target/package.json"
    assert captured["dry_run"] is False

    # JSON serialization conventions: two-space indent + trailing newline,
    # matching what `npm install` itself writes.
    assert captured["new_content"].endswith("\n")
    assert '\n  "dependencies"' in captured["new_content"]


def test_patch_does_not_touch_other_dependencies(client) -> None:
    tc, captured, lockfile_holder = client
    # Add a sibling dep that the request does NOT mention.
    lockfile_holder["value"] = {
        "name": "phase17-upgrade-demo",
        "version": "1.0.0",
        "dependencies": {"lodash": "4.17.20", "express": "4.18.0"},
    }
    r = tc.post("/patch", json=_valid_body())
    assert r.status_code == 200, r.text
    parsed = json.loads(captured["new_content"])
    assert parsed["dependencies"]["lodash"] == "4.17.21"
    assert parsed["dependencies"]["express"] == "4.18.0"


def test_patch_does_not_touch_other_keys(client) -> None:
    tc, captured, lockfile_holder = client
    lockfile_holder["value"] = {
        "name": "phase17-upgrade-demo",
        "version": "1.0.0",
        "scripts": {"start": "node index.js", "test": "vitest"},
        "license": "MIT",
        "dependencies": {"lodash": "4.17.20"},
    }
    r = tc.post("/patch", json=_valid_body())
    assert r.status_code == 200, r.text
    parsed = json.loads(captured["new_content"])
    assert parsed["name"] == "phase17-upgrade-demo"
    assert parsed["version"] == "1.0.0"
    assert parsed["scripts"] == {"start": "node index.js", "test": "vitest"}
    assert parsed["license"] == "MIT"


def test_patch_returns_422_when_package_not_in_dependencies(client) -> None:
    tc, captured, _ = client
    body = _valid_body() | {"package_name": "nonexistent"}
    r = tc.post("/patch", json=body)
    assert r.status_code == 422
    assert "nonexistent" in r.json()["detail"]
    # Policy short-circuit: no GitHub side effect.
    assert captured == {}


# Auth tests (Layer 3) --------------------------------------------------- #


def test_patch_missing_bearer_returns_401(client) -> None:
    tc, _, _ = client

    def deny_401():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
        )

    app.dependency_overrides[_verify_caller_dep] = deny_401
    r = tc.post("/patch", json=_valid_body())
    assert r.status_code == 401


def test_patch_wrong_audience_returns_401(client) -> None:
    tc, _, _ = client

    def deny_audience():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token: audience mismatch",
        )

    app.dependency_overrides[_verify_caller_dep] = deny_audience
    r = tc.post("/patch", json=_valid_body())
    assert r.status_code == 401


def test_patch_caller_not_in_allowlist_returns_403(client) -> None:
    tc, _, _ = client

    def deny_caller():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="caller 'nope@example.com' not in allowed_callers",
        )

    app.dependency_overrides[_verify_caller_dep] = deny_caller
    r = tc.post("/patch", json=_valid_body())
    assert r.status_code == 403


def test_healthz_does_not_require_auth(client) -> None:
    tc, _, _ = client

    def boom():
        raise HTTPException(status_code=401, detail="should not be called")

    app.dependency_overrides[_verify_caller_dep] = boom
    r = tc.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


# Schema tests (Layer 2 — pydantic forbid) ------------------------------- #


def test_extra_field_in_body_rejected(client) -> None:
    tc, _, _ = client
    body = _valid_body() | {"sneaky_field": "evil"}
    r = tc.post("/patch", json=body)
    assert r.status_code == 422, r.text


def test_missing_target_repo_field_rejected(client) -> None:
    tc, _, _ = client
    body = _valid_body()
    del body["target_repo"]
    r = tc.post("/patch", json=body)
    assert r.status_code == 422


# Layer 3 integration ---------------------------------------------------- #


def test_real_verify_caller_dep_wired_with_env(monkeypatch) -> None:
    """Without ``dependency_overrides`` the real ``_verify_caller_dep`` must
    forward OWN_URL + ALLOWED_CALLERS (read from env at boot) to
    ``driftscribe_lib.auth.verify_caller``.

    Mirrors the same integration check used in the other workers — we
    monkeypatch the module-level constants directly because, in a unified
    pytest run, another worker's test module may have populated OWN_URL
    before this module was imported (Python caches the import;
    ``os.environ.setdefault`` at the top of this file would then be a no-op
    and the constant would carry the other worker's value).
    """
    seen: dict = {}

    def fake_verify(request, *, own_url, allowed_callers):
        seen["own_url"] = own_url
        seen["allowed_callers"] = set(allowed_callers)
        return "coordinator@test-proj.iam.gserviceaccount.com"

    monkeypatch.setattr(upgrade_docs_main, "verify_caller", fake_verify)
    monkeypatch.setattr(
        upgrade_docs_main.ds_github,
        "open_docs_pr",
        lambda **kw: {
            "dry_run": False,
            "url": "https://github.com/x/y/pull/1",
            "number": 1,
            "labeled": True,
            "label_error": None,
        },
    )
    monkeypatch.setattr(upgrade_docs_main, "_get_repo", lambda: object())
    monkeypatch.setattr(
        upgrade_docs_main,
        "_read_lockfile",
        lambda _repo, _path: {"dependencies": {"lodash": "4.17.20"}},
    )
    monkeypatch.setattr(
        upgrade_docs_main, "OWN_URL", "https://upgrade-docs.example.com"
    )
    monkeypatch.setattr(
        upgrade_docs_main,
        "ALLOWED_CALLERS",
        frozenset({"coordinator@test-proj.iam.gserviceaccount.com"}),
    )
    # Pin TARGET_REPO so a cross-test mutation can't desync the body.
    monkeypatch.setattr(
        upgrade_docs_main, "TARGET_REPO", "adi-prasetyo/driftscribe"
    )

    c = TestClient(app)
    r = c.post(
        "/patch",
        json=_valid_body(),
        headers={"Authorization": "Bearer faketoken"},
    )
    assert r.status_code == 200
    assert seen["own_url"] == "https://upgrade-docs.example.com"
    assert seen["allowed_callers"] == {
        "coordinator@test-proj.iam.gserviceaccount.com",
    }


# Coordinator-isolation invariant ---------------------------------------- #


def test_worker_does_not_import_coordinator_registry() -> None:
    """Spec-critical invariant: importing ``workers.upgrade_docs.main`` must
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
        os.environ["OWN_URL"] = "https://upgrade-docs.example.com"
        os.environ["ALLOWED_CALLERS"] = "coordinator@test-proj.iam.gserviceaccount.com"

        import workers.upgrade_docs.main  # noqa: F401

        leaked = sorted(
            m for m in sys.modules if m == "agent" or m.startswith("agent.")
        )
        if leaked:
            sys.stderr.write("LEAKED: " + ",".join(leaked) + "\\n")
            sys.exit(1)
        sys.exit(0)
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
    assert result.returncode == 0, (
        "upgrade-docs worker leaked coordinator imports — workers must "
        f"stay isolated from agent.* code.\nstderr: {result.stderr}\n"
        f"stdout: {result.stdout}"
    )
