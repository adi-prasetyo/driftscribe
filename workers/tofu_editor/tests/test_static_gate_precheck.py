"""In-process AGENT-mode static-gate pre-check tests for ``/open-pr`` (D1-4).

Complements ``test_path_allowlist.py`` (path/branch/base allowlist) and
``test_open_pr.py`` (happy path). Here we pin the CONTENT gate the worker runs
in-process after the path allowlist and before any GitHub call: the SAME
:func:`tools.iac_static_gate.evaluate` CI runs in AGENT mode, so a
content-policy violation (NEW provider, ``module`` block, ``provisioner`` /
arbitrary-execution construct, …) is rejected with **422** and ZERO GitHub
side effect — exactly like CI would reject the resulting PR, but fail-fast
before the PR exists.

The dirty/clean HCL bodies below are reused verbatim from the static-gate's
own suite (``tests/unit/test_iac_static_gate.py``) so the worker test exercises
the real gate behavior and can never drift from a hand-rolled HCL string that
trips an unexpected (or no) rule:

- provisioner body  -> ``test_arbitrary_execution_constructs_rejected``
- ``module`` block  -> ``test_any_module_block_is_rejected``
- clean resource    -> ``test_clean_agent_pr_passes`` (also the happy-path body
  in ``test_open_pr.py``)
"""
import os

import pytest
from fastapi.testclient import TestClient

# Env MUST be set before importing workers.tofu_editor.main (boot-env reads at
# import time and KeyErrors if any of the four are missing).
os.environ.setdefault("IAC_EDITOR_TARGET_REPO", "adi-prasetyo/driftscribe")
os.environ.setdefault("GITHUB_TOKEN", "test-token")
os.environ.setdefault("OWN_URL", "https://tofu-editor.example.com")
os.environ.setdefault(
    "ALLOWED_CALLERS",
    "driftscribe-agent@test-proj.iam.gserviceaccount.com",
)

from workers.tofu_editor import main as tofu_editor_main  # noqa: E402
from workers.tofu_editor.main import _verify_caller_dep, app  # noqa: E402

# Known-DIRTY AGENT-mode HCL bodies, copied verbatim from
# tests/unit/test_iac_static_gate.py so they reliably trip a real gate rule.
DIRTY_PROVISIONER = (
    'resource "google_x" "y" { provisioner "local-exec" { command = "echo hi" } }'
)
DIRTY_MODULE = 'module "vpc" { source = "./vpc" }'

# Known-CLEAN AGENT-mode HCL body (evaluate -> no violations). Same string the
# happy-path test in test_open_pr.py uses, so the pre-check provably does not
# block legitimate HCL.
CLEAN_RESOURCE = 'resource "google_storage_bucket" "b" {}\n'


@pytest.fixture
def client(monkeypatch):
    """TestClient with the GitHub write seam captured and auth bypassed.

    ``captured`` is appended to by the fake ``open_iac_pr``; for a rejected
    request we assert ``captured == []`` to prove the gate short-circuited
    BEFORE any GitHub side effect. ``_get_repo`` is stubbed to a sentinel so no
    real GitHub client is ever constructed.
    """
    captured: list = []

    def fake_open_iac_pr(repo, **kwargs):
        captured.append({"repo": repo, **kwargs})
        return {
            "url": "https://github.com/adi-prasetyo/driftscribe/pull/88",
            "number": 88,
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


def _body_with_tf(content: str) -> dict:
    """A request that PASSES the path/branch/base allowlist (so it reaches the
    static gate) carrying ``content`` as the single iac/ ``.tf`` file."""
    return {
        "target_repo": "adi-prasetyo/driftscribe",
        "branch": "infra/add-resource",
        "base": "main",
        "title": "feat(iac): add a resource",
        "body": "Adds one resource under iac/.",
        "files": [{"path": "iac/x.tf", "content": content}],
    }


def test_provisioner_content_returns_422_no_github_call(client) -> None:
    tc, captured = client
    r = tc.post("/open-pr", json=_body_with_tf(DIRTY_PROVISIONER))
    assert r.status_code == 422, r.text

    detail = r.json()["detail"]
    assert detail["error"] == "static_gate"
    assert detail["violations"], "expected a non-empty violations list"
    assert any(v["rule"] == "arbitrary-execution" for v in detail["violations"])

    # Rejected BEFORE GitHub — no PR opened.
    assert captured == []


def test_module_block_content_returns_422_no_github_call(client) -> None:
    # A second dirty case for a DIFFERENT rule, proving it's the gate doing the
    # rejecting — not one specific banned string.
    tc, captured = client
    r = tc.post("/open-pr", json=_body_with_tf(DIRTY_MODULE))
    assert r.status_code == 422, r.text

    detail = r.json()["detail"]
    assert detail["error"] == "static_gate"
    assert any(
        v["rule"] == "module-block-forbidden" for v in detail["violations"]
    )
    assert captured == []


def test_clean_resource_reaches_github_and_returns_200(client) -> None:
    # The pre-check must NOT block legitimate HCL: a clean google resource
    # passes the gate and reaches open_iac_pr.
    tc, captured = client
    r = tc.post("/open-pr", json=_body_with_tf(CLEAN_RESOURCE))
    assert r.status_code == 200, r.text

    # Exactly one GitHub call — the gate let it through.
    assert len(captured) == 1
    assert captured[0]["files"] == [
        {"path": "iac/x.tf", "content": CLEAN_RESOURCE}
    ]
    assert r.json()["pr_number"] == 88


# ---------------------------------------------------------------------------
# Phase-2 import admission: in-process gate checks (Codex important #2)
# ---------------------------------------------------------------------------

# A valid adopt pair (bucket resource + co-located import block, plain literal id).
CLEAN_ADOPT_PAIR = (
    'resource "google_storage_bucket" "old_uploads" {\n'
    '  name     = "my-old-uploads"\n'
    '  location = "ASIA-NORTHEAST1"\n'
    '}\n'
    'import {\n'
    '  to = google_storage_bucket.old_uploads\n'
    '  id = "my-old-uploads"\n'
    '}\n'
)

# A bad import block: id uses a variable expression → import-id-not-literal.
BAD_IMPORT_ID_VAR = (
    'resource "google_storage_bucket" "b" {}\n'
    'import { to = google_storage_bucket.b  id = var.bucket_name }\n'
)


def test_adopt_pair_reaches_github(client) -> None:
    """A well-formed adopt pair (import + matching resource, literal id) must
    pass the in-process gate and reach the fake GitHub client."""
    tc, captured = client
    r = tc.post("/open-pr", json=_body_with_tf(CLEAN_ADOPT_PAIR))
    assert r.status_code == 200, r.text
    assert len(captured) == 1, "expected exactly one GitHub call"


def test_bad_import_id_returns_422_no_github_call(client) -> None:
    """An import block with a variable id must be rejected with 422 naming
    import-id-not-literal, and no GitHub call must be made."""
    tc, captured = client
    r = tc.post("/open-pr", json=_body_with_tf(BAD_IMPORT_ID_VAR))
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "static_gate"
    assert any(v["rule"] == "import-id-not-literal" for v in detail["violations"])
    assert captured == [], "no GitHub call should be made when gate rejects"
