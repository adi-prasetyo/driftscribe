"""load_iac_plan_tool — bounded, fail-soft, sensitive-masked plan Q&A surface.

Pins (item 12 design §4–5):
- NO summary when unverifiable or integrity fails (never describe a
  possibly-tampered plan).
- Summary IS returned alongside denylist violations (deliberate divergence
  from the approval page's card suppression — framing pinned in the prompt).
- AttrChange display strings pass through ALREADY masked — assert the
  literal "(sensitive)" marker, never a raw value.
- Fail-soft: every failure path returns an error dict; the tool never raises.

Patching note: these tests use ``monkeypatch.setattr(_adk_tools_mod, attr, value)``
(module-object form, not the dotted-string form) to avoid a stale-module
interaction with ``test_adk_agent_imports_cleanly_without_pulling_dangerous_
sdks``.  That test temporarily pops ``agent.adk_tools`` from ``sys.modules``
and reimports ``agent.adk_agent``, which makes Python set the ``adk_tools``
attribute on the parent ``agent`` package to the newly-imported module.  After
the finally-block restores ``sys.modules["agent.adk_tools"]`` to the original,
``import agent.adk_tools as …`` inside a test function silently returns the
stale attribute (``agent.adk_tools``) rather than ``sys.modules["agent.adk_
tools"]``, landing the patch on the wrong module object.

Capturing the module reference at collection time (``_adk_tools_mod = import
agent.adk_tools``) is executed *before* any test runs, so it always holds the
original module whose ``__dict__`` is ``load_iac_plan_tool.__globals__``.
"""
import agent.adk_tools as _adk_tools_mod  # captured at collection — always mod1

import pytest

from agent.adk_tools import load_iac_plan_tool
from agent.config import get_settings
from agent.iac_artifacts import IacPlanView
from driftscribe_lib.iac_plan_summary import (
    AttrChange,
    ChangeEntry,
    PlanSummary,
)


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def _make_view(**kwargs) -> IacPlanView:
    """Build a minimal IacPlanView with sensible defaults."""
    defaults = dict(
        metadata={
            "schema_version": "c2.v1",
            "repo": "adi-prasetyo/driftscribe",
            "pr_number": 7,
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "workflow_run_id": "100",
            "workflow_run_attempt": "1",
            "artifact_uri_plan": "gs://bkt/pr-7/aaa/run-100-1/plan.tfplan",
            "artifact_uri_json": "gs://bkt/pr-7/aaa/run-100-1/plan.json",
            "generation_plan": "1",
            "generation_json": "2",
            "plan_sha256": "c" * 64,
            "plan_json_sha256": "d" * 64,
            "opentofu_version": "1.12.0",
            "provider_lockfile_sha256": "e" * 64,
        },
        tofu_show_text="",
        integrity_ok=True,
        denylist_violations=[],
        unverifiable=False,
        _artifact_uri_metadata="gs://bkt/pr-7/aaa/run-100-1/metadata.json",
        _generation_metadata="3",
    )
    defaults.update(kwargs)
    return IacPlanView(**defaults)


@pytest.fixture(autouse=True)
def _clear_settings(monkeypatch):
    """Each test gets a clean settings cache with a minimal GCP_PROJECT."""
    monkeypatch.setenv("GCP_PROJECT", "testproj")
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    monkeypatch.delenv("TOFU_ARTIFACTS_BUCKET", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_not_found(monkeypatch):
    """Loader returns None → found=False with helpful error."""
    monkeypatch.setattr(_adk_tools_mod, "load_plan_view_from_gcs", lambda *a, **k: None)
    result = load_iac_plan_tool(7)
    assert result["found"] is False
    assert "error" in result
    assert "plan-builder" in result["error"]


def test_not_found_bucket_name(monkeypatch):
    """Loader receives bucket_name derived from GCP_PROJECT."""
    received = {}

    def _capture(pr_number, *, bucket_name, **kwargs):
        received["bucket_name"] = bucket_name
        return None

    monkeypatch.setattr(_adk_tools_mod, "load_plan_view_from_gcs", _capture)
    load_iac_plan_tool(7)
    assert received["bucket_name"] == "testproj-tofu-artifacts"


def test_invalid_pr_number(monkeypatch):
    """Non-positive integer → error dict, loader NOT called."""
    calls = []
    monkeypatch.setattr(_adk_tools_mod, "load_plan_view_from_gcs",
                        lambda *a, **k: calls.append(1) or None)
    assert load_iac_plan_tool(0)["found"] is False
    assert load_iac_plan_tool(-3)["found"] is False
    assert not calls  # loader was never invoked


def test_unverifiable_returns_no_summary(monkeypatch):
    """View with unverifiable=True → found=True, no summary key, error present."""
    view = _make_view(unverifiable=True, integrity_ok=False, metadata={})
    monkeypatch.setattr(_adk_tools_mod, "load_plan_view_from_gcs", lambda *a, **k: view)
    result = load_iac_plan_tool(7)
    assert result["found"] is True
    assert result["unverifiable"] is True
    assert "summary" not in result
    assert "error" in result
    assert "not be verified" in result["error"] or "unavailable" in result["error"]


def test_integrity_mismatch_returns_no_summary(monkeypatch):
    """integrity_ok=False → no summary key, error mentions integrity."""
    view = _make_view(integrity_ok=False)
    monkeypatch.setattr(_adk_tools_mod, "load_plan_view_from_gcs", lambda *a, **k: view)
    result = load_iac_plan_tool(7)
    assert result["found"] is True
    assert "summary" not in result
    assert "error" in result
    assert "integrity" in result["error"].lower()


def test_happy_path_summary_shape(monkeypatch):
    """Happy path: verify counts dict, entry keys, sensitive masking, blast, etc."""
    from driftscribe_lib.iac_plan_summary import BLAST_CANNOT_TOUCH_NOTE, blast_radius_phrase  # noqa: F401

    # Build a PlanSummary directly with a sensitive AttrChange to avoid
    # depending on summarize_plan parsing logic in this tool test.
    sensitive_attr = AttrChange(
        path="secret_key",
        before="(sensitive)",
        after="(sensitive)",
        sensitive=True,
    )
    normal_attr = AttrChange(
        path="uniform_bucket_level_access",
        before="false",
        after="true",
        sensitive=False,
    )
    entry_create = ChangeEntry(
        verb="create",
        rtype="google_storage_bucket",
        type_label="Storage Bucket",
        name="my_bucket",
        address="google_storage_bucket.my_bucket",
        location="US",
        attr_changes=(normal_attr,),
    )
    entry_update = ChangeEntry(
        verb="update",
        rtype="google_storage_bucket",
        type_label="Storage Bucket",
        name="other_bucket",
        address="google_storage_bucket.other_bucket",
        attr_changes=(sensitive_attr,),
    )
    summary = PlanSummary(
        entries=(entry_create, entry_update),
        n_create=1,
        n_update=1,
    )
    view = _make_view()
    # Inject summary via instance __dict__ (bypasses cached_property cache miss).
    view.__dict__["change_summary"] = summary

    monkeypatch.setattr(_adk_tools_mod, "load_plan_view_from_gcs", lambda *a, **k: view)
    result = load_iac_plan_tool(7)

    assert result["found"] is True
    assert result["unverifiable"] is False
    assert result["integrity_ok"] is True
    assert result["approval_page"] == "/iac-approvals/7"
    assert result.get("caveat"), "caveat must be non-empty"

    result_summary = result["summary"]
    assert result_summary is not None
    counts = result_summary["counts"]
    assert counts["create"] == 1
    assert counts["update"] == 1

    # Verify entries have the expected keys including resource_type (Codex must-fix 2)
    entries = result_summary["entries"]
    assert len(entries) == 2
    entry = entries[0]
    for key in ("verb", "resource_type", "name", "address", "location",
                "attr_changes", "imported", "deposed", "action_reason", "attrs_truncated"):
        assert key in entry, f"missing key {key!r} in entry"

    # resource_type must be the type_label, not rtype
    assert entry["resource_type"] == "Storage Bucket"

    # Sensitive attr: the "(sensitive)" literal with sensitive=True
    update_entry = entries[1]
    assert len(update_entry["attr_changes"]) == 1
    sens_attr = update_entry["attr_changes"][0]
    assert sens_attr["sensitive"] is True
    assert sens_attr["before"] == "(sensitive)"
    assert sens_attr["after"] == "(sensitive)"

    # blast_radius and cannot_touch
    assert "blast_radius" in result
    assert result["cannot_touch"] == BLAST_CANNOT_TOUCH_NOTE


def test_denylist_violations_with_summary(monkeypatch):
    """View with violations + integrity_ok → BOTH violations AND summary present."""
    summary = PlanSummary(
        entries=(
            ChangeEntry(
                verb="create",
                rtype="google_storage_bucket",
                type_label="Storage Bucket",
                name="my_bucket",
                address="google_storage_bucket.my_bucket",
            ),
        ),
        n_create=1,
    )
    view = _make_view(
        denylist_violations=[("protect-coordinator", "deletes the agent")],
        integrity_ok=True,
    )
    # Inject a pre-built change_summary by bypassing cached_property
    object.__setattr__(view, "__dict__", {**view.__dict__, "change_summary": summary})

    monkeypatch.setattr(_adk_tools_mod, "load_plan_view_from_gcs", lambda *a, **k: view)
    result = load_iac_plan_tool(7)

    assert result["found"] is True
    assert result.get("blocked") is True
    assert result["denylist_violations"] == [
        {"rule": "protect-coordinator", "detail": "deletes the agent"}
    ]
    assert result["summary"] is not None


def test_summary_unavailable(monkeypatch):
    """_plan_json shaped so summarize_plan returns None → summary is None."""
    view = _make_view()
    # A plan with no resource_changes makes summarize_plan return a PlanSummary
    # with empty entries (not None) — use None _plan_json to trigger None summary.
    view._plan_json = None
    if "change_summary" in view.__dict__:
        del view.__dict__["change_summary"]

    monkeypatch.setattr(_adk_tools_mod, "load_plan_view_from_gcs", lambda *a, **k: view)
    result = load_iac_plan_tool(7)

    # When _plan_json is None, change_summary is None → summary=None in output
    assert result["found"] is True
    assert result.get("summary") is None
    assert "summary_unavailable" in result


def test_loader_exception_is_fail_soft(monkeypatch):
    """Loader raises RuntimeError → error dict, no exception propagates."""
    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(_adk_tools_mod, "load_plan_view_from_gcs", _boom)
    result = load_iac_plan_tool(7)
    assert result["found"] is False
    assert "error" in result
    assert "boom" in result["error"]


def test_expected_repo_threaded(monkeypatch):
    """GITHUB_REPO env is forwarded as expected_repo; unset → None."""
    received = {}

    def _capture(pr_number, *, bucket_name, expected_repo=None, **kwargs):
        received["expected_repo"] = expected_repo
        return None

    monkeypatch.setattr(_adk_tools_mod, "load_plan_view_from_gcs", _capture)

    # With GITHUB_REPO set.
    monkeypatch.setenv("GITHUB_REPO", "adi-prasetyo/driftscribe")
    get_settings.cache_clear()
    load_iac_plan_tool(7)
    assert received["expected_repo"] == "adi-prasetyo/driftscribe"

    # With GITHUB_REPO unset (empty).
    monkeypatch.setenv("GITHUB_REPO", "")
    get_settings.cache_clear()
    load_iac_plan_tool(7)
    assert received["expected_repo"] is None
