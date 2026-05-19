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
from agent.workloads.spec import WorkloadSpec
from agent.workloads.registry import (
    ActionSpec,
    MissingWorkerEnvError,
    UnknownActionError,
    UnknownToolError,
    UnknownWorkerError,
    UnknownWorkloadError,
    WorkerEndpoint,
    WorkerSpec,
    WorkloadResolution,
    load_workload,
)

__all__ = [
    "ActionSpec",
    "MissingWorkerEnvError",
    "UnknownActionError",
    "UnknownToolError",
    "UnknownWorkerError",
    "UnknownWorkloadError",
    "WorkerEndpoint",
    "WorkerSpec",
    "WorkloadResolution",
    "WorkloadSpec",
    "load_workload",
]
