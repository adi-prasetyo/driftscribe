"""Integration tests for the USE_ADK=true branch of /recheck.

We can't run a live Gemini call in CI, so we mock `agent.main._run_adk_agent`
to return a known `DecisionProposal`. This pins the wiring (run_agent →
validate → render → perform_action → record_decision) without depending on
the model. End-to-end with a real Gemini call lives in the manual smoke
test (Task 6.3 Step 2 in the plan).
"""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from agent.config import get_settings
from agent.main import _reset_state_for_tests, app
from agent.models import ContractStatus, DecisionAction, DecisionProposal, EnvDiff


def _drift_issue_proposal() -> DecisionProposal:
    """A canonical drift_issue `DecisionProposal` the validator will accept."""
    return DecisionProposal(
        action=DecisionAction.DRIFT_ISSUE,
        env_diffs=[
            EnvDiff(
                name="PAYMENT_MODE",
                expected="mock",
                live="live",
                contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,
                debug_config_value=None,
                recent_pr_match=None,
            )
        ],
        target_docs_file=None,
        target_docs_section=None,
        rationale=(
            "PAYMENT_MODE drifted from 'mock' to 'live'; the contract "
            "marks this var as allow_manual_change=false, so this is a "
            "policy violation, not a docs update."
        ),
        confidence=0.92,
        requires_human_review=True,
    )


def test_use_adk_path_wires_through_to_perform_action(monkeypatch):
    """USE_ADK=true: agent proposes drift_issue → validate/render/perform run."""
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    mock_run_agent = AsyncMock(return_value=_drift_issue_proposal())
    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.read_live_env") as m_env,
    ):
        m_env.return_value = {"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "false"}
        client = TestClient(app)
        r = client.post("/recheck")

    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "drift_issue"
    assert body["github"]["dry_run"] is True
    assert "Drift:" in body["github"]["title"]
    assert body["requires_human_review"] is True
    # Rationale comes from the LLM proposal, not the classifier — pin that
    # the ADK path's prose is what we see in the response body.
    assert "policy violation" in body["rationale"]
    # Provenance label: USE_ADK=true → this proposal came from the ADK path.
    assert body["decision_path"] == "adk"
    # The agent should have been called exactly once.
    mock_run_agent.assert_awaited_once()


def test_use_adk_path_tolerates_cloud_run_read_failure(monkeypatch):
    """USE_ADK=true: read_live_env raising must NOT 502.

    The ADK agent's own tool call already read live state; the failure here
    only affects the idempotency-hash backing store. Per spec the fallback
    derives live_env from `proposal.env_diffs`.
    """
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    mock_run_agent = AsyncMock(return_value=_drift_issue_proposal())
    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.read_live_env") as m_env,
    ):
        m_env.side_effect = RuntimeError("permission denied on run.services.get")
        client = TestClient(app)
        r = client.post("/recheck")

    # 200 (not 502) — the non-ADK path's 502 semantic does NOT apply here.
    assert r.status_code == 200
    body = r.json()
    assert body["event_key"]  # non-empty derived from proposal.env_diffs fallback
    assert body["action"] == "drift_issue"


def test_use_adk_path_rejects_unsafe_proposal_with_502(monkeypatch):
    """USE_ADK=true: LLM proposes docs_pr for a SECRET-named var → 502.

    The deterministic validator catches this violation (the safety rules
    apply to both paths). On the ADK path we surface it as 502 with an
    "adk proposal rejected" detail so logs can distinguish "model misbehaved"
    from "model unreachable".
    """
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    unsafe = DecisionProposal(
        action=DecisionAction.DOCS_PR,
        env_diffs=[
            EnvDiff(
                name="STRIPE_SECRET_KEY",  # SECRET in name -> validator must reject docs_pr
                expected="sk_test_old",
                live="sk_test_new",
                contract_status=ContractStatus.PRESENT_ALLOW_MANUAL,
                debug_config_value=None,
                recent_pr_match="https://github.com/x/y/pull/1",
            )
        ],
        target_docs_file="demo/docs/runbook.md",
        target_docs_section="Runtime Configuration",
        rationale="rotation",
        confidence=0.99,
        requires_human_review=False,
    )
    mock_run_agent = AsyncMock(return_value=unsafe)
    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.read_live_env") as m_env,
    ):
        m_env.return_value = {}
        client = TestClient(app)
        r = client.post("/recheck")

    assert r.status_code == 502
    assert "adk proposal rejected" in r.json()["detail"]


def test_use_adk_path_surfaces_agent_failure_as_502(monkeypatch):
    """USE_ADK=true: agent raising (parse / validation / network) → 502.

    Distinct from the non-ADK path's "cloud run read failed" 502 — this is
    "ADK agent failed" so on-call can disambiguate model failures from GCP
    failures in logs.
    """
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    mock_run_agent = AsyncMock(side_effect=RuntimeError("ADK agent produced no final response"))
    with patch("agent.main._run_adk_agent", mock_run_agent):
        client = TestClient(app)
        r = client.post("/recheck")

    assert r.status_code == 502
    assert "adk agent failed" in r.json()["detail"]
