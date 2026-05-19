"""Integration golden for the deterministic ``/recheck`` path (Phase 17.A.2).

Codex blocker from the Phase 17 plan: byte-for-byte prompt/contract
goldens are necessary but not sufficient. An integration test must also
pin that the deterministic ``/recheck`` decision plus the downstream
worker call sequence have NOT changed across the 17.A.2 refactor.

Two paths existed pre-17 inside ``/recheck``:

- **Classifier path** (``USE_ADK=false``): fully deterministic. Reader
  Worker is called once for the live env; the validator + renderer +
  GitHub action layer are all deterministic functions of the proposal.
  This test exercises that path with a mocked Reader response and
  asserts:
  * the decision body (action, decision_path, dry_run, rendered_body
    shape) matches the pre-17 baseline,
  * the worker call sequence (which workers, in what order, with what
    payloads) matches the pre-17 baseline,
  * the contract content read by the coordinator matches the workload-
    local copy (workloads/drift/contract.yaml).

- **ADK path** (``USE_ADK=true``): non-deterministic because the LLM
  picks tool calls. We deliberately do NOT golden-test that here — the
  Phase 17 plan §17.A.2 says so explicitly.

Pre-17 baseline capture method: the worker call sequence below was
captured by running ``test_recheck_renders_drift_issue_when_live_violates_contract``
under the same harness pre-refactor. The refactor in 17.A.2 only
touches ``agent/adk_agent.py`` (USE_ADK=true path); the classifier
path's wiring through ``agent/main.py`` is untouched, so the baseline
should hold by construction. This test makes that construction-time
property a CI-time gate.

If this test ever fails, the failure is one of:

1. A genuine bug — a refactor accidentally added/removed a worker call
   or reshuffled the order. Revert the offending change.
2. An intentional behavior change — update the baseline below and
   document the migration in the relevant phase commit message.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi.testclient import TestClient

from agent.main import app


def _reader_envelope(env: dict[str, str]) -> dict:
    """Reader Worker /read response shape — matches the pre-17 helper in
    ``test_recheck_dry_run.py`` and ``test_recheck_use_adk_path.py``."""
    return {
        "service": "payment-demo",
        "region": "asia-northeast1",
        "project": "test-project",
        "env": env,
        "revision": "payment-demo-00001-abc",
    }


def _capture_worker_calls(mock_call):
    """Flatten ``unittest.mock.MagicMock`` call records into a list of
    ``(worker, payload)`` tuples — the unit the baseline cares about."""
    out = []
    for c in mock_call.call_args_list:
        args, kwargs = c
        # ``agent.main`` calls ``worker_client.call("reader", {})`` with
        # positional args. Defensive: tolerate kwargs form too.
        worker = args[0] if args else kwargs.get("worker")
        payload = args[1] if len(args) > 1 else kwargs.get("payload", {})
        out.append((worker, payload))
    return out


def test_drift_recheck_no_op_pinned_worker_call_sequence(monkeypatch):
    """Classifier path, contract-compliant live env → ``no_op``.

    Baseline (pre-17): exactly one worker call to ``reader`` with an
    empty payload. The classifier produces a NO_OP proposal, the
    renderer returns ``(no action)``, and ``_perform_action`` returns
    a no_op stub without touching any worker. No GitHub calls in dry
    run.
    """
    monkeypatch.setenv("USE_ADK", "false")
    from agent.config import get_settings
    get_settings.cache_clear()

    with patch("agent.main.worker_client.call") as m:
        m.return_value = _reader_envelope(
            {"PAYMENT_MODE": "mock", "FEATURE_NEW_CHECKOUT": "false"}
        )
        r = TestClient(app).post("/recheck")

    assert r.status_code == 200
    body = r.json()

    # Decision pin: no_op via classifier path, dry_run echoed through.
    assert body["action"] == "no_op"
    assert body["decision_path"] == "classifier"
    assert body["dry_run"] is True
    assert body["github"]["action"] == "no_op"
    assert body["github"]["url"] is None
    # No diffs for a contract-matching live env.
    assert body["diffs"] == []
    # No HITL needed for a no_op.
    assert body["requires_human_review"] is False

    # Worker call sequence pin: exactly one Reader call, no payload.
    calls = _capture_worker_calls(m)
    assert calls == [("reader", {})], (
        f"Worker call sequence diverged from the pre-17 no_op baseline.\n"
        f"  Expected: [('reader', {{}})]\n"
        f"  Actual:   {calls!r}"
    )


def test_drift_recheck_drift_issue_pinned_worker_call_sequence(monkeypatch):
    """Classifier path, allow_manual_change=false var drifts → ``drift_issue``.

    Baseline (pre-17): one Reader call. dry_run=true means the github
    action is a preview (no real PR/issue minted), so the worker mock
    sees only the Reader call.
    """
    monkeypatch.setenv("USE_ADK", "false")
    from agent.config import get_settings
    get_settings.cache_clear()

    with patch("agent.main.worker_client.call") as m:
        m.return_value = _reader_envelope(
            {"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "false"}
        )
        r = TestClient(app).post("/recheck")

    assert r.status_code == 200
    body = r.json()

    # Decision pin: drift_issue via classifier path.
    assert body["action"] == "drift_issue"
    assert body["decision_path"] == "classifier"
    assert body["dry_run"] is True
    # github: dry-run preview shape, no URL.
    assert body["github"]["dry_run"] is True
    assert "Drift:" in body["github"]["title"]
    assert body["github"]["url"] is None
    # Pin the diff content — PAYMENT_MODE drift, contract value vs live.
    diff_names = [d["name"] for d in body["diffs"]]
    assert diff_names == ["PAYMENT_MODE"]
    assert body["diffs"][0]["expected"] == "mock"
    assert body["diffs"][0]["live"] == "live"
    assert body["diffs"][0]["contract_status"] == "present_disallow_manual"

    # Worker call sequence pin: exactly one Reader call, no payload.
    calls = _capture_worker_calls(m)
    assert calls == [("reader", {})], (
        f"Worker call sequence diverged from the pre-17 drift_issue baseline.\n"
        f"  Expected: [('reader', {{}})]\n"
        f"  Actual:   {calls!r}"
    )


def test_drift_recheck_escalation_pinned_worker_call_sequence(monkeypatch):
    """Classifier path, live has an unknown var with no matching PR →
    ``escalation``.

    Baseline (pre-17): one Reader call; ``recent_prs=[]`` is the
    classifier's default so no PR-search is performed.
    """
    monkeypatch.setenv("USE_ADK", "false")
    from agent.config import get_settings
    get_settings.cache_clear()

    with patch("agent.main.worker_client.call") as m:
        m.return_value = _reader_envelope(
            {
                "PAYMENT_MODE": "mock",
                "FEATURE_NEW_CHECKOUT": "false",
                "NEW_THING": "x",
            }
        )
        r = TestClient(app).post("/recheck")

    assert r.status_code == 200
    body = r.json()

    assert body["action"] == "escalation"
    assert body["decision_path"] == "classifier"
    assert body["requires_human_review"] is True
    diff_names = [d["name"] for d in body["diffs"]]
    assert diff_names == ["NEW_THING"]
    assert body["diffs"][0]["contract_status"] == "absent"

    calls = _capture_worker_calls(m)
    assert calls == [("reader", {})], (
        f"Worker call sequence diverged from the pre-17 escalation baseline.\n"
        f"  Expected: [('reader', {{}})]\n"
        f"  Actual:   {calls!r}"
    )


def test_drift_recheck_uses_pre17_compatible_contract(monkeypatch):
    """The contract content the coordinator reads on the classifier path
    matches the workload-local copy byte-for-byte after a shape parse.

    Why both: ``CONTRACT_PATH`` still points at the legacy
    ``demo/ops-contract.yaml`` (integration conftest sets that), so the
    coordinator's settings layer reads the demo copy. The workload
    registry reads the new ``workloads/drift/contract.yaml`` copy. Both
    must yield the same parsed dict — if they ever drift, the LLM and
    the classifier would see different ground truths.

    Reads via ``yaml.safe_load`` so this catches semantic drift (a
    rewritten-but-equivalent YAML wouldn't be byte-equal but should
    still parse to the same dict). The byte-equal guard lives in
    ``tests/unit/test_drift_workload_loads.py``; this test is the
    parse-equivalence companion.
    """
    repo_root = Path(__file__).resolve().parents[2]
    demo_parsed = yaml.safe_load(
        (repo_root / "demo" / "ops-contract.yaml").read_text(encoding="utf-8")
    )
    workload_parsed = yaml.safe_load(
        (repo_root / "workloads" / "drift" / "contract.yaml").read_text(encoding="utf-8")
    )
    assert demo_parsed == workload_parsed, (
        "demo/ops-contract.yaml and workloads/drift/contract.yaml "
        "parsed to different dicts. Reconcile before the next deploy — "
        "the coordinator's settings layer reads the demo copy while the "
        "workload registry reads the workload-local copy."
    )
