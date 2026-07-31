"""ds-j0i — the seam between ``_do_rollback`` and the Rollback Worker payload.

``test_propose_reason_boundary`` proves the normalizer is correct and that the
coordinator's constant matches the worker's declared cap. Neither proves the
normalizer is actually WIRED INTO the propose call — and that gap is the whole
bug: every piece was individually fine on 2026-07-31 while the payload that
went out was still unbounded.

This is the same lesson ds-q38 paid for twice (a ContextVar capture that was
inert in production, and a projection tested everywhere except at the caller).
So these drive the real ``_do_rollback`` and assert on the dict that would
reach ``worker_client.call("rollback", ...)``.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agent.config import get_settings
from agent.main import _reset_state_for_tests, app
from agent.models import ContractStatus, DecisionAction, DecisionProposal, EnvDiff
from agent.renderer import ROLLBACK_REASON_ABSENT, ROLLBACK_REASON_MAX_CHARS

_DRIFTED = {"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "false"}


@pytest.fixture(autouse=True)
def _adk_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()


def _rollback_proposal(rationale: str, live: str = "live") -> DecisionProposal:
    return DecisionProposal(
        action=DecisionAction.ROLLBACK,
        env_diffs=[
            EnvDiff(
                name="PAYMENT_MODE",
                expected="mock",
                live=live,
                contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,
            )
        ],
        target_docs_file=None,
        target_docs_section=None,
        target_revision="payment-demo-00020-5qn",
        rationale=rationale,
        confidence=0.95,
        requires_human_review=True,
    )


def _run(rationale: str, live: str = "live") -> dict[str, Any]:
    """Drive a real /recheck rollback and return the propose payload."""
    captured: dict[str, Any] = {}

    async def _agent(*_a: Any, reader_sink: list | None = None, **_k: Any) -> Any:
        # ds-q38: the coherence gate refuses unless the agent reports a read
        # matching the coordinator's post-turn read, so the double must read.
        payload = worker_call("reader", {})
        if reader_sink is not None and isinstance(payload.get("env"), dict):
            reader_sink.append(
                {"env": dict(payload["env"]),
                 "revision": payload.get("revision") or None}
            )
        return _rollback_proposal(rationale, live)

    def worker_call(worker: str, payload: dict, *a: Any, **k: Any) -> Any:
        if worker == "reader":
            return {
                "service": "payment-demo", "region": "asia-northeast1",
                "project": "test-project", "env": _DRIFTED,
                "revision": "payment-demo-00042-cur",
            }
        if worker == "rollback":
            captured.update(payload)
            return {
                "approval_id": "8f14e45f-ceea-467a-9a3c-2b1d5f6a7b8c",
                "approval_token": "t" * 43,
                "approval_url":
                    "/approvals/8f14e45f-ceea-467a-9a3c-2b1d5f6a7b8c?t=" + "t" * 43,
                "expires_at": "2099-01-01T00:00:00+00:00",
            }
        if worker == "notifier":
            return {"status": "ok", "channel": "approval", "severity": "high",
                    "downstream_status": 200}
        raise AssertionError(f"unexpected worker: {worker!r}")

    with (
        patch("agent.main._run_adk_agent", AsyncMock(side_effect=_agent)),
        patch("agent.main.worker_client.call", side_effect=worker_call),
    ):
        r = TestClient(app).post("/recheck")
    assert r.status_code == 200, r.text
    assert captured, "premise: the rollback worker was actually called"
    return captured


def test_the_incident_rationale_reaches_the_worker_intact() -> None:
    """581 chars — the length that 422'd on prod — now passes through whole."""
    rationale = (
        "Variable PAYMENT_MODE has drifted from its contracted expected value "
        "of 'mock' to 'live'. " + "Context follows. " * 28
    )
    assert 500 < len(rationale) <= ROLLBACK_REASON_MAX_CHARS, "premise"
    payload = _run(rationale)
    # .strip() is deliberate (a whitespace-only rationale must become the
    # fallback, not a min_length=1 pass), so compare against the stripped form.
    assert payload["reason"] == rationale.strip(), "nothing clipped under the cap"


def test_an_enormous_rationale_is_bounded_before_it_leaves_the_coordinator() -> None:
    """The wiring, which is the thing this file exists for. If
    ``normalize_rollback_reason`` were removed from the call site, every other
    ds-j0i test would still pass and prod would 422 again."""
    payload = _run("A. " + "padding " * 4000 + "Verify the target first.")
    assert len(payload["reason"]) <= ROLLBACK_REASON_MAX_CHARS
    assert "omitted by DriftScribe" in payload["reason"]
    # The operator caveat is last in real model output, so the tail must survive
    # the trip through the call site too — not just through the helper.
    assert payload["reason"].endswith("Verify the target first.")


def test_an_empty_rationale_becomes_the_fallback_not_a_422() -> None:
    """The worker requires ``min_length=1``. An empty model rationale must
    become operator-facing text at the call site, not an empty string."""
    payload = _run("   ")
    assert payload["reason"] == ROLLBACK_REASON_ABSENT
    assert len(payload["reason"]) >= 1


def test_the_reason_is_scrubbed_before_it_is_clamped() -> None:
    """Order matters and is easy to get backwards. Redaction changes length, so
    clamping first could hand the worker an over-cap string after scrubbing —
    or cut a credential in half so the scrubber no longer matches it.

    The credentialed value here is long enough that its removal is visible, and
    the assertion is that the secret is gone, not merely that the text is
    short.
    """
    secret = "https://admin:hunter2SECRETVALUE@svc.internal/api"
    # The scrubber redacts values it can see in ``env_diffs`` — so the secret
    # has to be the DIFFED live value, exactly as it would be in production
    # when a credentialed env var drifts. Quoting it only in the prose would
    # test a scrubber that was never given anything to match.
    payload = _run(
        f"PAYMENT_MODE drifted to {secret} which violates policy.", live=secret
    )
    assert "hunter2SECRETVALUE" not in payload["reason"]
    assert secret not in payload["reason"]
    assert len(payload["reason"]) <= ROLLBACK_REASON_MAX_CHARS
