"""Structural invariants for .github/workflows/iac.yml.

These tests parse the YAML and assert security-critical structural facts
that no amount of "looks-fine" review can keep stable on its own. A red
test here is a release-blocker.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml


@pytest.fixture(scope="module")
def workflow() -> dict:
    p = pathlib.Path(__file__).resolve().parents[2] / ".github" / "workflows" / "iac.yml"
    with p.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _steps_of(workflow: dict, job_name: str) -> list[dict]:
    return list(workflow["jobs"][job_name].get("steps", []))


def _step_text(step: dict) -> str:
    """Render a step to a coarse text blob for keyword searches.

    Concatenates `run`, `uses`, `name`, and env values. Adequate for the
    presence/order assertions we make below.
    """
    parts = [step.get("name", ""), step.get("run", ""), step.get("uses", "")]
    for v in (step.get("env") or {}).values():
        parts.append(str(v))
    return "\n".join(parts)


def test_no_pull_request_target_trigger(workflow: dict):
    on = workflow.get(True) or workflow.get("on")  # PyYAML loads `on:` as bool True
    assert "pull_request_target" not in (on or {}), \
        "pull_request_target runs PR-controlled code with repo secrets — forbidden"


def test_only_plan_builder_has_id_token_write(workflow: dict):
    for name, job in workflow["jobs"].items():
        perms = job.get("permissions") or {}
        has_id = perms.get("id-token") == "write"
        if name == "plan-builder":
            assert has_id, "plan-builder MUST have id-token: write for WIF"
        else:
            assert not has_id, f"{name} must NOT have id-token: write (no WIF outside plan-builder)"


def test_plan_builder_if_pins_workflow_dispatch_and_main_ref(workflow: dict):
    job = workflow["jobs"]["plan-builder"]
    if_clause = job["if"]
    assert "workflow_dispatch" in if_clause
    assert "refs/heads/main" in if_clause


def test_static_gate_and_tofu_are_pr_only(workflow: dict):
    for name in ("static-gate", "tofu"):
        if_clause = workflow["jobs"][name].get("if", "")
        assert "pull_request" in if_clause, f"{name} must be gated to pull_request only"


def test_workflow_level_cancel_in_progress_is_conditional(workflow: dict):
    concurrency = workflow.get("concurrency") or {}
    cip = concurrency.get("cancel-in-progress")
    assert isinstance(cip, str) and "pull_request" in cip, \
        "workflow-level cancel-in-progress must be event-conditional (true on PR, false on dispatch)"


def test_diff_guard_uses_git_not_gh_api(workflow: dict):
    """The diff-guard must use local git against the pinned SHA pair, NOT
    `gh api .../pulls/<N>/files` — the latter reflects current PR state,
    not the resolved HEAD_SHA, leaving a force-push TOCTOU window."""
    for s in _steps_of(workflow, "plan-builder"):
        text = _step_text(s)
        if "diff-guard" in text:
            run = s.get("run", "")
            assert "git diff" in run, "diff-guard must call `git diff`"
            assert "$BASE_SHA" in run and "$HEAD_SHA" in run, \
                "diff-guard must diff between the resolved BASE_SHA and HEAD_SHA"
            assert "/pulls/" not in run and "gh api" not in run, \
                "diff-guard must NOT use the gh API /pulls/<N>/files endpoint (TOCTOU)"
            assert "--no-renames" in run, \
                "diff-guard must use --no-renames so a file renamed FROM outside iac/ TO iac/ does not slip through"
            assert " -z" in run or " -z " in run or "\t-z" in run, \
                "diff-guard must use -z (NUL-delimited) so filenames with newlines/quotes cannot break parsing"
            return
    raise AssertionError("plan-builder has no diff-guard step")


def test_diff_guard_runs_before_uv_sync(workflow: dict):
    """Must precede any Python invocation — otherwise PR-controlled
    uv.lock/pyproject.toml/tools could influence the trusted execution."""
    steps = _steps_of(workflow, "plan-builder")
    diff_idx = None
    uv_idx = None
    for i, s in enumerate(steps):
        text = _step_text(s)
        if "diff-guard" in text and diff_idx is None:
            diff_idx = i
        if ("uv sync" in s.get("run", "") or "astral-sh/setup-uv" in s.get("uses", "")) and uv_idx is None:
            uv_idx = i
    assert diff_idx is not None, "missing diff-guard step"
    assert uv_idx is not None, "missing uv setup/sync step"
    assert diff_idx < uv_idx, \
        "diff-guard MUST run BEFORE uv sync (PR's uv.lock cannot be trusted before)"


def test_static_gate_rerun_uses_hardcoded_agent_mode(workflow: dict):
    """Plan-builder's static-gate re-run must NOT derive MODE from PR labels/branch."""
    for s in _steps_of(workflow, "plan-builder"):
        run = s.get("run", "")
        if "iac_static_gate" in run:
            assert "--mode agent" in run, \
                "plan-builder static-gate re-run must hardcode --mode agent"
            return
    raise AssertionError("plan-builder has no iac_static_gate step")


def test_denylist_precedes_artifact_upload(workflow: dict):
    steps = _steps_of(workflow, "plan-builder")
    denylist_idx = None
    upload_idx = None
    for i, s in enumerate(steps):
        text = _step_text(s)
        if "iac_plan_denylist" in text and denylist_idx is None:
            denylist_idx = i
        if "iac_plan_artifact_upload" in text and upload_idx is None:
            upload_idx = i
    assert denylist_idx is not None, "missing denylist invocation in plan-builder"
    assert upload_idx is not None, "missing artifact upload invocation in plan-builder"
    assert denylist_idx < upload_idx, \
        "denylist MUST run BEFORE the artifact upload (else a denied plan becomes an artifact)"


def test_wif_auth_uses_repo_secrets_not_inline(workflow: dict):
    for s in _steps_of(workflow, "plan-builder"):
        if s.get("uses", "").startswith("google-github-actions/auth@"):
            with_block = s.get("with", {})
            assert with_block.get("workload_identity_provider", "").startswith("${{ secrets."), \
                "workload_identity_provider must be a secret reference, not inline"
            assert with_block.get("service_account", "").startswith("${{ secrets."), \
                "service_account must be a secret reference, not inline"
            return
    raise AssertionError("plan-builder has no google-github-actions/auth step")


def test_setup_opentofu_pins_tofu_version(workflow: dict):
    for s in _steps_of(workflow, "plan-builder"):
        if s.get("uses", "").startswith("opentofu/setup-opentofu@"):
            with_block = s.get("with", {})
            assert "tofu_version" in with_block, \
                "setup-opentofu MUST pin tofu_version (C4 compares this)"
            return
    raise AssertionError("plan-builder has no setup-opentofu step")


def test_plan_builder_concurrency_does_not_cancel(workflow: dict):
    job = workflow["jobs"]["plan-builder"]
    conc = job.get("concurrency") or {}
    assert conc.get("cancel-in-progress") is False, \
        "plan-builder job concurrency must NOT cancel-in-progress (would orphan an upload)"
    assert "inputs.pr_number" in conc.get("group", ""), \
        "plan-builder concurrency group must include inputs.pr_number for per-PR serialization"


def test_workflow_level_permissions_floor_is_contents_read_only(workflow: dict):
    """Workflow-level permissions are inherited by jobs that don't override.

    `static-gate` and `tofu` don't declare their own `permissions:` block, so
    they inherit the workflow-level floor. A regression that adds
    ``id-token: write`` at the workflow level would silently grant WIF
    credentials to BOTH non-plan-builder jobs — bypassing the per-job check
    in ``test_only_plan_builder_has_id_token_write``.
    """
    wf_perms = workflow.get("permissions") or {}
    assert wf_perms.get("id-token") != "write", \
        "workflow-level id-token: write would be inherited by static-gate/tofu — forbidden"
    assert wf_perms.get("contents") == "read", \
        "workflow-level permissions floor must be `contents: read` (minimal)"
