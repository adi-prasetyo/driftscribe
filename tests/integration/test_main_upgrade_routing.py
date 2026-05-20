"""Integration tests for the upgrade workload's request-entry contract
resolve (Phase 17.C.4 step 4).

The handler's pre-resolve must surface a malformed upgrade contract as
503 BEFORE the LLM is booted — see
:func:`agent.main._eager_resolve_upgrade_contract`. Pre-fix, an unknown
``target_name`` or a schema-shaped failure would only surface on first
agent tool call (mid-conversation runtime error). Codex 2026-05-20
follow-up made the resolve eager.

Coverage:

- ``/chat`` with ``workload="upgrade"`` and a healthy contract → 200
  (routing flows through; ``run_chat`` is mocked).
- ``/chat`` with ``workload="upgrade"`` and ``UnknownUpgradeTargetError``
  from the upgrade-contract parser → 503 with "upgrade contract not
  loadable" in the detail. ``run_chat`` is NOT invoked.
- ``/recheck`` with ``workload="upgrade"`` and the same contract failure
  → 503.

The upgrade workload's worker URL env vars are set so the
:func:`load_workload` step passes; the failure is injected at the
contract-parser layer via ``patch``.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agent.config import get_settings
from agent.main import app


@pytest.fixture(autouse=True)
def _use_adk_on_with_upgrade_urls(monkeypatch):
    """Force USE_ADK=true and wire the upgrade worker URL env vars.

    Without ``USE_ADK=true`` the /chat handler 503s before ever calling
    ``load_workload`` (drift-side guard, not the upgrade-contract
    one we're testing). Without the upgrade worker URLs,
    ``load_workload("upgrade")`` 503s at the worker-env step before the
    contract-resolve runs. We want the workload to LOAD cleanly so the
    contract-resolve is reached.
    """
    monkeypatch.setenv("USE_ADK", "true")
    monkeypatch.setenv("UPGRADE_READER_URL", "https://upgrade-reader.test")
    monkeypatch.setenv("UPGRADE_DOCS_URL", "https://upgrade-docs.test")
    get_settings.cache_clear()


def _ok_chat_return(workload_label: str) -> dict:
    return {
        "reply": f"ran under {workload_label}",
        "tool_calls": [],
        "session_id": "test-sid",
    }


def test_chat_upgrade_workload_with_healthy_contract_routes_through(
    monkeypatch,
):
    """``POST /chat workload=upgrade`` with a valid contract → 200.

    Smoke-pin that the contract-resolve doesn't reject the bundled
    ``workloads/upgrade/contract.yaml`` (which targets
    ``phase17_demo`` — a real entry in ``UPGRADE_TARGET_REGISTRY``).
    Without this positive case, a regression that 503'd EVERY upgrade
    request would only surface as a missing 200 in 17.C.5's e2e —
    later in the feedback loop than we want.
    """
    fake_run_chat = AsyncMock(return_value=_ok_chat_return("upgrade"))
    with patch("agent.adk_agent.run_chat", fake_run_chat):
        client = TestClient(app)
        r = client.post(
            "/chat",
            json={"prompt": "triage advisories", "workload": "upgrade"},
        )

    assert r.status_code == 200, r.text
    fake_run_chat.assert_awaited_once()
    _, kwargs = fake_run_chat.call_args
    assert kwargs.get("workload") == "upgrade"


def test_chat_upgrade_workload_with_bad_contract_returns_503():
    """``POST /chat workload=upgrade`` when the contract parser raises
    :class:`UnknownUpgradeTargetError` → 503 with "upgrade contract
    not loadable" in the detail.

    The contract parser is invoked from
    :func:`agent.main._eager_resolve_upgrade_contract`; we patch it
    there to inject the failure without editing the on-disk YAML
    (which other tests rely on). Asserts that:

    1. The status code is 503 (deploy-not-wired surface), NOT 500
       (which would indicate the handler is treating this as a
       coordinator bug).
    2. The detail message includes the failure mode so an operator
       can self-diagnose without grepping source.
    3. ``run_chat`` is NEVER invoked — the eager resolve fired
       BEFORE the LLM was booted, which is the load-bearing property
       this task added.
    """
    from agent.workloads import UnknownUpgradeTargetError

    fake_run_chat = AsyncMock()
    # Patch the load_upgrade_contract symbol where _eager_resolve_upgrade_contract
    # imports it (lazy import inside the function — patch the source module).
    with (
        patch(
            "agent.upgrade_contract.load_upgrade_contract",
            side_effect=UnknownUpgradeTargetError(
                "upgrade target 'phase17_demo' is not in UPGRADE_TARGET_REGISTRY"
            ),
        ),
        patch("agent.adk_agent.run_chat", fake_run_chat),
    ):
        client = TestClient(app)
        r = client.post(
            "/chat",
            json={"prompt": "triage advisories", "workload": "upgrade"},
        )

    assert r.status_code == 503, r.text
    detail = r.json()["detail"].lower()
    assert "upgrade contract not loadable" in detail
    fake_run_chat.assert_not_awaited()


def test_recheck_upgrade_workload_with_bad_contract_returns_503(monkeypatch):
    """Same 503 contract for ``/recheck workload=upgrade``.

    The eager resolve sits at the top of ``_do_recheck`` after the
    workload load-and-cache step; both ADK and classifier paths run it.
    The classifier branch refuses non-drift workloads earlier so this
    only fires on the ADK path — we set ``USE_ADK=true`` to reach it.
    Asserts the 503 surfaces BEFORE the drift contract load (which
    would 500 if it ran on a missing path), and BEFORE ``_run_adk_agent``
    is called.
    """
    from agent.workloads import UnknownUpgradeTargetError

    fake_run_agent = AsyncMock()
    with (
        patch(
            "agent.upgrade_contract.load_upgrade_contract",
            side_effect=UnknownUpgradeTargetError(
                "upgrade target 'phase17_demo' is not in UPGRADE_TARGET_REGISTRY"
            ),
        ),
        patch("agent.main._run_adk_agent", fake_run_agent),
        patch("agent.main.load_contract") as m_load_contract,
    ):
        client = TestClient(app)
        r = client.post("/recheck", json={"workload": "upgrade"})

    assert r.status_code == 503, r.text
    detail = r.json()["detail"].lower()
    assert "upgrade contract not loadable" in detail
    # The eager resolve fires BEFORE both the drift contract load and
    # the agent dispatch.
    m_load_contract.assert_not_called()
    fake_run_agent.assert_not_awaited()


def test_chat_upgrade_workload_with_malformed_yaml_returns_503():
    """Phase 17.C.4 follow-up (Codex post-merge review — Important #1):
    ``load_upgrade_contract`` re-raises ``yaml.YAMLError`` as
    ``ValueError`` (see ``agent.upgrade_contract.load_upgrade_contract``).
    The eager-resolve handler MUST catch ``ValueError`` too; otherwise a
    malformed YAML would 500 instead of the intended 503.

    Pins the catch-tuple shape so a future refactor that drops
    ``ValueError`` from the eager-resolve catch fails CI loudly.
    """
    fake_run_chat = AsyncMock()
    with (
        patch(
            "agent.upgrade_contract.load_upgrade_contract",
            side_effect=ValueError(
                "failed to parse upgrade contract /tmp/contract.yaml: "
                "mapping values are not allowed here"
            ),
        ),
        patch("agent.adk_agent.run_chat", fake_run_chat),
    ):
        client = TestClient(app)
        r = client.post(
            "/chat",
            json={"prompt": "triage", "workload": "upgrade"},
        )

    assert r.status_code == 503, r.text
    detail = r.json()["detail"].lower()
    assert "upgrade contract not loadable" in detail
    fake_run_chat.assert_not_awaited()


def test_recheck_upgrade_workload_returns_503_unimplemented(monkeypatch):
    """Phase 17.C.4 follow-up (Codex post-merge review — Blocker):
    ``/recheck workload=upgrade`` returns 503 with an explicit
    "not implemented in this build" message BEFORE invoking the
    drift-specific post-agent plumbing.

    The downstream ``_do_recheck`` flow loads the drift ``OpsContract``,
    validates with the drift validator (which requires ``env_diffs`` for
    non-NO_OP actions), and renders/performs through drift-only branches
    with no UPGRADE_PR support. Letting an upgrade /recheck reach that
    code path would either crash on missing branches or produce a
    misleading "validator rejected proposal" error. We fail fast at
    request entry instead; Task 17.C.5 will wire the upgrade-specific
    /recheck execution path.

    Asserts the 503 fires BEFORE the drift contract load AND BEFORE the
    ADK agent dispatch — pins the order so a future "tidy" can't move
    the guard later. ``USE_ADK=true`` is required because the existing
    classifier-non-drift refusal would otherwise short-circuit with a
    different (still 503) message; the unimplemented guard is what's
    under test here.
    """
    # USE_ADK=true and the upgrade worker URLs come from the autouse
    # fixture at the top of this file; this test only needs to assert
    # that the unimplemented guard fires.
    fake_run_agent = AsyncMock()
    with (
        patch("agent.main._run_adk_agent", fake_run_agent),
        patch("agent.main.load_contract") as m_load_contract,
    ):
        client = TestClient(app)
        r = client.post("/recheck", json={"workload": "upgrade"})

    assert r.status_code == 503, r.text
    detail = r.json()["detail"].lower()
    assert "not implemented" in detail
    assert "/recheck" in detail or "recheck" in detail
    # The guard fires BEFORE both the drift contract load and the agent
    # dispatch. If the post-agent plumbing ever ran on an upgrade
    # request, m_load_contract would be called and these assertions
    # would catch it.
    m_load_contract.assert_not_called()
    fake_run_agent.assert_not_awaited()
