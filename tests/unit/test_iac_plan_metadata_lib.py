"""Lock the canonical location of the ``c2.v1`` schema in ``driftscribe_lib``.

Phase C3 promoted the schema out of ``tools/`` (which is not an installed
package and is not shipped in worker containers) into ``driftscribe_lib`` so the
C3 approval signer + the future C4 ``tofu-apply`` worker can import it at
runtime. These tests guard against a regression that silently moves the
definition back, and prove the ``tools`` shim re-exports the SAME objects.
"""
from __future__ import annotations

import json

import pytest

from driftscribe_lib import iac_plan_metadata as lib
from driftscribe_lib.iac_plan_metadata import (
    METADATA_SCHEMA_VERSION,
    MetadataInput,
    build_metadata,
    serialize_metadata,
)


def _valid_input() -> MetadataInput:
    sha = "a" * 40
    h64 = "b" * 64
    prefix = f"gs://driftscribe-hack-2026-tofu-artifacts/pr-12/{sha}/run-100-1/"
    return MetadataInput(
        repo="adi-p/driftscribe",
        pr_number=12,
        head_sha=sha,
        base_sha="c" * 40,
        workflow_run_id="100",
        workflow_run_attempt="1",
        artifact_uri_plan=prefix + "plan.tfplan",
        artifact_uri_json=prefix + "plan.json",
        generation_plan="1700000000000001",
        generation_json="1700000000000002",
        plan_sha256=h64,
        plan_json_sha256="d" * 64,
        opentofu_version="1.12.0",
        provider_lockfile_sha256="e" * 64,
    )


def test_schema_version_constant() -> None:
    assert METADATA_SCHEMA_VERSION == "c2.v1"
    assert lib.METADATA_SCHEMA_VERSION == "c2.v1"


def test_build_metadata_happy_path_has_15_keys() -> None:
    md = build_metadata(_valid_input())
    assert md["schema_version"] == "c2.v1"
    assert len(md) == 15


def test_build_metadata_rejects_malformed_field() -> None:
    bad = MetadataInput(**{**_valid_input().__dict__, "head_sha": "nothex"})
    with pytest.raises(ValueError):
        build_metadata(bad)


def test_serialize_metadata_is_deterministic_sorted() -> None:
    md = build_metadata(_valid_input())
    out = serialize_metadata(md)
    assert out.endswith("\n")
    # sorted keys + stable round-trip
    assert out == serialize_metadata(md)
    assert json.loads(out) == md


def test_tools_shim_reexports_the_same_objects() -> None:
    """The ``tools`` CLI module must re-export the lib objects identically — not
    a divergent copy — so there is exactly one schema definition."""
    from tools import iac_plan_metadata as shim

    assert shim.MetadataInput is MetadataInput
    assert shim.build_metadata is build_metadata
    assert shim.serialize_metadata is serialize_metadata
    assert shim.METADATA_SCHEMA_VERSION == METADATA_SCHEMA_VERSION
