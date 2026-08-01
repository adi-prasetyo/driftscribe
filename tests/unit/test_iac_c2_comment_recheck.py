"""ds-2wy — the C2 comment step must not post to a PR that moved on.

`.github/workflows/iac.yml` asserts the PR is OPEN at PLAN time. Minutes later
the same job posts the C2 marker comment. The per-PR concurrency group
serializes plan-builder runs against each other but NOT against an operator's
approval POST, so a run that began while the PR was open can finish after that
PR was merged or force-pushed. Because `find_latest_c2_comment` is newest-wins,
the marker it would post becomes the generation `/iac-approvals/{n}` binds to —
one that can never be applied.

Asserting "the recheck appears before the POST in the file" would be theatre: it
proves ordering, not behavior. These tests EXTRACT the guard's shell out of the
YAML and RUN it against a stubbed `gh`, so a guard that reads the wrong field,
inverts a comparison, or forgets to exit is caught here rather than in
production.
"""
from __future__ import annotations

import pathlib
import subprocess

import pytest
import yaml

_HEAD = "a" * 40
_ROOT = pathlib.Path(__file__).resolve().parents[2]
_MARKER = "REACHED_COMMENT_POST"


def _step_run() -> str:
    wf = yaml.safe_load((_ROOT / ".github/workflows/iac.yml").read_text())
    steps = wf["jobs"]["plan-builder"]["steps"]
    return next(
        s["run"] for s in steps if s.get("name") == "Post tofu show diff to PR"
    )


def _guard_shell() -> str:
    """The recheck block, lifted verbatim from the real workflow step."""
    run = _step_run()
    start = run.index("NOW_JSON=$(gh pr view")
    # Through the end of the skip branch — everything after it is the POST the
    # guard exists to prevent.
    end = run.index("# POST via the REST issues/comments endpoint")
    block = run[start:end]
    assert "SKIP_REASON" in block and "exit 0" in block, (
        "the extracted block is not the guard — the step was restructured and "
        "this test's anchors need updating rather than deleting"
    )
    return block


def test_the_real_comment_post_is_downstream_of_the_guard():
    """The behavioral tests below run the guard with a SYNTHETIC marker standing
    in for the comment POST, so on their own they cannot notice the real POST
    being moved ABOVE the guard — the guard would still "work" and still skip,
    while the comment went out anyway. This pins the one thing they cannot see.
    """
    run = _step_run()
    guard_at = run.index("NOW_JSON=$(gh pr view")
    post_at = run.index("/repos/$GITHUB_REPOSITORY/issues/$PR_NUMBER/comments")
    assert guard_at < post_at, (
        "the PR recheck must precede the comment POST — otherwise the marker is "
        "already published by the time the guard decides to skip"
    )
    assert run.count("issues/$PR_NUMBER/comments") == 1, (
        "a second comment POST in this step would need its own guard"
    )


def _run(pr_json: str, *, head_sha: str = _HEAD, tmp_path) -> str:
    summary = tmp_path / "summary.md"
    summary.touch()
    script = f"""
set -euo pipefail
PR_NUMBER=42
GITHUB_REPOSITORY=owner/repo
HEAD_SHA={head_sha}
GITHUB_STEP_SUMMARY={summary}
gh() {{ printf '%s' {pr_json!r}; }}

{_guard_shell()}

echo "{_MARKER}"
"""
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, (
        f"the guard must exit 0 (skipping is not a failure)\n{proc.stderr}"
    )
    return proc.stdout


def _pr(state="OPEN", head=_HEAD, base="main") -> str:
    return f'{{"state":"{state}","headRefOid":"{head}","baseRefName":"{base}"}}'


def test_open_pr_at_the_planned_head_posts_the_comment(tmp_path):
    """The normal case must be untouched — this guard must not cost us C2."""
    assert _MARKER in _run(_pr(), tmp_path=tmp_path)


@pytest.mark.parametrize(
    ("pr_json", "because"),
    [
        (_pr(state="MERGED"), "merged while the plan was building — the ds-2wy case"),
        (_pr(state="CLOSED"), "closed while the plan was building"),
        (_pr(head="b" * 40), "force-pushed past the planned head"),
        (_pr(base="release"), "retargeted off main"),
    ],
)
def test_a_pr_that_moved_on_skips_the_comment(pr_json, because, tmp_path):
    out = _run(pr_json, tmp_path=tmp_path)
    assert _MARKER not in out, f"must skip: {because}"
    assert "C2 comment skipped" in out


def test_the_skip_explains_itself_in_the_step_summary(tmp_path):
    """A silent skip would read as "no plan was built" during an incident."""
    summary = tmp_path / "summary.md"
    _run(_pr(state="MERGED"), tmp_path=tmp_path)
    text = summary.read_text()
    assert "C2 plan comment SKIPPED" in text
    assert "no longer OPEN" in text


def test_head_comparison_is_exact_not_a_prefix(tmp_path):
    """A `case`/glob rewrite of this guard would silently accept a prefix."""
    out = _run(_pr(head=_HEAD[:39] + "b"), tmp_path=tmp_path)
    assert _MARKER not in out
