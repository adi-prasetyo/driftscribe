"""Unit tests for the pure D5 fan-out decomposition/validation core.

Phase D5-1: the offline foundation for parallel sub-agent fan-out. These
tests pin :class:`agent.fanout.SliceSpec`, the typed
:class:`agent.fanout.FanoutError` (with its branch-able
:class:`agent.fanout.FanoutFailureKind`), and
:func:`agent.fanout.validate_slice_specs`.

The validation rules are deliberately fail-closed and mirror the file-level
:mod:`driftscribe_lib.iac_editor_policy` allowlist (``iac/``-only, ``.tf``/
``.md`` suffix, foundation guard, traversal), re-raising the library's
:class:`EditorPolicyError` as the fan-out module's own ``FanoutError`` so the
library error never leaks out. A POLICY-kind failure is how the later
orchestrator slice decides fail-closed; that ``kind`` (not the HTTP status) is
the branch point, so these tests assert on ``kind`` explicitly.
"""
from __future__ import annotations

import pytest

from agent.fanout import (
    MAX_SLICES,
    FanoutError,
    FanoutFailureKind,
    SliceSpec,
    validate_slice_specs,
)
from driftscribe_lib.iac_editor_policy import (
    EditorPolicyError,
    validate_iac_path,
    _validate_one_path,
)


def _spec(target_path: str, goal: str = "author a thing") -> SliceSpec:
    return SliceSpec(goal=goal, target_path=target_path)


# validate_slice_specs — count bounds ------------------------------------ #


def test_empty_list_rejected_policy() -> None:
    with pytest.raises(FanoutError) as e:
        validate_slice_specs([])
    assert e.value.status == 422
    assert e.value.kind is FanoutFailureKind.POLICY


def test_too_many_slices_rejected() -> None:
    specs = [_spec(f"iac/f{i}.tf") for i in range(MAX_SLICES + 1)]
    assert len(specs) == 9  # > MAX_SLICES (8)
    with pytest.raises(FanoutError) as e:
        validate_slice_specs(specs)
    assert e.value.kind is FanoutFailureKind.POLICY


# validate_slice_specs — disjoint paths ---------------------------------- #


def test_duplicate_target_path_rejected() -> None:
    with pytest.raises(FanoutError) as e:
        validate_slice_specs([_spec("iac/a.tf"), _spec("iac/a.tf")])
    assert e.value.kind is FanoutFailureKind.POLICY
    assert "duplicate" in e.value.detail
    assert "iac/a.tf" in e.value.detail


# validate_slice_specs — path policy (translated from the lib) ----------- #


def test_foundation_path_rejected_translated() -> None:
    # iac/versions.tf is in PROTECTED_FOUNDATION (operator-only).
    with pytest.raises(FanoutError) as e:
        validate_slice_specs([_spec("iac/versions.tf")])
    assert e.value.kind is FanoutFailureKind.POLICY
    assert e.value.status == 403


def test_traversal_path_rejected() -> None:
    with pytest.raises(FanoutError) as e:
        validate_slice_specs([_spec("iac/../x.tf")])
    assert e.value.kind is FanoutFailureKind.POLICY


def test_non_iac_path_rejected() -> None:
    with pytest.raises(FanoutError) as e:
        validate_slice_specs([_spec("foo/bar.tf")])
    assert e.value.kind is FanoutFailureKind.POLICY


def test_wrong_suffix_rejected() -> None:
    with pytest.raises(FanoutError) as e:
        validate_slice_specs([_spec("iac/x.txt")])
    assert e.value.kind is FanoutFailureKind.POLICY


def test_editor_policy_error_does_not_leak() -> None:
    # The library error must be translated, never propagated raw.
    with pytest.raises(FanoutError):
        validate_slice_specs([_spec("iac/versions.tf")])


# SliceSpec — model-level validation ------------------------------------- #


def test_empty_goal_rejected() -> None:
    with pytest.raises(Exception):
        SliceSpec(goal="", target_path="iac/a.tf")


def test_whitespace_goal_rejected() -> None:
    with pytest.raises(Exception):
        SliceSpec(goal="   \n\t ", target_path="iac/a.tf")


def test_slicespec_forbids_extra_fields() -> None:
    with pytest.raises(Exception):
        SliceSpec(goal="g", target_path="iac/a.tf", sneaky="x")


def test_slicespec_doc_citations_default_empty() -> None:
    s = SliceSpec(goal="g", target_path="iac/a.tf")
    assert s.doc_citations == []


# validate_slice_specs — happy path -------------------------------------- #


def test_valid_two_slice_set_ok() -> None:
    specs = [_spec("iac/a.tf"), _spec("iac/b.tf")]
    assert validate_slice_specs(specs) is None


# validate_iac_path parity with the private _validate_one_path ----------- #


def test_validate_iac_path_parity_valid() -> None:
    # Both the new public wrapper and the private function accept a valid path.
    assert validate_iac_path("iac/a.tf") is None
    assert _validate_one_path("iac/a.tf") is None


def test_validate_iac_path_parity_foundation() -> None:
    # Both raise EditorPolicyError on a foundation file, identically.
    with pytest.raises(EditorPolicyError):
        validate_iac_path("iac/versions.tf")
    with pytest.raises(EditorPolicyError):
        _validate_one_path("iac/versions.tf")
