"""Unit tests for tools.iac_plan_denylist (Phase C1 — design doc §5.2).

Table-driven over hand-authored plan-JSON fixtures under
``tests/fixtures/iac_plan_denylist/``. Each single-rule fixture exercises
exactly one rule; multi-rule aggregation has dedicated fixtures.
"""
import dataclasses

import pytest

from tools import iac_plan_denylist  # noqa: F401
from tools.iac_plan_denylist import DenylistInput, Violation, evaluate


def test_module_imports():
    assert iac_plan_denylist is not None


def test_violation_is_frozen_dataclass():
    v = Violation(rule="x", detail="y")
    assert dataclasses.is_dataclass(v)
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.rule = "z"  # type: ignore[misc]


def test_empty_plan_with_empty_resource_changes_passes():
    di = DenylistInput(plan={"format_version": "1.2", "resource_changes": []})
    assert evaluate(di) == []
