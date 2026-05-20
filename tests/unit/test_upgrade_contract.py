"""Tests for ``UpgradeContract`` — the upgrade workload's decision-rules
YAML schema (Phase 17.C.1).

This is the *decision* layer for the upgrade workload, analogous to
``OpsContract`` for drift. **Authority is intentionally absent**: per
Codex 2026-05-20 review, the contract YAML carries NO ``target_repo``,
``lockfile_path``, or ``advisory_source`` — those live in
``UPGRADE_TARGET_REGISTRY`` (see ``test_upgrade_target_registry.py``).
The contract references the target by symbolic name only.

Property pins:

1. The bundled ``workloads/upgrade/contract.yaml`` parses cleanly with
   the live ``UpgradeTarget`` resolution (target_name → registry).
2. Missing required fields raise pydantic ``ValidationError``.
3. ``target_name`` is a ``Literal["phase17_demo"]`` — unknown values
   rejected at pydantic validation (defense in depth alongside the
   registry's runtime ``UnknownUpgradeTargetError``).
4. Decision severity enums are constrained to
   ``{low, medium, high, critical}``.
5. Decision ``version_jump`` values are constrained to
   ``{patch, minor, major}``.
6. ``extra="forbid"`` on the schema — typos or sneaky extra YAML
   fields fail loudly (same property as ``WorkloadSpec``).
7. **The on-disk contract has no authority fields** (no
   ``target_repo`` / ``lockfile_path`` / ``advisory_source`` strings).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from agent.upgrade_contract import (
    KNOWN_ACTION_KEYS,
    UpgradeContract,
    UpgradeDecisionRule,
    load_upgrade_contract,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLED_CONTRACT = REPO_ROOT / "workloads" / "upgrade" / "contract.yaml"


VALID_YAML = """\
target_name: phase17_demo
decisions:
  no_op:
    severity_max: low
  docs_pr:
    severity_min: medium
    version_jump: [patch, minor]
  upgrade_pr:
    severity_min: medium
    version_jump: [patch, minor]
    requires_approval: false
  escalation:
    version_jump: [major]
"""


def test_bundled_contract_yaml_loads(tmp_path):
    """The contract YAML shipped under ``workloads/upgrade/`` must parse
    against the live schema. Pins the on-disk file to the same shape
    the loader expects."""
    contract = load_upgrade_contract(BUNDLED_CONTRACT)
    assert isinstance(contract, UpgradeContract)
    assert contract.target_name == "phase17_demo"
    # All four canonical decisions present. Names match
    # ACTION_REGISTRY (note: ``escalation``, not ``escalate`` — the
    # plan's draft used the verb form, but the existing action
    # vocabulary uses the noun form, and the contract aligns with
    # ACTION_REGISTRY for the validator wiring).
    for name in ("no_op", "docs_pr", "upgrade_pr", "escalation"):
        assert name in contract.decisions


def test_bundled_contract_yaml_contains_no_authority_fields():
    """Codex 2026-05-20 blocker — the YAML must NOT carry
    ``target_repo``, ``lockfile_path``, or ``advisory_source``. Those
    are authority fields that live in code (UPGRADE_TARGET_REGISTRY).
    A regression that re-introduces them in YAML would let a manifest
    flip redirect the agent at a different repo. Pin the absence at
    the raw-text level so a refactor that loosens ``extra="forbid"``
    can't silently mask the leak.
    """
    raw = yaml.safe_load(BUNDLED_CONTRACT.read_text())
    assert "target_repo" not in raw, (
        "contract.yaml must not carry target_repo — authority lives in "
        "UPGRADE_TARGET_REGISTRY (Phase 17.C.1 Codex blocker)"
    )
    assert "lockfile_path" not in raw, (
        "contract.yaml must not carry lockfile_path — authority lives in "
        "UPGRADE_TARGET_REGISTRY (Phase 17.C.1 Codex blocker)"
    )
    assert "advisory_source" not in raw, (
        "contract.yaml must not carry advisory_source — authority lives "
        "in UPGRADE_TARGET_REGISTRY (Phase 17.C.1 Codex blocker)"
    )


def test_valid_yaml_parses(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(VALID_YAML)
    contract = load_upgrade_contract(p)
    assert contract.target_name == "phase17_demo"
    assert contract.decisions["no_op"].severity_max == "low"
    assert contract.decisions["docs_pr"].severity_min == "medium"
    assert contract.decisions["docs_pr"].version_jump == ["patch", "minor"]
    assert contract.decisions["upgrade_pr"].requires_approval is False
    assert contract.decisions["escalation"].version_jump == ["major"]


def test_missing_target_name_raises(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("decisions: {}\n")
    with pytest.raises(ValidationError):
        load_upgrade_contract(p)


def test_missing_decisions_raises(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("target_name: phase17_demo\n")
    with pytest.raises(ValidationError):
        load_upgrade_contract(p)


def test_unknown_target_name_rejected_by_pydantic(tmp_path):
    """Defense in depth: the registry's runtime resolver would catch
    an unknown name, but the pydantic ``Literal`` catches typos at
    parse time so the operator sees a precise field error."""
    p = tmp_path / "c.yaml"
    p.write_text("target_name: attacker_repo\ndecisions:\n  no_op:\n    severity_max: low\n")
    with pytest.raises(ValidationError, match="target_name"):
        load_upgrade_contract(p)


def test_invalid_severity_value_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "target_name: phase17_demo\n"
        "decisions:\n"
        "  no_op:\n"
        "    severity_max: catastrophic\n"
    )
    with pytest.raises(ValidationError):
        load_upgrade_contract(p)


def test_invalid_version_jump_value_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "target_name: phase17_demo\n"
        "decisions:\n"
        "  upgrade_pr:\n"
        "    version_jump: [hyperpatch]\n"
    )
    with pytest.raises(ValidationError):
        load_upgrade_contract(p)


def test_extra_top_level_field_rejected(tmp_path):
    """``extra="forbid"`` on the contract schema: a stray field in the
    YAML must fail loudly (same property as ``WorkloadSpec``). This
    is what prevents a future ``target_repo`` from sneaking back in."""
    p = tmp_path / "c.yaml"
    p.write_text(
        "target_name: phase17_demo\n"
        "target_repo: attacker/repo\n"
        "decisions:\n"
        "  no_op:\n"
        "    severity_max: low\n"
    )
    with pytest.raises(ValidationError):
        load_upgrade_contract(p)


def test_extra_field_in_decision_rule_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "target_name: phase17_demo\n"
        "decisions:\n"
        "  no_op:\n"
        "    severity_max: low\n"
        "    surprise_field: hello\n"
    )
    with pytest.raises(ValidationError):
        load_upgrade_contract(p)


def test_load_upgrade_contract_wraps_missing_file(tmp_path):
    missing = tmp_path / "does-not-exist.yaml"
    with pytest.raises(FileNotFoundError, match="does-not-exist.yaml"):
        load_upgrade_contract(missing)


def test_load_upgrade_contract_wraps_yaml_parse_error(tmp_path):
    p = tmp_path / "broken.yaml"
    p.write_text("target_name: [unbalanced\n")
    with pytest.raises(ValueError, match="broken.yaml"):
        load_upgrade_contract(p)


def test_upgrade_decision_rule_allows_empty_version_jump(tmp_path):
    """``no_op`` uses ``severity_max`` only — no ``version_jump``. The
    schema must allow the field to be absent (defaults to empty)."""
    p = tmp_path / "c.yaml"
    p.write_text(
        "target_name: phase17_demo\n"
        "decisions:\n"
        "  no_op:\n"
        "    severity_max: low\n"
    )
    contract = load_upgrade_contract(p)
    rule = contract.decisions["no_op"]
    assert isinstance(rule, UpgradeDecisionRule)
    assert rule.version_jump == []
    assert rule.severity_min is None


def test_contract_resolve_target_returns_registry_record():
    """``UpgradeContract.resolve_target()`` returns the registry entry
    keyed by the contract's ``target_name``. Pins the convenience
    method's contract: it's a thin wrapper over
    :func:`resolve_upgrade_target`."""
    from agent.workloads.registry import UPGRADE_TARGET_REGISTRY

    contract = load_upgrade_contract(BUNDLED_CONTRACT)
    target = contract.resolve_target()
    assert target is UPGRADE_TARGET_REGISTRY[contract.target_name]


def test_load_upgrade_contract_resolves_target_at_load_time():
    """Boot-time authority pin (positive direction): the loader returns
    a parsed contract whose ``target_name`` survives the registry
    resolution that happens inside ``load_upgrade_contract``."""
    contract = load_upgrade_contract(BUNDLED_CONTRACT)
    assert contract.target_name in {"phase17_demo"}


def test_load_upgrade_contract_fails_when_registry_lacks_target(monkeypatch):
    """Boot-time authority pin — **negative direction** (Codex 2026-05-20
    review follow-up). The plan requires ``load_upgrade_contract`` to
    fail boot on unknown ``target_name``. The pydantic ``Literal``
    catches ordinary unknown values at parse time (those raise
    ``ValidationError``), but a Literal-valid name that's missing from
    the registry — e.g. a future operator extends the Literal but
    forgets to add the registry entry — must also fail at *load* time,
    not at first agent call.

    We simulate the mismatch by swapping the module-level
    ``UPGRADE_TARGET_REGISTRY`` to an empty proxy. The bundled
    contract's ``target_name`` is still ``"phase17_demo"`` (Literal-
    valid), but the registry has no entry, so ``resolve_target()``
    inside the loader must raise ``UnknownUpgradeTargetError``.
    """
    from types import MappingProxyType

    import agent.workloads.registry as registry_mod
    from agent.workloads.registry import UnknownUpgradeTargetError

    monkeypatch.setattr(
        registry_mod, "UPGRADE_TARGET_REGISTRY", MappingProxyType({})
    )
    with pytest.raises(UnknownUpgradeTargetError, match="phase17_demo"):
        load_upgrade_contract(BUNDLED_CONTRACT)


def test_bundled_contract_decisions_subset_of_action_registry():
    """The contract's decision keys must also be a subset of
    ``ACTION_REGISTRY`` (the global allowlist of decision-action names).

    Today ``load_workload("upgrade")`` fails earlier (on the reserved
    ``None`` upgrade tools), but once 17.C.2 / 17.C.3 ship those
    callables the workload's action-name resolution will run and reject
    any unknown name. This cross-check moves that failure forward to
    the contract-load step so a typo in ``contract.yaml`` surfaces
    here rather than as a coordinator-boot error after the upgrade
    tools land.
    """
    from agent.workloads.registry import ACTION_REGISTRY

    contract = load_upgrade_contract(BUNDLED_CONTRACT)
    registry_actions = set(ACTION_REGISTRY)
    contract_decisions = set(contract.decisions.keys())
    missing = contract_decisions - registry_actions
    assert not missing, (
        f"contract.yaml decisions {sorted(missing)} are not in "
        f"ACTION_REGISTRY {sorted(registry_actions)} — add the action "
        "to the registry first."
    )


def test_load_upgrade_contract_rejects_unknown_decision_key(tmp_path):
    """Task 17.C.3a (rule 6 — contract integrity). The loader must
    reject any decision key outside :data:`KNOWN_ACTION_KEYS` at load
    time, not at decision time. Previously the bundled contract was
    pinned only by the cross-check tests below; this hardens the same
    property in production by adding a pydantic ``model_validator`` to
    :class:`UpgradeContract` itself. A future hand-edit of
    ``contract.yaml`` that introduces a ``made_up_action`` key now
    fails immediately at :func:`load_upgrade_contract`.
    """
    p = tmp_path / "c.yaml"
    p.write_text(
        "target_name: phase17_demo\n"
        "decisions:\n"
        "  made_up_action: {}\n"
    )
    with pytest.raises(ValidationError, match="made_up_action"):
        load_upgrade_contract(p)


def test_known_action_keys_constant_is_subset_of_action_registry():
    """``KNOWN_ACTION_KEYS`` is the upgrade contract's own opinion of
    which decision actions are valid — a strict subset of
    ``ACTION_REGISTRY``. (Drift-only actions like ``drift_issue`` and
    ``rollback`` live in the global registry but are not valid
    upgrade decisions.)

    Deviation note vs the plan: the plan suggested asserting
    ``KNOWN_ACTION_KEYS == set(ACTION_REGISTRY)``, but those sets are
    intentionally different — ``ACTION_REGISTRY`` carries drift
    actions too. The correct invariant is **subset**: a key in
    ``KNOWN_ACTION_KEYS`` must always be a real action the registry
    knows about, so a typo in the contract module that referenced
    e.g. ``"escalate"`` (verb) instead of ``"escalation"`` (noun) is
    caught here.
    """
    from agent.workloads.registry import ACTION_REGISTRY

    assert KNOWN_ACTION_KEYS <= set(ACTION_REGISTRY), (
        f"KNOWN_ACTION_KEYS {sorted(KNOWN_ACTION_KEYS)} has entries "
        f"not in ACTION_REGISTRY {sorted(ACTION_REGISTRY)} — add the "
        "action to the registry or fix the constant."
    )


def test_known_action_keys_constant_matches_documented_set():
    """Pin the exact contents of ``KNOWN_ACTION_KEYS`` so a future
    accidental widening (e.g. adding ``"rollback"`` to the constant)
    is caught here. The documented allowlist is
    {no_op, docs_pr, upgrade_pr, escalation}.
    """
    assert KNOWN_ACTION_KEYS == frozenset(
        {"no_op", "docs_pr", "upgrade_pr", "escalation"}
    )


def test_bundled_contract_decisions_subset_of_workload_action_names():
    """The contract's decision keys must be a subset of the upgrade
    workload's declared ``action_names``. If a decision rule fires
    that names an action the workload hasn't enabled, the validator
    has no ``ActionSpec`` to gate against and the coordinator wiring
    would silently route to nothing.

    Codex 2026-05-20 follow-up: the first cut of this task had the
    contract use ``escalate`` (verb) while the workload + registry
    used ``escalation`` (noun), and ``docs_pr`` was absent from the
    workload's action list entirely. This cross-check pins the
    invariant so a future drift in either file surfaces here, not at
    decision-time.
    """
    import yaml as _yaml

    contract = load_upgrade_contract(BUNDLED_CONTRACT)
    workload_yaml = REPO_ROOT / "workloads" / "upgrade" / "workload.yaml"
    workload_raw = _yaml.safe_load(workload_yaml.read_text())
    workload_actions = set(workload_raw["action_names"])
    contract_decisions = set(contract.decisions.keys())
    missing = contract_decisions - workload_actions
    assert not missing, (
        f"contract.yaml decisions {sorted(missing)} are not in "
        f"workload.yaml action_names {sorted(workload_actions)} — "
        "either add them to the workload or remove them from the "
        "contract. See ACTION_REGISTRY for the canonical action vocabulary."
    )
