"""Tests for the optional plan-builder dispatch on /open-pr (Task 2).

Covers the dispatch_plan_builder gate: only dispatches when:
 - dispatch_plan_builder=True AND
 - the PR is newly opened (not reused)
 - dispatch_workflow succeeds

Dispatch is fail-soft: any exception → plan_builder_dispatched=False, PR still returned.
Worker hardcodes workflow/ref/inputs — request body cannot influence them.
"""
import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("IAC_EDITOR_TARGET_REPO", "adi-prasetyo/driftscribe")
os.environ.setdefault("GITHUB_TOKEN", "test-token")
os.environ.setdefault("OWN_URL", "https://tofu-editor.example.com")
os.environ.setdefault(
    "ALLOWED_CALLERS",
    "driftscribe-agent@test-proj.iam.gserviceaccount.com",
)

from workers.tofu_editor import main as tofu_editor_main  # noqa: E402
from workers.tofu_editor.main import _verify_caller_dep, app  # noqa: E402


def _auth_override():
    app.dependency_overrides[_verify_caller_dep] = (
        lambda: "driftscribe-agent@test-proj.iam.gserviceaccount.com"
    )


def _clear_overrides():
    app.dependency_overrides.clear()


def _valid_body(*, dispatch_plan_builder: bool = False) -> dict:
    return {
        "target_repo": "adi-prasetyo/driftscribe",
        "branch": "infra/dispatch-test",
        "base": "main",
        "title": "feat(iac): dispatch test",
        "body": "Testing dispatch.",
        "files": [
            {
                "path": "iac/test.tf",
                "content": 'resource "google_storage_bucket" "b" {}\n',
            }
        ],
        "dispatch_plan_builder": dispatch_plan_builder,
    }


def _fake_pr_result(*, reused: bool = False) -> dict:
    return {
        "url": "https://github.com/adi-prasetyo/driftscribe/pull/99",
        "number": 99,
        "branch": "infra/dispatch-test",
        "labeled": True,
        "label_error": None,
        "reused": reused,
    }


@pytest.fixture(autouse=True)
def auth(monkeypatch):
    _auth_override()
    yield
    _clear_overrides()


def test_dispatch_called_on_new_pr_when_flag_true(monkeypatch):
    """dispatch_plan_builder=True + new PR → dispatch_workflow called with HARDCODED args."""
    dispatch_calls = []

    monkeypatch.setattr(tofu_editor_main.ds_github, "open_iac_pr", lambda repo, **kw: _fake_pr_result())
    monkeypatch.setattr(tofu_editor_main, "_get_repo", lambda: object())
    monkeypatch.setattr(
        tofu_editor_main.ds_github,
        "dispatch_workflow",
        lambda repo, workflow_filename, ref, inputs: dispatch_calls.append(
            {"workflow_filename": workflow_filename, "ref": ref, "inputs": inputs}
        ),
    )

    tc = TestClient(app)
    r = tc.post("/open-pr", json=_valid_body(dispatch_plan_builder=True))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["plan_builder_dispatched"] is True
    # Assert hardcoded args — request body cannot influence workflow/ref/inputs
    assert len(dispatch_calls) == 1
    assert dispatch_calls[0]["workflow_filename"] == "iac.yml"
    assert dispatch_calls[0]["ref"] == "main"
    assert dispatch_calls[0]["inputs"] == {"pr_number": "99"}


def test_dispatch_not_called_when_flag_false(monkeypatch):
    """dispatch_plan_builder=False → dispatch_workflow NOT called."""
    dispatch_calls = []

    monkeypatch.setattr(tofu_editor_main.ds_github, "open_iac_pr", lambda repo, **kw: _fake_pr_result())
    monkeypatch.setattr(tofu_editor_main, "_get_repo", lambda: object())
    monkeypatch.setattr(
        tofu_editor_main.ds_github,
        "dispatch_workflow",
        lambda *a, **kw: dispatch_calls.append(a),
    )

    tc = TestClient(app)
    r = tc.post("/open-pr", json=_valid_body(dispatch_plan_builder=False))
    assert r.status_code == 200, r.text
    assert r.json()["plan_builder_dispatched"] is False
    assert dispatch_calls == []


def test_dispatch_not_called_on_reused_pr(monkeypatch):
    """dispatch_plan_builder=True but PR is reused → dispatch_workflow NOT called."""
    dispatch_calls = []

    monkeypatch.setattr(tofu_editor_main.ds_github, "open_iac_pr", lambda repo, **kw: _fake_pr_result(reused=True))
    monkeypatch.setattr(tofu_editor_main, "_get_repo", lambda: object())
    monkeypatch.setattr(
        tofu_editor_main.ds_github,
        "dispatch_workflow",
        lambda *a, **kw: dispatch_calls.append(a),
    )

    tc = TestClient(app)
    r = tc.post("/open-pr", json=_valid_body(dispatch_plan_builder=True))
    assert r.status_code == 200, r.text
    assert r.json()["plan_builder_dispatched"] is False
    assert dispatch_calls == []


def test_dispatch_fail_soft(monkeypatch):
    """dispatch_workflow raises → response still 200/opened, plan_builder_dispatched=False."""
    monkeypatch.setattr(tofu_editor_main.ds_github, "open_iac_pr", lambda repo, **kw: _fake_pr_result())
    monkeypatch.setattr(tofu_editor_main, "_get_repo", lambda: object())

    def _raise(*a, **kw):
        raise RuntimeError("GitHub API error")

    monkeypatch.setattr(tofu_editor_main.ds_github, "dispatch_workflow", _raise)

    tc = TestClient(app)
    r = tc.post("/open-pr", json=_valid_body(dispatch_plan_builder=True))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "opened"
    assert data["plan_builder_dispatched"] is False
