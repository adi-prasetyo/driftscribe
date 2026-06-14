"""Unit tests for the shared :func:`agent.adk_tools.derive_iac_pr_authority`.

D5-6 extracted the iac-editor PR authority derivation (target_repo + the
collision-safe ``infra/`` branch) into ONE injectable, deterministic helper so
the single-agent ``open_infra_pr_tool`` and the D5 fan-out orchestrator cannot
drift apart in how they derive routing/authority. These tests pin:

1. the helper's deterministic derivation (with injected clock/rng), and
2. that ``open_infra_pr_tool`` routes THROUGH the helper (no independent
   derivation remains in the tool — patching the helper to a sentinel proves
   the tool passes exactly that target_repo/branch to the worker).
"""
from __future__ import annotations

from agent import adk_tools
from agent.adk_tools import IacPrAuthority, derive_iac_pr_authority


def test_derive_iac_pr_authority_is_deterministic_with_injected_clock_and_rng(
    monkeypatch,
):
    """With injected ``clock``/``rng`` the derivation is fully deterministic:
    the branch is ``infra/<slug>-<int(clock)>-<rng()>`` and ``target_repo`` is
    the resolved editor target. The slug lowercases + sanitizes the title."""
    monkeypatch.setattr(
        adk_tools, "_get_iac_editor_target", lambda: "owner/sentinel-repo"
    )

    authority = derive_iac_pr_authority(
        "Add S3 Bucket: prod/data (v2)!",
        clock=lambda: 1700000000.0,
        rng=lambda: "ab12",
    )

    assert isinstance(authority, IacPrAuthority)
    assert authority.target_repo == "owner/sentinel-repo"
    # The slug: lowercased, non-[a-z0-9_-] runs collapse to "-", stripped ends.
    assert authority.branch == "infra/add-s3-bucket-prod-data-v2-1700000000-ab12"


def test_derive_iac_pr_authority_empty_slug_falls_back_to_infra(monkeypatch):
    """A title that slugs to empty falls back to the literal ``infra`` slug."""
    monkeypatch.setattr(
        adk_tools, "_get_iac_editor_target", lambda: "owner/sentinel-repo"
    )

    authority = derive_iac_pr_authority(
        "  !!!  ", clock=lambda: 1700000000.0, rng=lambda: "ab12"
    )

    assert authority.branch == "infra/infra-1700000000-ab12"


def test_derive_iac_pr_authority_caps_slug_at_80(monkeypatch):
    """A pathologically long title yields a slug capped at 80 chars (the tail
    ``-<ts>-<hex>`` is appended after the cap)."""
    monkeypatch.setattr(
        adk_tools, "_get_iac_editor_target", lambda: "owner/sentinel-repo"
    )

    authority = derive_iac_pr_authority(
        "a" * 300, clock=lambda: 1700000000.0, rng=lambda: "ab12"
    )

    slug = authority.branch[len("infra/") : -len("-1700000000-ab12")]
    assert len(slug) <= 80
    assert authority.branch.endswith("-1700000000-ab12")


def test_derive_iac_pr_authority_defaults_to_real_time_and_secrets(monkeypatch):
    """Without injected clock/rng it uses time.time / secrets.token_hex(2):
    the branch carries an int timestamp and a 4-hex-digit suffix."""
    import re

    monkeypatch.setattr(
        adk_tools, "_get_iac_editor_target", lambda: "owner/sentinel-repo"
    )

    authority = derive_iac_pr_authority("add bucket")

    assert re.fullmatch(r"infra/add-bucket-\d+-[0-9a-f]{4}", authority.branch), (
        authority.branch
    )


def test_open_infra_pr_tool_routes_through_shared_helper(monkeypatch):
    """``open_infra_pr_tool`` derives NO authority of its own — it routes the
    helper's result straight to the worker. Patch the helper to a sentinel
    authority and assert the worker received exactly that target_repo/branch."""
    sentinel = IacPrAuthority(
        target_repo="owner/sentinel-repo", branch="infra/sentinel-branch"
    )
    monkeypatch.setattr(adk_tools, "derive_iac_pr_authority", lambda title: sentinel)

    captured: dict = {}

    def _fake_call_open_infra_pr(*, target_repo, branch, title, body, files, dispatch_plan_builder=False):
        captured.update(
            target_repo=target_repo, branch=branch, title=title, body=body, files=files
        )
        return {"status": "opened", "pr_number": 7, "pr_url": "u", "branch": branch}

    monkeypatch.setattr(
        adk_tools.worker_client, "call_open_infra_pr", _fake_call_open_infra_pr
    )

    # Content must be valid, import-free HCL so the freehand-import guard passes.
    result = adk_tools.open_infra_pr_tool(
        files=[{"path": "iac/bucket.tf", "content": 'variable "b" {}\n'}],
        title="Add bucket",
        body="body",
    )

    assert captured["target_repo"] == "owner/sentinel-repo"
    assert captured["branch"] == "infra/sentinel-branch"
    assert result["branch"] == "infra/sentinel-branch"
    assert result["pr_number"] == 7
