"""`WorkloadSpec` — the parsed shape of `workloads/<name>/workload.yaml`.

Symbolic names only. Real URLs/secrets/repos/audiences are resolved at
load time by :mod:`agent.workloads.registry` against a code-side
allowlist. The schema is intentionally strict (``extra="forbid"``) so a
typo or sneaky extra field in YAML cannot silently extend the surface.

See the Phase 17 plan §17.A.1 for the full security rationale.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class WorkloadSpec(BaseModel):
    """Manifest describing one workload (e.g. ``drift`` or ``upgrade``).

    Attributes:
        name: workload identifier. Constrained to the closed set
            ``{"drift", "upgrade"}`` so a YAML typo (``"drft"``) is
            caught by pydantic, not by a later runtime branch.
        display_name: short human-readable label (operator-facing).
        description: one-paragraph description of what this workload
            detects and acts on. Surfaces in operator UI / docs.
        system_prompt_file: path *relative to this workload's
            directory* — e.g. ``system_prompt.txt`` resolves to
            ``workloads/<name>/system_prompt.txt``. Resolved by the
            registry, not by the schema, so the schema stays a pure
            data-shape check.
        contract_file: optional path to the workload's declarative
            contract (e.g. drift's ``ops-contract.yaml``). May be
            ``None`` for workloads whose ground truth comes from a
            different source (upgrade reads ``package.json``).
        enabled_tool_names: list of *symbolic* tool names. Each must
            be a key in :data:`agent.workloads.registry.TOOL_REGISTRY`,
            which the registry's loader enforces.
        worker_names: list of *symbolic* worker names. Each must be a
            key in :data:`agent.workloads.registry.WORKER_REGISTRY`.
        observation_kind: the shape of input data this workload
            ingests. Constrained to a closed set so adding a new
            observation type requires an explicit schema change.
        action_names: list of *symbolic* action names. Each must be a
            key in :data:`agent.workloads.registry.ACTION_REGISTRY`.
            Used to populate operator-facing pickers and to gate which
            decisions the validator will accept for this workload.
    """

    # extra="forbid": a stray field in YAML must fail loudly. The whole
    # point of the manifest schema is to keep the surface narrow.
    model_config = ConfigDict(extra="forbid")

    name: Literal["drift", "upgrade"]
    display_name: str
    description: str
    system_prompt_file: str
    contract_file: str | None = None
    enabled_tool_names: list[str]
    worker_names: list[str]
    observation_kind: Literal["cloud_run_env", "repo_lockfile"]
    action_names: list[str]
