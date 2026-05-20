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
def _use_adk_on(monkeypatch):
    """Force USE_ADK=true for /chat routing tests.

    The four drift worker URL env vars AND the workload cache lifecycle
    are owned by the autouse fixture in ``tests/integration/conftest.py``
    (set on every integration test). This fixture only adds the
    ``USE_ADK=true`` flip that /chat needs, plus the matching
    ``get_settings`` cache bust.

    Tests in this file that exercise the classifier path of /recheck
    flip ``USE_ADK=false`` locally — that override stacks on top of
    this autouse, matching the convention in other test files.
    """
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()


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


def test_chat_missing_developer_knowledge_api_key_returns_503(monkeypatch) -> None:
    """Phase 17.B.2 (Codex review I-5): a missing
    :envvar:`DEVELOPER_KNOWLEDGE_API_KEY` must surface as 503, not 500.

    Structurally identical to ``test_chat_upgrade_workload_returns_503_when_env_unset``:
    a missing API key is a "deploy not wired" condition (Secret Manager
    binding for ``developer-knowledge-api-key`` absent), same operator
    surface as a missing worker URL. Same 503, same self-diagnosable
    error message.

    NOTE: This test pins the pre-resolve seam — the handler's
    :func:`load_workload` catch tuple includes
    :class:`MissingDeveloperKnowledgeApiKeyError`. As of Phase 17.B.3
    the wrapper callables ARE wired into ``build_agent`` via the
    drift workload's ``enabled_tool_names``, so an in-flight
    ``run_chat`` can also raise the missing-key error on first MCP
    tool call. The run_chat-side seam is pinned by
    :func:`test_chat_run_chat_missing_developer_knowledge_api_key_returns_503`
    above. A 17.B.4 integration test will exercise the full path
    against a mock MCP server (real
    :func:`build_developer_knowledge_toolset`); both seams' 503
    mapping must hold there too.
    """
    from agent.mcp.developer_knowledge import (
        MissingDeveloperKnowledgeApiKeyError,
    )

    def _raise_missing_key(_workload):
        raise MissingDeveloperKnowledgeApiKeyError(
            "DEVELOPER_KNOWLEDGE_API_KEY is unset"
        )

    fake_run_chat = AsyncMock()
    with (
        patch("agent.main.load_workload", side_effect=_raise_missing_key),
        patch("agent.adk_agent.run_chat", fake_run_chat),
    ):
        client = TestClient(app)
        r = client.post("/chat", json={"prompt": "hi"})

    assert r.status_code == 503, r.text
    detail = r.json()["detail"].lower()
    # The handler echoes the original error message in the detail —
    # makes the missing-key surface visible to the operator.
    assert "developer_knowledge_api_key" in detail
    # And the standard "not deployed" framing.
    assert "not deployed" in detail
    fake_run_chat.assert_not_awaited()


def test_chat_run_chat_missing_developer_knowledge_api_key_returns_503() -> None:
    """Phase 17.B.3: ``MissingDeveloperKnowledgeApiKeyError`` raised
    from INSIDE ``run_chat`` (i.e. the LLM's first MCP tool call hit
    the env-var miss) must surface as 503, not 502.

    Distinct from ``test_chat_missing_developer_knowledge_api_key_returns_503``
    above (which patches ``load_workload`` to simulate the pre-resolve
    seam). The pre-resolve seam doesn't trip the MCP env-var check —
    resolving the symbolic tool name ``search_developer_docs`` to the
    wrapper callable is a pure dict lookup that doesn't read
    :envvar:`DEVELOPER_KNOWLEDGE_API_KEY`. The env-var read happens
    lazily on first ``build_developer_knowledge_toolset()`` call inside
    the wrapper — which is reached from inside ``run_chat`` once the
    LLM picks the MCP tool.

    Without an explicit handler ordered BEFORE the broader
    ``RuntimeError`` catch in ``/chat``, the missing-key exception
    (which subclasses ``RuntimeError``) would collapse to 502
    ("chat agent failed") — the wrong operator surface. This test pins
    the 503 mapping by simulating the run_chat-side raise via an
    ``AsyncMock`` side_effect.
    """
    from agent.mcp.developer_knowledge import (
        MissingDeveloperKnowledgeApiKeyError,
    )

    fake = AsyncMock(
        side_effect=MissingDeveloperKnowledgeApiKeyError(
            "DEVELOPER_KNOWLEDGE_API_KEY is unset"
        )
    )
    with patch("agent.adk_agent.run_chat", fake):
        client = TestClient(app)
        r = client.post("/chat", json={"prompt": "hi"})

    assert r.status_code == 503, r.text
    detail = r.json()["detail"].lower()
    # The run_chat-side handler narrows its detail to the Developer
    # Knowledge subsystem (Phase 17.B.1's Secret Manager binding) —
    # distinct from the pre-resolve seam's broader "not deployed"
    # framing because this catch is reached only via the DK error path.
    assert "developer_knowledge_api_key" in detail
    assert "developer knowledge mcp" in detail
    # Pin that the model-misbehaved 502 path was NOT taken (which would
    # have surfaced "chat agent failed" instead).
    assert "chat agent failed" not in detail


def test_recheck_missing_developer_knowledge_api_key_returns_503(monkeypatch) -> None:
    """Phase 17.B.2 (Codex review I-5): the same 503 mapping must hold
    for ``/recheck``. Two exception tuples on the recheck path
    (load_workload pre-resolve, _run_adk_agent body) both include
    :class:`MissingDeveloperKnowledgeApiKeyError`. This test exercises
    the pre-resolve tuple by stubbing ``load_workload``; the
    _run_adk_agent tuple is verified by inspection in
    ``test_main_exception_tuples_include_missing_dk_key`` below.
    """
    from agent.mcp.developer_knowledge import (
        MissingDeveloperKnowledgeApiKeyError,
    )

    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()

    def _raise_missing_key(_workload):
        raise MissingDeveloperKnowledgeApiKeyError(
            "DEVELOPER_KNOWLEDGE_API_KEY is unset"
        )

    with patch("agent.main.load_workload", side_effect=_raise_missing_key):
        client = TestClient(app)
        r = client.post("/recheck", json={"workload": "drift"})

    assert r.status_code == 503, r.text
    detail = r.json()["detail"].lower()
    assert "developer_knowledge_api_key" in detail
    assert "not deployed" in detail


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
# /recheck workload selection — both ADK and classifier paths
# --------------------------------------------------------------------------- #


def test_recheck_upgrade_workload_returns_503_on_classifier_path(monkeypatch) -> None:
    """``POST /recheck`` with ``{"workload": "upgrade"}`` while
    ``USE_ADK=false`` (the classifier path) → 503.

    Codex review of the initial 17.A.3 implementation flagged this as a
    routing leak: pre-fix, ``_do_recheck`` only consulted the
    ``workload`` kwarg inside the ``if s.use_adk`` branch, so the
    classifier path silently ran drift's logic regardless of the
    requested workload. The fix pre-resolves the workload at the top of
    ``_do_recheck`` so BOTH the ADK and classifier paths surface 503 on
    an undeployed workload — with a single uniform message.

    The classifier path is the default in tests (``USE_ADK=false`` from
    the autouse conftest), so this test exercises it without any extra
    setup. The drift worker URLs are wired by the autouse fixture; the
    upgrade URLs are unset, so resolution fails at the tool-resolution
    step (upgrade tools are reserved ``None`` placeholders) — same
    503-shaped outcome as the /chat upgrade test above.
    """
    monkeypatch.setenv("USE_ADK", "false")
    monkeypatch.delenv("UPGRADE_READER_URL", raising=False)
    monkeypatch.delenv("UPGRADE_DOCS_URL", raising=False)
    get_settings.cache_clear()

    client = TestClient(app)
    r = client.post("/recheck", json={"workload": "upgrade"})

    assert r.status_code == 503, r.text
    detail = r.json()["detail"].lower()
    assert "upgrade" in detail
    assert "not deployed" in detail or "not configured" in detail


def test_recheck_classifier_path_refuses_non_drift_workload_even_when_resolvable(
    monkeypatch,
) -> None:
    """Phase 17.A.3 (Codex follow-up review): a latent leak guard.

    Today ``load_workload("upgrade")`` fails because the upgrade tools
    are reserved ``None`` placeholders. But once 17.B/17.C/17.E ship,
    the resolution will succeed — and without an explicit guard the
    classifier path would happily run drift's classifier on an upgrade
    request (drift's ``classify`` function is co-designed with drift's
    contract+live-env shape; it has no idea what upgrade is).

    This test simulates the post-17.E world by patching
    ``load_workload`` to return a stub resolution (any non-raising
    value will do — the handler only uses the return path to decide
    whether to proceed, not its contents on the classifier branch).
    The pin: with ``USE_ADK=false`` and ``workload="upgrade"``, the
    handler returns 503 BEFORE invoking the classifier or any
    worker call.
    """
    monkeypatch.setenv("USE_ADK", "false")
    get_settings.cache_clear()

    # Stub load_workload so the pre-resolve check passes regardless of
    # env state. The Phase 17.C.4 follow-up added an eager upgrade-
    # contract resolve after the load_workload step that reads
    # ``resolution.spec.name`` — give the stub a usable shape via
    # SimpleNamespace so the eager resolve treats it as drift (no-op)
    # and falls through to the classifier-path guard.
    from types import SimpleNamespace
    drift_stub = SimpleNamespace(spec=SimpleNamespace(name="drift"))
    with (
        patch("agent.main.load_workload", return_value=drift_stub),
        patch("agent.main.worker_client.call") as m_worker,
    ):
        client = TestClient(app)
        r = client.post("/recheck", json={"workload": "upgrade"})

    assert r.status_code == 503, r.text
    detail = r.json()["detail"].lower()
    assert "classifier path" in detail or "use_adk" in detail.lower()
    # The guard fires BEFORE any worker call — pin that the Reader
    # Worker is never touched in this branch.
    m_worker.assert_not_called()


def test_recheck_classifier_guard_fires_before_drift_contract_load(
    monkeypatch, tmp_path,
) -> None:
    """Phase 17.A (Codex review, Fix Important #1): the non-drift
    classifier refusal must fire BEFORE the drift contract load.

    Pre-fix ordering on ``_do_recheck``:

        1. ``load_workload(workload)`` pre-resolve
        2. ``load_contract(s.contract_path)`` (drift-specific)
        3. classifier non-drift refusal

    In the future world where ``load_workload("upgrade")`` resolves
    (17.B/17.C/17.E shipped) but the operator left ``USE_ADK=false``,
    a broken drift contract would surface as 500 at step 2 — masking
    the real diagnosis ("you're on the wrong path for this workload")
    behind a misleading "contract load failed" message.

    Post-fix: the non-drift refusal moves above the contract load, so
    a broken drift contract + an upgrade-on-classifier-path request
    surfaces the 503 first.

    This test pins that ordering. ``CONTRACT_PATH`` is pointed at a
    file that doesn't exist (would 500 the contract load), and
    ``load_workload`` is stubbed so the pre-resolve passes. The
    expected response is 503 ("classifier path is drift-only"), not
    500 ("contract load failed").
    """
    monkeypatch.setenv("USE_ADK", "false")
    # Point CONTRACT_PATH at a nonexistent file — load_contract would 500
    # if we ever reach it.
    bad_contract = tmp_path / "definitely_missing.yaml"
    assert not bad_contract.exists()
    monkeypatch.setenv("CONTRACT_PATH", str(bad_contract))
    get_settings.cache_clear()

    # Stub load_workload so the pre-resolve passes — simulating the
    # post-17.E world where upgrade resolves cleanly. Phase 17.C.4
    # follow-up: the eager upgrade-contract resolve reads
    # ``resolution.spec.name`` — give it a drift-shaped stub so the
    # resolve is a no-op and the classifier-path guard remains the
    # load-bearing rejection.
    from types import SimpleNamespace
    drift_stub = SimpleNamespace(spec=SimpleNamespace(name="drift"))
    with (
        patch("agent.main.load_workload", return_value=drift_stub),
        patch("agent.main.worker_client.call") as m_worker,
        patch("agent.main.load_contract") as m_load_contract,
    ):
        client = TestClient(app)
        r = client.post("/recheck", json={"workload": "upgrade"})

    # The classifier-path guard must fire — 503, not 500.
    assert r.status_code == 503, r.text
    detail = r.json()["detail"].lower()
    assert "classifier path" in detail or "use_adk" in detail
    # Pin the ordering: load_contract was NEVER called. If it had been
    # called and raised, we'd have seen 500 above and this assertion
    # would never be reached, but pin it explicitly for future-readers.
    m_load_contract.assert_not_called()
    m_worker.assert_not_called()


def test_recheck_unknown_workload_returns_422() -> None:
    """``POST /recheck`` with ``workload="does_not_exist"`` → 422 from
    pydantic's Literal validation, same shape as the /chat version of
    this test."""
    client = TestClient(app)
    r = client.post("/recheck", json={"workload": "does_not_exist"})
    assert r.status_code == 422, r.text


def test_recheck_default_body_keeps_drift_workload(monkeypatch) -> None:
    """Backward-compat: ``POST /recheck`` with no body works as it did
    pre-17 — defaults to ``workload="drift"`` and runs the classifier
    path. Every pre-17 integration test (28+ in test_recheck_dry_run.py
    alone) calls ``client.post("/recheck")`` with no body; this test
    pins that path explicitly so a future refactor can't silently break
    them all at once.

    Forces ``USE_ADK=false`` because the test_workload_routing autouse
    fixture defaults to ``USE_ADK=true`` (the routing tests around
    /chat need it). For the classifier path we want the deterministic
    branch.
    """
    monkeypatch.setenv("USE_ADK", "false")
    get_settings.cache_clear()

    with patch("agent.main.worker_client.call") as m:
        # Reader Worker envelope with a contract-matching env → no_op.
        m.return_value = {
            "service": "payment-demo",
            "region": "asia-northeast1",
            "project": "test-proj",
            "env": {"PAYMENT_MODE": "mock", "FEATURE_NEW_CHECKOUT": "false"},
            "revision": "payment-demo-00001-abc",
        }
        client = TestClient(app)
        r = client.post("/recheck")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "no_op"
    assert body["decision_path"] == "classifier"


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

    # Positive: every drift callable is present (six original drift
    # callables plus the two Developer Knowledge MCP wrappers added in
    # 17.B.3 — drift uses these to ground docs PR bodies in
    # authoritative Cloud Run env-variable guidance).
    expected_drift = {
        "read_live_env_tool",
        "propose_rollback_tool",
        "patch_docs_tool",
        "notify_tool",
        "search_recent_prs_tool",
        "load_contract_tool",
        "search_developer_docs",
        "retrieve_developer_doc",
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
