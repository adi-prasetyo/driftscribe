"""Tests for tools.iac_plan_diff_summary — the C2 PR-comment formatter."""

import pytest

from tools.iac_plan_diff_summary import (
    GH_COMMENT_BUDGET,
    SummaryInput,
    format_summary,
)


def _valid_input(**overrides):
    head = "a" * 40
    run_dir = "run-1234567890-1"
    base = dict(
        plan_text="Plan: 1 to add, 0 to change, 0 to destroy.\n",
        head_sha=head,
        plan_sha256="c" * 64,
        plan_json_sha256="d" * 64,
        generation_plan="1700000000000000",
        generation_json="1700000000000001",
        generation_metadata="1700000000000002",
        artifact_uri_plan=f"gs://driftscribe-hack-2026-tofu-artifacts/pr-42/{head}/{run_dir}/plan.tfplan",
        artifact_uri_json=f"gs://driftscribe-hack-2026-tofu-artifacts/pr-42/{head}/{run_dir}/plan.json",
        artifact_uri_metadata=f"gs://driftscribe-hack-2026-tofu-artifacts/pr-42/{head}/{run_dir}/metadata.json",
        opentofu_version="1.12.0",
    )
    base.update(overrides)
    return SummaryInput(**base)


def test_summary_contains_canonical_header_fields():
    out = format_summary(_valid_input())
    # The header is the part BEFORE the collapsible block — must surface
    # head_sha, BOTH content hashes, ALL THREE generations, ALL THREE URIs,
    # tofu version. C3 reads this comment as the canonical artifact pointer.
    assert "a" * 40 in out, "head_sha missing"
    assert "c" * 64 in out, "plan_sha256 missing"
    assert "d" * 64 in out, "plan_json_sha256 missing"
    assert "1700000000000000" in out, "generation_plan missing"
    assert "1700000000000001" in out, "generation_json missing"
    assert "1700000000000002" in out, "generation_metadata missing"
    assert "plan.tfplan" in out, "plan.tfplan URI missing"
    assert "plan.json" in out, "plan.json URI missing"
    assert "metadata.json" in out, "metadata.json URI missing"
    assert "1.12.0" in out, "opentofu_version missing"


def test_summary_wraps_plan_text_in_details_element():
    out = format_summary(_valid_input(plan_text="ADD resource.foo\n"))
    assert "<details>" in out and "</details>" in out
    assert "ADD resource.foo" in out


def test_summary_uses_code_fence_inside_details():
    out = format_summary(_valid_input(plan_text="x\n"))
    assert "```" in out


def test_summary_picks_fence_longer_than_any_backtick_run_in_plan():
    # tofu show output rarely contains backticks, but a PR could include a
    # description / comment with backticks that ends up in the plan text.
    # Fixed 3-backtick fence would break the code block. We must use a fence
    # longer than the longest backtick run in plan_text.
    plan = "Some text with ```triple``` and ````four``` backticks\n"
    out = format_summary(_valid_input(plan_text=plan))
    # The longest run in input is 4 backticks; fence must be >=5.
    fence_lines = [line for line in out.splitlines() if line and all(ch == "`" for ch in line)]
    assert fence_lines, "no fence found in output"
    assert min(len(f) for f in fence_lines) >= 5, fence_lines


def test_summary_short_plan_is_not_truncated():
    out = format_summary(_valid_input(plan_text="short\n"))
    assert "short" in out
    assert "(truncated" not in out


def test_summary_long_plan_is_truncated_to_budget():
    # Generate plan text larger than the budget.
    huge = ("X" * 1000 + "\n") * 100  # ~100KB
    out = format_summary(_valid_input(plan_text=huge))
    assert len(out) <= GH_COMMENT_BUDGET
    assert "(truncated" in out
    # The truncation marker must include the original size so reviewers can
    # see how much was dropped.
    assert str(len(huge)) in out


def test_summary_is_idempotent_for_same_input():
    a = format_summary(_valid_input(plan_text="x\n"))
    b = format_summary(_valid_input(plan_text="x\n"))
    assert a == b


def test_summary_strips_ansi_escapes_from_plan_text():
    # `tofu show -no-color` should already be clean, but defense-in-depth.
    out = format_summary(_valid_input(plan_text="\x1b[31mRED\x1b[0m\n"))
    assert "\x1b[" not in out
    assert "RED" in out


def test_gh_comment_budget_constant_is_reasonable():
    # GitHub's hard PR-comment limit is ~65,536 chars; we budget below it.
    assert 50000 <= GH_COMMENT_BUDGET <= 65000


@pytest.mark.parametrize("field", ["head_sha"])
def test_format_rejects_malformed_head_sha(field):
    with pytest.raises(ValueError, match=field):
        format_summary(_valid_input(**{field: "G" * 40}))


@pytest.mark.parametrize("field", ["plan_sha256", "plan_json_sha256"])
def test_format_rejects_malformed_sha256(field):
    with pytest.raises(ValueError, match=field):
        format_summary(_valid_input(**{field: "G" * 64}))


def test_summary_raises_when_header_overhead_exceeds_budget(monkeypatch):
    """If the header alone exceeds GH_COMMENT_BUDGET, fail loudly — never
    silently emit a comment that GitHub will reject."""
    monkeypatch.setattr("tools.iac_plan_diff_summary.GH_COMMENT_BUDGET", 100)
    with pytest.raises(ValueError, match="GH_COMMENT_BUDGET"):
        format_summary(_valid_input(plan_text="x\n"))


def test_summary_truncated_output_stays_within_budget_with_long_uri_and_huge_size_digits():
    """True mutation test for I-1 (the magic-256 padding):

    A 50 MB plan + a 400-char artifact URI inflates the truncation notice to
    ~565 bytes. The OLD code reserved a fixed 256-byte pad; the resulting
    comment exceeded ``GH_COMMENT_BUDGET`` by ~309 bytes and would have been
    silently rejected/truncated by GitHub. The fix sizes the pad from the
    actual notice template, so both branches stay <= ``GH_COMMENT_BUDGET``.
    """
    long_uri = "gs://driftscribe-hack-2026-tofu-artifacts/pr-42/" + ("z" * 350) + "/plan.tfplan"
    assert len(long_uri) >= 400
    huge = "X" * 50_000_000  # 8-digit char count
    out = format_summary(_valid_input(
        plan_text=huge,
        artifact_uri_plan=long_uri,
    ))
    assert len(out) <= GH_COMMENT_BUDGET
    assert "(truncated" in out
    # The long URI must actually appear in the truncation notice (proves
    # the test is exercising the notice path, not just the header).
    assert long_uri in out


# --------------------------------------------------------------------------- #
# C6 — iac-tree.json sidecar comment lines (optional)
# --------------------------------------------------------------------------- #

_TREE = "e" * 64
_GEN_TREE = "1700000000000003"
_URI_TREE = (
    "gs://driftscribe-hack-2026-tofu-artifacts/pr-42/" + ("a" * 40)
    + "/run-1234567890-1/iac-tree.json"
)


def test_sidecar_lines_absent_by_default():
    """Pre-C6 callers (no sidecar fields) emit NO iac-tree lines."""
    out = format_summary(_valid_input())
    assert "iac-tree generation" not in out
    assert "iac_tree_hash" not in out


def test_sidecar_lines_present_when_provided():
    out = format_summary(_valid_input(
        generation_iac_tree=_GEN_TREE,
        artifact_uri_iac_tree=_URI_TREE,
        iac_tree_hash=_TREE,
    ))
    assert f"- **iac-tree generation:** `{_GEN_TREE}`" in out
    assert f"- **artifact iac-tree.json:** `{_URI_TREE}`" in out
    assert f"- **iac_tree_hash:** `{_TREE}`" in out


def test_sidecar_lines_roundtrip_through_parser():
    from agent.iac_artifacts import parse_c2_pr_comment

    out = format_summary(_valid_input(
        generation_iac_tree=_GEN_TREE,
        artifact_uri_iac_tree=_URI_TREE,
        iac_tree_hash=_TREE,
    ))
    ref = parse_c2_pr_comment(out, comment_id=7)
    assert ref is not None
    assert ref.generation_iac_tree == _GEN_TREE
    assert ref.iac_tree_hash == _TREE


def test_format_rejects_malformed_iac_tree_hash():
    with pytest.raises(ValueError):
        format_summary(_valid_input(iac_tree_hash="G" * 64))
    with pytest.raises(ValueError):
        format_summary(_valid_input(iac_tree_hash="e" * 63))


def test_format_rejects_malformed_generation_iac_tree():
    with pytest.raises(ValueError):
        format_summary(_valid_input(generation_iac_tree="not-a-number"))
