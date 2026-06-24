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
            ``{"drift", "upgrade", "explore", "provision"}`` so a YAML
            typo (``"drft"``) is caught by pydantic, not by a later
            runtime branch. ``"explore"`` is the chat-only, strictly
            read-only workload (no autonomous ``/recheck`` path; see the
            ``observation_kind="none"`` note below). ``"provision"`` is
            also chat-only (Phase D) — it authors OpenTofu (IaC) edits
            and opens ONE iac/-only PR for the gated apply pipeline; it
            never touches live infra directly, so it likewise has no
            ``/recheck`` path.
        display_name: short human-readable label (operator-facing).
            Phase 17.G renamed these from domain labels to a "crew" of
            named agent identities (``drift`` → ``Anchor``, ``upgrade``
            → ``Patch``, plus ``Provision`` / ``Explore``). The symbolic
            ``name`` above is FROZEN; only this display identity changes.
        descriptor: the one-or-two-word domain subtitle shown under the
            identity (e.g. ``Anchor`` + ``Cloud Run config``). Operator
            UI renders ``display_name`` as the bold identity and this as
            the gray descriptor; docs lead with the identity and gloss
            the domain. Required — a workload with no descriptor is a
            manifest bug.
        description: one-paragraph description of what this workload
            detects and acts on. Surfaces in operator UI / docs.
        system_prompt_file: path *relative to this workload's
            directory* — e.g. ``system_prompt.txt`` resolves to
            ``workloads/<name>/system_prompt.txt``. Resolved by the
            registry, not by the schema, so the schema stays a pure
            data-shape check.
        chat_system_prompt_file: optional path *relative to this
            workload's directory* for the ``/chat`` (free-form
            operator interface) system prompt. Phase 17.C.4 (Option A
            from the plan) — distinct from ``system_prompt_file``
            because the two surfaces want different wording: the
            ``/recheck`` prompt instructs the LLM to emit a structured
            DecisionProposal JSON, while the ``/chat`` prompt
            describes the workload's tool surface in operator-facing
            terms. ``None`` (the default) tells the registry to fall
            back to ``system_prompt`` for ``/chat`` — workloads that
            want the same prompt for both surfaces don't need to
            duplicate the file.
        contract_file: optional path to the workload's declarative
            contract (e.g. drift's ``ops-contract.yaml``). May be
            ``None`` for workloads whose ground truth comes from a
            different source (upgrade reads ``package.json``).
        enabled_tool_names: list of *symbolic* tool names. Each must
            be a key in :data:`agent.workloads.registry.TOOL_REGISTRY`,
            which the registry's loader enforces.
        worker_names: list of *symbolic* worker names. Each must be a
            key in :data:`agent.workloads.registry.WORKER_REGISTRY`.
        observation_kind: the shape/type of input data this workload's
            decision logic is *designed* to ingest (intent), NOT proof of
            a wired autonomous trigger. Constrained to a closed set so
            adding a new observation type requires an explicit schema
            change. ``"none"`` marks a chat-only workload (``explore`` /
            ``provision``) with no observation source — ``/recheck`` is
            route-refused for it. IMPORTANT: a non-``"none"`` value does
            NOT mean the workload runs on its own — ``upgrade`` declares
            ``repo_lockfile`` but its ``/recheck`` returns 503 and no
            trigger is bound to it. The single source of truth for "runs
            autonomously" is ``AUTONOMOUS_TRIGGER_WORKLOADS`` in
            ``agent.main`` (only ``drift`` today); the operator-facing
            "Autonomous" signal is derived from that set, never from this
            field.
        action_names: list of *symbolic* action names. Each must be a
            key in :data:`agent.workloads.registry.ACTION_REGISTRY`.
            Used to populate operator-facing pickers and to gate which
            decisions the validator will accept for this workload.
    """

    # extra="forbid": a stray field in YAML must fail loudly. The whole
    # point of the manifest schema is to keep the surface narrow.
    model_config = ConfigDict(extra="forbid")

    name: Literal["drift", "upgrade", "provision", "explore"]
    display_name: str
    descriptor: str
    description: str
    system_prompt_file: str
    chat_system_prompt_file: str | None = None
    contract_file: str | None = None
    enabled_tool_names: list[str]
    worker_names: list[str]
    observation_kind: Literal["cloud_run_env", "repo_lockfile", "none"]
    action_names: list[str]
