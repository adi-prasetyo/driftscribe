"""ds-q38 — the Reader read that corroborates the agent's observation.

The coherence gate refuses to record anything when this read fails. That is the
right call for correctness, but on the autonomous lane the refusal has no retry
driver behind it: a failed read is not a Cloud Run mutation, so it produces no
Eventarc delivery, and the coalescer only reruns on a delivery that sets
``dirty``. Hence :func:`_read_with_retry`.

Codex round 5 flagged that the retry was asserted only through an all-fail
integration test, which would still pass if the helper made a SINGLE attempt —
i.e. the liveness fix was unpinned in exactly the way that lets it silently
regress to the behaviour it replaced. These pin the attempt count, the sleeps
between attempts, and the per-attempt timeout.

Round 6 then corrected what the BUDGET tests are allowed to claim. Two
different bounds live here and conflating them is how the false guarantee got
written in the first place:

* the **configured** budget (per-attempt timeout x attempts + sleeps) is only a
  comparison against the 30s default this read would otherwise inherit — a
  scalar httpx timeout is per-phase, and ID-token minting happens outside it,
  so this is not wall-clock;
* the **admission budget** is NOT a deadline either — round 7 corrected that
  too. It gates whether the next attempt may start, so it bounds the attempt
  COUNT under slowness; it cannot interrupt a call already in flight, and three
  ordinary failures still cost more than it.

Total wall clock is bounded by neither. Saying so plainly here because this
same guarantee was overstated twice.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from agent.main import (
    _READER_ATTEMPT_TIMEOUT_S,
    _READER_RETRY_ADMISSION_S,
    _READER_RETRY_DELAYS,
    _read_with_retry,
)

_PAYLOAD = {"env": {"PAYMENT_MODE": "live"}, "revision": "payment-demo-00021-t8k"}


class _Boom(Exception):
    """Stands in for a WorkerClientError / transport failure."""


def _harness(*outcomes: Any):
    """Patch the reader call to yield ``outcomes`` in order, and swallow the
    sleeps so the test does not actually wait. Returns (calls, slept)."""
    calls: list[dict] = []
    slept: list[float] = []
    remaining = list(outcomes)

    def _call(worker: str, payload: dict, **kwargs: Any) -> dict:
        calls.append({"worker": worker, "payload": payload, **kwargs})
        outcome = remaining.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def _sleep(d: float) -> None:
        slept.append(d)

    return calls, slept, _call, _sleep


async def test_a_healthy_reader_is_called_once_and_never_slept_on() -> None:
    """The overwhelmingly common path must cost nothing extra."""
    calls, slept, _call, _sleep = _harness(_PAYLOAD)
    with (
        patch("agent.main.worker_client.call", _call),
        patch("agent.main.asyncio.sleep", _sleep),
    ):
        assert await _read_with_retry() == _PAYLOAD
    assert len(calls) == 1
    assert slept == []


async def test_one_transient_failure_is_retried_and_succeeds() -> None:
    """The case the helper exists for: a blip must not cost the whole audit."""
    calls, slept, _call, _sleep = _harness(_Boom("connreset"), _PAYLOAD)
    with (
        patch("agent.main.worker_client.call", _call),
        patch("agent.main.asyncio.sleep", _sleep),
    ):
        assert await _read_with_retry() == _PAYLOAD
    assert len(calls) == 2, "a single failure must not end the read"
    assert slept == [_READER_RETRY_DELAYS[0]]


async def test_two_transient_failures_are_retried_and_succeed() -> None:
    """Pins the SECOND retry specifically. Without this, a helper that made
    exactly two attempts would pass every other test in this file."""
    calls, slept, _call, _sleep = _harness(_Boom("1"), _Boom("2"), _PAYLOAD)
    with (
        patch("agent.main.worker_client.call", _call),
        patch("agent.main.asyncio.sleep", _sleep),
    ):
        assert await _read_with_retry() == _PAYLOAD
    assert len(calls) == 3
    assert slept == list(_READER_RETRY_DELAYS)


async def test_a_reader_that_is_genuinely_down_gives_up_and_reraises() -> None:
    """Bounded, and it re-raises the LAST failure rather than the first — the
    caller logs it, and the most recent one describes the current state of the
    world. Giving up is correct: the gate then refuses to record, which loses an
    audit (recoverable) instead of poisoning a key (not)."""
    last = _Boom("still down")
    calls, slept, _call, _sleep = _harness(_Boom("1"), _Boom("2"), last)
    with (
        patch("agent.main.worker_client.call", _call),
        patch("agent.main.asyncio.sleep", _sleep),
    ):
        with pytest.raises(_Boom) as excinfo:
            await _read_with_retry()
    assert excinfo.value is last
    assert len(calls) == 3, "bounded: it must not spin"
    assert slept == list(_READER_RETRY_DELAYS), "no sleep after the last attempt"


async def test_every_attempt_carries_the_explicit_per_attempt_timeout() -> None:
    """``worker_client.call`` has no "reader" entry in _WORKER_DEFAULT_TIMEOUTS,
    so omitting this would silently inherit the 30s default and make three
    attempts cost ~92s. The timeout is the entire reason the retry is
    affordable, so it is asserted on EVERY attempt, not just the first."""
    calls, _slept, _call, _sleep = _harness(_Boom("1"), _Boom("2"), _PAYLOAD)
    with (
        patch("agent.main.worker_client.call", _call),
        patch("agent.main.asyncio.sleep", _sleep),
    ):
        await _read_with_retry()
    assert [c.get("timeout") for c in calls] == [_READER_ATTEMPT_TIMEOUT_S] * 3


def test_the_configured_retry_budget_stays_under_the_default_it_replaced() -> None:
    """The budget comparison, named for what it actually proves.

    ⚠️ This is a CONFIGURED-budget assertion, not a wall-clock one, and the
    distinction is the whole reason the test is named this way. An earlier
    version claimed the ladder "cannot push any caller past a deadline it used
    to make" — false, as Codex round 6 pointed out:

    * a scalar httpx timeout applies **per phase** (connect / read / write /
      pool), so one attempt is not bounded end to end by 9s;
    * ``worker_client.call`` mints an ID token before the HTTPX client is
      reached, with its own metadata-server retries, repeated per attempt.

    So what is pinned here is narrow and true: the configured per-attempt
    budget times the attempt count, plus the sleeps, stays under the 30s
    default this read would otherwise inherit. That keeps a future change from
    quietly restoring the ~92s ladder.

    There is no end-to-end bound anywhere in this file to point at. The next
    test covers retry ADMISSION, which bounds the attempt count under slowness
    and nothing more.
    """
    from agent.worker_client import _HTTPX_TIMEOUT, _WORKER_DEFAULT_TIMEOUTS

    assert "reader" not in _WORKER_DEFAULT_TIMEOUTS, (
        "premise: the reader inherits the default timeout, which is why an "
        "explicit per-attempt budget is required"
    )
    attempts = len(_READER_RETRY_DELAYS) + 1
    configured = _READER_ATTEMPT_TIMEOUT_S * attempts + sum(_READER_RETRY_DELAYS)
    assert configured < _HTTPX_TIMEOUT, (
        f"configured retry budget {configured}s must stay under the "
        f"{_HTTPX_TIMEOUT}s single call it replaced"
    )


async def test_a_slow_reader_stops_accumulating_attempts() -> None:
    """Admission control, named for what it is — NOT a deadline.

    ⚠️ Read the numbers before trusting the name. This simulates attempts of
    20s each, so the run it describes takes ~40.5s of simulated time and then
    stops. That is well past ``_READER_RETRY_ADMISSION_S``, and it is supposed
    to be: the check gates whether the NEXT attempt may start, and cannot
    interrupt one already running. An earlier version of this test called that
    "stopping at a 25s deadline" while asserting only the call count — the
    assertion was fine, the claim around it was not (Codex round 7).

    So what is pinned is the attempt COUNT under slowness: retries must not
    keep stacking on a reader that is answering slowly, which is the case where
    retrying makes things worse rather than better.
    """
    now = [0.0]
    calls: list[int] = []

    def _call(worker: str, payload: dict, **_kw: Any) -> dict:
        calls.append(1)
        now[0] += 20.0  # slow, but not failing fast
        raise _Boom("slow")

    async def _sleep(d: float) -> None:
        now[0] += d

    class _Loop:
        def time(self) -> float:
            return now[0]

    with (
        patch("agent.main.worker_client.call", _call),
        patch("agent.main.asyncio.sleep", _sleep),
        patch("agent.main.asyncio.get_running_loop", lambda: _Loop()),
    ):
        with pytest.raises(_Boom):
            await _read_with_retry()

    assert len(calls) == 2, (
        "a third attempt must not be admitted once 20s+ has elapsed against "
        f"the {_READER_RETRY_ADMISSION_S}s admission budget; got {len(calls)}"
    )
    assert now[0] > _READER_RETRY_ADMISSION_S, (
        "and this is deliberately NOT a wall-clock bound: the run overshoots "
        "the admission budget because the attempt already in flight cannot be "
        "interrupted. Pinned so the weaker guarantee is not re-read as a "
        "deadline."
    )


async def test_a_fast_failing_reader_still_gets_all_three_attempts() -> None:
    """The admission budget must not silently eat the retries it is meant to
    bound — the failure mode of a too-tight budget is the liveness bug this
    whole helper exists to prevent. (Called a budget, not a deadline: see the
    module docstring for why that distinction was overstated twice.)"""
    now = [0.0]
    calls: list[int] = []

    def _call(worker: str, payload: dict, **_kw: Any) -> dict:
        calls.append(1)
        now[0] += 0.05  # connection refused: fails fast
        raise _Boom("refused")

    async def _sleep(d: float) -> None:
        now[0] += d

    class _Loop:
        def time(self) -> float:
            return now[0]

    with (
        patch("agent.main.worker_client.call", _call),
        patch("agent.main.asyncio.sleep", _sleep),
        patch("agent.main.asyncio.get_running_loop", lambda: _Loop()),
    ):
        with pytest.raises(_Boom):
            await _read_with_retry()

    assert len(calls) == len(_READER_RETRY_DELAYS) + 1
