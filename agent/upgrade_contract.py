"""``UpgradeContract`` — upgrade workload decision-rules schema (Phase 17.C.1).

This is the upgrade workload's *decision* layer, analogous to
:class:`agent.contract.OpsContract` for drift. The schema is
deliberately narrow: it carries **decision thresholds only** (severity,
version-jump policy, requires-approval flags). It carries **no
authority fields** — no ``target_repo``, no ``lockfile_path``, no
``advisory_source``. Those live in
:data:`agent.workloads.registry.UPGRADE_TARGET_REGISTRY` and are
referenced by symbolic ``target_name`` only.

Codex 2026-05-20 blocker: an earlier draft of this contract put the
three authority fields in YAML. That contradicts Phase 17.A.1's
symbolic-names-only rule — a YAML flip would redirect the agent at a
different repository. The split below pins the authority in code while
keeping the operator-tunable decision rules in YAML where they belong.

Sibling-loader design (vs extending :class:`WorkloadResolution`):

- Drift's :class:`agent.contract.OpsContract` is loaded by a sibling
  :func:`agent.contract.load_contract` rather than via the workload
  registry. We follow the same pattern for upgrade — the contract is a
  decision-layer artifact, not a routing-layer one, and keeping it out
  of :class:`WorkloadResolution` avoids burdening every workload with
  a contract-shaped field. Touches the least existing code; the
  registry stays focused on tool/worker/action authority.
- The loader resolves ``target_name`` against
  :data:`UPGRADE_TARGET_REGISTRY` at *load* time, raising
  :class:`UnknownUpgradeTargetError` for unknown names. The pydantic
  ``Literal`` on the field gives an earlier, more precise failure for
  typos; the registry resolution is what actually grants the workload
  its target repo / lockfile / advisory source, and pinning that
  check to coordinator boot is the property Codex's 2026-05-20 review
  asked for ("Loader fails boot on unknown target_name").

Schema:

.. code-block:: yaml

    target_name: phase17_demo                      # symbolic; UPGRADE_TARGET_REGISTRY key
    decisions:
      no_op:      { severity_max: low }
      docs_pr:    { severity_min: medium, version_jump: [patch, minor] }
      upgrade_pr: { severity_min: medium, version_jump: [patch, minor], requires_approval: false }
      escalation: { version_jump: [major] }

``extra="forbid"`` on every model so a sneaky ``target_repo:`` (or any
other authority field) in the YAML fails loudly rather than being
silently ignored.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

from agent.workloads.registry import UpgradeTarget, resolve_upgrade_target


# Severity vocabulary mirrors the GitHub Advisory severity enum so the
# upgrade-reader worker can pass through advisory severity strings
# unchanged. Constrained as a ``Literal`` so a YAML typo (``"medum"``)
# fails at pydantic validation, not at decision-time.
SeverityLevel = Literal["low", "medium", "high", "critical"]

# Semver version-jump axes. Phase 17 scopes the upgrade-docs worker to
# patch + minor bumps (major bumps must route to ``escalation``).
VersionJump = Literal["patch", "minor", "major"]


class UpgradeDecisionRule(BaseModel):
    """A single decision rule — the conditions under which the agent
    should pick the named decision.

    Field semantics:

    - ``severity_max``: the rule fires only when the matched advisory's
      severity is at or below this level. Used by ``no_op`` to gate
      "low-severity vulns are noise" without bumping the version.
    - ``severity_min``: the rule fires only when severity is at or
      above this level. Used by ``docs_pr`` / ``upgrade_pr`` /
      ``escalation`` to filter low-severity noise from upgrade paths.
    - ``version_jump``: which semver jump axes this rule allows.
      ``upgrade_pr`` permits ``[patch, minor]`` only; ``escalation``
      takes anything with ``major`` (caller should escalate to a human
      rather than auto-bump a major version).
    - ``requires_approval``: whether the decision must gate through the
      HITL approval store before execution. Defaults to ``False`` —
      upgrade PRs are reversible (close-PR), but the field is present
      so a future ``rollback``-shaped decision can re-use the shape.

    All fields default to safe-no-op values: an empty rule matches
    nothing (severity bounds default to None, version_jump defaults to
    empty list). ``extra="forbid"`` prevents stray fields.
    """

    model_config = ConfigDict(extra="forbid")

    severity_max: SeverityLevel | None = None
    severity_min: SeverityLevel | None = None
    version_jump: list[VersionJump] = []
    requires_approval: bool = False


class UpgradeContract(BaseModel):
    """The parsed shape of ``workloads/upgrade/contract.yaml``.

    Attributes:
        target_name: Symbolic name of the upgrade target. Resolves to a
            :class:`UpgradeTarget` via
            :data:`agent.workloads.registry.UPGRADE_TARGET_REGISTRY`.
            Constrained to a ``Literal`` so a YAML typo fails at
            pydantic validation; the runtime resolver
            (:func:`resolve_upgrade_target`) is defense in depth.
        decisions: Mapping of decision-action name → rule. The keys
            mirror :class:`agent.workloads.registry.ActionSpec` names
            the upgrade workload exposes (``no_op``, ``docs_pr``,
            ``upgrade_pr``, ``escalation``). Not constrained to a
            ``Literal`` here because the action vocabulary evolves with
            workload features; the registry-level check happens when
            the workload boots. A cross-check test in
            ``tests/unit/test_upgrade_contract.py`` pins that the
            bundled contract's decision keys are a subset of the
            upgrade workload's declared ``action_names``.
    """

    # extra="forbid" so a sneaky ``target_repo`` (the field this whole
    # design avoids putting in YAML) can't slip in silently. Same
    # property as WorkloadSpec.
    model_config = ConfigDict(extra="forbid")

    # Today only "phase17_demo" — expand the Literal when new entries
    # land in UPGRADE_TARGET_REGISTRY. Keeping the Literal in sync with
    # the registry is a deliberate code change so an operator can't
    # widen the surface by editing only YAML.
    target_name: Literal["phase17_demo"]
    decisions: dict[str, UpgradeDecisionRule]

    def resolve_target(self) -> UpgradeTarget:
        """Resolve the contract's symbolic target name to its
        authority record. Convenience for callers that hold a parsed
        contract — same as
        :func:`agent.workloads.registry.resolve_upgrade_target` with
        the contract's own ``target_name``.
        """
        return resolve_upgrade_target(self.target_name)


def load_upgrade_contract(path: Path | str) -> UpgradeContract:
    """Load and validate an upgrade contract YAML.

    Mirrors :func:`agent.contract.load_contract`'s exception shape so
    callers don't need to special-case which contract they're loading:
    missing file → ``FileNotFoundError``; malformed YAML → ``ValueError``
    with the path in the message; schema violation → pydantic
    ``ValidationError``.

    Boot-time authority resolution (Phase 17.C.1 — Codex 2026-05-20
    follow-up): the loader actively resolves ``target_name`` against
    :data:`UPGRADE_TARGET_REGISTRY` before returning. If the registry
    has no entry for the parsed name, :class:`UnknownUpgradeTargetError`
    surfaces at *load* time, never at first agent call. The pydantic
    ``Literal`` already catches typos at parse time, but the registry
    resolution is what actually grants the workload its target repo /
    lockfile / advisory source — running it here pins the failure to
    coordinator boot. The resolved record is discarded by this function
    (callers re-invoke :meth:`UpgradeContract.resolve_target` when
    they need the record); the call is for its side effect of failing
    early.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise FileNotFoundError(f"upgrade contract not found: {path}") from e
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"failed to parse upgrade contract {path}: {e}") from e
    contract = UpgradeContract.model_validate(raw)
    # Boot-time authority check. Surfaces UnknownUpgradeTargetError now
    # instead of at first agent call. Mirrors how `load_workload()`
    # resolves tool / worker / action names at load time rather than
    # deferring to runtime.
    contract.resolve_target()
    return contract
