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


# --- Adopt/import design §4.3: importing entries are CREATE-CLASS ---


def _pj_importing(actions, importing):
    return {
        "resource_changes": [
            {
                "address": "google_storage_bucket.b",
                "change": {"actions": actions, "importing": importing},
            }
        ]
    }


def test_importing_noop_is_create_class():
    """A pure import plans as ["no-op"] + importing — it writes a NEW address
    into state at apply, so it must route through the strict C6 merge-first
    path (state-without-config is the §2 delete-proposal failure mode)."""
    assert plan_has_create(_pj_importing(["no-op"], {"id": "b-name"})) is True


def test_importing_update_is_create_class():
    assert plan_has_create(_pj_importing(["update"], {"id": "b-name"})) is True


def test_importing_null_is_treated_as_absent():
    """`importing: null` is NOT an import (same semantics as iac_plan_summary)."""
    assert plan_has_create(_pj_importing(["no-op"], None)) is False


def test_importing_malformed_value_is_still_create_class():
    """Even a malformed (non-dict) importing value routes strict — fail-closed."""
    assert plan_has_create(_pj_importing(["no-op"], "not-a-dict")) is True
