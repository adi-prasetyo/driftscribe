"""ds-thm — ``normalize_notifier_body``: the cap holds, and the cut is repaired.

This is the highest-risk piece of ds-thm, for a reason worth stating: the naive
implementation ("clamp to the cap, then close an orphaned fence") emits
``cap + 4`` characters and recreates the exact 422 the whole bead exists to
prevent. So the invariant under test is not "roughly fits" — it is that no
input of any shape produces output over the cap, INCLUDING the inputs that
trigger a repair.

Why repair at all, when the Notifier already repairs fences: it repairs only
the Discord ``content`` field. The handler sets ``text`` to the full body
verbatim ("Only ``content`` is capped"), so a cut made HERE that strands a
fence reaches Slack and every generic receiver unrepaired.
"""
from __future__ import annotations

import re

import pytest

from agent.renderer import NOTIFIER_BODY_MAX_CHARS, normalize_notifier_body

CAP = NOTIFIER_BODY_MAX_CHARS
_FENCE_RUN = re.compile(r"`{3,}")
_URL = "<https://driftscribe.example.com/approvals/abc?t=" + "T" * 43 + ">"


def _fences(text: str) -> int:
    return len(_FENCE_RUN.findall(text))


# --------------------------------------------------------------------------- #
# The cap, unconditionally
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("n", [0, 1, CAP - 1, CAP, CAP + 1, CAP * 3])
def test_the_cap_holds_for_plain_text(n: int) -> None:
    assert len(normalize_notifier_body("x" * n)) <= CAP


@pytest.mark.parametrize("pad", range(0, 40))
def test_the_cap_holds_across_the_repair_boundary(pad: int) -> None:
    """Lengths either side of the cut where the fence repair fires.

    The failure this catches is precise: reserve the repair AFTER cutting and
    these produce ``CAP + 4``. Sweeping a window rather than testing one length
    is what makes the off-by-one impossible to miss.
    """
    body = "```yaml\n" + "k: v\n" * ((CAP // 5) + pad)
    out = normalize_notifier_body(body)
    assert len(out) <= CAP, f"exceeded the cap by {len(out) - CAP} at pad={pad}"


def test_a_body_that_already_fits_is_returned_verbatim() -> None:
    body = "## DriftScribe\n\nAll good.\n\n" + _URL
    assert normalize_notifier_body(body) == body


@pytest.mark.parametrize("n", [CAP - 9, CAP - 8, CAP - 1, CAP])
def test_a_body_the_worker_would_accept_is_never_cut(n: int) -> None:
    """The repair reserve pays for repairs a CUT makes, so it must not decide
    WHETHER to cut. Subtracting it from the fits-already test truncated bodies
    of 9993..10000 against a 10000 cap — content the worker accepts verbatim,
    thrown away by the fix meant to protect it (Codex review of this change).
    """
    body = "x" * n
    assert normalize_notifier_body(body) == body


def test_empty_input_is_not_the_normalizers_problem() -> None:
    """The minimum is a POLICY decision made per call site (notify_tool refuses,
    the rollback body can never be empty), so this stays a pure size function."""
    assert normalize_notifier_body("") == ""


# --------------------------------------------------------------------------- #
# The two repair artifacts, which are different bugs
# --------------------------------------------------------------------------- #

def test_an_opener_stranded_in_the_head_is_closed() -> None:
    """Otherwise everything after it — the approval URL included — renders
    inside a code block: present, visible, and not clickable."""
    body = "intro\n```yaml\n" + "k: v\n" * 4000 + "\ntrailing prose\n" + _URL
    out = normalize_notifier_body(body)
    assert len(out) <= CAP
    before_url = out[: out.index("https://")]
    assert _fences(before_url) % 2 == 0, "the URL rendered inside a code block"


def test_an_orphan_closer_in_the_tail_gets_its_opener_back() -> None:
    """The mirror-image artifact, and the one a head-only repair misses.

    A tail holding a closer whose opener was deleted WAS inside a code block
    before the cut. Left alone that closer acts as an OPENER in the reassembled
    text and swallows everything after it. Prepending an opener reproduces the
    original nesting instead of guessing at it.
    """
    body = "a\n```\n" + "code\n" * 3000 + "```\nepilogue\n" + _URL
    out = normalize_notifier_body(body)
    assert len(out) <= CAP
    before_url = out[: out.index("https://")]
    assert _fences(before_url) % 2 == 0


def test_a_six_backtick_run_counts_as_one_delimiter() -> None:
    """``str.count("```")`` reports two for a six-backtick run and concludes,
    wrongly, that a block is balanced. Counting RUNS gets it right — the same
    distinction the Notifier's own counter makes."""
    body = "x\n``````\n" + "y\n" * 5000 + _URL
    out = normalize_notifier_body(body)
    assert len(out) <= CAP
    before_url = out[: out.index("https://")]
    assert _fences(before_url) % 2 == 0


def _url_renders_as_a_link(text: str) -> bool:
    """Ask a REAL CommonMark parser, not a backtick counter.

    This is the load-bearing choice in this file. Three hand-rolled fence
    heuristics were each wrong, and each shipped with a hand-rolled assertion
    that shared its blind spot — a parity check cannot detect a parity bug. So
    the oracle is markdown-it: render, and look for an actual link token
    carrying the URL. Inside a code block there is no link token, which is
    exactly the operator-visible failure ("present, but not clickable").
    """
    from markdown_it import MarkdownIt

    html = MarkdownIt().enable("linkify").render(text)
    return f'href="{_URL[1:-1]}"' in html


@pytest.mark.parametrize(
    "opener, mid",
    [
        ("```", ""),
        ("````", ""),
        ("``````", ""),
        ("`" * 12, ""),
        # Codex's counterexample: a 12-backtick opener with a THREE-backtick
        # line after it. Too short to close a 12-backtick fence, but a parity
        # counter sees two runs, calls it balanced, and leaves the block open
        # through the URL.
        ("`" * 12, "```\n"),
        ("````````", "```\n``````\n"),
    ],
)
def test_the_url_stays_clickable_for_every_fence_arrangement(
    opener: str, mid: str
) -> None:
    body = "intro\n" + opener + "\n" + mid + ("k: v\n" * 4000) + "\nepi\n" + _URL
    out = normalize_notifier_body(body)
    assert len(out) <= CAP
    assert _URL in out
    assert _url_renders_as_a_link(out), (
        f"a {len(opener)}-backtick block swallowed the approval URL"
    )


def test_a_fence_run_split_by_the_cut_still_leaves_the_url_reachable() -> None:
    """The cut can land INSIDE a delimiter, leaving a partial run in the head —
    a shorter fence than the author wrote, which the deleted closer no longer
    matches."""
    body = "a\n" + "`" * 8 + "\n" + ("x" * 3 + "\n") * 4000 + "\ntail\n" + _URL
    out = normalize_notifier_body(body)
    assert len(out) <= CAP
    assert _url_renders_as_a_link(out)


def test_inline_backtick_runs_in_prose_are_not_treated_as_fences() -> None:
    """The other direction, and the one the 'prepend an opener' repair got
    backwards: an inline ``` ``` ``` inside ordinary prose is not a fence, so
    prepending an opener to 'balance' it CREATES an unclosed block and makes
    harmless Markdown fail. Codex reproduced exactly that."""
    body = "x" * 6000 + "\nprose with ``` inline delimiters here\n\n" + _URL
    out = normalize_notifier_body(body)
    assert len(out) <= CAP
    assert _url_renders_as_a_link(out)


def test_an_untruncated_body_keeps_its_code_blocks_intact() -> None:
    """Neutralization is scoped to the truncated path. A body that fits is
    returned verbatim, fences and all — which is every ordinary notification."""
    body = "Observed:\n```yaml\nPAYMENT_MODE: live\n```\n\n" + _URL
    assert normalize_notifier_body(body) == body
    assert "```" in normalize_notifier_body(body)


def test_the_tail_survives_so_the_url_does() -> None:
    """The whole reason the cut is middle-out. A head slice would keep the
    header and drop the only actionable thing in the message."""
    body = "# header\n" + "filler " * 4000 + "\n\nApprove here: " + _URL
    out = normalize_notifier_body(body)
    assert out.endswith(_URL)
    assert "omitted by DriftScribe" in out


def test_model_text_containing_the_marker_cannot_confuse_the_repair() -> None:
    """The repair works on the head/tail FRAGMENTS, never by re-splitting the
    joined string on the marker — so a rationale that quotes the marker is just
    text, not a parsing hazard."""
    marker_ish = "[… 42 characters omitted by DriftScribe …]"
    body = "```\n" + (marker_ish + "\n") * 400 + "```\n" + _URL
    out = normalize_notifier_body(body)
    assert len(out) <= CAP
    before_url = out[: out.index("https://")]
    assert _fences(before_url) % 2 == 0


def test_multibyte_bodies_are_bounded_in_characters() -> None:
    """pydantic counts characters; a byte-based clamp would cut a JA body about
    three times too early while still passing the worker."""
    out = normalize_notifier_body("設定が変更されました。" * 2000)
    assert len(out) <= CAP
    assert len(out.encode("utf-8")) > CAP
