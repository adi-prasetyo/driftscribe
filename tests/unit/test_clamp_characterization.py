"""Byte-for-byte characterization of ``normalize_rollback_reason``.

ds-thm refactors this function onto a shared ``clamp_middle_out`` helper so the
notifier-body and close-reason clamps do not each re-derive the marker
arithmetic. That arithmetic is subtle (the marker is sized against the FULL
length so the real, smaller omitted count can never exceed its own reservation)
and this is production-proven code that ds-j0i validated on a live incident.

So these tests are written and green BEFORE the refactor, and they assert exact
output rather than properties. A property test would let the refactor shift a
boundary by one character and still pass; the point here is that nothing moves
at all. Codex round 2 called this out as the highest-drift-risk item in the
change, and it is the cheapest possible insurance.

If a future change intends to alter this output, these are the tests to update
deliberately — they are a tripwire, not a specification.
"""
from __future__ import annotations

import pytest

from agent.renderer import (
    ROLLBACK_REASON_ABSENT,
    ROLLBACK_REASON_MAX_CHARS,
    clamp_middle_out,
    normalize_rollback_reason,
)


# --------------------------------------------------------------------------- #
# The shared helper's own contract, tested directly rather than only through
# its wrappers — the degenerate inputs are exactly the ones no wrapper reaches.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("cap", [0, -1, -1000])
def test_a_non_positive_cap_raises_instead_of_silently_inverting(cap: int) -> None:
    """``text[:negative]`` slices from the END under Python's negative-index
    semantics, so a helper that "handled" this would emit nearly the whole
    string while reporting that it had clamped. Callers pass module constants,
    so a non-positive cap is a programming error and must surface as one."""
    with pytest.raises(ValueError):
        clamp_middle_out("some text", cap)


def test_a_cap_too_small_for_the_marker_still_respects_the_cap() -> None:
    """Degenerate but reachable if a future consumer declares a tiny bound.
    The marker is dropped rather than overflowing the budget it was meant to
    fit inside."""
    head, marker, tail = clamp_middle_out("x" * 500, 10)
    assert len(head + marker + tail) <= 10


def test_the_reserve_is_subtracted_from_the_budget_not_the_output() -> None:
    """With a reserve, the parts must leave room for the caller's own repair —
    that is the whole reason the parameter exists."""
    parts = clamp_middle_out("x" * 5000, 1000, reserve=50)
    assert parts is not None
    assert len(parts[0] + parts[1] + parts[2]) <= 1000 - 50


def test_text_that_fits_needs_no_cut_even_with_a_reserve() -> None:
    """The reserve pays for a repair; no cut means no repair means no reserve.
    Returning a split here would truncate content the consumer accepts."""
    assert clamp_middle_out("x" * 1000, 1000, reserve=50) is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", ROLLBACK_REASON_ABSENT),
        ("   ", ROLLBACK_REASON_ABSENT),
        ("\n\t \n", ROLLBACK_REASON_ABSENT),
        ("PAYMENT_MODE drifted.", "PAYMENT_MODE drifted."),
        ("  padded  ", "padded"),
    ],
)
def test_short_and_empty_inputs_are_unchanged(raw: str, expected: str) -> None:
    assert normalize_rollback_reason(raw) == expected


def test_exactly_at_the_cap_is_returned_verbatim() -> None:
    """The boundary an off-by-one refactor would move first."""
    text = "x" * ROLLBACK_REASON_MAX_CHARS
    assert normalize_rollback_reason(text) == text


def test_one_over_the_cap_truncates() -> None:
    text = "x" * (ROLLBACK_REASON_MAX_CHARS + 1)
    out = normalize_rollback_reason(text)
    assert out != text
    assert len(out) <= ROLLBACK_REASON_MAX_CHARS


# The exact split for a known input. Hand-computed from the pre-refactor
# implementation so the refactor cannot silently redistribute the budget
# between head and tail: marker reserved against len(text), remaining budget
# halved with the odd character going to the TAIL (budget - budget // 2).
def test_the_head_tail_split_is_pinned_exactly() -> None:
    text = "H" * 3000 + "T" * 3000  # 6000 chars, well over the cap
    out = normalize_rollback_reason(text)

    marker_reserved = len("\n\n[… 6000 characters omitted by DriftScribe …]\n\n")
    budget = ROLLBACK_REASON_MAX_CHARS - marker_reserved
    head_budget = budget // 2
    tail_budget = budget - head_budget
    omitted = 6000 - head_budget - tail_budget

    expected = (
        "H" * head_budget
        + f"\n\n[… {omitted} characters omitted by DriftScribe …]\n\n"
        + "T" * tail_budget
    )
    assert out == expected
    assert len(out) <= ROLLBACK_REASON_MAX_CHARS


def test_the_operator_caveat_at_the_tail_survives() -> None:
    """The reason the split keeps a tail at all (ds-j0i): the model puts its
    caveat last, and a head-only cut drops exactly the sentence an operator
    most needs before approving a rollback."""
    caveat = "The chosen candidate is unverified; check it before approving."
    out = normalize_rollback_reason("A. " + "padding " * 4000 + caveat)
    assert out.endswith(caveat)
    assert out.startswith("A. padding")


@pytest.mark.parametrize("n", [1999, 2000, 2001, 2049, 4096, 10000, 65536])
def test_no_input_length_ever_exceeds_the_cap(n: int) -> None:
    """Includes the digit-rollover lengths where the marker's own rendered
    width changes, which is where a reservation bug would surface."""
    assert len(normalize_rollback_reason("z" * n)) <= ROLLBACK_REASON_MAX_CHARS


def test_multibyte_text_is_counted_in_CHARACTERS_not_bytes() -> None:
    """pydantic's max_length counts characters. A byte-based clamp would cut a
    Japanese rationale roughly three times too early while still passing the
    worker — silently losing two thirds of the operator's context."""
    out = normalize_rollback_reason("設定" * 2000)
    assert len(out) <= ROLLBACK_REASON_MAX_CHARS
    assert len(out.encode("utf-8")) > ROLLBACK_REASON_MAX_CHARS
