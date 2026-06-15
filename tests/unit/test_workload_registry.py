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
    WorkloadManifestMismatchError,
    WorkloadPathTraversalError,
    WorkloadResolution,
    load_workload,
    workload_contract_path,
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
    """Coordinator session-memory tools are reserved by name but not
    yet implemented (17.B.5 wires session memory). They appear as None
    so the registry can detect 'reserved but not yet ready' separately
    from 'unknown name entirely'.

    Wiring history for the names this test used to cover:

    - ``search_developer_docs`` / ``retrieve_developer_doc`` were
      reserved in 17.A.1 and got real callables in 17.B.2 — exercised
      by :mod:`tests.unit.test_mcp_developer_knowledge`'s registry-
      resolution check.
    - ``upgrade_read_dependencies`` / ``upgrade_propose_pr`` were
      reserved in 17.A.1 and got real callables in 17.C.4 — exercised
      by :mod:`tests.unit.test_upgrade_tools` and the resolution path
      in :mod:`tests.unit.test_upgrade_workload_loads`.
    """
    for name in ("get_session_state", "set_session_state"):
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
    """A YAML referencing a still-reserved tool (e.g. coordinator
    session-memory tools, slated for 17.B's coordinator-memory work)
    must fail with a clear error, not a NoneType crash when the LLM
    calls it.

    Phase 17.C.4 updated the example tool from
    ``upgrade_read_dependencies`` (now a real callable post-17.C.4) to
    ``get_session_state`` (still reserved as ``None`` in
    ``TOOL_REGISTRY`` for the planned session-memory feature). The
    invariant under test is the same — referencing a name whose
    registry entry is ``None`` must surface as
    :class:`ReservedToolNotImplementedError` at load time.
    """
    bad = _MINIMAL_DRIFT_YAML.replace(
        "  - drift_read_live_env",
        "  - drift_read_live_env\n  - get_session_state",
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
    raise a clear error until 17.E ships the worker URL env vars it
    depends on.

    Pre-17.C.4 the failure surfaced at the tool-resolution step
    (upgrade_* tools were reserved as ``None``). 17.C.4 flipped both
    upgrade tools to real callables, so the failure now moves to the
    worker-resolution step:
    :class:`MissingWorkerEnvError` for ``UPGRADE_READER_URL`` until
    17.E sets it. The coordinator must NEVER silently boot a half-wired
    upgrade workload — pin the predictable failure mode either way
    (tuple match for forward-compat with the move).
    """
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


# --------------------------------------------------------------------------- #
# Phase 17.A Codex review — Fix Important #2a: WorkloadResolution
# tools/workers/actions are MappingProxyType (immutable views).
# --------------------------------------------------------------------------- #


def test_workload_resolution_maps_are_immutable_mapping_proxies(drift_env):
    """``WorkloadResolution.tools/workers/actions`` are
    :class:`types.MappingProxyType` views so a caller that grabs a
    reference can't widen the workload's authority by in-place mutation.

    ``dataclass(frozen=True)`` only blocks reassigning the *field* (e.g.
    ``resolution.tools = {}`` raises); it does NOT block mutating the
    underlying dict (``resolution.tools["x"] = ...`` would succeed for a
    plain ``dict``). MappingProxyType blocks the mutation too, which is
    what we actually want for a security allowlist. Same property pin as
    the top-level :data:`TOOL_REGISTRY` / :data:`WORKER_REGISTRY` /
    :data:`ACTION_REGISTRY` allowlists (see
    test_registries_are_immutable_mapping_proxies above).

    One assertion per field — matches the registry test pattern.
    """
    resolution = load_workload("drift")
    with pytest.raises(TypeError):
        resolution.tools["attacker_tool"] = lambda: None  # type: ignore[index]
    with pytest.raises(TypeError):
        resolution.workers["attacker_worker"] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        resolution.actions["attacker_action"] = None  # type: ignore[index]


# --------------------------------------------------------------------------- #
# Phase 17.A Codex review — Fix Important #2b: manifest-name verification.
# --------------------------------------------------------------------------- #


def test_load_workload_rejects_manifest_with_mismatched_name(tmp_path, drift_env):
    """``load_workload(name)`` must assert the parsed
    :class:`WorkloadSpec.name` matches the requested ``name``. If an
    operator typos the YAML's ``name:`` field, every other registry
    lookup would silently route against the wrong manifest. We fail
    loud at load time instead — see :class:`WorkloadManifestMismatchError`.

    The fixture writes a YAML that declares ``name: drift`` under an
    ``upgrade/`` directory; loading it as ``upgrade`` (via the
    ``_load_from_path`` backend, which is what ``load_workload`` calls
    after path resolution) must raise the mismatch error.
    """
    # The shared `_write_workload` helper uses "drift" as the dir name;
    # we need a non-drift dir so the requested name differs from the
    # YAML's declared name.
    workload_dir = tmp_path / "weird_dir"
    workload_dir.mkdir()
    (workload_dir / "workload.yaml").write_text(_MINIMAL_DRIFT_YAML)
    (workload_dir / "system_prompt.txt").write_text("test prompt")

    with pytest.raises(WorkloadManifestMismatchError, match="drift"):
        registry_mod._load_from_path(
            workload_dir / "workload.yaml", expected_name="upgrade"
        )


def test_load_from_path_without_expected_name_skips_check(tmp_path, drift_env):
    """``_load_from_path`` without ``expected_name`` (the test-only
    call shape) must not raise the mismatch error — the verification
    is opt-in for tests that exercise other branches. The public
    :func:`load_workload` always passes ``expected_name``."""
    workload_dir = tmp_path / "weird_dir"
    workload_dir.mkdir()
    (workload_dir / "workload.yaml").write_text(_MINIMAL_DRIFT_YAML)
    (workload_dir / "system_prompt.txt").write_text("test prompt")

    # Should succeed without raising.
    resolution = registry_mod._load_from_path(workload_dir / "workload.yaml")
    assert resolution.spec.name == "drift"


# --------------------------------------------------------------------------- #
# Phase 17.A Codex review — Fix Important #2c: path-traversal guard.
# --------------------------------------------------------------------------- #


def test_load_workload_rejects_path_traversal_in_name():
    """``load_workload(name)`` must reject a ``name`` arg that resolves
    to a path outside the ``workloads/`` root.

    Today the pydantic ``Literal`` on request bodies protects callers
    that come through ``/chat`` and ``/recheck``, but
    :func:`load_workload` itself takes a bare ``str`` — defense in
    depth so a future caller that forwards an unvalidated request body
    field can't escape the workloads dir with ``name="../etc/passwd"``.

    The guard raises :class:`WorkloadPathTraversalError` (subclass of
    ``ValueError``) instead of ``FileNotFoundError`` — the latter would
    leak the attempted path in its message, defeating the purpose.
    """
    with pytest.raises(WorkloadPathTraversalError):
        load_workload("../etc/passwd")


def test_load_workload_path_traversal_error_subclasses_value_error():
    """``WorkloadPathTraversalError`` is a ``ValueError`` subclass so
    callers using value-shaped catches pick it up with the same idiom.
    Pinning the subclass relationship here so a future refactor that
    swaps the base class doesn't silently break callers that catch
    ``ValueError``."""
    assert issubclass(WorkloadPathTraversalError, ValueError)


# --------------------------------------------------------------------------- #
# workload_contract_path: worker-free contract resolution (Codex 2026-05-25).
# Public API used by agent.adk_tools._get_upgrade_target to derive the
# upgrade target WITHOUT resolving the upgrade workload's mutation workers.
# These pin parity with load_workload's name guards plus the contract-path /
# no-contract returns.
# --------------------------------------------------------------------------- #


def test_workload_contract_path_returns_upgrade_contract_without_worker_env():
    """``workload_contract_path("upgrade")`` resolves the real upgrade
    contract path with NO worker URL env set — the whole point of the
    helper (it must not resolve upgrade_docs / notifier)."""
    path = workload_contract_path("upgrade")
    assert path is not None
    assert path.is_absolute()
    assert path.name == "contract.yaml"
    assert path.parent.name == "upgrade"


def test_workload_contract_path_returns_none_for_explore():
    """Explore declares no contract_file, so the helper returns None."""
    assert workload_contract_path("explore") is None


def test_workload_contract_path_unknown_name_raises():
    """Parity with load_workload: unknown workload → UnknownWorkloadError."""
    with pytest.raises(UnknownWorkloadError, match="kubernetes"):
        workload_contract_path("kubernetes")


def test_workload_contract_path_rejects_path_traversal():
    """Parity with load_workload: the same traversal guard applies."""
    with pytest.raises(WorkloadPathTraversalError):
        workload_contract_path("../etc/passwd")
