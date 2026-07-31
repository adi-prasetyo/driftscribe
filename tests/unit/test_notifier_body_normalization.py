"""ds-thm — ``normalize_notifier_body``: the cap holds, and the link survives.

Two invariants, and the second one cost four attempts to get right.

**The cap.** No input of any shape may produce output over it — including the
inputs that trigger post-cut work, since "clamp to the cap, then append" emits
more than the cap and recreates the exact 422 this clamp exists to prevent.

**The link.** A cut through arbitrary Markdown can leave a code delimiter
stranded, and anything inside code renders as inert text — so the approval URL
arrives present, visible, and unclickable, which is not delivery. It matters
here specifically because the Notifier repairs fences only on the Discord
``content`` field; it sets ``text`` to the full body verbatim ("Only
``content`` is capped"), so a cut made HERE reaches Slack and every generic
receiver unrepaired.

**The oracle is markdown-it, not a backtick counter.** Four fence *repairs*
were wrong before the current neutralize-everything approach, and every one
shipped alongside a hand-rolled assertion that shared its blind spot — a parity
check cannot detect a parity bug, and a backtick counter cannot see a ``~~~``
fence at all. So correctness questions here are settled by rendering the result
and looking for a real link token.
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
# The link survives the cut
#
# Every fixture below is a VALID document: the fence it opens is also closed,
# and the URL sits outside it, so the original renders a real link. The closer
# is then placed where the middle-out cut deletes it, which is the whole
# mechanism — a truncation that turns a balanced document into an unbalanced
# one. ``_assert_link_survives`` re-checks that premise on every call, because
# an earlier round of these fixtures opened a fence and never closed it: the
# ORIGINAL was already broken, so they could not have shown anything about the
# clamp either way.
# --------------------------------------------------------------------------- #

def _balanced(opener: str, closer: str | None = None, mid: str = "") -> str:
    """A document whose fence is opened in the HEAD and closed in the part the
    cut deletes, with the URL surviving in the tail."""
    closer = opener if closer is None else closer
    return (
        "intro\n" + opener + "\n"
        + "k: v\n" * 1200            # keeps the opener inside the retained head
        + mid
        + "z\n" * 2000               # ... and pushes the closer into the middle
        + closer + "\n"
        + "w\n" * 3000               # tail filler, no delimiters
        + "\nApprove here:\n\n" + _URL + "\n"
    )


def _url_renders_as_a_link(text: str) -> bool:
    """Ask a REAL CommonMark parser, not a backtick counter.

    This is the load-bearing choice in this file. Four hand-rolled fence
    heuristics were each wrong, and each shipped with a hand-rolled assertion
    that shared its blind spot — a parity check cannot detect a parity bug, and
    a backtick counter cannot see a ``~~~`` fence at all. So the oracle is
    markdown-it: render, and look for a real link token. Inside code there is
    no link token, which is precisely the operator-visible failure ("present,
    but not clickable").

    ``_URL`` is in ``<...>`` autolink form because the rollback renderer emits
    it that way. Do NOT switch fixtures to bare URLs: ``linkify`` is an OPT-IN
    markdown-it plugin requiring ``linkify-it-py``, which is not installed, so
    ``.enable("linkify")`` silently does nothing and a bare URL never links —
    in the original OR the truncated output. Two candidate fixtures were thrown
    out for exactly that: they "failed" for a reason that had nothing to do
    with the code under test. :func:`_assert_link_survives` guards against it.
    """
    from markdown_it import MarkdownIt

    return f'href="{_URL[1:-1]}"' in MarkdownIt().render(text)


def _assert_link_survives(body: str) -> None:
    """Normalize ``body`` and assert the URL is still clickable — after first
    checking it was clickable to begin with.

    The premise check is the point. Without it a fixture whose ORIGINAL does
    not render a link passes or fails for reasons unrelated to truncation, and
    reads as coverage it does not provide. That mistake was made twice here.
    """
    assert _url_renders_as_a_link(body), (
        "premise: this fixture must render a link BEFORE truncation, or the "
        "assertion below proves nothing about the clamp"
    )
    out = normalize_notifier_body(body)
    assert len(out) <= CAP
    assert _url_renders_as_a_link(out)


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
    _assert_link_survives(_balanced(opener, mid=mid))


def test_a_fence_run_split_by_the_cut_still_leaves_the_url_reachable() -> None:
    """The cut can land INSIDE a delimiter, leaving a partial run in the head —
    a shorter fence than the author wrote, which the deleted closer no longer
    matches."""
    _assert_link_survives(_balanced("`" * 8))


def test_inline_backtick_runs_in_prose_are_not_treated_as_fences() -> None:
    """The other direction, and the one the 'prepend an opener' repair got
    backwards: an inline ``` ``` ``` inside ordinary prose is not a fence, so
    prepending an opener to 'balance' it CREATES an unclosed block and makes
    harmless Markdown fail. Codex reproduced exactly that."""
    _assert_link_survives(
        "x" * 6000 + "\n\nprose with ``` inline delimiters here\n\n" + _URL + "\n"
    )


def test_a_tilde_fence_is_neutralized_too() -> None:
    """CommonMark has TWO fence characters. A backtick-only rule leaves the
    other half live, and the failure is identical: the URL sits inside an
    unclosed ``~~~`` block and renders as text."""
    _assert_link_survives(_balanced("~~~yaml", closer="~~~"))


def test_removing_a_backtick_cannot_create_a_tilde_fence() -> None:
    """The neutralizer must not manufacture the delimiter it removes.

    ``re.sub`` matches the ORIGINAL string and never rescans its own output, so
    a single alternation over both alphabets turns ``"~~`~"`` into ``"~~~"`` —
    a brand-new fence, created by the function whose entire job is to leave
    none. Backticks are therefore removed in a pass of their own, before
    tildes.
    """
    from agent.renderer import _neutralize_fences

    # "~~" + "`" + "~" -> pass 1 leaves "~~~", which pass 2 then removes.
    assert _neutralize_fences("~~`~") == ""
    for probe in ("~~`~", "~`~`~`~", "~~`~~`~~"):
        assert not re.search(r"~{3,}", _neutralize_fences(probe)), probe
        assert "`" not in _neutralize_fences(probe)


@pytest.mark.parametrize(
    "delims",
    ["`", "``", "```", "````````", "~~~", "~~~~~~", "`` ` ``` ~~~", "~~`~"],
)
def test_a_truncated_body_retains_no_code_delimiter_of_any_kind(delims: str) -> None:
    """The invariant the implementation actually provides, asserted directly.

    This is deliberately a property test rather than a scenario. Codex reported
    that one- and two-backtick spans can re-pair *inside the retained tail* and
    enclose the URL, and I could not reproduce it: my constructions either
    stayed under the cap (so no cut happened) or failed to render a link in the
    ORIGINAL too, which would make it a pre-existing defect rather than one the
    cut introduced.

    Rather than ship a scenario test that passes for a reason I cannot name —
    the failure mode this whole change keeps hitting — the alphabet was widened
    to cover the class anyway (it only ever shrinks, so it costs nothing) and
    the test asserts the thing that is true by construction: after a cut, no
    delimiter capable of opening code survives in the output. Nothing can
    re-pair if nothing remains, which makes the reproduction question moot.
    """
    body = f"prefix {delims} filler\n" + ("y" * 60 + "\n") * 300 + f"{delims} tail\n" + _URL
    out = normalize_notifier_body(body)
    assert len(out) <= CAP
    assert "`" not in out, "a backtick survived a truncated fragment"
    assert not re.search(r"~{3,}", out), "a tilde fence survived a truncated fragment"
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
