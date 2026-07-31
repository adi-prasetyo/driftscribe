"""End-to-end tests for the Docs Agent's ``/patch`` endpoint (Phase 11.4).

Mirrors ``workers/reader/tests/test_read.py``: env is seeded before import,
the FastAPI app's dependencies are swapped via ``app.dependency_overrides``,
and the GitHub side-effect (``open_docs_pr``) is monkey-patched on the
``workers.docs.main`` module so we never touch github.com.
"""

import pytest
from fastapi import HTTPException, status
from fastapi.testclient import TestClient
from workers._testenv import import_worker_main

# Canonical boot env, applied before the import below. The values live in
# workers/_testenv.py, not here: worker mains capture config at import and
# Python caches modules, so the FIRST importer in the pytest process decides
# them for everyone (ds-2n1).
import_worker_main("workers.docs.main")

from workers.docs import main as docs_main  # noqa: E402
from workers.docs.main import _verify_caller_dep, app  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    """TestClient with GitHub patching stubbed and auth bypassed.

    ``captured`` is a dict the fake ``open_docs_pr`` fills with the kwargs it
    received, so tests can assert the worker forwarded the right arguments.
    """
    captured: dict = {}

    def fake_open_docs_pr(**kwargs):
        captured.update(kwargs)
        return {
            "dry_run": False,
            "url": "https://github.com/adi-prasetyo/driftscribe/pull/42",
            "number": 42,
            "labeled": True,
            "label_error": None,
        }

    # ``open_docs_pr`` is bound to the worker module via `from … import` at
    # module load. Patch it where it's used, not at its source.
    monkeypatch.setattr(docs_main.ds_github, "open_docs_pr", fake_open_docs_pr)
    # Skip the github.com round-trip that would otherwise happen inside
    # ``_get_repo`` — return a sentinel that the fake ``open_docs_pr`` ignores.
    monkeypatch.setattr(docs_main, "_get_repo", lambda: object())

    # Default to "auth passed" — failure tests override this again.
    app.dependency_overrides[_verify_caller_dep] = (
        lambda: "coordinator@test-proj.iam.gserviceaccount.com"
    )
    yield TestClient(app), captured
    app.dependency_overrides.clear()


def _valid_body() -> dict:
    return {
        "file_path": "demo/docs/runbook.md",
        "new_content": "# Updated runbook\n",
        "branch": "driftscribe/test-1234",
        "base": "main",
        "title": "docs(driftscribe): update runbook",
        "body": "rendered docs PR body",
    }


def test_patch_happy_path(client) -> None:
    tc, captured = client
    r = tc.post("/patch", json=_valid_body())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["number"] == 42
    assert body["labeled"] is True
    assert body["url"].endswith("/pull/42")
    # Worker forwarded the correct args to the GitHub helper.
    assert captured["file_path"] == "demo/docs/runbook.md"
    assert captured["base"] == "main"
    assert captured["branch"] == "driftscribe/test-1234"
    assert captured["dry_run"] is False


def test_invalid_base_rejected(client) -> None:
    tc, captured = client
    body = _valid_body() | {"base": "develop"}
    r = tc.post("/patch", json=body)
    assert r.status_code == 403
    # Layer 2 invariant: policy violation MUST short-circuit before the
    # GitHub side effect runs.
    assert captured == {}


def test_invalid_branch_prefix_rejected(client) -> None:
    tc, captured = client
    body = _valid_body() | {"branch": "feature/foo"}
    r = tc.post("/patch", json=body)
    assert r.status_code == 403
    assert captured == {}


@pytest.mark.parametrize(
    "branch",
    [
        # Empty suffix.
        "driftscribe/",
        # Path traversal in ref name.
        "driftscribe/../main",
        "driftscribe/foo/../bar",
        # Whitespace / control chars.
        "driftscribe/foo bar",
        "driftscribe/foo\nbar",
        "driftscribe/foo\tbar",
        # Disallowed punctuation.
        "driftscribe/foo:bar",
        "driftscribe/foo~bar",
        "driftscribe/foo^bar",
        "driftscribe/foo?bar",
        "driftscribe/foo*bar",
        "driftscribe/foo[bar]",
        # Excessive length.
        "driftscribe/" + "a" * 250,
    ],
)
def test_malformed_branch_rejected(client, branch) -> None:
    tc, captured = client
    body = _valid_body() | {"branch": branch}
    r = tc.post("/patch", json=body)
    assert r.status_code == 403, (branch, r.text)
    assert captured == {}


def test_invalid_path_rejected(client) -> None:
    tc, captured = client
    body = _valid_body() | {"file_path": "agent/main.py"}
    r = tc.post("/patch", json=body)
    assert r.status_code == 403
    assert captured == {}


def test_path_traversal_rejected(client) -> None:
    tc, captured = client
    body = _valid_body() | {"file_path": "demo/docs/../infra/foo.md"}
    r = tc.post("/patch", json=body)
    assert r.status_code == 403
    assert captured == {}


def test_trailing_newline_path_rejected(client) -> None:
    """``re.match`` + ``$`` would let ``demo/docs/runbook.md\\n`` through.
    The worker uses ``fullmatch`` + ``\\Z`` to close that gap."""
    tc, captured = client
    body = _valid_body() | {"file_path": "demo/docs/runbook.md\n"}
    r = tc.post("/patch", json=body)
    assert r.status_code == 403
    assert captured == {}


def test_extra_field_rejected(client) -> None:
    """Layer 2: ``repo`` is hardcoded via env, the caller cannot supply it."""
    tc, _ = client
    body = _valid_body() | {"repo": "attacker/evil"}
    r = tc.post("/patch", json=body)
    assert 400 <= r.status_code < 500


def test_missing_bearer_returns_401(client) -> None:
    tc, _ = client

    def deny_401():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
        )

    app.dependency_overrides[_verify_caller_dep] = deny_401
    r = tc.post("/patch", json=_valid_body())
    assert r.status_code == 401


def test_caller_not_in_allowlist_returns_403(client) -> None:
    tc, _ = client

    def deny_caller():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="caller service account not allowed",
        )

    app.dependency_overrides[_verify_caller_dep] = deny_caller
    r = tc.post("/patch", json=_valid_body())
    assert r.status_code == 403


def test_healthz_does_not_require_auth(client) -> None:
    tc, _ = client

    def boom():
        raise HTTPException(status_code=401, detail="should not be called")

    app.dependency_overrides[_verify_caller_dep] = boom
    r = tc.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_real_verify_caller_dep_wired_with_env(monkeypatch) -> None:
    """Layer 3 integration check: without ``dependency_overrides`` the real
    ``_verify_caller_dep`` must forward OWN_URL + ALLOWED_CALLERS (read from
    env at boot) to ``driftscribe_lib.auth.verify_caller``.

    The constants are read as the module captured them, NOT re-pinned with
    ``monkeypatch.setattr``. Pinning them was how this test used to defend
    against another worker's env winning the import race — but it also meant
    the assertions below only proved the fixture had just written those
    values, so the boot-time capture this test names in its own title went
    unexercised. ``workers/_testenv.py`` removes the race at the source, which
    is what makes reading the real constants safe (ds-2n1).
    """
    seen: dict = {}

    def fake_verify(request, *, own_url, allowed_callers):
        seen["own_url"] = own_url
        seen["allowed_callers"] = set(allowed_callers)
        return "coordinator@test-proj.iam.gserviceaccount.com"

    monkeypatch.setattr(docs_main, "verify_caller", fake_verify)
    # Stub the GitHub side so the handler can complete without network.
    monkeypatch.setattr(
        docs_main.ds_github,
        "open_docs_pr",
        lambda **kw: {
            "dry_run": False,
            "url": "https://github.com/x/y/pull/1",
            "number": 1,
            "labeled": True,
            "label_error": None,
        },
    )
    monkeypatch.setattr(docs_main, "_get_repo", lambda: object())

    # No dependency_overrides — exercise the real _verify_caller_dep.
    c = TestClient(app)
    r = c.post(
        "/patch",
        json=_valid_body(),
        headers={"Authorization": "Bearer faketoken"},
    )
    assert r.status_code == 200
    assert seen["own_url"] == "https://docs.example.com"
    assert seen["allowed_callers"] == {
        "coordinator@test-proj.iam.gserviceaccount.com",
    }
