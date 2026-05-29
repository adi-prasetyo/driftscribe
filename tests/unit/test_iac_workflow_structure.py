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


# ---------------------------------------------------------------------------
# Regression pins for the live-only blockers found by the 2026-05-29 C2
# smoke-test (run 26620367059 finally went green only after these fixes).
# Each test below would have been red against the pre-fix workflow. A future
# edit that reintroduces any of these failures is a release-blocker.
# ---------------------------------------------------------------------------

_KMS_VAR_ENV = "TF_VAR_tofu_state_kms_key"


def _tofu_steps(workflow: dict) -> list[dict]:
    """plan-builder steps that invoke the `tofu` CLI in their `run` body."""
    out = []
    for s in _steps_of(workflow, "plan-builder"):
        run = s.get("run", "")
        if "tofu " in run or run.strip().startswith("tofu"):
            out.append(s)
    return out


def test_tofu_show_steps_supply_kms_var_via_tf_var(workflow: dict):
    """Both `tofu show` invocations must decrypt the KMS-encrypted saved plan
    (iac/versions.tf encryption.plan enforced=true). `tofu show` accepts no
    `-var`, so the key MUST be supplied via the TF_VAR_* env. The 2026-05-29
    dispatch died here: `Failed to request input from user for variable
    var.tofu_state_kms_key`."""
    show_steps = [s for s in _tofu_steps(workflow) if "show " in s.get("run", "")]
    assert len(show_steps) >= 2, \
        "expected at least two `tofu show` steps (show -json and the PR-comment show -no-color)"
    for s in show_steps:
        env = s.get("env") or {}
        assert _KMS_VAR_ENV in env, (
            f"`tofu show` step {s.get('name')!r} must set env {_KMS_VAR_ENV} "
            f"(show takes no -var; the plan is KMS-encrypted)"
        )


def test_all_tofu_steps_use_tf_var_not_dash_var_for_kms_key(workflow: dict):
    """The KMS key is supplied through ONE mechanism — TF_VAR_tofu_state_kms_key —
    on every tofu step. The old `-var "tofu_state_kms_key=..."` form must not
    return (it cannot work for `tofu show`, and mixing the two channels invites
    the show steps being forgotten again)."""
    tofu_steps = _tofu_steps(workflow)
    assert tofu_steps, "plan-builder has no tofu steps"
    for s in tofu_steps:
        run = s.get("run", "")
        assert "tofu_state_kms_key=" not in run, (
            f"tofu step {s.get('name')!r} still passes the KMS key via -var "
            f"(`tofu_state_kms_key=`); use the {_KMS_VAR_ENV} env instead"
        )
    # init + plan must carry the env (they evaluate the encryption block).
    for needle in ("init", "plan"):
        matches = [s for s in tofu_steps if f" {needle} " in f" {s.get('run','')} " or
                   f"-chdir=iac {needle}" in s.get("run", "")]
        assert matches, f"expected a `tofu {needle}` step"
        assert any((s.get("env") or {}).get(_KMS_VAR_ENV) for s in matches), \
            f"the `tofu {needle}` step must set env {_KMS_VAR_ENV}"


def test_wif_auth_sets_project_id(workflow: dict):
    """The WIF auth step must pin project_id so GOOGLE_CLOUD_PROJECT is exported
    for downstream tooling (the google-cloud-storage uploader resolves a project
    at Client() construction)."""
    for s in _steps_of(workflow, "plan-builder"):
        if s.get("uses", "").startswith("google-github-actions/auth@"):
            assert (s.get("with") or {}).get("project_id"), \
                "WIF auth step must set `project_id`"
            return
    raise AssertionError("plan-builder has no google-github-actions/auth step")


def test_pr_comment_uses_rest_api_not_gh_pr_comment(workflow: dict):
    """The diff comment must be posted via the REST issues/comments endpoint,
    NOT `gh pr comment` — the latter uses the GraphQL addComment mutation which
    403s under the Actions GITHUB_TOKEN ("Resource not accessible by
    integration", cli/cli #8374/#10464)."""
    comment_steps = [
        s for s in _steps_of(workflow, "plan-builder")
        if "/comments" in s.get("run", "") or "gh pr comment" in s.get("run", "")
    ]
    assert comment_steps, "plan-builder has no PR-comment step"
    for s in comment_steps:
        run = s.get("run", "")
        # Inspect only EXECUTED lines — `#` comment lines legitimately mention
        # `gh pr comment` to explain why it was replaced.
        executed = "\n".join(ln for ln in run.splitlines() if not ln.strip().startswith("#"))
        assert "gh pr comment" not in executed, \
            "must not use `gh pr comment` (GraphQL addComment 403s under GITHUB_TOKEN)"
        assert "gh api" in executed and "/comments" in executed, \
            "PR comment must POST to the REST issues/{n}/comments endpoint via `gh api`"
