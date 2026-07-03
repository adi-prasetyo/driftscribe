"""Integration tests for ``GET /infra/graph/preview?pr=N`` (ClickOps Wave 2 item 6).

The route resolves the PR's C2 plan artifact through the SAME ladder as the
approval GET and returns a redaction-safe ghost-node overlay DTO. It is
advisory and read-only: always-200 with ``{available: false, reason}`` for
every not-available outcome.

Auth + monkeypatch patterns mirror ``test_infra_graph_endpoint.py`` for the
token guard and ``test_iac_approval_get.py`` for the ``_resolve_iac_plan`` /
view / decision-state fixtures.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import agent.main as main_mod
from agent.config import get_settings
from agent.iac_artifacts import C2CommentRef, IacPlanView
from agent.main import app, get_state, _iac_event_key, _record_iac_decision
from driftscribe_lib.infra_graph import plan_overlay

# --------------------------------------------------------------------------- #
# Shared constants (mirrors test_iac_approval_get.py for PR 42)
# --------------------------------------------------------------------------- #

_HEAD = "a" * 40
_PLAN_SHA = "b" * 64
_PLAN_JSON_SHA = "c" * 64
_BUCKET = "test-proj-tofu-artifacts"
_PREFIX = f"gs://{_BUCKET}/pr-42/{_HEAD}/run-7-1/"
_META_URI = _PREFIX + "metadata.json"
_GEN_META = "1700000000000003"

_SUMMARY_PLAN = {
    "format_version": "1.2",
    "resource_changes": [
        {
            "address": "google_pubsub_topic.orders",
            "mode": "managed",
            "type": "google_pubsub_topic",
            "name": "orders",
            "change": {
                "actions": ["create"],
                "before": None,
                "after": {"name": "orders-topic"},
                "before_sensitive": False,
                "after_sensitive": False,
                "after_unknown": {"id": True},
            },
        },
    ],
}


def _metadata() -> dict:
    return {
        "schema_version": "c2.v1",
        "repo": "theghostsquad00/driftscribe",
        "pr_number": 42,
        "head_sha": _HEAD,
        "base_sha": "d" * 40,
        "workflow_run_id": "7700000001",
        "workflow_run_attempt": "1",
        "artifact_uri_plan": _PREFIX + "plan.tfplan",
        "artifact_uri_json": _PREFIX + "plan.json",
        "generation_plan": "1700000000000001",
        "generation_json": "1700000000000002",
        "plan_sha256": _PLAN_SHA,
        "plan_json_sha256": _PLAN_JSON_SHA,
        "opentofu_version": "1.12.0",
        "provider_lockfile_sha256": "e" * 64,
    }


def _ref() -> C2CommentRef:
    return C2CommentRef(
        head_sha=_HEAD,
        plan_sha256=_PLAN_SHA,
        plan_json_sha256=_PLAN_JSON_SHA,
        generation_plan="1700000000000001",
        generation_json="1700000000000002",
        generation_metadata=_GEN_META,
        artifact_uri_plan=_PREFIX + "plan.tfplan",
        artifact_uri_json=_PREFIX + "plan.json",
        artifact_uri_metadata=_META_URI,
        opentofu_version="1.12.0",
        comment_id=556677,
        tofu_show_text="# google_pubsub_topic.orders will be created",
    )


def _view(**overrides) -> IacPlanView:
    base = dict(
        metadata=_metadata(),
        tofu_show_text=_ref().tofu_show_text,
        integrity_ok=True,
        denylist_violations=[],
        unverifiable=False,
        _artifact_uri_metadata=_META_URI,
        _generation_metadata=_GEN_META,
    )
    base.update(overrides)
    return IacPlanView(**base)


def _patch_resolve(monkeypatch, *, ref, view) -> None:
    """Patch the three module seams the route resolves through (mirrors approval GET)."""
    monkeypatch.setattr("agent.main.get_repo", lambda token, repo: object())
    monkeypatch.setattr(
        main_mod.iac_artifacts, "find_latest_c2_comment", lambda repo, pr: ref
    )
    monkeypatch.setattr(
        main_mod.iac_artifacts,
        "load_plan_view",
        lambda r, *, bucket_name, client=None, expected_repo=None: view,
    )


@pytest.fixture
def _configured(monkeypatch):
    """Full-configured settings: GitHub + token + artifacts bucket."""
    monkeypatch.setenv("GITHUB_REPO", "theghostsquad00/driftscribe")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")
    monkeypatch.setenv("DRIFTSCRIBE_TOKEN", "static-server-token")
    monkeypatch.setenv("TOFU_ARTIFACTS_BUCKET", _BUCKET)
    monkeypatch.setenv("DRY_RUN", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def _inmemory(monkeypatch):
    """Pin get_state() to InMemory (no Firestore)."""
    monkeypatch.setenv("GCP_PROJECT", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _seed_decision(*, apply_status: str, merge_state: str) -> None:
    """Seed a terminal-or-otherwise decision for PR 42 into the InMemory store."""
    s = get_settings()
    ek = _iac_event_key(s.github_repo, 42, _HEAD, _GEN_META)
    state = get_state()
    state.record_event(ek, {"pr_number": 42})
    _record_iac_decision(
        state, ek, apply_status=apply_status, merge_state=merge_state,
        head_sha=_HEAD, pr_number=42, approver="op@example.com",
    )


# --------------------------------------------------------------------------- #
# Token guard
# --------------------------------------------------------------------------- #


@pytest.mark.no_auth_override
class TestPreviewTokenGuard:
    def _set_token(self, monkeypatch, value: str) -> None:
        monkeypatch.setenv("DRIFTSCRIBE_TOKEN", value)
        get_settings.cache_clear()

    def test_requires_token(self, monkeypatch):
        self._set_token(monkeypatch, "tok-preview")
        _patch_resolve(monkeypatch, ref=_ref(), view=_view(_plan_json=_SUMMARY_PLAN))
        client = TestClient(app)
        assert client.get("/infra/graph/preview?pr=42").status_code == 401

    def test_correct_token_succeeds(self, monkeypatch):
        self._set_token(monkeypatch, "tok-preview")
        _patch_resolve(monkeypatch, ref=_ref(), view=_view(_plan_json=_SUMMARY_PLAN))
        client = TestClient(app)
        resp = client.get(
            "/infra/graph/preview?pr=42",
            headers={"X-DriftScribe-Token": "tok-preview"},
        )
        assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Cache-Control
# --------------------------------------------------------------------------- #


def test_no_store_header(_configured, monkeypatch):
    _patch_resolve(monkeypatch, ref=_ref(), view=_view(_plan_json=_SUMMARY_PLAN))
    resp = TestClient(app).get("/infra/graph/preview?pr=42")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"


# --------------------------------------------------------------------------- #
# Query param validation
# --------------------------------------------------------------------------- #


def test_pr_must_be_positive_int(_configured, monkeypatch):
    client = TestClient(app)
    assert client.get("/infra/graph/preview?pr=0").status_code == 422
    assert client.get("/infra/graph/preview?pr=abc").status_code == 422
    assert client.get("/infra/graph/preview?pr=-1").status_code == 422


# --------------------------------------------------------------------------- #
# Artifact ladder — no_plan
# --------------------------------------------------------------------------- #


def test_no_plan_when_unconfigured(monkeypatch):
    """No GitHub config → _resolve returns (None, None) → no_plan."""
    monkeypatch.setenv("GITHUB_REPO", "")
    monkeypatch.setenv("GITHUB_TOKEN", "")
    get_settings.cache_clear()
    resp = TestClient(app).get("/infra/graph/preview?pr=42")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["reason"] == "no_plan"
    assert body["entries"] == []


def test_no_plan_when_view_none(_configured, monkeypatch):
    """Configured but view is None (artifact unresolvable) → no_plan.

    ``(ref, None)`` means the whole resolution failed (transport/GCS error
    while fetching the artifact a comment pointed at) — no artifact was ever
    characterized, so there is nothing to call an "error" in. This mirrors the
    approval GET's "No verifiable plan artifact" PENDING rung (calm note,
    not red). ``artifact_error`` is reserved for a RESOLVED view that fails
    verification (unverifiable / integrity / denylist / PR-pin mismatch).
    """
    _patch_resolve(monkeypatch, ref=_ref(), view=None)
    resp = TestClient(app).get("/infra/graph/preview?pr=42")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["reason"] == "no_plan"


# --------------------------------------------------------------------------- #
# Artifact ladder — artifact_error
# --------------------------------------------------------------------------- #


def test_artifact_error_unverifiable(_configured, monkeypatch):
    view = _view(unverifiable=True, integrity_ok=False)
    _patch_resolve(monkeypatch, ref=_ref(), view=view)
    resp = TestClient(app).get("/infra/graph/preview?pr=42")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["reason"] == "artifact_error"


def test_artifact_error_integrity(_configured, monkeypatch):
    view = _view(integrity_ok=False)
    _patch_resolve(monkeypatch, ref=_ref(), view=view)
    resp = TestClient(app).get("/infra/graph/preview?pr=42")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["reason"] == "artifact_error"


def test_artifact_error_denylist(_configured, monkeypatch):
    view = _view(denylist_violations=[("protect-coordinator", "deletes svc")])
    _patch_resolve(monkeypatch, ref=_ref(), view=view)
    resp = TestClient(app).get("/infra/graph/preview?pr=42")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["reason"] == "artifact_error"


def test_artifact_error_inconsistent(_configured, monkeypatch):
    view = _view()
    _patch_resolve(monkeypatch, ref=_ref(), view=view)
    monkeypatch.setattr("agent.main._iac_artifact_consistent", lambda *a, **k: False)
    resp = TestClient(app).get("/infra/graph/preview?pr=42")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["reason"] == "artifact_error"


# --------------------------------------------------------------------------- #
# Terminal-decision ladder — resolved
# --------------------------------------------------------------------------- #


def test_resolved_when_applied_and_merged(_configured, _inmemory, monkeypatch):
    _patch_resolve(monkeypatch, ref=_ref(), view=_view(_plan_json=_SUMMARY_PLAN))
    _seed_decision(apply_status="applied", merge_state="merged")
    resp = TestClient(app).get("/infra/graph/preview?pr=42")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["reason"] == "resolved"


def test_resolved_when_failed(_configured, _inmemory, monkeypatch):
    _patch_resolve(monkeypatch, ref=_ref(), view=_view(_plan_json=_SUMMARY_PLAN))
    _seed_decision(apply_status="failed", merge_state="merged")
    resp = TestClient(app).get("/infra/graph/preview?pr=42")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["reason"] == "resolved"


def test_resolved_when_failed_state_suspect(_configured, _inmemory, monkeypatch):
    _patch_resolve(monkeypatch, ref=_ref(), view=_view(_plan_json=_SUMMARY_PLAN))
    _seed_decision(apply_status="failed_state_suspect", merge_state="merged")
    resp = TestClient(app).get("/infra/graph/preview?pr=42")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["reason"] == "resolved"


def test_resolved_when_ambiguous(_configured, _inmemory, monkeypatch):
    _patch_resolve(monkeypatch, ref=_ref(), view=_view(_plan_json=_SUMMARY_PLAN))
    _seed_decision(apply_status="ambiguous", merge_state="merged")
    resp = TestClient(app).get("/infra/graph/preview?pr=42")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["reason"] == "resolved"


# --------------------------------------------------------------------------- #
# Non-terminal decisions stay available
# --------------------------------------------------------------------------- #


def test_waiting_for_rebake_stays_available(_configured, _inmemory, monkeypatch):
    _patch_resolve(monkeypatch, ref=_ref(), view=_view(_plan_json=_SUMMARY_PLAN))
    _seed_decision(apply_status="waiting_for_rebake", merge_state="merged")
    resp = TestClient(app).get("/infra/graph/preview?pr=42")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True


# --------------------------------------------------------------------------- #
# Decision lookup failure is best-effort
# --------------------------------------------------------------------------- #


def test_decision_lookup_failure_is_best_effort(_configured, _inmemory, monkeypatch):
    """A raised find_decision_for_event must NOT bring down the preview."""
    _patch_resolve(monkeypatch, ref=_ref(), view=_view(_plan_json=_SUMMARY_PLAN))

    def _boom(*_a, **_k):
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(get_state(), "find_decision_for_event", _boom)
    resp = TestClient(app).get("/infra/graph/preview?pr=42")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True


# --------------------------------------------------------------------------- #
# Read-route asymmetry: pause/dry-run/token-unset do NOT block preview
# --------------------------------------------------------------------------- #


def test_paused_preview_still_available(_configured, _inmemory, monkeypatch):
    """The preview is a read-only route; pause gates mutations only."""
    _patch_resolve(monkeypatch, ref=_ref(), view=_view(_plan_json=_SUMMARY_PLAN))
    # Activate pause via the real POST endpoint (in-memory store)
    client = TestClient(app)
    r = client.post("/pause", json={"paused": True, "reason": "test"})
    assert r.status_code == 200
    resp = client.get("/infra/graph/preview?pr=42")
    assert resp.status_code == 200
    assert resp.json()["available"] is True


def test_dry_run_preview_still_available(_configured, monkeypatch):
    """Dry-run suppresses mutations but must not suppress the read-only preview."""
    monkeypatch.setenv("DRY_RUN", "true")
    get_settings.cache_clear()
    _patch_resolve(monkeypatch, ref=_ref(), view=_view(_plan_json=_SUMMARY_PLAN))
    resp = TestClient(app).get("/infra/graph/preview?pr=42")
    assert resp.status_code == 200
    assert resp.json()["available"] is True


def test_token_unset_preview_still_available(monkeypatch):
    """Token-unset suppresses approve but must not suppress the read-only preview."""
    monkeypatch.setenv("GITHUB_REPO", "theghostsquad00/driftscribe")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")
    monkeypatch.setenv("DRIFTSCRIBE_TOKEN", "")
    monkeypatch.setenv("TOFU_ARTIFACTS_BUCKET", _BUCKET)
    monkeypatch.setenv("DRY_RUN", "false")
    get_settings.cache_clear()
    _patch_resolve(monkeypatch, ref=_ref(), view=_view(_plan_json=_SUMMARY_PLAN))
    resp = TestClient(app).get("/infra/graph/preview?pr=42")
    assert resp.status_code == 200
    assert resp.json()["available"] is True


# --------------------------------------------------------------------------- #
# Documented DIVERGENCE: terminal decision outranks pending gates (Decision 2)
#
# In the approval GET, the terminal-decision lookup only runs when can_approve
# is True, so under token-unset/dry-run/paused the page SHOWS the summary card
# even for a terminally resolved plan.  The preview route runs the terminal
# lookup UNCONDITIONALLY, so it returns "resolved" while the approval GET still
# renders its summary region.  Both assertions live in the same test so the
# intent is unmissable.
# --------------------------------------------------------------------------- #


def test_terminal_outranks_pending_gates(_configured, _inmemory, monkeypatch):
    """Decision 2 DIVERGENCE: terminal + paused → preview=resolved, GET=summary shown."""
    view_with_plan = _view(_plan_json=_SUMMARY_PLAN)
    _patch_resolve(monkeypatch, ref=_ref(), view=view_with_plan)
    _seed_decision(apply_status="applied", merge_state="merged")

    client = TestClient(app)

    # --- Variant 1: terminal + paused ---
    r = client.post("/pause", json={"paused": True, "reason": "kill-switch"})
    assert r.status_code == 200

    # Preview: terminal decision is looked up unconditionally → resolved
    preview_resp = client.get("/infra/graph/preview?pr=42")
    assert preview_resp.status_code == 200
    preview_body = preview_resp.json()
    assert preview_body["available"] is False
    assert preview_body["reason"] == "resolved"

    # Approval GET: terminal lookup only runs when can_approve; paused means
    # can_approve=False → terminal lookup skipped → summary card still shown.
    approval_resp = client.get("/iac-approvals/42")
    assert approval_resp.status_code == 200
    approval_body = approval_resp.text
    # The summary card region is present (show_summary=True, pause is "pending"
    # not "error"), confirming the approval GET still shows the card.
    assert 'data-testid="change-summary"' in approval_body

    # --- Variant 2: terminal + dry-run ---
    # Unpause first, then set dry-run
    r2 = client.post("/pause", json={"paused": False})
    assert r2.status_code == 200
    monkeypatch.setenv("DRY_RUN", "true")
    get_settings.cache_clear()
    # Re-patch after settings change so the resolve still works
    _patch_resolve(monkeypatch, ref=_ref(), view=view_with_plan)

    preview_resp2 = client.get("/infra/graph/preview?pr=42")
    assert preview_resp2.status_code == 200
    preview_body2 = preview_resp2.json()
    assert preview_body2["available"] is False
    assert preview_body2["reason"] == "resolved"

    approval_resp2 = client.get("/iac-approvals/42")
    assert approval_resp2.status_code == 200
    assert 'data-testid="change-summary"' in approval_resp2.text

    # --- Variant 3: terminal + token-unset ---
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("DRIFTSCRIBE_TOKEN", "")
    get_settings.cache_clear()
    _patch_resolve(monkeypatch, ref=_ref(), view=view_with_plan)

    preview_resp3 = client.get("/infra/graph/preview?pr=42")
    assert preview_resp3.status_code == 200
    preview_body3 = preview_resp3.json()
    assert preview_body3["available"] is False
    assert preview_body3["reason"] == "resolved"

    approval_resp3 = client.get("/iac-approvals/42")
    assert approval_resp3.status_code == 200
    assert 'data-testid="change-summary"' in approval_resp3.text


# --------------------------------------------------------------------------- #
# summary_unavailable
# --------------------------------------------------------------------------- #


def test_summary_unavailable(_configured, monkeypatch):
    """View with _plan_json=None → change_summary=None → summary_unavailable."""
    view = _view()  # _plan_json=None (default)
    _patch_resolve(monkeypatch, ref=_ref(), view=view)
    resp = TestClient(app).get("/infra/graph/preview?pr=42")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["reason"] == "summary_unavailable"


# --------------------------------------------------------------------------- #
# Happy path: body matches plan_overlay()
# --------------------------------------------------------------------------- #


def test_happy_path_matches_plan_overlay(_configured, monkeypatch):
    """The full available body must equal plan_overlay(pr, view.change_summary)."""
    view = _view(_plan_json=_SUMMARY_PLAN)
    _patch_resolve(monkeypatch, ref=_ref(), view=view)
    resp = TestClient(app).get("/infra/graph/preview?pr=42")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    # Build the expected DTO via the pure lib function
    expected = plan_overlay(42, view.change_summary)
    assert body == expected


# --------------------------------------------------------------------------- #
# Parity matrix: approval GET summary region ⟺ preview availability
#
# For shared conditions (excluding the terminal-divergence rows, which are
# covered by test_terminal_outranks_pending_gates), assert that the approval
# GET renders the summary region EXACTLY when the preview resolves as
# available or reason=="summary_unavailable" — i.e. they agree on whether
# the artifact ladder passed.
#
# "summary region present" = any of change-summary / change-summary-empty /
# summary-unavailable is present in the approval GET body.
# --------------------------------------------------------------------------- #

_PARITY_CASES = [
    # (label, setup, expect_available_or_summary_unavail)
    # Artifact ladder failures → both surfaces suppressed
    ("no_plan_none_ref",       dict(ref=None),                          False),
    ("unverifiable",           dict(view_kw={"unverifiable": True, "integrity_ok": False}), False),
    ("integrity_fail",         dict(view_kw={"integrity_ok": False}),   False),
    ("denylist",               dict(view_kw={"denylist_violations": [("r", "d")]}), False),
    ("inconsistent",           dict(inconsistent=True,
                                    view_kw={"_plan_json": _SUMMARY_PLAN}), False),
    # Post-artifact conditions → both surfaces agree to show summary region
    ("summary_unavailable",    dict(view_kw={}),                        True),   # _plan_json=None → summary_unavailable
    ("happy_with_plan",        dict(view_kw={"_plan_json": _SUMMARY_PLAN}), True),
    # token-unset/dry-run/paused don't block either surface (NOT divergence
    # rows — divergence is only terminal+pending-gate combos, covered by
    # test_terminal_outranks_pending_gates)
    ("token_unset",            dict(token_unset=True,
                                    view_kw={"_plan_json": _SUMMARY_PLAN}), True),
    ("dry_run",                dict(dry_run=True,
                                    view_kw={"_plan_json": _SUMMARY_PLAN}), True),
    ("paused",                 dict(paused=True,
                                    view_kw={"_plan_json": _SUMMARY_PLAN}), True),
    # A NON-terminal recorded decision keeps both surfaces showing
    ("waiting_for_rebake",     dict(seed_decision=dict(apply_status="waiting_for_rebake",
                                                       merge_state="merged"),
                                    view_kw={"_plan_json": _SUMMARY_PLAN}), True),
]


@pytest.mark.parametrize("label,setup,expect_ok", _PARITY_CASES, ids=[c[0] for c in _PARITY_CASES])
def test_parity_matrix(_configured, monkeypatch, label, setup, expect_ok):  # noqa: ARG001
    """Approval-GET summary region presence ⟺ preview available-or-summary-unavailable."""
    # Rows that touch the StateStore (pause toggle / decision seeding) need the
    # InMemory store — _configured sets DRY_RUN=false and the conftest sets
    # GCP_PROJECT=test-proj, which would otherwise resolve a (CI-unreachable)
    # FirestoreStateStore. Same mechanism as the _inmemory fixture.
    if setup.get("paused") or setup.get("seed_decision"):
        monkeypatch.setenv("GCP_PROJECT", "")
        get_settings.cache_clear()
    # Apply setup overrides
    if setup.get("token_unset"):
        monkeypatch.setenv("DRIFTSCRIBE_TOKEN", "")
        get_settings.cache_clear()
    if setup.get("dry_run"):
        monkeypatch.setenv("DRY_RUN", "true")
        get_settings.cache_clear()

    ref = setup.get("ref", _ref())
    view_kw = setup.get("view_kw", {})
    view = _view(**view_kw) if ref is not None else None
    _patch_resolve(monkeypatch, ref=ref, view=view)
    if setup.get("inconsistent"):
        monkeypatch.setattr("agent.main._iac_artifact_consistent", lambda *a, **k: False)
    if setup.get("seed_decision"):
        _seed_decision(**setup["seed_decision"])

    client = TestClient(app)

    if setup.get("paused"):
        r = client.post("/pause", json={"paused": True, "reason": "parity-matrix"})
        assert r.status_code == 200, r.text

    # Preview response
    preview_resp = client.get("/infra/graph/preview?pr=42")
    assert preview_resp.status_code == 200
    preview_body = preview_resp.json()
    preview_ok = preview_body["available"] or preview_body.get("reason") == "summary_unavailable"

    # Approval GET response
    approval_resp = client.get("/iac-approvals/42")
    assert approval_resp.status_code == 200
    approval_text = approval_resp.text
    summary_region_present = any(
        marker in approval_text
        for marker in (
            'data-testid="change-summary"',
            'data-testid="change-summary-empty"',
            'data-testid="summary-unavailable"',
        )
    )

    # Both surfaces must agree
    assert preview_ok == expect_ok, (
        f"[{label}] expected preview ok={expect_ok}, got {preview_body}"
    )
    assert summary_region_present == expect_ok, (
        f"[{label}] expected summary region={expect_ok}, got text snippet: "
        f"{approval_text[:200]}"
    )
    # The two surfaces must agree with each other
    assert preview_ok == summary_region_present, (
        f"[{label}] parity violated: preview_ok={preview_ok}, "
        f"summary_region_present={summary_region_present}"
    )
