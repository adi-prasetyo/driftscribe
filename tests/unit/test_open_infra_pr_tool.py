"""Unit tests for the iac-editor authoring tool (Phase D2-2).

:func:`agent.adk_tools.open_infra_pr_tool` is the LLM-facing, authority-clean
surface that routes a validated multi-file infra PR to the tofu-editor worker.
It mirrors :func:`upgrade_propose_pr_tool` (authority-clean) and
:func:`patch_docs_tool` (computes its own collision-safe branch).

Invariant under test: the LLM supplies ONLY decision content (``files``,
``title``, ``body``). Every routing/authority field — ``target_repo``,
``branch``, ``base``, ``label``, the worker endpoint — is derived server-side
and the LLM can never influence it. Letting the LLM pick the branch name (or the
repo) would invite ``branch="main"`` / ``branch="../.."`` foot-guns and
capability widening; pre-binding the values in the coordinator wrapper is what
keeps the tofu-editor's re-validation genuinely defense-in-depth.
"""
from __future__ import annotations

import inspect
import re

import pytest

# Branch must satisfy the worker's allowlist: ASCII [A-Za-z0-9._/-], no "..",
# and the slug part is only [a-z0-9_-] (lowercased title with disallowed runs
# collapsed to "-"), wrapped as infra/{slug}-{unix_ts}-{4-hex}.
_BRANCH_RE = re.compile(r"^infra/.+-\d+-[0-9a-f]{4}$")
_WORKER_ALLOWLIST_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


# --------------------------------------------------------------------------- #
# Signature pin — authority/routing fields must NOT appear as LLM-facing params
# --------------------------------------------------------------------------- #


def test_open_infra_pr_tool_signature_is_authority_clean():
    """The tool's LLM-facing signature is EXACTLY ``{files, title, body}``.

    A regression that "helpfully" added ``repo`` / ``branch`` / ``base`` /
    ``label`` / ``target_repo`` / ``endpoint`` would let the LLM redirect the PR
    (capability widening) or pick a foot-gun branch name. The tofu-editor worker
    re-validates these, but that defense must NOT be the primary boundary.
    """
    from agent.adk_tools import open_infra_pr_tool

    sig = inspect.signature(open_infra_pr_tool)
    params = set(sig.parameters)
    assert params == {"files", "title", "body"}, (
        f"open_infra_pr_tool exposes unexpected param(s) "
        f"{sorted(params - {'files', 'title', 'body'})} to the LLM — only the "
        f"decision content (files/title/body) is allowed."
    )
    forbidden = {"repo", "branch", "base", "label", "target_repo", "endpoint"}
    leak = forbidden & params
    assert not leak, (
        f"open_infra_pr_tool exposes authority/routing field(s) {sorted(leak)} "
        f"to the LLM. These must be derived server-side."
    )


# --------------------------------------------------------------------------- #
# Happy path — routes through call_open_infra_pr with derived authority fields
# --------------------------------------------------------------------------- #


def test_open_infra_pr_tool_happy_path_routes_with_derived_authority(monkeypatch):
    """A happy call routes through ``call_open_infra_pr`` with the pinned
    ``target_repo`` and a server-derived ``infra/`` branch; the LLM's
    ``files`` / ``title`` / ``body`` pass through; the result is compact + has
    a next-steps reminder mentioning the plan-builder / approval.
    """
    from agent import adk_tools
    from agent.workloads.registry import resolve_iac_editor_target

    monkeypatch.delenv("IAC_EDITOR_TARGET_REPO_OVERRIDE", raising=False)
    expected_repo = resolve_iac_editor_target()

    captured: dict = {}

    def _fake_call_open_infra_pr(*, target_repo, branch, title, body, files):
        captured.update(
            target_repo=target_repo,
            branch=branch,
            title=title,
            body=body,
            files=files,
        )
        return {
            "status": "opened",
            "pr_number": 7,
            "pr_url": "https://github.com/adi-prasetyo/driftscribe/pull/7",
            "branch": branch,
        }

    monkeypatch.setattr(
        adk_tools.worker_client, "call_open_infra_pr", _fake_call_open_infra_pr
    )

    files = [{"path": "iac/network.tf", "content": "# vpc\n"}]
    result = adk_tools.open_infra_pr_tool(
        files=files,
        title="Add VPC network",
        body="Provision the shared VPC.",
    )

    # Authority/routing — derived server-side.
    assert captured["target_repo"] == expected_repo
    assert _BRANCH_RE.match(captured["branch"]), captured["branch"]
    assert _WORKER_ALLOWLIST_RE.match(captured["branch"]), captured["branch"]
    assert ".." not in captured["branch"]
    # Decision content — pass-through, unchanged.
    assert captured["files"] is files
    assert captured["title"] == "Add VPC network"
    assert captured["body"] == "Provision the shared VPC."

    # Compact, LLM-useful result.
    assert result["status"] == "opened"
    assert result["pr_number"] == 7
    assert result["pr_url"] == "https://github.com/adi-prasetyo/driftscribe/pull/7"
    assert result["branch"] == captured["branch"]
    assert isinstance(result["next_steps"], str)
    nxt = result["next_steps"].lower()
    assert "plan-builder" in nxt
    assert "approv" in nxt


def test_open_infra_pr_tool_result_falls_back_to_derived_branch(monkeypatch):
    """If the worker omits ``branch`` in its response, the result echoes the
    server-derived branch rather than ``None`` (so the operator can find the PR).
    """
    from agent import adk_tools

    captured: dict = {}

    def _fake(*, target_repo, branch, title, body, files):
        captured["branch"] = branch
        return {"status": "opened", "pr_number": 9, "pr_url": "https://x/pull/9"}

    monkeypatch.setattr(adk_tools.worker_client, "call_open_infra_pr", _fake)

    result = adk_tools.open_infra_pr_tool(
        files=[{"path": "iac/a.tf", "content": ""}], title="x", body="y"
    )
    assert result["branch"] == captured["branch"]


# --------------------------------------------------------------------------- #
# Branch derivation — slugified, lowercase, allowlist-safe; empty-ish fallback
# --------------------------------------------------------------------------- #


def _capture_branch(monkeypatch, *, title: str) -> str:
    from agent import adk_tools

    captured: dict = {}

    def _fake(*, target_repo, branch, title, body, files):
        captured["branch"] = branch
        return {"status": "opened", "pr_number": 1, "pr_url": "u", "branch": branch}

    monkeypatch.setattr(adk_tools.worker_client, "call_open_infra_pr", _fake)
    adk_tools.open_infra_pr_tool(
        files=[{"path": "iac/a.tf", "content": ""}], title=title, body="b"
    )
    return captured["branch"]


def test_open_infra_pr_tool_branch_slug_lowercases_and_sanitizes(monkeypatch):
    """A title with spaces / punctuation / uppercase yields a slugified
    ``infra/...`` branch: lowercased, only ``[a-z0-9_-]`` in the slug part, and
    worker-allowlist-safe (ASCII, no ``..``).
    """
    branch = _capture_branch(monkeypatch, title="Add S3 Bucket: prod/data (v2)!")

    assert branch.startswith("infra/")
    assert _BRANCH_RE.match(branch), branch
    assert _WORKER_ALLOWLIST_RE.match(branch), branch
    assert ".." not in branch

    # Slug part = between "infra/" and the trailing "-{ts}-{hex}".
    slug = re.sub(r"-\d+-[0-9a-f]{4}$", "", branch[len("infra/"):])
    assert slug == slug.lower()
    assert re.fullmatch(r"[a-z0-9_-]+", slug), slug
    # Uppercase letters and forbidden punctuation are gone.
    assert "S3" not in slug and "/" not in slug and ":" not in slug


def test_open_infra_pr_tool_empty_title_falls_back_to_infra(monkeypatch):
    """An empty-ish title (only punctuation/whitespace) falls back to the slug
    ``infra``, i.e. ``infra/infra-{ts}-{hex}``.
    """
    branch = _capture_branch(monkeypatch, title="  !!!  ")

    assert _BRANCH_RE.match(branch), branch
    slug = re.sub(r"-\d+-[0-9a-f]{4}$", "", branch[len("infra/"):])
    assert slug == "infra"


def test_open_infra_pr_tool_caps_branch_for_pathological_title(monkeypatch):
    """A ~300-char title still yields a branch the worker accepts: the tail
    (everything after ``infra/``) is <= 200 chars, allowlist-safe, has no ``..``,
    and the authoritative ``validate_branch`` does NOT raise.
    """
    from driftscribe_lib.iac_editor_policy import validate_branch

    title = "Add " + "very long resource name " * 12  # ~300 chars
    assert len(title) > 200
    branch = _capture_branch(monkeypatch, title=title)

    assert branch.startswith("infra/")
    tail = branch[len("infra/"):]
    assert len(tail) <= 200, len(tail)
    assert _WORKER_ALLOWLIST_RE.match(branch), branch
    assert ".." not in branch
    # Authoritative check — must not raise.
    validate_branch(branch)


# --------------------------------------------------------------------------- #
# Override — IAC_EDITOR_TARGET_REPO_OVERRIDE redirects routing at call time
# --------------------------------------------------------------------------- #


def test_open_infra_pr_tool_honors_target_repo_override(monkeypatch):
    """Setting ``IAC_EDITOR_TARGET_REPO_OVERRIDE`` routes the PR to the override
    repo — proving the resolver reads the env at call time, not import time.
    """
    from agent import adk_tools

    monkeypatch.setenv("IAC_EDITOR_TARGET_REPO_OVERRIDE", "acme/driftscribe-e2e-target")

    captured: dict = {}

    def _fake(*, target_repo, branch, title, body, files):
        captured["target_repo"] = target_repo
        return {"status": "opened", "pr_number": 3, "pr_url": "u", "branch": branch}

    monkeypatch.setattr(adk_tools.worker_client, "call_open_infra_pr", _fake)

    adk_tools.open_infra_pr_tool(
        files=[{"path": "iac/a.tf", "content": ""}], title="t", body="b"
    )
    assert captured["target_repo"] == "acme/driftscribe-e2e-target"


# --------------------------------------------------------------------------- #
# Registry resolver — pin by default, override when env set (read at call time)
# --------------------------------------------------------------------------- #


def test_resolve_iac_editor_target_pin_by_default(monkeypatch):
    from agent.workloads.registry import IAC_EDITOR_TARGET, resolve_iac_editor_target

    monkeypatch.delenv("IAC_EDITOR_TARGET_REPO_OVERRIDE", raising=False)
    assert resolve_iac_editor_target() == IAC_EDITOR_TARGET
    assert IAC_EDITOR_TARGET == "adi-prasetyo/driftscribe"


def test_resolve_iac_editor_target_honors_override(monkeypatch):
    from agent.workloads.registry import resolve_iac_editor_target

    monkeypatch.setenv("IAC_EDITOR_TARGET_REPO_OVERRIDE", "acme/driftscribe-e2e-target")
    assert resolve_iac_editor_target() == "acme/driftscribe-e2e-target"


@pytest.mark.parametrize("override", ["adi-prasetyo/driftscribe", "acme/other"])
def test_resolve_iac_editor_target_override_read_at_call_time(monkeypatch, override):
    """Each call re-reads the env, so monkeypatching after import takes effect."""
    from agent.workloads.registry import resolve_iac_editor_target

    monkeypatch.setenv("IAC_EDITOR_TARGET_REPO_OVERRIDE", override)
    assert resolve_iac_editor_target() == override
