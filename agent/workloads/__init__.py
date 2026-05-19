"""Workload manifests + coordinator-side registry (Phase 17.A).

The coordinator is workload-aware: each incoming request names a workload
(e.g. ``drift`` or ``upgrade``), the coordinator loads that workload's
:class:`WorkloadSpec` from disk, and uses the spec to pick the right tool
subset, system prompt, and worker endpoints.

The split between the two modules in this package mirrors the security
boundary called out in the Phase 17 plan:

- :mod:`agent.workloads.spec` — the *manifest* schema. YAML carries
  symbolic names only (tool names, worker names, action names). It
  contains no URLs, secrets, repos, or audiences.
- :mod:`agent.workloads.registry` — the *authority*. Symbolic names
  are resolved to real callables and :class:`WorkerEndpoint` objects
  from a code-side allowlist. Flipping a YAML value cannot widen the
  agent's capabilities; it can only choose from this allowlist.

This is a Codex-flagged design property — see the Phase 17 plan header
for the threat model.
"""
# Re-export the workload ContextVar helpers from the package-root
# :mod:`agent.workload_context` module (NOT a submodule here — see
# that module's docstring for the circular-import rationale that
# forced the unusual location). The re-export keeps the public API
# surface intuitive: callers import ``set_workload`` etc. from
# ``agent.workloads``, the obvious home, without knowing about the
# physical-location workaround.
from agent.workload_context import current_workload, reset_workload, set_workload
from agent.workloads.spec import WorkloadSpec
from agent.workloads.registry import (
    UPGRADE_TARGET_REGISTRY,
    ActionSpec,
    MissingWorkerEnvError,
    ReservedToolNotImplementedError,
    UnknownActionError,
    UnknownToolError,
    UnknownUpgradeTargetError,
    UnknownWorkerError,
    UnknownWorkloadError,
    UpgradeTarget,
    WorkerEndpoint,
    WorkerSpec,
    WorkloadManifestMismatchError,
    WorkloadPathTraversalError,
    WorkloadResolution,
    load_workload,
    resolve_upgrade_target,
)

__all__ = [
    "ActionSpec",
    "MissingWorkerEnvError",
    "ReservedToolNotImplementedError",
    "UPGRADE_TARGET_REGISTRY",
    "UnknownActionError",
    "UnknownToolError",
    "UnknownUpgradeTargetError",
    "UnknownWorkerError",
    "UnknownWorkloadError",
    "UpgradeTarget",
    "WorkerEndpoint",
    "WorkerSpec",
    "WorkloadManifestMismatchError",
    "WorkloadPathTraversalError",
    "WorkloadResolution",
    "WorkloadSpec",
    "current_workload",
    "load_workload",
    "resolve_upgrade_target",
    "reset_workload",
    "set_workload",
]
