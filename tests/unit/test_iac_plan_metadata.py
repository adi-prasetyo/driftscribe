"""Tests for tools.iac_plan_metadata — the C2 metadata builder."""

from tools import iac_plan_metadata


def test_module_imports():
    """The module must be importable."""
    assert iac_plan_metadata is not None
