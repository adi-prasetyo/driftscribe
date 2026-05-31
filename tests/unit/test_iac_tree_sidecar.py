"""Tests for the c6.v1 iac-tree.json sidecar schema (Phase C6a-1).

The sidecar carries the canonical iac_tree_hash plus every field the worker
cross-checks against the HMAC-signed c2.v1 metadata before trusting the hash. The
schema mirrors driftscribe_lib.iac_plan_metadata: validate-or-raise (never a partial
record) + byte-stable serialization.
"""

import json

import pytest

from driftscribe_lib import iac_tree
from driftscribe_lib.iac_tree import (
    SIDECAR_SCHEMA_VERSION,
    SidecarInput,
    build_sidecar,
    serialize_sidecar,
)

_HEAD = "a" * 40
_BASE = "b" * 40
_SHA = "c" * 64
_JSON_SHA = "d" * 64
_TREE = "e" * 64


def _valid(**overrides):
    base = dict(
        repo="adi-prasetyo/driftscribe",
        pr_number=42,
        head_sha=_HEAD,
        base_sha=_BASE,
        workflow_run_id="1234567890",
        workflow_run_attempt="1",
        plan_sha256=_SHA,
        plan_json_sha256=_JSON_SHA,
        iac_tree_hash=_TREE,
    )
    base.update(overrides)
    return SidecarInput(**base)


def test_module_imports():
    assert iac_tree is not None
    assert SIDECAR_SCHEMA_VERSION == "c6.v1"


def test_build_valid_record_has_all_fields():
    md = build_sidecar(_valid())
    assert md["schema_version"] == "c6.v1"
    assert md["repo"] == "adi-prasetyo/driftscribe"
    assert md["pr_number"] == 42
    assert md["head_sha"] == _HEAD
    assert md["base_sha"] == _BASE
    assert md["workflow_run_id"] == "1234567890"
    assert md["workflow_run_attempt"] == "1"
    assert md["plan_sha256"] == _SHA
    assert md["plan_json_sha256"] == _JSON_SHA
    assert md["iac_tree_hash"] == _TREE


@pytest.mark.parametrize(
    "overrides",
    [
        {"repo": "no-slash"},
        {"pr_number": 0},
        {"pr_number": -1},
        {"pr_number": True},  # bool is not an int here
        {"head_sha": "A" * 40},  # uppercase hex rejected
        {"head_sha": "a" * 39},
        {"base_sha": "xyz"},
        {"workflow_run_id": "abc"},
        {"workflow_run_id": ""},
        {"workflow_run_attempt": "0"},  # GHA run_attempt is 1-indexed
        {"workflow_run_attempt": "x"},
        {"plan_sha256": "a" * 63},
        {"plan_json_sha256": "A" * 64},  # uppercase rejected
        {"iac_tree_hash": "nope"},
        {"iac_tree_hash": "a" * 63},
    ],
)
def test_build_rejects_malformed(overrides):
    with pytest.raises(ValueError):
        build_sidecar(_valid(**overrides))


def test_serialize_is_byte_stable_and_sorted():
    md = build_sidecar(_valid())
    out = serialize_sidecar(md)
    assert out.endswith("\n")
    assert out == serialize_sidecar(md)  # idempotent
    # sorted keys + parses back
    parsed = json.loads(out)
    assert parsed == md
    assert list(parsed.keys()) == sorted(parsed.keys())


def test_serialize_roundtrips_through_build():
    md = build_sidecar(_valid())
    reparsed = json.loads(serialize_sidecar(md))
    assert reparsed["iac_tree_hash"] == _TREE
