"""Format a `tofu show -no-color` diff into a PR comment for the C2 plan-builder.

Produces a Markdown body that GitHub accepts within its ~65 KB PR-comment
limit. The plan text is wrapped in a collapsible <details> block with a
code fence; a leading header surfaces the immutable identifiers (head_sha,
plan_sha256, generation, artifact URI, OpenTofu version) so a reviewer can
copy them straight into the C3 approval form once that exists.

Pure-stdlib. No GitHub API client — the workflow shells `gh pr comment`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# GitHub's documented PR-comment hard limit is 65,536 chars. We leave a
# margin for the header + Markdown wrapper + truncation marker.
GH_COMMENT_BUDGET = 60_000

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ANSI  = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


@dataclass(frozen=True)
class SummaryInput:
    plan_text: str
    head_sha: str
    plan_sha256: str
    plan_json_sha256: str
    generation_plan: str
    generation_json: str
    generation_metadata: str
    artifact_uri_plan: str
    artifact_uri_json: str
    artifact_uri_metadata: str
    opentofu_version: str


_BACKTICK_RUN = re.compile(r"`+")


def _validate(inp: SummaryInput) -> None:
    if not _HEX40.fullmatch(inp.head_sha):
        raise ValueError(f"head_sha: must be 40 lowercase hex (got {inp.head_sha!r})")
    if not _HEX64.fullmatch(inp.plan_sha256):
        raise ValueError(f"plan_sha256: must be 64 lowercase hex (got {inp.plan_sha256!r})")
    if not _HEX64.fullmatch(inp.plan_json_sha256):
        raise ValueError(f"plan_json_sha256: must be 64 lowercase hex (got {inp.plan_json_sha256!r})")


def _pick_fence(text: str) -> str:
    """Choose a backtick fence longer than any backtick run in the text.

    Default fence is 3 backticks; if the text contains a 3- or 4-backtick
    run, the fence must extend to >=5 to avoid early-terminating the code
    block. Markdown does not require equal fence lengths — opening and
    closing fences must be the same width, both at least 3.
    """
    longest_run = 0
    for m in _BACKTICK_RUN.finditer(text):
        longest_run = max(longest_run, len(m.group(0)))
    return "`" * max(3, longest_run + 1)


def format_summary(inp: SummaryInput) -> str:
    _validate(inp)
    clean = _ANSI.sub("", inp.plan_text)

    header_lines = [
        "### DriftScribe IaC — `tofu plan` (Phase C2 plan-builder)",
        "",
        f"- **head_sha:** `{inp.head_sha}`",
        f"- **plan_sha256:** `{inp.plan_sha256}` (generation `{inp.generation_plan}`)",
        f"- **plan_json_sha256:** `{inp.plan_json_sha256}` (generation `{inp.generation_json}`)",
        f"- **metadata generation:** `{inp.generation_metadata}`",
        f"- **artifact plan.tfplan:** `{inp.artifact_uri_plan}`",
        f"- **artifact plan.json:** `{inp.artifact_uri_json}`",
        f"- **artifact metadata.json:** `{inp.artifact_uri_metadata}`",
        f"- **opentofu:** `{inp.opentofu_version}`",
        "",
    ]
    header = "\n".join(header_lines)

    fence = _pick_fence(clean)

    # Build the truncation notice using the real ``len(clean)`` so we can
    # size the budget exactly without a magic padding constant. The notice
    # is always reserved in scaffold sizing, even when not used — the only
    # cost is that a non-truncated output may come in slightly under
    # ``GH_COMMENT_BUDGET``; the invariant ``len(out) <= GH_COMMENT_BUDGET``
    # is preserved in BOTH branches.
    def _build_notice(orig: int, kept: int) -> str:
        return (
            f"\n(truncated; original {orig} chars, kept {kept} chars; "
            f"fetch full diff via `gcloud storage cat {inp.artifact_uri_plan}` "
            f"or `tofu show <local plan>`)\n"
        )

    scaffold = (
        "<details><summary>tofu show</summary>\n\n"
        + fence + "\n"
        # body goes here
        + fence + "\n"
        + "</details>\n"
    )
    # Worst-case notice digit-strings use ``len(clean)`` for BOTH placeholders
    # — actual notice (with ``kept`` <= ``orig``) cannot be longer.
    provisional_notice_size = len(_build_notice(len(clean), len(clean)))
    scaffold_overhead = len(header) + len(scaffold) + provisional_notice_size
    budget_for_plan = GH_COMMENT_BUDGET - scaffold_overhead

    if budget_for_plan < 0:
        # Header + scaffold + notice exceeds the entire budget. This is a
        # construction-time error, not a truncation case: the workflow MUST
        # fail loudly rather than silently emit a comment >65 KB.
        raise ValueError(
            f"format_summary: header+scaffold overhead {scaffold_overhead} "
            f"exceeds GH_COMMENT_BUDGET={GH_COMMENT_BUDGET}"
        )

    if len(clean) > budget_for_plan:
        body_plan = clean[:budget_for_plan] + _build_notice(len(clean), budget_for_plan)
    else:
        body_plan = clean

    return (
        header
        + "<details><summary>tofu show</summary>\n\n"
        + fence + "\n"
        + body_plan
        + ("\n" if not body_plan.endswith("\n") else "")
        + fence + "\n"
        + "</details>\n"
    )


def _main(argv: list[str], stdin_text: str) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="iac_plan_diff_summary")
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--plan-json-sha256", required=True)
    parser.add_argument("--generation-plan", required=True)
    parser.add_argument("--generation-json", required=True)
    parser.add_argument("--generation-metadata", required=True)
    parser.add_argument("--artifact-uri-plan", required=True)
    parser.add_argument("--artifact-uri-json", required=True)
    parser.add_argument("--artifact-uri-metadata", required=True)
    parser.add_argument("--opentofu-version", required=True)
    ns = parser.parse_args(argv)
    try:
        body = format_summary(SummaryInput(
            plan_text=stdin_text,
            head_sha=ns.head_sha,
            plan_sha256=ns.plan_sha256,
            plan_json_sha256=ns.plan_json_sha256,
            generation_plan=ns.generation_plan,
            generation_json=ns.generation_json,
            generation_metadata=ns.generation_metadata,
            artifact_uri_plan=ns.artifact_uri_plan,
            artifact_uri_json=ns.artifact_uri_json,
            artifact_uri_metadata=ns.artifact_uri_metadata,
            opentofu_version=ns.opentofu_version,
        ))
    except ValueError as e:
        import sys as _sys
        print(str(e), file=_sys.stderr)
        return 1
    print(body, end="")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    import sys as _sys
    _sys.exit(_main(_sys.argv[1:], _sys.stdin.read()))
