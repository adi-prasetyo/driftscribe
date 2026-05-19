"""Tests for the workload registry — the authority layer (Phase 17.A.1).

The registry is the central authority on which URLs/secrets/repos a
workload can touch. `WorkloadSpec` YAML names tools and workers
*symbolically*; the registry resolves those names to real callables
and `WorkerEndpoint`s. Flipping a YAML value cannot change *what URL*
the agent calls — it can only choose from the allowlist.

Property pins:

1. `load_workload("drift")` succeeds when drift worker env vars are
   set (it's the existing, supported workload).
2. A YAML with a tool name not in `TOOL_REGISTRY` raises
   `UnknownToolError` at load time (not at first agent call).
3. A YAML with a worker name not in `WORKER_REGISTRY` raises
   `UnknownWorkerError` at load time.
4. A YAML with an action name not in `ACTION_REGISTRY` raises
   `UnknownActionError`.
5. `load_workload("unknown")` raises `UnknownWorkloadError`.
6. Drift worker URLs missing from env raise a clear error when
   `load_workload("drift")` is called.
7. Loaded specs are cached (repeated calls return the same object).
"""
from __future__ import annotations

from pathlib import Path

import pytest

import agent.workloads.registry as registry_mod
from agent.workloads.registry import (
    ACTION_REGISTRY,
    TOOL_REGISTRY,
    WORKER_REGISTRY,
    ActionSpec,
    MissingWorkerEnvError,
    UnknownActionError,
    UnknownToolError,
    UnknownWorkerError,
    UnknownWorkloadError,
    WorkerEndpoint,
    WorkloadResolution,
    load_workload,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Reset the module-level cache before each test so monkeypatched
    env vars are honored."""
    registry_mod._WORKLOAD_CACHE.clear()
    yield
    registry_mod._WORKLOAD_CACHE.clear()


@pytest.fixture
def drift_env(monkeypatch):
    """Set every drift worker URL env var the registry expects."""
    monkeypatch.setenv("READER_URL", "https://reader.example.com")
    monkeypatch.setenv("DOCS_URL", "https://docs.example.com")
    monkeypatch.setenv("ROLLBACK_URL", "https://rollback.example.com")
    monkeypatch.setenv("NOTIFIER_URL", "https://notifier.example.com")


def _write_workload(tmp_path: Path, yaml_text: str) -> Path:
    workload_dir = tmp_path / "drift"
    workload_dir.mkdir()
    (workload_dir / "workload.yaml").write_text(yaml_text)
    (workload_dir / "system_prompt.txt").write_text("test prompt")
    return workload_dir / "workload.yaml"


# --------------------------------------------------------------------------- #
# Authority shape: TOOL_REGISTRY / WORKER_REGISTRY / ACTION_REGISTRY exist
# --------------------------------------------------------------------------- #


def test_tool_registry_has_drift_symbolic_names():
    """Drift tools must be present and resolvable (callables). Tools
    deferred to later sub-phases may be present as None."""
    for name in (
        "drift_read_live_env",
        "drift_patch_docs",
        "drift_propose_rollback",
        "notify",
        "load_contract",
        "search_recent_prs",
    ):
        assert name in TOOL_REGISTRY, f"missing drift tool: {name}"
        assert callable(TOOL_REGISTRY[name]), f"{name} not callable"


def test_tool_registry_reserves_future_tool_names_as_none():
    """Upgrade-workload and MCP-only tools are reserved by name but
    not yet implemented (17.B/17.C). They appear as None so the
    registry can detect 'reserved but not yet ready' separately from
    'unknown name entirely'."""
    for name in (
        "upgrade_read_dependencies",
        "upgrade_propose_pr",
        "search_developer_docs",
        "retrieve_developer_doc",
        "get_session_state",
        "set_session_state",
    ):
        assert name in TOOL_REGISTRY, f"missing reserved tool slot: {name}"
        assert TOOL_REGISTRY[name] is None


def test_action_registry_covers_existing_drift_actions():
    for name in ("docs_pr", "drift_issue", "escalation", "no_op", "rollback"):
        assert name in ACTION_REGISTRY
        spec = ACTION_REGISTRY[name]
        assert isinstance(spec, ActionSpec)
        assert spec.name == name
    # Rollback requires approval; no_op and friends do not.
    assert ACTION_REGISTRY["rollback"].requires_approval is True
    assert ACTION_REGISTRY["no_op"].requires_approval is False


def test_registries_are_immutable_mapping_proxies():
    """The three allowlists are the central security surface. They are
    exposed as :class:`types.MappingProxyType` views so any caller that
    grabs a reference cannot widen the surface by in-place mutation
    (which is what ``Final`` alone allows). One assertion per registry."""
    with pytest.raises(TypeError):
        TOOL_REGISTRY["attacker_tool"] = lambda: None  # type: ignore[index]
    with pytest.raises(TypeError):
        WORKER_REGISTRY["attacker_worker"] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        ACTION_REGISTRY["attacker_action"] = None  # type: ignore[index]


# --------------------------------------------------------------------------- #
# Happy path: load_workload("drift")
# --------------------------------------------------------------------------- #


def test_load_workload_drift_succeeds(drift_env):
    resolution = load_workload("drift")
    assert isinstance(resolution, WorkloadResolution)
    assert resolution.spec.name == "drift"
    # Tool callables resolved.
    for tool_name in resolution.spec.enabled_tool_names:
        assert tool_name in resolution.tools
        assert callable(resolution.tools[tool_name])
    # Worker endpoints resolved.
    for worker_name in resolution.spec.worker_names:
        ep = resolution.workers[worker_name]
        assert isinstance(ep, WorkerEndpoint)
        assert ep.url.startswith("https://")
        assert ep.audience == ep.url
    # Action specs resolved.
    for action_name in resolution.spec.action_names:
        assert resolution.actions[action_name].name == action_name


def test_load_workload_is_cached(drift_env):
    a = load_workload("drift")
    b = load_workload("drift")
    assert a is b


def test_load_workload_unknown_name_raises():
    with pytest.raises(UnknownWorkloadError, match="kubernetes"):
        load_workload("kubernetes")


# --------------------------------------------------------------------------- #
# Authority enforcement: symbolic names that aren't allowlisted fail at boot.
# --------------------------------------------------------------------------- #


_MINIMAL_DRIFT_YAML = """\
name: drift
display_name: Cloud Run env drift
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


def test_unknown_tool_name_raises_at_load(tmp_path, drift_env):
    bad = _MINIMAL_DRIFT_YAML.replace(
        "  - drift_read_live_env",
        "  - drift_read_live_env\n  - shell_exec",
    )
    yaml_path = _write_workload(tmp_path, bad)
    with pytest.raises(UnknownToolError, match="shell_exec"):
        registry_mod._load_from_path(yaml_path)


def test_unknown_worker_name_raises_at_load(tmp_path, drift_env):
    bad = _MINIMAL_DRIFT_YAML.replace(
        "  - drift_reader",
        "  - drift_reader\n  - attacker_worker",
    )
    yaml_path = _write_workload(tmp_path, bad)
    with pytest.raises(UnknownWorkerError, match="attacker_worker"):
        registry_mod._load_from_path(yaml_path)


def test_unknown_action_name_raises_at_load(tmp_path, drift_env):
    bad = _MINIMAL_DRIFT_YAML.replace(
        "  - no_op",
        "  - no_op\n  - delete_everything",
    )
    yaml_path = _write_workload(tmp_path, bad)
    with pytest.raises(UnknownActionError, match="delete_everything"):
        registry_mod._load_from_path(yaml_path)


def test_tool_reserved_but_not_yet_implemented_raises(tmp_path, drift_env):
    """A YAML referencing an upgrade tool today (before 17.B/17.C ships
    the callables) must fail with a clear error, not a NoneType crash
    when the LLM calls it."""
    bad = _MINIMAL_DRIFT_YAML.replace(
        "  - drift_read_live_env",
        "  - drift_read_live_env\n  - upgrade_read_dependencies",
    )
    yaml_path = _write_workload(tmp_path, bad)
    with pytest.raises(UnknownToolError, match="not yet implemented"):
        registry_mod._load_from_path(yaml_path)


# --------------------------------------------------------------------------- #
# Env-var resolution for worker URLs.
# --------------------------------------------------------------------------- #


def test_missing_drift_worker_env_raises_at_load(monkeypatch):
    # Clear every URL env var so the resolution must fail.
    for var in ("READER_URL", "DOCS_URL", "ROLLBACK_URL", "NOTIFIER_URL"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(MissingWorkerEnvError, match="READER_URL"):
        load_workload("drift")


def test_load_workload_upgrade_fails_clearly_until_dependencies_ship(monkeypatch, drift_env):
    """Upgrade env vars are optional at module-load time (won't fail
    boot of the coordinator) — instead, `load_workload("upgrade")` must
    raise a clear error until 17.B/17.C/17.E ship the tools and env
    vars it depends on.

    Today the failure surfaces at the tool-resolution step because
    upgrade tools are reserved-but-not-implemented (None in
    TOOL_REGISTRY). When those land in 17.C the failure will move to
    the worker-resolution step (MissingWorkerEnvError for
    UPGRADE_READER_URL) until 17.E sets those env vars. Either way the
    coordinator must NEVER silently boot a half-wired upgrade
    workload."""
    for var in ("UPGRADE_READER_URL", "UPGRADE_DOCS_URL"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises((UnknownToolError, MissingWorkerEnvError)):
        load_workload("upgrade")


def test_missing_upgrade_worker_env_raises_when_resolved_directly(monkeypatch):
    """Direct path: the worker resolver itself must raise on missing
    UPGRADE_* env vars. This pins the failure mode that will surface
    out of `load_workload("upgrade")` once 17.C ships the upgrade tool
    callables and tool resolution no longer fails first."""
    for var in ("UPGRADE_READER_URL", "UPGRADE_DOCS_URL"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(MissingWorkerEnvError, match="UPGRADE_READER_URL"):
        registry_mod._resolve_worker("upgrade_reader")
