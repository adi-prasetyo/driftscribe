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
* the **deadline** is the real end-to-end bound, checked against the loop clock
  between attempts, and it is what stops a slow-but-not-failing reader from
  stacking three attempts up.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from agent.main import (
    _READER_ATTEMPT_TIMEOUT_S,
    _READER_RETRY_DEADLINE_S,
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
    quietly restoring the ~92s ladder. The end-to-end bound that IS enforced
    lives in the next test.
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


async def test_a_slow_reader_stops_the_ladder_at_the_deadline() -> None:
    """The bound that is genuinely enforced end to end.

    Per-attempt timeouts are per-phase, so three attempts against a slow (not
    failing) reader could stack up well past the configured budget. The elapsed
    check against the loop clock is what stops that, and it is checked BEFORE
    the sleep so the sleep cannot be what overshoots.

    Simulated by advancing a fake loop clock inside the failing call, which is
    the only way to exercise a deadline without actually burning the time.
    """
    now = [0.0]
    calls: list[int] = []

    def _call(worker: str, payload: dict, **_kw: Any) -> dict:
        calls.append(1)
        now[0] += 20.0  # a slow attempt, well inside the deadline on its own
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
        "after 20s elapsed, a second attempt is fine but a third must be cut "
        f"off by the {_READER_RETRY_DEADLINE_S}s deadline; got {len(calls)}"
    )


async def test_a_fast_failing_reader_still_gets_all_three_attempts() -> None:
    """The deadline must not silently eat the retries it is meant to bound —
    the failure mode of a too-tight budget is the liveness bug this whole
    helper exists to prevent."""
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
