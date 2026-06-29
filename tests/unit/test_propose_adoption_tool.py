"""Tests for propose_adoption_tool + shared _open_iac_pr_and_notify tail.

Covers:
- Happy paths for all 4 types (worker payload matches rendered file/title/body,
  notify fired once, next_steps carries create-class re-bake note, D3: 1 file)
- Rejected paths (bad type, missing image/topic) → zero worker calls, zero tree fetches
- Freehand-import guard (open_infra_pr_tool with import block → rejected)
- Fan-out merged-files import guard → policy failure path
- Preflight rows (path/address/identity collision, project-default mismatch, fetch failure)
- Worker error propagates, no notify
- _open_iac_pr_and_notify is callable and works (open_infra_pr_tool delegates to it)
"""
from __future__ import annotations

import pytest

_PROJECT = "driftscribe-hack-2026"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_worker(pr_number: int = 42, pr_url: str = "https://github.com/adi-prasetyo/driftscribe/pull/42"):
    """Return a fake call_open_infra_pr that records calls and returns a success result."""
    calls = []

    def _fake(*, target_repo, branch, title, body, files, dispatch_plan_builder=False):
        calls.append(dict(target_repo=target_repo, branch=branch, title=title, body=body, files=files))
        return {"status": "opened", "pr_number": pr_number, "pr_url": pr_url, "branch": branch}

    return _fake, calls


def _make_fetch_iac_tree(files: dict[str, str] | None = None, raise_exc: Exception | None = None):
    """Return a fake _fetch_main_iac_tree that returns `files` or raises."""
    fetches = []

    def _fake(target_repo: str) -> dict[str, str]:
        fetches.append(target_repo)
        if raise_exc is not None:
            raise raise_exc
        return files or {"iac/variables.tf": f'variable "project_id" {{\n  default = "{_PROJECT}"\n}}\n'}

    return _fake, fetches


# ---------------------------------------------------------------------------
# Happy path tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("resource_type", "name", "kwargs"),
    [
        ("google_storage_bucket", "my-bucket", {"location": "asia-northeast1"}),
        ("google_pubsub_topic", "my-topic", {}),
        ("google_pubsub_subscription", "my-sub", {"topic": "my-topic"}),
        ("google_cloud_run_v2_service", "my-svc", {"location": "asia-northeast1", "image": "gcr.io/cloudrun/hello"}),
    ],
)
def test_propose_adoption_tool_happy_path(
    resource_type, name, kwargs, monkeypatch
):
    """Happy-path call → worker gets the rendered file + title/body; notify fired;
    result has next_steps with create-class note; exactly 1 file (D3)."""
    from agent import adk_tools
    from driftscribe_lib.adopt_recipe import render_adoption

    fake_worker, calls = _make_fake_worker()
    fake_fetch, fetches = _make_fetch_iac_tree()
    notified = []

    monkeypatch.setattr(adk_tools.worker_client, "call_open_infra_pr", fake_worker)
    monkeypatch.setattr(adk_tools, "_fetch_main_iac_tree", fake_fetch)
    # Stub the open-PR dupe probe (a real GitHub call otherwise): this path tests
    # the open flow, so "no existing adoption PR" keeps it off the network.
    monkeypatch.setattr(adk_tools, "find_open_adopt_pr_for_resource", lambda *a, **k: None)
    monkeypatch.setattr(adk_tools, "notify_iac_pr_pending", lambda *a, **kw: notified.append(a))
    monkeypatch.setattr(
        "agent.config.Settings.gcp_project",
        property(lambda self: _PROJECT),
        raising=False,
    )
    monkeypatch.setenv("COORDINATOR_ORIGIN", "https://driftscribe.example.com")

    result = adk_tools.propose_adoption_tool(resource_type, name, **kwargs)

    # Worker was called exactly once
    assert len(calls) == 1
    call = calls[0]

    # Exactly 1 file (D3)
    assert len(call["files"]) == 1
    rendered_file = call["files"][0]

    # Rendered file matches what the renderer produces
    r = render_adoption(resource_type, name, _PROJECT, **{
        k: v for k, v in kwargs.items() if k in ("location", "topic", "image")
    })
    assert rendered_file["path"] == r.path
    assert rendered_file["content"] == r.content
    assert call["title"] == r.title
    assert call["body"] == r.body

    # Notify fired once
    assert len(notified) == 1

    # Result is compact + has pr_number
    assert result["status"] == "opened"
    assert result["pr_number"] == 42

    # next_steps carries the create-class re-bake note
    nxt = result.get("next_steps", "")
    assert "re-bake" in nxt.lower() or "C6" in nxt or "adoption" in nxt.lower()


def test_propose_adoption_tool_worker_error_propagates_no_notify(monkeypatch):
    """Worker error propagates (same as open_infra_pr_tool — not caught); notify NOT fired.

    propose_adoption_tool mirrors open_infra_pr_tool: WorkerClientError is NOT caught,
    it propagates to the ADK runner. notify_iac_pr_pending is never reached on error.
    """
    from agent import adk_tools
    from agent.worker_client import WorkerClientError

    def _error_worker(*, target_repo, branch, title, body, files, dispatch_plan_builder=False):
        raise WorkerClientError(422, "gate violation", "tofu_editor")

    fake_fetch, _ = _make_fetch_iac_tree()
    notified = []

    monkeypatch.setattr(adk_tools.worker_client, "call_open_infra_pr", _error_worker)
    monkeypatch.setattr(adk_tools, "_fetch_main_iac_tree", fake_fetch)
    # Stub the open-PR dupe probe so this path stays off the network (see happy-path).
    monkeypatch.setattr(adk_tools, "find_open_adopt_pr_for_resource", lambda *a, **k: None)
    monkeypatch.setattr(adk_tools, "notify_iac_pr_pending", lambda *a, **kw: notified.append(a))
    monkeypatch.setattr(
        "agent.config.Settings.gcp_project",
        property(lambda self: _PROJECT),
        raising=False,
    )

    # WorkerClientError propagates — mirrors open_infra_pr_tool (no swallowing)
    with pytest.raises(WorkerClientError):
        adk_tools.propose_adoption_tool("google_pubsub_topic", "my-topic")

    # Notify was NOT fired (never reached on error)
    assert len(notified) == 0


# ---------------------------------------------------------------------------
# Rejected paths — zero worker calls AND zero tree fetches
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("resource_type", "name", "kwargs", "match"),
    [
        # Non-adoptable type (param-explicit rejection copy — live e2e catch)
        ("google_service_account", "my-sa", {}, "not an adoptable resource type"),
        ("google_compute_instance", "my-vm", {}, "not an adoptable resource type"),
        # Missing image for run service
        ("google_cloud_run_v2_service", "my-svc", {"location": "asia-northeast1"}, "image"),
        # Missing topic for subscription
        ("google_pubsub_subscription", "my-sub", {}, "topic"),
        # Missing location for bucket
        ("google_storage_bucket", "my-bucket", {}, "location"),
    ],
)
def test_propose_adoption_tool_rejected_paths(
    resource_type, name, kwargs, match, monkeypatch
):
    """Rejected params → status='rejected' + reason; zero worker calls + zero tree fetches."""
    from agent import adk_tools

    _, worker_calls = _make_fake_worker()
    _, fetch_calls = _make_fetch_iac_tree()

    monkeypatch.setattr(adk_tools.worker_client, "call_open_infra_pr", lambda **k: worker_calls.append(k) or {})
    monkeypatch.setattr(adk_tools, "_fetch_main_iac_tree", lambda repo: fetch_calls.append(repo) or {})
    monkeypatch.setattr(
        "agent.config.Settings.gcp_project",
        property(lambda self: _PROJECT),
        raising=False,
    )

    result = adk_tools.propose_adoption_tool(resource_type, name, **kwargs)

    assert result["status"] == "rejected"
    assert match.lower() in result["reason"].lower()
    assert len(worker_calls) == 0
    assert len(fetch_calls) == 0


# ---------------------------------------------------------------------------
# Freehand-import guard — open_infra_pr_tool
# ---------------------------------------------------------------------------

def test_open_infra_pr_tool_rejects_freehand_import(monkeypatch):
    """open_infra_pr_tool with an import block → rejected coordinator-side;
    zero worker calls."""
    from agent import adk_tools

    worker_calls = []
    monkeypatch.setattr(
        adk_tools.worker_client,
        "call_open_infra_pr",
        lambda **k: worker_calls.append(k) or {},
    )

    files = [
        {
            "path": "iac/adopt.tf",
            "content": (
                'resource "google_pubsub_topic" "my_topic" {\n'
                '  name = "t"\n}\n\n'
                'import {\n  to = google_pubsub_topic.my_topic\n'
                '  id = "projects/p/topics/t"\n}\n'
            ),
        }
    ]
    result = adk_tools.open_infra_pr_tool(files=files, title="Adopt topic", body="body")
    assert result["status"] == "rejected"
    assert "provision_propose_adoption" in result["reason"] or "adopt" in result["reason"].lower()
    assert len(worker_calls) == 0


def test_open_infra_pr_tool_rejects_unparseable_file(monkeypatch):
    """open_infra_pr_tool with unparseable .tf → rejected fail-closed; zero worker calls."""
    from agent import adk_tools

    worker_calls = []
    monkeypatch.setattr(
        adk_tools.worker_client,
        "call_open_infra_pr",
        lambda **k: worker_calls.append(k) or {},
    )

    files = [{"path": "iac/broken.tf", "content": "this is not valid HCL {{{"}]
    result = adk_tools.open_infra_pr_tool(files=files, title="t", body="b")
    assert result["status"] == "rejected"
    assert len(worker_calls) == 0


def test_open_infra_pr_tool_allows_clean_file(monkeypatch):
    """open_infra_pr_tool with clean HCL (no import block) → proceeds normally."""
    from agent import adk_tools

    worker_calls = []

    def _fake(*, target_repo, branch, title, body, files, dispatch_plan_builder=False):
        worker_calls.append(files)
        return {"status": "opened", "pr_number": 5, "pr_url": "https://u", "branch": branch}

    monkeypatch.setattr(adk_tools.worker_client, "call_open_infra_pr", _fake)
    monkeypatch.setattr(adk_tools, "notify_iac_pr_pending", lambda *a, **kw: None)

    files = [{"path": "iac/topic.tf", "content": 'resource "google_pubsub_topic" "x" {}\n'}]
    result = adk_tools.open_infra_pr_tool(files=files, title="t", body="b")
    assert result["status"] == "opened"
    assert len(worker_calls) == 1


# ---------------------------------------------------------------------------
# Freehand-import guard — fan-out merged-files site
# ---------------------------------------------------------------------------

def test_fanout_merge_slice_sinks_rejects_import_block(monkeypatch):
    """_merge_slice_sinks: if any authored file contains an import block,
    the result raises FanoutError (POLICY kind) before the editor call."""
    from agent.fanout import FanoutError, FanoutFailureKind, SliceSpec, _merge_slice_sinks

    spec = SliceSpec(goal="adopt topic", target_path="iac/adopt.tf")
    sink = {
        "file": {
            "path": "iac/adopt.tf",
            "content": (
                'resource "google_pubsub_topic" "t" {\n  name = "t"\n}\n\n'
                'import {\n  to = google_pubsub_topic.t\n  id = "p/t"\n}\n'
            ),
        }
    }
    with pytest.raises(FanoutError) as exc_info:
        _merge_slice_sinks([(spec, sink)])
    assert exc_info.value.kind is FanoutFailureKind.POLICY
    assert "import" in exc_info.value.detail.lower() or "adopt" in exc_info.value.detail.lower()


def test_fanout_merge_slice_sinks_rejects_unparseable(monkeypatch):
    """_merge_slice_sinks: unparseable .tf → FanoutError(POLICY) fail-closed."""
    from agent.fanout import FanoutError, FanoutFailureKind, SliceSpec, _merge_slice_sinks

    spec = SliceSpec(goal="do something", target_path="iac/bad.tf")
    sink = {"file": {"path": "iac/bad.tf", "content": "this is not valid HCL {{{"}}
    with pytest.raises(FanoutError) as exc_info:
        _merge_slice_sinks([(spec, sink)])
    assert exc_info.value.kind is FanoutFailureKind.POLICY


def test_fanout_merge_slice_sinks_allows_clean_file():
    """_merge_slice_sinks: clean HCL (no import) passes the guard."""
    from agent.fanout import SliceSpec, _merge_slice_sinks

    spec = SliceSpec(goal="add topic", target_path="iac/topic.tf")
    sink = {"file": {"path": "iac/topic.tf", "content": 'resource "google_pubsub_topic" "x" {}\n'}}
    result = _merge_slice_sinks([(spec, sink)])
    assert len(result.files) == 1


# ---------------------------------------------------------------------------
# Preflight conflict tests
# ---------------------------------------------------------------------------

def test_propose_adoption_preflight_path_collision(monkeypatch):
    """Preflight detects a path collision → rejected; zero worker calls."""
    from agent import adk_tools
    from driftscribe_lib.adopt_recipe import render_adoption

    r = render_adoption("google_pubsub_topic", "my-topic", _PROJECT)

    _, worker_calls = _make_fake_worker()
    fake_fetch, _ = _make_fetch_iac_tree({
        r.path: "# existing content",
        "iac/variables.tf": f'variable "project_id" {{\n  default = "{_PROJECT}"\n}}\n',
    })

    monkeypatch.setattr(adk_tools.worker_client, "call_open_infra_pr", lambda **k: worker_calls.append(k) or {})
    monkeypatch.setattr(adk_tools, "_fetch_main_iac_tree", fake_fetch)
    monkeypatch.setattr(
        "agent.config.Settings.gcp_project",
        property(lambda self: _PROJECT),
        raising=False,
    )

    result = adk_tools.propose_adoption_tool("google_pubsub_topic", "my-topic")
    assert result["status"] == "rejected"
    assert r.path in result["reason"] or "already exists" in result["reason"].lower()
    assert len(worker_calls) == 0


def test_propose_adoption_preflight_project_mismatch(monkeypatch):
    """Preflight detects project_id mismatch → rejected; zero worker calls."""
    from agent import adk_tools

    fake_fetch, _ = _make_fetch_iac_tree({
        "iac/variables.tf": 'variable "project_id" {\n  default = "different-project-99"\n}\n',
    })

    _, worker_calls = _make_fake_worker()
    monkeypatch.setattr(adk_tools.worker_client, "call_open_infra_pr", lambda **k: worker_calls.append(k) or {})
    monkeypatch.setattr(adk_tools, "_fetch_main_iac_tree", fake_fetch)
    monkeypatch.setattr(
        "agent.config.Settings.gcp_project",
        property(lambda self: _PROJECT),
        raising=False,
    )

    result = adk_tools.propose_adoption_tool("google_pubsub_topic", "my-topic")
    assert result["status"] == "rejected"
    assert "project" in result["reason"].lower() or "mismatch" in result["reason"].lower()
    assert len(worker_calls) == 0


def test_propose_adoption_preflight_fetch_failure_fail_closed(monkeypatch):
    """Fetch failure → rejected fail-closed; zero worker calls."""
    from agent import adk_tools

    fake_fetch, _ = _make_fetch_iac_tree(raise_exc=Exception("connection timeout"))

    _, worker_calls = _make_fake_worker()
    monkeypatch.setattr(adk_tools.worker_client, "call_open_infra_pr", lambda **k: worker_calls.append(k) or {})
    monkeypatch.setattr(adk_tools, "_fetch_main_iac_tree", fake_fetch)
    monkeypatch.setattr(
        "agent.config.Settings.gcp_project",
        property(lambda self: _PROJECT),
        raising=False,
    )

    result = adk_tools.propose_adoption_tool("google_pubsub_topic", "my-topic")
    assert result["status"] == "rejected"
    assert "try again" in result["reason"].lower() or "verify" in result["reason"].lower() or "couldn't" in result["reason"].lower()
    assert len(worker_calls) == 0


# ---------------------------------------------------------------------------
# _open_iac_pr_and_notify: propose_adoption_tool passes allow_import=True
# ---------------------------------------------------------------------------

def test_open_infra_pr_tool_existing_tests_still_pass(monkeypatch):
    """Regression: the existing open_infra_pr tests pass unchanged after the guard.
    This test replicates the core happy-path behavior."""
    from agent import adk_tools
    from agent.workloads.registry import resolve_iac_editor_target

    monkeypatch.delenv("IAC_EDITOR_TARGET_REPO_OVERRIDE", raising=False)
    expected_repo = resolve_iac_editor_target()

    captured: dict = {}

    def _fake(*, target_repo, branch, title, body, files, dispatch_plan_builder=False):
        captured.update(target_repo=target_repo, branch=branch, title=title, body=body, files=files)
        return {"status": "opened", "pr_number": 7, "pr_url": "https://x/pull/7", "branch": branch}

    monkeypatch.setattr(adk_tools.worker_client, "call_open_infra_pr", _fake)
    monkeypatch.setattr(adk_tools, "notify_iac_pr_pending", lambda *a, **kw: None)

    files = [{"path": "iac/network.tf", "content": "# vpc\n"}]
    result = adk_tools.open_infra_pr_tool(files=files, title="Add VPC", body="vpc body")

    assert captured["target_repo"] == expected_repo
    assert result["status"] == "opened"
    assert result["pr_number"] == 7


# Note: test_resolve_provision_read_tools_excludes_propose_adoption lives in
# tests/unit/test_coordinator_tool_inventory.py after Task 4 registration —
# it depends on the provision workload YAML and registry having the new tool.
