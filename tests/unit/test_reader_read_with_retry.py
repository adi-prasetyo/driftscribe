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
between attempts, the per-attempt timeout, and the total budget.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from agent.main import (
    _READER_ATTEMPT_TIMEOUT_S,
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


def test_the_retry_costs_less_wall_clock_than_the_read_it_replaced() -> None:
    """The budget argument, as an invariant rather than a comment.

    This read sits inside a request that has already spent 20-120s on the agent
    turn and may still owe the Rollback Worker's /propose, against Cloud Run's
    300s request timeout. Retrying is only safe because the whole ladder is
    cheaper than the single 30s-default call it replaced — so adding retries
    cannot push a caller past a deadline it previously made.

    If a future change raises the per-attempt timeout or adds an attempt, this
    fails and the deadline question gets re-asked deliberately instead of a
    timeout regression shipping unnoticed.
    """
    from agent.worker_client import _HTTPX_TIMEOUT, _WORKER_DEFAULT_TIMEOUTS

    assert "reader" not in _WORKER_DEFAULT_TIMEOUTS, (
        "premise: the reader inherits the default timeout, which is why an "
        "explicit per-attempt budget is required"
    )
    attempts = len(_READER_RETRY_DELAYS) + 1
    worst_case = _READER_ATTEMPT_TIMEOUT_S * attempts + sum(_READER_RETRY_DELAYS)
    assert worst_case < _HTTPX_TIMEOUT, (
        f"retry ladder worst case {worst_case}s must stay under the {_HTTPX_TIMEOUT}s "
        "single call it replaced"
    )
