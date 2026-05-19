"""Integration tests for per-request workload routing (Phase 17.A.3).

The coordinator selects which workload (``drift`` or ``upgrade``) to run
per request via the ``workload`` field on ``/chat`` and ``/recheck``.
``/eventarc`` hardcodes ``workload="drift"`` server-side — a payload that
tries to say otherwise is ignored.

Coverage:

- ``/chat`` defaults to ``workload="drift"`` when the field is omitted —
  the coordinator loads the drift spec and the agent built for the call
  carries drift tools.
- ``/chat`` with an explicit ``workload="drift"`` does the same.
- ``/chat`` with ``workload="upgrade"`` — drift workers' env vars are
  set, but upgrade workers' URL env vars are NOT. ``load_workload``
  raises :class:`agent.workloads.MissingWorkerEnvError`; the handler
  surfaces 503 with a clear "not deployed" message rather than 500.
- ``/chat`` with ``workload="does_not_exist"`` — pydantic validation
  fails with 422 before any agent boot.
- ``/eventarc`` with ``{"workload": "upgrade"}`` smuggled in the
  payload — the handler ignores it; the drift workload is loaded
  regardless. Asserts the resolution passed to ``_run_adk_agent`` is
  drift's, not upgrade's.

Mock surface:

- ``agent.adk_agent.run_chat`` is patched as an ``AsyncMock`` so we
  don't need a live LLM call. We inspect the ``workload`` keyword
  argument it receives — that's what proves the routing actually
  happened.
- ``agent.main._run_adk_agent`` is patched for the eventarc test so the
  ``_do_recheck`` body executes through to the agent dispatch without
  needing a real ADK turn.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agent.config import get_settings
from agent.main import app
from agent.models import ContractStatus, DecisionAction, DecisionProposal, EnvDiff


@pytest.fixture(autouse=True)
def _drift_workload_env(monkeypatch):
    """Set drift worker URL env vars + clear the workload cache.

    The routing handlers transitively call :func:`agent.workloads.load_workload`,
    which reads ``READER_URL`` / ``DOCS_URL`` / ``ROLLBACK_URL`` /
    ``NOTIFIER_URL`` at resolve time. We do NOT set the upgrade worker
    URLs so the upgrade-503 test naturally fails its env check.

    Cache cleared on setup AND teardown so tests don't leak resolutions
    into one another (different env states must yield different
    resolutions; a stale cached entry would mask a routing bug).
    """
    monkeypatch.setenv("READER_URL", "https://reader.test")
    monkeypatch.setenv("DOCS_URL", "https://docs.test")
    monkeypatch.setenv("ROLLBACK_URL", "https://rollback.test")
    monkeypatch.setenv("NOTIFIER_URL", "https://notifier.test")
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    import agent.workloads.registry as registry_mod
    registry_mod._WORKLOAD_CACHE.clear()
    yield
    registry_mod._WORKLOAD_CACHE.clear()


def _ok_chat_return(workload_label: str) -> dict:
    """Canned ``run_chat`` return payload — the routing tests only care
    about which workload the handler dispatched, not the LLM output."""
    return {
        "reply": f"ran under {workload_label}",
        "tool_calls": [],
        "session_id": "test-sid",
    }


# --------------------------------------------------------------------------- #
# /chat workload selection
# --------------------------------------------------------------------------- #


def test_chat_default_workload_is_drift() -> None:
    """``POST /chat`` with no ``workload`` field defaults to ``"drift"``.

    Backward-compat: every pre-17 /chat caller (curl in the demo script,
    test clients in test_chat_endpoint.py) omits the field. They MUST
    keep working without modification.
    """
    fake = AsyncMock(return_value=_ok_chat_return("drift"))
    with patch("agent.adk_agent.run_chat", fake):
        client = TestClient(app)
        r = client.post("/chat", json={"prompt": "hi"})

    assert r.status_code == 200, r.text
    # Routing assertion: the handler resolved workload="drift" and passed
    # the resulting WorkloadResolution into run_chat. We don't inspect the
    # resolution's contents here (the 17.A.1/17.A.2 tests already pin its
    # shape); we just confirm the workload string flowed through.
    fake.assert_awaited_once()
    _, kwargs = fake.call_args
    assert kwargs.get("workload") == "drift", (
        f"default workload should route as 'drift', got {kwargs.get('workload')!r}"
    )


def test_chat_explicit_drift_workload_routes_to_drift() -> None:
    """``POST /chat`` with ``workload="drift"`` explicitly named routes
    the same as the default. Same assertion as the default test — pins
    that the explicit form has no separate code path."""
    fake = AsyncMock(return_value=_ok_chat_return("drift"))
    with patch("agent.adk_agent.run_chat", fake):
        client = TestClient(app)
        r = client.post("/chat", json={"prompt": "hi", "workload": "drift"})

    assert r.status_code == 200, r.text
    fake.assert_awaited_once()
    _, kwargs = fake.call_args
    assert kwargs.get("workload") == "drift"


def test_chat_upgrade_workload_returns_503_when_env_unset(monkeypatch) -> None:
    """``POST /chat`` with ``workload="upgrade"`` while the upgrade worker
    URLs are unset → 503.

    Phase 17.A.3 design (Option A from the plan): lazy-load the workload
    on first use. The upgrade workload's manifest references
    ``UPGRADE_READER_URL`` / ``UPGRADE_DOCS_URL`` which aren't wired
    until Phase 17.E. Hitting /chat with ``workload="upgrade"`` before
    those are set must NOT 500 the coordinator — it should 503 with a
    clear "workload is not deployed" message so the operator knows the
    deploy is incomplete, not that the request was malformed.

    Codex blocker (17.A.3): structurally-valid requests for an
    undeployed workload are a 503-shaped condition, not 500.
    """
    # Belt-and-suspenders: make sure UPGRADE_*_URL really are unset for
    # this test. The autouse fixture above only sets drift URLs, but a
    # leaky outer env could shadow that.
    monkeypatch.delenv("UPGRADE_READER_URL", raising=False)
    monkeypatch.delenv("UPGRADE_DOCS_URL", raising=False)

    # run_chat MUST NOT be reached — the 503 fires before any agent boot.
    fake = AsyncMock()
    with patch("agent.adk_agent.run_chat", fake):
        client = TestClient(app)
        r = client.post("/chat", json={"prompt": "hi", "workload": "upgrade"})

    assert r.status_code == 503, r.text
    detail = r.json()["detail"].lower()
    assert "upgrade" in detail
    # The error message should hint at deploy phase / env wiring so the
    # operator can self-diagnose without grepping source.
    assert (
        "not deployed" in detail
        or "not configured" in detail
        or "url" in detail
        or "env" in detail
    ), f"503 detail should hint at deploy/env config: {detail!r}"
    fake.assert_not_awaited()


def test_chat_unknown_workload_returns_422() -> None:
    """``POST /chat`` with ``workload="does_not_exist"`` → 422.

    The ChatRequest's ``workload`` field is ``Literal["drift", "upgrade"]``;
    pydantic rejects unknown values before the handler body runs. The
    response shape is FastAPI's standard validation error envelope.
    """
    fake = AsyncMock()
    with patch("agent.adk_agent.run_chat", fake):
        client = TestClient(app)
        r = client.post(
            "/chat",
            json={"prompt": "hi", "workload": "does_not_exist"},
        )

    assert r.status_code == 422, r.text
    fake.assert_not_awaited()


# --------------------------------------------------------------------------- #
# /eventarc hardcodes drift — Codex blocker
# --------------------------------------------------------------------------- #


def _audit_log_body_with_workload(workload: str) -> dict:
    """Audit-log envelope with a smuggled ``workload`` field at the top
    level. The audit log schema doesn't define this field — the test is
    explicitly probing the "caller tries to widen authority" surface.
    """
    return {
        "workload": workload,  # SMUGGLED — must be ignored
        "protoPayload": {
            "@type": "type.googleapis.com/google.cloud.audit.AuditLog",
            "methodName": "google.cloud.run.v2.Services.UpdateService",
            "resourceName": (
                "projects/test-proj/locations/asia-northeast1/services/payment-demo"
            ),
            "serviceName": "run.googleapis.com",
        },
        "resource": {
            "type": "cloud_run_revision",
            "labels": {
                "service_name": "payment-demo",
                "location": "asia-northeast1",
                "project_id": "test-proj",
            },
        },
    }


def test_eventarc_ignores_workload_in_payload_and_routes_to_drift(
    monkeypatch,
) -> None:
    """``POST /eventarc`` with ``{"workload": "upgrade"}`` smuggled into
    the payload → handler hardcodes ``workload="drift"`` server-side.

    Codex blocker (17.A.3): the trigger payload's authority must not
    extend to workload selection. Eventarc fires on Cloud Run audit
    logs, which are drift's input source by definition. An
    event-triggered upgrade workload — if one is ever added — would get
    its own endpoint with its own server-side binding.

    The proof: ``_do_recheck`` is called with ``trigger="eventarc"``
    (which it always was) — the dispatch through the /recheck pipeline
    is what loads the workload, and the workload string the handler
    chose is "drift" regardless of what the payload tried to inject.
    The handler signature ``_do_recheck(trigger)`` doesn't even take a
    workload param yet — once it does (this task), the assertion below
    pins that it received ``"drift"``.
    """
    monkeypatch.setenv("EVENTARC_AUDIENCE", "https://driftscribe-agent-xyz.a.run.app")
    monkeypatch.setenv("GCP_PROJECT", "test-proj")
    monkeypatch.setenv("TARGET_SERVICE", "payment-demo")
    monkeypatch.setenv("TARGET_REGION", "asia-northeast1")
    get_settings.cache_clear()

    recheck_result = {
        "decision_id": "test-dec-eventarc",
        "event_key": "eventarc-payment-demo-x",
        "action": "no_op",
        "trigger": "eventarc",
    }
    mock_recheck = AsyncMock(return_value=recheck_result)
    expected_email = "eventarc-trigger-sa@test-proj.iam.gserviceaccount.com"
    valid_audience = "https://driftscribe-agent-xyz.a.run.app"

    with (
        patch("agent.main.verify_oauth2_token") as m_verify,
        patch("agent.main._do_recheck", mock_recheck),
    ):
        m_verify.return_value = {"email": expected_email, "aud": valid_audience}
        client = TestClient(app)
        r = client.post(
            "/eventarc",
            json=_audit_log_body_with_workload("upgrade"),
            headers={"Authorization": "Bearer fake-token"},
        )

    assert r.status_code == 200, r.text
    assert r.json() == recheck_result

    # The Codex-blocker assertion: regardless of the smuggled
    # ``workload="upgrade"`` in the body, _do_recheck must be called
    # with ``workload="drift"``. Accept either positional or keyword
    # form so we don't over-specify the handler's call signature.
    mock_recheck.assert_awaited_once()
    args, kwargs = mock_recheck.call_args
    # Existing contract: trigger is the first positional arg.
    assert args[0] == "eventarc" or kwargs.get("trigger") == "eventarc"
    # New contract: workload is passed as a kwarg, hardcoded to "drift".
    assert kwargs.get("workload") == "drift", (
        f"/eventarc must hardcode workload='drift'; got {kwargs.get('workload')!r}. "
        f"call_args=({args!r}, {kwargs!r})"
    )


# --------------------------------------------------------------------------- #
# Prompt-injection / capability-bound: drift workload's agent has drift
# tools, not upgrade tools — even when the prompt explicitly asks.
# --------------------------------------------------------------------------- #


def test_chat_drift_workload_agent_has_drift_tools_not_upgrade_tools() -> None:
    """Even if the operator's prompt asks for an upgrade-only capability,
    the agent the coordinator built for ``workload="drift"`` carries the
    drift tool set, NOT the upgrade tool set.

    This is the routing-layer half of the capability-bound invariant.
    The other half (the TOOL_REGISTRY allowlist) is pinned in 17.A.1 /
    17.A.2 unit tests. Together they prove:

    1. The drift workload's symbolic ``enabled_tool_names`` resolves to
       Python callables from the registry (17.A.2 inventory test).
    2. When a request says ``workload="drift"``, the agent built for
       that request gets exactly those callables — not the upgrade ones
       (this test).

    Implementation note: we inspect the agent built by ``build_agent``
    rather than running the LLM. The LLM-level prompt-injection
    resistance lives in Gemini; the *coordinator's* job is to make sure
    even a maximally-compromised LLM can't invoke a tool that wasn't
    handed to it. That's the property under test here.
    """
    from agent.adk_agent import build_agent
    from agent.workloads import load_workload

    drift_resolution = load_workload("drift")
    agent = build_agent(drift_resolution)

    tool_names = {
        getattr(t, "__name__", repr(t)) for t in agent.tools
    }

    # Positive: every drift callable is present.
    expected_drift = {
        "read_live_env_tool",
        "propose_rollback_tool",
        "patch_docs_tool",
        "notify_tool",
        "search_recent_prs_tool",
        "load_contract_tool",
    }
    assert expected_drift.issubset(tool_names), (
        f"drift workload agent missing tools: {expected_drift - tool_names}"
    )

    # Negative: NO upgrade-only callable is present (they don't exist
    # as real callables yet — 17.C wires them — but if the routing layer
    # ever leaked tool callables across workloads, this assertion would
    # catch a future regression).
    upgrade_only_names = {"upgrade_read_dependencies", "upgrade_propose_pr"}
    leak = tool_names & upgrade_only_names
    assert not leak, (
        f"drift workload agent has upgrade-only tools: {leak}. "
        f"Routing must be capability-bound — the agent for workload=X "
        f"may only carry workload=X's tools."
    )


def test_chat_eventarc_dispatched_agent_has_drift_tools_regardless_of_payload(
    monkeypatch,
) -> None:
    """End-to-end companion to the previous test, through /eventarc.

    Even though the payload tries to smuggle ``workload="upgrade"``, the
    handler's hardcoded ``"drift"`` flows through ``_do_recheck`` to the
    agent factory, and the dispatched ``_run_adk_agent`` runs with a
    drift-built agent.

    We patch ``_run_adk_agent`` to capture the workload it was called
    with — that's the load-bearing assertion that pins drift even when
    the payload tried to flip it.
    """
    monkeypatch.setenv("EVENTARC_AUDIENCE", "https://driftscribe-agent-xyz.a.run.app")
    monkeypatch.setenv("GCP_PROJECT", "test-proj")
    monkeypatch.setenv("TARGET_SERVICE", "payment-demo")
    monkeypatch.setenv("TARGET_REGION", "asia-northeast1")
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()

    # Stub _run_adk_agent: return a no-op proposal so _do_recheck completes
    # without exercising the validator/renderer/perform-action chain.
    no_op_proposal = DecisionProposal(
        action=DecisionAction.NO_OP,
        env_diffs=[
            EnvDiff(
                name="PAYMENT_MODE",
                expected="mock",
                live="mock",
                contract_status=ContractStatus.PRESENT_ALLOW_MANUAL,
                debug_config_value=None,
                recent_pr_match=None,
            )
        ],
        rationale="contract matches live",
        confidence=1.0,
        requires_human_review=False,
    )
    mock_run_agent = AsyncMock(return_value=no_op_proposal)

    expected_email = "eventarc-trigger-sa@test-proj.iam.gserviceaccount.com"
    valid_audience = "https://driftscribe-agent-xyz.a.run.app"

    with (
        patch("agent.main.verify_oauth2_token") as m_verify,
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.worker_client.call") as m_worker,
    ):
        # Reader Worker is called for the live env hash; return a
        # contract-matching env so the proposal is accepted as no_op.
        m_worker.return_value = {
            "service": "payment-demo",
            "region": "asia-northeast1",
            "project": "test-proj",
            "env": {"PAYMENT_MODE": "mock", "FEATURE_NEW_CHECKOUT": "false"},
            "revision": "payment-demo-00001-abc",
        }
        m_verify.return_value = {"email": expected_email, "aud": valid_audience}

        client = TestClient(app)
        r = client.post(
            "/eventarc",
            json=_audit_log_body_with_workload("upgrade"),
            headers={"Authorization": "Bearer fake-token"},
        )

    assert r.status_code == 200, r.text

    # Pin: _run_adk_agent was called with workload="drift" — the smuggled
    # ``workload="upgrade"`` in the payload was ignored.
    mock_run_agent.assert_awaited_once()
    _, kwargs = mock_run_agent.call_args
    assert kwargs.get("workload") == "drift", (
        f"_run_adk_agent under /eventarc must run drift workload; "
        f"got {kwargs.get('workload')!r}"
    )
