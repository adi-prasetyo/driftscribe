"""ds-thm — every bounded payload, validated against its consumer's real model.

The ds-j0i lesson, paid for on production: the normalizer, the schema and the
call site can each be correct while the WIRING between them is not. Unwiring
``normalize_rollback_reason`` from its call site left all 78 boundary and worker
unit tests green while prod 422'd.

So these drive the REAL producers, capture the payload that would go on the
wire, and hand it to the consumer's OWN pydantic model. A test that runs the
worker's validator cannot drift from the worker's schema.

That complements — and does not replace — the equality pins in
``tests/unit/test_worker_bound_mirrors.py``. Validation alone would accept a
coordinator constant of 9000 against a worker cap of 10000; equality alone
would not notice the call site forgetting to call the normalizer. Both, or
neither is worth much.
"""
from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, patch

# Set before the worker imports (they read config at import and fail closed),
# then RESTORED below. Leaving them set leaks this module's config into every
# suite running later in the same pytest process — see the same note in
# tests/unit/test_worker_bound_mirrors.py for the failure it caused.
_PRIOR_ENV: dict[str, str | None] = {}

# Values match each worker's own test file exactly — `setdefault` is
# first-import-wins across the whole pytest process, so a different-but-valid
# value here silently overrides what another suite asserts on.
for _k, _v in {
    "GCP_PROJECT": "test-proj",
    "ALLOWED_CALLERS": "coordinator@test-proj.iam.gserviceaccount.com",
    "NOTIFY_WEBHOOK_URL": "https://webhook.example.com/test",
    "COORDINATOR_URL": "https://coord.example.com",
    "TARGET_REPO": "adi-prasetyo/driftscribe",
    "UPGRADE_TARGET_REPO": "adi-prasetyo/driftscribe",
    "GITHUB_TOKEN": "test-token",
    "APPROVAL_HMAC_KEY": "test-hmac-key",
}.items():
    _PRIOR_ENV[_k] = os.environ.get(_k)
    os.environ.setdefault(_k, _v)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from agent import adk_tools  # noqa: E402
from agent.config import get_settings  # noqa: E402
from agent.main import _reset_state_for_tests, app  # noqa: E402
from agent.models import (  # noqa: E402
    ContractStatus,
    DecisionAction,
    DecisionProposal,
    EnvDiff,
)
# OWN_URL is the one value three workers pin differently; set each worker's own
# canon immediately before importing it (see test_worker_bound_mirrors.py).
_PRIOR_ENV["OWN_URL"] = os.environ.get("OWN_URL")

os.environ["OWN_URL"] = "https://notifier.example.com"
from workers.notifier.main import NotifyRequest  # noqa: E402

os.environ["OWN_URL"] = "https://upgrade-docs.example.com"
from workers.upgrade_docs.main import ClosePrRequest  # noqa: E402

for _k, _old in _PRIOR_ENV.items():
    if _old is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _old

_DRIFTED = {"PAYMENT_MODE": "live"}


@pytest.fixture(autouse=True)
def _adk_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()


# --------------------------------------------------------------------------- #
# Seam 1 — the autonomous rollback notification body
# --------------------------------------------------------------------------- #

def _run_rollback(rationale: str) -> dict[str, Any]:
    """Drive a real /recheck rollback; return the captured notifier payload."""
    captured: dict[str, Any] = {}

    async def _agent(*_a: Any, reader_sink: list | None = None, **_k: Any) -> Any:
        # ds-q38's coherence gate refuses unless the agent reports a read
        # matching the coordinator's own post-turn read.
        payload = worker_call("reader", {})
        if reader_sink is not None and isinstance(payload.get("env"), dict):
            reader_sink.append(
                {"env": dict(payload["env"]), "revision": payload.get("revision")}
            )
        return DecisionProposal(
            action=DecisionAction.ROLLBACK,
            env_diffs=[
                EnvDiff(
                    name="PAYMENT_MODE",
                    expected="mock",
                    live="live",
                    contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,
                )
            ],
            target_revision="payment-demo-00020-5qn",
            rationale=rationale,
            confidence=0.95,
            requires_human_review=True,
        )

    def worker_call(worker: str, payload: dict, *a: Any, **k: Any) -> Any:
        if worker == "reader":
            return {
                "service": "payment-demo", "region": "asia-northeast1",
                "project": "test-project", "env": _DRIFTED,
                "revision": "payment-demo-00042-cur",
            }
        if worker == "rollback":
            return {
                "approval_id": "8f14e45f-ceea-467a-9a3c-2b1d5f6a7b8c",
                "approval_token": "t" * 43,
                "approval_url":
                    "https://coordinator.example.com/approvals/"
                    "8f14e45f-ceea-467a-9a3c-2b1d5f6a7b8c?t=" + "t" * 43,
                "expires_at": "2099-01-01T00:00:00+00:00",
            }
        if worker == "notifier":
            captured.update(payload)
            return {"status": "ok", "channel": "approval", "severity": "high",
                    "downstream_status": 200}
        raise AssertionError(f"unexpected worker: {worker!r}")

    with (
        patch("agent.main._run_adk_agent", AsyncMock(side_effect=_agent)),
        patch("agent.main.worker_client.call", side_effect=worker_call),
    ):
        r = TestClient(app).post("/recheck")
    assert r.status_code == 200, r.text
    assert captured, "premise: the notifier was actually called"
    return captured


def test_an_enormous_rationale_still_produces_a_valid_notify_request() -> None:
    """The wiring this file exists for. Drop ``normalize_notifier_body`` from
    the call site and every unit test still passes while this goes red."""
    payload = _run_rollback("A. " + "padding " * 6000 + "Verify the target.")
    NotifyRequest(**payload)  # the worker's OWN validator


def test_the_approval_url_survives_the_coordinator_side_cut() -> None:
    """A bounded notification that dropped the link would be worse than a
    failed one: it reads as delivered while stranding the operator."""
    payload = _run_rollback("A. " + "padding " * 6000)
    assert "/approvals/8f14e45f-ceea-467a-9a3c-2b1d5f6a7b8c?t=" in payload["body"]


@pytest.mark.parametrize(
    "worker_url, expected_href",
    [
        # The reachable case: a worker booted with an empty COORDINATOR_URL.
        (
            "/approvals/8f14e45f-ceea-467a-9a3c-2b1d5f6a7b8c?t=" + "t" * 43,
            "https://coord.example.com/approvals/"
            "8f14e45f-ceea-467a-9a3c-2b1d5f6a7b8c?t=" + "t" * 43,
        ),
        # ⚠️ An UPPERCASE scheme. ``_validated_approval`` accepts it (urlsplit
        # lowercases the scheme), so a ``startswith(("http://", "https://"))``
        # test disagrees with the validator and treats it as relative —
        # producing ``https://coord.example.com/HTTPS://worker…``, a link that
        # renders perfectly and points at a coordinator path that does not
        # exist. Two checks for one question must not use two definitions.
        (
            "HTTPS://worker.example.com/approvals/"
            "8f14e45f-ceea-467a-9a3c-2b1d5f6a7b8c?t=" + "t" * 43,
            "HTTPS://worker.example.com/approvals/"
            "8f14e45f-ceea-467a-9a3c-2b1d5f6a7b8c?t=" + "t" * 43,
        ),
    ],
)
def test_a_relative_approval_url_is_made_absolute_before_it_is_rendered(
    monkeypatch: pytest.MonkeyPatch, worker_url: str, expected_href: str
) -> None:
    """ds-thm: ``_approval_url_matches`` deliberately accepts the relative
    ``/approvals/{id}?t=…`` form (a worker whose COORDINATOR_URL has drifted
    would otherwise lose rollbacks entirely), but a CommonMark autolink
    requires an ABSOLUTE URI — ``<​/approvals/…>`` renders as literal text with
    no link at all.

    So the shapes the validator admits are wider than the shapes the renderer
    can make clickable: this bead's producer/consumer mismatch, in URL shape
    rather than length. Canonicalized against our own configured origin rather
    than rejected, because rejecting after the approval is minted would strand
    it (ds-hdt).
    """
    from markdown_it import MarkdownIt

    monkeypatch.setenv("COORDINATOR_ORIGIN", "https://coord.example.com")
    get_settings.cache_clear()

    captured: dict[str, Any] = {}

    async def _agent(*_a: Any, reader_sink: list | None = None, **_k: Any) -> Any:
        payload = worker_call("reader", {})
        if reader_sink is not None:
            reader_sink.append(
                {"env": dict(payload["env"]), "revision": payload.get("revision")}
            )
        return DecisionProposal(
            action=DecisionAction.ROLLBACK,
            env_diffs=[
                EnvDiff(
                    name="PAYMENT_MODE", expected="mock", live="live",
                    contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,
                )
            ],
            target_revision="payment-demo-00020-5qn",
            rationale="drifted", confidence=0.95, requires_human_review=True,
        )

    def worker_call(worker: str, payload: dict, *a: Any, **k: Any) -> Any:
        if worker == "reader":
            return {
                "service": "payment-demo", "region": "asia-northeast1",
                "project": "test-project", "env": _DRIFTED,
                "revision": "payment-demo-00042-cur",
            }
        if worker == "rollback":
            return {
                "approval_id": "8f14e45f-ceea-467a-9a3c-2b1d5f6a7b8c",
                "approval_token": "t" * 43,
                "approval_url": worker_url,
                "expires_at": "2099-01-01T00:00:00+00:00",
            }
        if worker == "notifier":
            captured.update(payload)
            return {"status": "ok"}
        raise AssertionError(worker)

    with (
        patch("agent.main._run_adk_agent", AsyncMock(side_effect=_agent)),
        patch("agent.main.worker_client.call", side_effect=worker_call),
    ):
        r = TestClient(app).post("/recheck")
    assert r.status_code == 200, r.text
    assert captured, "premise: the notifier was called"

    assert f'href="{expected_href}"' in MarkdownIt().render(captured["body"]), (
        "the approval url did not render as a link to the right place — the "
        "operator receives a notification that is unusable or misdirected"
    )


def test_an_ordinary_rationale_is_not_truncated_at_all() -> None:
    """Truncation is a last resort, not the common path — ds-j0i observed 581
    characters against a 2000 cap. If this ever starts failing, the clamp is
    firing on ordinary traffic and the bound is the thing to re-examine."""
    payload = _run_rollback("PAYMENT_MODE drifted from 'mock' to 'live'. " * 8)
    NotifyRequest(**payload)
    assert "omitted by DriftScribe" not in payload["body"]


# --------------------------------------------------------------------------- #
# Seam 2 — notify_tool (model-authored body)
# --------------------------------------------------------------------------- #

def test_a_model_authored_notify_body_is_bounded_before_it_leaves() -> None:
    captured: dict[str, Any] = {}

    def _call(worker: str, payload: dict, *a: Any, **k: Any) -> Any:
        captured.update(payload)
        return {"status": "ok"}

    with patch("agent.adk_tools.worker_client.call", side_effect=_call):
        adk_tools.notify_tool("alert", "high", "x" * 50_000)
    NotifyRequest(**captured)


@pytest.mark.parametrize("body", ["", "   ", "\n\t"])
def test_an_empty_notify_body_never_reaches_the_worker(body: str) -> None:
    """``min_length=1`` would 422, and ``notify_tool`` reports only the STATUS
    to the model — so it would learn "422" and never that the body was empty."""
    called = False

    def _call(*a: Any, **k: Any) -> Any:
        nonlocal called
        called = True
        return {"status": "ok"}

    with patch("agent.adk_tools.worker_client.call", side_effect=_call):
        out = adk_tools.notify_tool("alert", "high", body)
    assert not called, "an empty body must not be sent at all"
    assert out["delivered"] is False
    assert "empty" in out["error"]


# --------------------------------------------------------------------------- #
# Seam 3 — upgrade_close_pr_tool (model-authored reason)
# --------------------------------------------------------------------------- #

def _close_pr(reason: str) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def _call(worker: str, payload: dict, *a: Any, **k: Any) -> Any:
        captured.update(payload)
        return {"closed": True}

    with patch("agent.adk_tools.worker_client.call", side_effect=_call):
        adk_tools.upgrade_close_pr_tool(7, reason)
    assert captured, "premise: the worker was actually called"
    return captured


def test_a_long_close_reason_is_bounded() -> None:
    ClosePrRequest(**_close_pr("because " * 5000))


def test_an_empty_close_reason_still_closes_the_pr_and_says_so() -> None:
    """Opposite call from notify_tool, deliberately: closing the PR is the
    action the OPERATOR asked for and the reason is auxiliary audit context.
    Preserve the action — but disclose the omission rather than invent a
    motive the model never supplied."""
    payload = _close_pr("   ")
    ClosePrRequest(**payload)
    assert "no reason was supplied" in payload["reason"]


# --------------------------------------------------------------------------- #
# Seam 4 — propose_rollback_tool refuses a bad revision instead of laundering
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "bad",
    [
        "x" * 100,                       # over the worker's 64
        "Payment-Demo-00024",            # uppercase
        "../../etc/passwd",              # traversal shape
        "payment-demo-00024-f6v\nrm -rf /",  # embedded newline
        # ⚠️ TRAILING newline — the one that got through. Python's ``$`` also
        # matches just before a final newline, so ``re.match`` accepted this
        # while the worker's Rust-regex pattern rejected it, putting the model
        # right back on the opaque-422 path this guard exists to remove. The
        # in-string case above cannot catch it: only a TRAILING newline hits
        # the ``$`` leniency. Swap ``fullmatch`` back to ``match`` and this is
        # the case that goes red.
        "payment-demo-00024-f6v\n",
        "9-starts-with-digit",
        "",
    ],
)
def test_a_malformed_revision_never_reaches_the_worker(bad: str) -> None:
    """REFUSE, never clamp: a truncated identifier is not a degraded one, it
    names a DIFFERENT revision — and could name another real one, shifting
    production traffic somewhere nobody proposed."""
    called = False

    def _call(*a: Any, **k: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("worker must not be called")

    with patch("agent.adk_tools.worker_client.call", side_effect=_call):
        out = adk_tools.propose_rollback_tool(bad, "roll it back")
    assert not called
    assert "error" in out and "approval_url" not in out
    assert "previous_revisions" in out["error"], (
        "the refusal must tell the model where to get a real revision name — "
        "this lane launders the worker's error (ds-y5i), so a bare 'refused' "
        "leaves it with no way to recover"
    )


def test_a_well_formed_revision_still_goes_through() -> None:
    """The guard must not become the outage. Pins that the syntax check accepts
    the real shape produced by the reader's ``previous_revisions``."""
    seen: dict[str, Any] = {}

    # The ds-y5i projection rejects a response whose approval_url does not
    # carry the approval_id it reports. An "impossible" fixture that ignores
    # that hid a defect TWICE during ds-y5i, so this one is built the way the
    # worker really builds it: one id, threaded through both fields.
    approval_id = "8f14e45f-ceea-467a-9a3c-2b1d5f6a7b8c"
    token = "t" * 43

    def _call(worker: str, payload: dict, *a: Any, **k: Any) -> Any:
        seen.update(payload)
        return {
            "approval_id": approval_id,
            "approval_token": token,
            "approval_url":
                f"https://coordinator.example.com/approvals/{approval_id}?t={token}",
            "expires_at": "2099-01-01T00:00:00+00:00",
        }

    with (
        patch("agent.adk_tools.worker_client.call", side_effect=_call),
        patch("agent.adk_tools._record_chat_rollback_decision", return_value=True),
        patch("agent.adk_tools._notify_approval_pending"),
    ):
        out = adk_tools.propose_rollback_tool("payment-demo-00024-f6v", "drifted")
    assert seen.get("target_revision") == "payment-demo-00024-f6v"
    assert "error" not in out


# --------------------------------------------------------------------------- #
# Seam 5 — the IaC authoring path refuses instead of aborting the turn
# --------------------------------------------------------------------------- #

def _open_iac(files: list[dict], title: str, body: str) -> dict[str, Any]:
    called = False

    def _call(*a: Any, **k: Any) -> Any:
        nonlocal called
        called = True
        return {"status": "opened", "pr_number": 1, "pr_url": "https://x/pull/1"}

    # The post-open tail writes an authoring trace to Firestore and fires a
    # notification; neither is under test here and both would reach for real
    # backends. Stubbed so this stays hermetic and fast.
    with (
        patch("agent.adk_tools.worker_client.call_open_infra_pr", side_effect=_call),
        patch("agent.adk_tools.record_authoring_trace"),
        patch("agent.adk_tools.notify_iac_pr_pending"),
    ):
        out = adk_tools._open_iac_pr_and_notify(files, title, body)
    out["_worker_called"] = called
    return out


_OK_FILE = [{"path": "iac/main.tf", "content": 'resource "x" "y" {}\n'}]


@pytest.mark.parametrize(
    "files, title, body, why",
    [
        (_OK_FILE, "t" * 201, "b", "title over MAX_TITLE"),
        (_OK_FILE, "infra: ok", "b" * 20_001, "body over MAX_BODY"),
        ([], "infra: ok", "b", "empty file list"),
        ([{"path": "iac/main.tf", "content": ""}], "infra: ok", "b", "blank content"),
        ([_OK_FILE[0]] * 33, "infra: ok", "b", "over MAX_FILES"),
    ],
)
def test_an_oversize_iac_request_is_refused_not_raised(
    files: list[dict], title: str, body: str, why: str
) -> None:
    """These used to reach the worker, whose 422 ADK re-raises — ``Runner`` has
    no ``on_tool_error``, so the TURN ABORTS with a 502 and the model never
    receives a function response at all. The docstrings calling that "a
    feedback loop" were describing an intent the runtime does not implement.

    A structured refusal is the thing that actually closes the loop.
    """
    out = _open_iac(files, title, body)
    assert out["_worker_called"] is False, f"{why}: must not reach the worker"
    assert out["status"] == "rejected"
    assert "not submitted" in out["reason"].lower()


def test_a_valid_iac_request_is_untouched() -> None:
    """The guard must not become the outage it was added to prevent."""
    out = _open_iac(_OK_FILE, "infra: add a bucket", "Adds one bucket.")
    assert out["_worker_called"] is True
    assert out["status"] == "opened"


def test_a_file_policy_violation_is_refused_through_the_REAL_tool() -> None:
    """The cases above drive the shared helper. This one goes through
    ``open_infra_pr_tool`` itself, so the ordering of its own freehand-import
    guard against the shared validator is covered too — a refusal that only
    works when called directly would be no refusal at all.
    """
    called = False

    def _call(**_kw: Any) -> Any:
        nonlocal called
        called = True
        return {"status": "opened", "pr_number": 1, "pr_url": "u"}

    with patch("agent.adk_tools.worker_client.call_open_infra_pr", side_effect=_call):
        out = adk_tools.open_infra_pr_tool(
            files=[{"path": "iac/main.tf", "content": "#" * 200_001}],
            title="infra: add a bucket",
            body="Adds one bucket.",
        )
    assert called is False
    assert out["status"] == "rejected"


def test_oversize_content_is_refused_rather_than_truncated() -> None:
    """Truncating HCL does not degrade the proposal, it CHANGES what
    infrastructure is being proposed — the same reason fix 5 refuses a
    malformed revision name instead of clipping it."""
    big = [{"path": "iac/main.tf", "content": "#" * 200_001}]
    out = _open_iac(big, "infra: big", "body")
    assert out["status"] == "rejected"
    assert "do not shorten file contents" in out["reason"]


@pytest.mark.parametrize(
    "status, needle",
    [(400, "currently serving traffic"), (404, "does not know that revision")],
)
def test_a_worker_refusal_becomes_a_coordinator_authored_next_step(
    status: int, needle: str
) -> None:
    """A well-shaped but wrong name passes the local guard and only the worker
    can refuse it. ds-y5i forbids forwarding ``e.body``; the STATUS is safe, so
    the message is written here and keyed off the code."""
    from agent import worker_client

    def _call(*a: Any, **k: Any) -> Any:
        raise worker_client.WorkerClientError(status, "leaky body", "rollback")

    with patch("agent.adk_tools.worker_client.call", side_effect=_call):
        out = adk_tools.propose_rollback_tool("payment-demo-00099-zzz", "why")
    assert needle in out["error"]
    assert "leaky body" not in out["error"], "ds-y5i: never forward the worker body"
