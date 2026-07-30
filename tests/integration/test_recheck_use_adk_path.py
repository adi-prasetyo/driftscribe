"""Integration tests for the USE_ADK=true branch of /recheck.

We can't run a live Gemini call in CI, so we mock `agent.main._run_adk_agent`
to return a known `DecisionProposal`. This pins the wiring (run_agent →
validate → render → perform_action → record_decision) without depending on
the model. End-to-end with a real Gemini call lives in the manual smoke
test (Task 6.3 Step 2 in the plan).
"""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from agent import worker_client
from agent.config import get_settings
from agent.main import _reset_state_for_tests, app, get_state
from agent.models import ContractStatus, DecisionAction, DecisionProposal, EnvDiff

def _adk(proposal):
    """A ``_run_adk_agent`` double that reads live state the way the real agent
    does, then reports what it saw (ds-q38).

    The production agent reaches live state through ``read_live_env_tool`` ->
    ``worker_client.call("reader", {})``, and ``run_agent`` reports each such
    response back to ``_do_recheck`` via ``reader_sink`` so the coordinator can
    confirm the decision it is about to persist describes the world its
    idempotency key names. Performing the same call against whatever dispatcher
    the test patched in keeps this double faithful automatically.
    """

    async def _run(*_a, reader_sink=None, **_k):
        if reader_sink is not None:
            try:
                payload = worker_client.call("reader", {})
            except Exception:
                # DELIBERATE SIMPLIFICATION, and the direction matters. In
                # production a Reader failure inside the tool ABORTS the turn:
                # read_live_env_tool does not catch WorkerClientError and ADK
                # re-raises a tool exception when no on_tool_error callback
                # handles it (build_agent installs none), so _do_recheck sees
                # "adk agent failed" -> 502 and never reaches the coherence
                # gate. Swallowing here lets a test keep exercising the
                # COORDINATOR-side read failure it actually cares about, with
                # the agent's own read having succeeded. The real abort is
                # pinned by test_a_reader_failure_inside_the_turn_is_a_502.
                payload = None
            if isinstance(payload, dict) and isinstance(payload.get("env"), dict):
                reader_sink.append(
                    {"env": dict(payload["env"]),
                     "revision": payload.get("revision") or None}
                )
        return proposal

    return AsyncMock(side_effect=_run)




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


def _reader_envelope(env: dict[str, str]) -> dict:
    """Shape a Reader Worker /read response around the given ``env`` dict.

    Mirrors the helper in ``test_recheck_dry_run.py``. Kept duplicated
    (rather than pulled into a conftest) because the two test files
    exercise distinct branches and a future refactor of one shouldn't
    silently move the other.
    """
    return {
        "service": "payment-demo",
        "region": "asia-northeast1",
        "project": "test-project",
        "env": env,
        "revision": "payment-demo-00001-abc",
    }


def test_use_adk_path_wires_through_to_perform_action(monkeypatch):
    """USE_ADK=true: agent proposes drift_issue → validate/render/perform run."""
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    mock_run_agent = _adk(_drift_issue_proposal())
    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.worker_client.call") as m_env,
    ):
        m_env.return_value = _reader_envelope(
            {"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "false"}
        )
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


def _drift_proposal_quoting_secret(secret_url: str) -> DecisionProposal:
    """Like ``_drift_issue_proposal`` but the live value is a credentialed URL
    and the rationale quotes it verbatim — so the serve-time scrub (PR 2) has
    something real to redact regardless of the var name (``should_redact`` fires
    on the credentialed value)."""
    return DecisionProposal(
        action=DecisionAction.DRIFT_ISSUE,
        env_diffs=[
            EnvDiff(
                name="PAYMENT_MODE",
                expected="mock",
                live=secret_url,
                contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,
                debug_config_value=None,
                recent_pr_match=None,
            )
        ],
        target_docs_file=None,
        target_docs_section=None,
        rationale=(
            f"PAYMENT_MODE drifted from 'mock' to {secret_url}; the contract "
            "marks this var as allow_manual_change=false, so this is a policy "
            "violation, not a docs update."
        ),
        confidence=0.92,
        requires_human_review=True,
    )


def test_recheck_response_scrubs_secret_in_rationale(monkeypatch):
    """PR 2 — the POST /recheck response body must not carry a secret quoted in
    the LLM rationale. Exercises the real fresh ``_do_recheck`` path (agent
    mocked) + the handler's ``scrub_decision_rationale`` wrap."""
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    secret_url = "https://admin:hunter2SECRET@svc.internal/api"
    mock_run_agent = _adk(_drift_proposal_quoting_secret(secret_url))
    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.worker_client.call") as m_env,
    ):
        m_env.return_value = _reader_envelope({"PAYMENT_MODE": secret_url})
        client = TestClient(app)
        r = client.post("/recheck")

    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "drift_issue"
    # The credentialed value is scrubbed out of the rationale prose...
    assert "hunter2SECRET" not in body["rationale"]
    assert secret_url not in body["rationale"]
    assert "PAYMENT_MODE" in body["rationale"]   # var name survives
    # ...and diffs[] are scrubbed too. Note this value is credentialed by
    # SHAPE (a `user:pass@host` URL) under a NON-secret name, so it exercises
    # the value_looks_credentialed half of should_redact, not the name half.
    assert body["diffs"][0]["live"] == "(redacted)"
    assert body["diffs"][0]["name"] == "PAYMENT_MODE"


def test_use_adk_path_refuses_to_record_when_the_reader_read_fails(monkeypatch):
    """USE_ADK=true, Reader Worker unreachable: the decision is NOT recorded.

    This inverts a deliberate older behaviour, so the reason matters. The old
    contract let the pipeline continue by hashing the idempotency key from
    ``proposal.env_diffs``, on the argument that the agent's own tool call had
    already read live state. ds-q38 showed both halves of that argument fail:

    * ``env_diffs`` is a SUBSET the model chooses and can legally be empty
      (``no_op`` with no diffs passes the validator), so the key can describe a
      different world than the decision — including the ``{}``-env key for a
      populated service, which then collides with a genuinely empty one.
    * "the agent already read it" is an assumption, not an observation. Exactly
      that assumption produced the poisoned production row: a ``no_op`` reasoned
      over pre-deploy state was filed under the DRIFTED env's key and outranked
      every correct rollback for it afterwards, permanently, because a ``no_op``
      row has no TTL.

    So with no corroborating read of our own, the run refuses (409) instead of
    persisting a row nothing can vouch for. Losing an audit is recoverable — the
    next event re-discovers the drift; a poisoned idempotency key is not.
    """
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    mock_run_agent = _adk(_drift_issue_proposal())
    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.worker_client.call") as m_env,
    ):
        # The ADK path catches any exception from the worker call (the LLM's
        # tool call already read live state via the Reader Worker; this read
        # is purely for the idempotency hash). Raising WorkerClientError
        # exercises the fallback path even though the post-Phase-13 control
        # flow has the bare `except Exception` already broad enough.
        from agent.worker_client import WorkerClientError
        m_env.side_effect = WorkerClientError(
            403, "permission denied on run.services.get", "reader"
        )
        client = TestClient(app)
        r = client.post("/recheck")

    # 409, not 502: retryable — the reader may come back — and distinct from
    # the validator's non-retryable "the model responded and the safety gate
    # refused" 502.
    assert r.status_code == 409, r.text
    # With the reader down the agent could not read either, so the refusal
    # names that first; the invariant asserted here is that NOTHING was
    # recorded, whichever half of the observation was missing.
    assert "could not be confirmed to describe the state" in r.text
    # The point of the refusal: no row, so no key is claimed under a world
    # nothing observed.
    assert get_state().list_decisions(limit=50) == []


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
    mock_run_agent = _adk(unsafe)
    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.worker_client.call") as m_env,
    ):
        m_env.return_value = _reader_envelope({})
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
