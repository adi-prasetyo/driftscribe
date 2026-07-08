"""Tests for agent.capabilities and the registry._parse_spec refactor.

Task 2 of the capability-card plan (2026-06-10).  Covers:

- _parse_spec symbol validation (env-free), sub-cases (a)–(e)
- load_workload_spec public wrapper
- Drift-pin tests for every constant in agent.capabilities
- build_capabilities() shape and JSON-serializability
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import agent.workloads.registry as registry_mod
from agent.workloads.registry import (
    WORKER_REGISTRY,
    ReservedToolNotImplementedError,
    UnknownActionError,
    UnknownToolError,
    UnknownWorkerError,
    load_workload_spec,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_MINIMAL_DRIFT_YAML = """\
name: drift
display_name: Cloud Run env drift
descriptor: Cloud Run config
description: test
system_prompt_file: system_prompt.txt
contract_file: null
enabled_tool_names:
  - drift_read_live_env
worker_names:
  - drift_reader
observation_kind: cloud_run_env
action_names:
  - no_op
"""


def _write_workload(tmp_path: Path, yaml_text: str) -> Path:
    """Write a workload YAML at tmp_path/drift/workload.yaml."""
    workload_dir = tmp_path / "drift"
    workload_dir.mkdir(exist_ok=True)
    (workload_dir / "workload.yaml").write_text(yaml_text)
    (workload_dir / "system_prompt.txt").write_text("test prompt")
    return workload_dir / "workload.yaml"


@pytest.fixture(autouse=True)
def _clear_cache():
    registry_mod._WORKLOAD_CACHE.clear()
    yield
    registry_mod._WORKLOAD_CACHE.clear()


# --------------------------------------------------------------------------- #
# _parse_spec symbol validation
# --------------------------------------------------------------------------- #


def test_parse_spec_unknown_tool_raises(tmp_path):
    """(a) Unknown tool name → UnknownToolError without reading env."""
    bad = _MINIMAL_DRIFT_YAML.replace(
        "  - drift_read_live_env",
        "  - drift_read_live_env\n  - shell_exec",
    )
    yaml_path = _write_workload(tmp_path, bad)
    with pytest.raises(UnknownToolError, match="shell_exec"):
        registry_mod._parse_spec(yaml_path)


def test_parse_spec_reserved_tool_raises(tmp_path):
    """(b) Reserved tool (get_session_state = None) → ReservedToolNotImplementedError."""
    bad = _MINIMAL_DRIFT_YAML.replace(
        "  - drift_read_live_env",
        "  - drift_read_live_env\n  - get_session_state",
    )
    yaml_path = _write_workload(tmp_path, bad)
    with pytest.raises(ReservedToolNotImplementedError):
        registry_mod._parse_spec(yaml_path)


def test_parse_spec_unknown_worker_raises_without_env(tmp_path, monkeypatch):
    """(c) Unknown worker → UnknownWorkerError with all worker-URL env vars deleted."""
    # Delete all worker URL env vars to prove no env is read
    for spec in WORKER_REGISTRY.values():
        monkeypatch.delenv(spec.url_env, raising=False)
    bad = _MINIMAL_DRIFT_YAML.replace(
        "  - drift_reader",
        "  - drift_reader\n  - attacker_worker",
    )
    yaml_path = _write_workload(tmp_path, bad)
    with pytest.raises(UnknownWorkerError, match="attacker_worker"):
        registry_mod._parse_spec(yaml_path)


def test_parse_spec_unknown_action_raises(tmp_path):
    """(d) Unknown action → UnknownActionError."""
    bad = _MINIMAL_DRIFT_YAML.replace(
        "  - no_op",
        "  - no_op\n  - delete_everything",
    )
    yaml_path = _write_workload(tmp_path, bad)
    with pytest.raises(UnknownActionError, match="delete_everything"):
        registry_mod._parse_spec(yaml_path)


def test_parse_spec_happy_path_no_env(tmp_path, monkeypatch):
    """(e) Happy path returns the spec with all worker-URL env vars deleted."""
    for spec in WORKER_REGISTRY.values():
        monkeypatch.delenv(spec.url_env, raising=False)
    yaml_path = _write_workload(tmp_path, _MINIMAL_DRIFT_YAML)
    result = registry_mod._parse_spec(yaml_path)
    assert result.name == "drift"


# --------------------------------------------------------------------------- #
# load_workload_spec public wrapper
# --------------------------------------------------------------------------- #


def test_load_workload_spec_returns_spec_env_free(monkeypatch):
    """load_workload_spec works even with all worker URL env vars unset."""
    for spec in WORKER_REGISTRY.values():
        monkeypatch.delenv(spec.url_env, raising=False)
    result = load_workload_spec("drift")
    assert result.name == "drift"


def test_load_workload_spec_all_workloads(monkeypatch):
    """All four workloads can be parsed without worker URLs set."""
    for spec in WORKER_REGISTRY.values():
        monkeypatch.delenv(spec.url_env, raising=False)
    for name in ("drift", "upgrade", "explore", "provision"):
        result = load_workload_spec(name)
        assert result.name == name


# --------------------------------------------------------------------------- #
# agent.capabilities drift-pin tests
# --------------------------------------------------------------------------- #

from agent.capabilities import (  # noqa: E402
    CATEGORY_ORDER,
    HUMAN_GATES,
    RULE_CATEGORIES,
    TOOL_DESCRIPTIONS,
    WORKER_DESCRIPTIONS,
    WORKLOAD_NAMES,
    build_capabilities,
)
from agent.workloads.registry import ACTION_REGISTRY, TOOL_REGISTRY  # noqa: E402


def test_tool_descriptions_cover_exactly_the_tool_registry():
    assert set(TOOL_DESCRIPTIONS) == set(TOOL_REGISTRY)


def test_worker_descriptions_cover_exactly_the_worker_registry():
    assert set(WORKER_DESCRIPTIONS) == set(WORKER_REGISTRY)


def test_rule_categories_cover_exactly_the_rule_descriptions():
    from driftscribe_lib.iac_plan_denylist import RULE_DESCRIPTIONS
    assert set(RULE_CATEGORIES) == set(RULE_DESCRIPTIONS)
    assert set(RULE_CATEGORIES.values()) <= set(CATEGORY_ORDER)


def test_adoptable_type_labels_cover_exactly_the_allowlist():
    """ADOPTABLE_TYPE_LABELS must have exactly one label per type in
    ADOPTABLE_RESOURCE_TYPES — no stale, no missing entries."""
    from agent.capabilities import ADOPTABLE_TYPE_LABELS
    from driftscribe_lib.iac_plan_denylist import ADOPTABLE_RESOURCE_TYPES
    assert set(ADOPTABLE_TYPE_LABELS) == ADOPTABLE_RESOURCE_TYPES


def test_every_approval_gated_action_has_a_human_gate():
    gated = {n for n, s in ACTION_REGISTRY.items() if s.requires_approval}
    assert gated <= {g["id"] for g in HUMAN_GATES}


def test_chat_only_coherence_with_main(monkeypatch):
    # observation_kind == "none" (declarative) must equal main's
    # CHAT_ONLY_WORKLOAD_NAMES (enforcement: /recheck route-refusal).
    for spec in WORKER_REGISTRY.values():
        monkeypatch.delenv(spec.url_env, raising=False)
    from agent.main import CHAT_ONLY_WORKLOAD_NAMES
    declared = {
        n for n in WORKLOAD_NAMES
        if load_workload_spec(n).observation_kind == "none"
    }
    assert declared == set(CHAT_ONLY_WORKLOAD_NAMES)


# --------------------------------------------------------------------------- #
# build_capabilities() shape
# --------------------------------------------------------------------------- #


def test_build_capabilities_shape(monkeypatch):
    for spec in WORKER_REGISTRY.values():
        monkeypatch.delenv(spec.url_env, raising=False)
    dto = build_capabilities()
    assert dto["version"] == 1
    assert [w["name"] for w in dto["workloads"]] == list(WORKLOAD_NAMES)
    prov = next(w for w in dto["workloads"] if w["name"] == "provision")
    assert prov["autonomous"] is False
    # Autonomy-truth (Phase 17.G crew rename): ONLY drift/Anchor is autonomous.
    # upgrade/Patch's observation_kind is ``repo_lockfile`` (intent) but it has
    # no wired trigger — /recheck upgrade 503s — so the DTO flag must be False.
    # This is the bug the rename fixes: the flag was derived from
    # observation_kind != "none", which wrongly marked Patch autonomous.
    by_name = {w["name"]: w for w in dto["workloads"]}
    assert by_name["drift"]["autonomous"] is True
    assert by_name["upgrade"]["autonomous"] is False
    assert by_name["explore"]["autonomous"] is False
    # Identity rename + descriptor landed in the DTO.
    assert by_name["drift"]["display_name"] == "Anchor"
    assert by_name["upgrade"]["display_name"] == "Patch"
    for w in dto["workloads"]:
        assert isinstance(w["descriptor"], str) and w["descriptor"].strip(), (
            f"workload {w['name']!r} missing a non-empty descriptor"
        )
    open_pr = next(t for t in prov["tools"] if t["name"] == "provision_open_infra_pr")
    assert open_pr["write_capable"] is True
    read_env = next(t for t in prov["tools"] if t["name"] == "drift_read_live_env")
    assert read_env["write_capable"] is False
    assert {g["id"] for g in dto["human_gates"]} == {"iac_apply", "rollback"}
    assert len(dto["denylist"]["rules"]) == 20
    # Pin the FULL promised sort, not just category grouping.
    rules = dto["denylist"]["rules"]
    assert rules == sorted(
        rules, key=lambda r: (CATEGORY_ORDER.index(r["category"]), r["id"])
    )
    # Adoptable types: 4 entries, sorted, both str fields present.
    adoptable = dto["denylist"]["adoptable_resource_types"]
    assert len(adoptable) == 4
    assert adoptable == sorted(adoptable, key=lambda x: x["type"])
    for entry in adoptable:
        assert isinstance(entry["type"], str) and isinstance(entry["label"], str)


def test_autonomous_trigger_workloads_is_exactly_drift(monkeypatch):
    """Autonomy-truth guard (Phase 17.G). The operator-facing "Autonomous"
    badge must mean a WIRED trigger, not aspiration. Only ``drift`` has one
    (Eventarc → /eventarc, hardcoded drift); ``upgrade``'s /recheck 503s and
    explore/provision are chat-only — so the set is exactly {"drift"}. If a
    Patch trigger is ever wired, this test (and the badge) move together."""
    for spec in WORKER_REGISTRY.values():
        monkeypatch.delenv(spec.url_env, raising=False)
    from agent.main import AUTONOMOUS_TRIGGER_WORKLOADS, CHAT_ONLY_WORKLOAD_NAMES

    assert AUTONOMOUS_TRIGGER_WORKLOADS == frozenset({"drift"})
    assert "upgrade" not in AUTONOMOUS_TRIGGER_WORKLOADS
    # Triggered and chat-only are disjoint camps.
    assert AUTONOMOUS_TRIGGER_WORKLOADS.isdisjoint(CHAT_ONLY_WORKLOAD_NAMES)
    # The full taxonomy: every workload is either autonomously triggered,
    # chat-only, or the special unwired case (upgrade — intent-only /recheck
    # that 503s, pinned by test_recheck_upgrade_* and the main.py:1233 guard).
    assert (
        set(AUTONOMOUS_TRIGGER_WORKLOADS)
        | set(CHAT_ONLY_WORKLOAD_NAMES)
        | {"upgrade"}
    ) == set(WORKLOAD_NAMES)


def test_frontend_catalog_matches_backend(monkeypatch):
    """Cross-surface sync: the checked-in SPA crew catalog
    (frontend/src/lib/workloads.catalog.json) must match the backend YAML
    ``display_name``/``descriptor`` and the derived autonomy group. This is
    the single guard against the picker and /capabilities drifting apart.
    Reads the JSON (not the .ts) so Python never regex-parses TypeScript."""
    for spec in WORKER_REGISTRY.values():
        monkeypatch.delenv(spec.url_env, raising=False)
    from agent.main import AUTONOMOUS_TRIGGER_WORKLOADS

    repo_root = Path(__file__).resolve().parents[2]
    catalog_path = repo_root / "frontend" / "src" / "lib" / "workloads.catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))

    # Same workloads on both surfaces — no missing, no extra.
    assert {e["value"] for e in catalog} == set(WORKLOAD_NAMES)
    assert len(catalog) == len(WORKLOAD_NAMES)
    for entry in catalog:
        spec = load_workload_spec(entry["value"])
        assert entry["name"] == spec.display_name, entry["value"]
        assert entry["descriptor"] == spec.descriptor, entry["value"]
        expected_group = (
            "autonomous"
            if entry["value"] in AUTONOMOUS_TRIGGER_WORKLOADS
            else "on-demand"
        )
        assert entry["group"] == expected_group, entry["value"]


def test_build_capabilities_is_json_serializable_and_env_free(monkeypatch):
    # Must not require worker URL env vars (unlike load_workload). Codex
    # review: derive the list from WORKER_REGISTRY so a future worker's
    # env var cannot be missed by this test.
    for spec in WORKER_REGISTRY.values():
        monkeypatch.delenv(spec.url_env, raising=False)
    json.dumps(build_capabilities())
