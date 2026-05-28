"""Unit tests for tools.iac_plan_denylist (Phase C1 — design doc §5.2).

Table-driven over hand-authored plan-JSON fixtures under
``tests/fixtures/iac_plan_denylist/``. Each single-rule fixture exercises
exactly one rule; multi-rule aggregation has dedicated fixtures.
"""
import dataclasses
from pathlib import Path

import pytest

from tools import iac_plan_denylist  # noqa: F401
from tools.iac_plan_denylist import (
    DenylistInput,
    Violation,
    evaluate,
    load_plan_json,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "iac_plan_denylist"


def _load(name: str) -> str:
    """Read a fixture file as text. Caller decides whether to json-parse."""
    return (FIXTURES / name).read_text(encoding="utf-8")


def _rules(violations: list[Violation]) -> list[str]:
    return [v.rule for v in violations]


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


# --- Task 3: load_plan_json + structural rules ---


@pytest.mark.parametrize("fixture", [
    "unparseable_empty_file.json",
    "unparseable_not_object.json",
])
def test_load_plan_json_handles_unparseable(fixture):
    parsed, violation = load_plan_json(_load(fixture))
    assert parsed is None
    assert violation is not None
    assert violation.rule == "plan-json-unparseable"


def test_load_plan_json_happy_path_returns_dict_and_no_violation():
    parsed, violation = load_plan_json('{"format_version": "1.2", "resource_changes": []}')
    assert parsed == {"format_version": "1.2", "resource_changes": []}
    assert violation is None


@pytest.mark.parametrize("fixture", [
    "missing_resource_changes.json",
    "resource_changes_not_list.json",
])
def test_missing_or_non_list_resource_changes_is_denied(fixture):
    parsed, _ = load_plan_json(_load(fixture))
    assert parsed is not None
    assert "plan-json-missing-resource-changes" in _rules(evaluate(DenylistInput(plan=parsed)))


@pytest.mark.parametrize("fixture", [
    "resource_changes_entry_not_dict.json",
    "change_not_dict.json",
])
def test_entry_or_change_not_dict_is_malformed(fixture):
    parsed, _ = load_plan_json(_load(fixture))
    assert parsed is not None
    assert "plan-json-malformed-change" in _rules(evaluate(DenylistInput(plan=parsed)))
