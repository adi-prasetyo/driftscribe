"""Tests for driftscribe_lib.iac_plan_classify.plan_has_create (Phase C6a-3).

The predicate the coordinator (routing) and the worker (gate enforcement) BOTH use,
so it lives in the lib both deployables ship. Fail-closed: malformed ⇒ create-class.
"""

import pytest

from driftscribe_lib.iac_plan_classify import plan_has_create


def _pj(actions, address="google_x.y", typ="google_x"):
    return {"resource_changes": [{"address": address, "type": typ, "change": {"actions": actions}}]}


def test_create_is_create_class():
    assert plan_has_create(_pj(["create"])) is True


@pytest.mark.parametrize("actions", [["delete", "create"], ["create", "delete"]])
def test_replace_counts_as_create(actions):
    """A replace recreates the resource → create-class (both orderings)."""
    assert plan_has_create(_pj(actions)) is True


def test_update_only_is_not_create():
    assert plan_has_create(_pj(["update"])) is False


@pytest.mark.parametrize("actions", [["no-op"], ["read"]])
def test_noop_read_ignored(actions):
    assert plan_has_create(_pj(actions)) is False


def test_empty_resource_changes_is_not_create():
    assert plan_has_create({"resource_changes": []}) is False


def test_mixed_update_and_create_is_create():
    pj = {
        "resource_changes": [
            {"address": "a", "change": {"actions": ["update"]}},
            {"address": "b", "change": {"actions": ["create"]}},
        ]
    }
    assert plan_has_create(pj) is True


def test_module_create_is_create_class():
    """A module.* create routes through the create path (the worker still REFUSES
    module.* in resource_set_guard — this only affects routing/gating)."""
    assert plan_has_create(_pj(["create"], address="module.m.google_x.y")) is True


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "not-a-dict",
        42,
        {},  # no resource_changes
        {"resource_changes": "not-a-list"},
        {"resource_changes": [None]},
        {"resource_changes": ["not-a-dict"]},
        {"resource_changes": [{"address": "a", "change": "not-a-dict"}]},
        {"resource_changes": [{"address": "a", "change": {"actions": "not-a-list"}}]},
        {"resource_changes": [{"address": "a"}]},  # no change key
    ],
)
def test_malformed_fails_closed_to_create(bad):
    assert plan_has_create(bad) is True
