"""Tests for tools.iac_plan_metadata — the C2 metadata builder."""

import json

import pytest

from tools import iac_plan_metadata
from tools.iac_plan_metadata import (
    METADATA_SCHEMA_VERSION,
    MetadataInput,
    build_metadata,
    serialize_metadata,
)


def test_module_imports():
    """The module must be importable."""
    assert iac_plan_metadata is not None


_RUN_DIR = "run-1234567890-1"
_HEAD = "a" * 40


def _valid_input(**overrides):
    base = dict(
        repo="adi-prasetyo/driftscribe",
        pr_number=42,
        head_sha=_HEAD,
        base_sha="b" * 40,
        workflow_run_id="1234567890",
        workflow_run_attempt="1",
        artifact_uri_plan=f"gs://driftscribe-hack-2026-tofu-artifacts/pr-42/{_HEAD}/{_RUN_DIR}/plan.tfplan",
        artifact_uri_json=f"gs://driftscribe-hack-2026-tofu-artifacts/pr-42/{_HEAD}/{_RUN_DIR}/plan.json",
        generation_plan="1700000000000000",
        generation_json="1700000000000001",
        plan_sha256="c" * 64,
        plan_json_sha256="d" * 64,
        opentofu_version="1.12.0",
        provider_lockfile_sha256="e" * 64,
    )
    base.update(overrides)
    return MetadataInput(**base)


def test_schema_version_constant():
    assert METADATA_SCHEMA_VERSION == "c2.v1"


def test_build_metadata_returns_all_fifteen_keys():
    md = build_metadata(_valid_input())
    expected_keys = {
        "schema_version",
        "repo", "pr_number",
        "head_sha", "base_sha",
        "workflow_run_id", "workflow_run_attempt",
        "artifact_uri_plan", "artifact_uri_json",
        "generation_plan", "generation_json",
        "plan_sha256", "plan_json_sha256",
        "opentofu_version", "provider_lockfile_sha256",
    }
    assert set(md.keys()) == expected_keys


def test_build_metadata_schema_version_value():
    md = build_metadata(_valid_input())
    assert md["schema_version"] == "c2.v1"


def test_pr_number_is_int_in_serialized_form():
    md = build_metadata(_valid_input(pr_number=42))
    assert md["pr_number"] == 42
    blob = serialize_metadata(md)
    parsed = json.loads(blob)
    assert parsed["pr_number"] == 42
    assert isinstance(parsed["pr_number"], int)


def test_workflow_run_id_and_attempt_and_generations_are_strings():
    md = build_metadata(_valid_input())
    assert isinstance(md["workflow_run_id"], str)
    assert isinstance(md["workflow_run_attempt"], str)
    assert isinstance(md["generation_plan"], str)
    assert isinstance(md["generation_json"], str)


def test_workflow_run_attempt_must_be_positive_digits():
    with pytest.raises(ValueError, match="workflow_run_attempt"):
        build_metadata(_valid_input(workflow_run_attempt="0"))
    with pytest.raises(ValueError, match="workflow_run_attempt"):
        build_metadata(_valid_input(workflow_run_attempt="abc"))


def test_serialize_metadata_is_deterministic():
    md1 = build_metadata(_valid_input())
    md2 = build_metadata(_valid_input())
    assert serialize_metadata(md1) == serialize_metadata(md2)


def test_serialize_metadata_ends_with_newline():
    blob = serialize_metadata(build_metadata(_valid_input()))
    assert blob.endswith("\n")


def test_serialize_metadata_uses_sorted_keys():
    blob = serialize_metadata(build_metadata(_valid_input()))
    # The first key after the opening brace must be lexicographically
    # smallest among ours: "artifact_uri_json".
    first_line_with_key = blob.split("\n")[1].lstrip()
    assert first_line_with_key.startswith('"artifact_uri_json"'), blob


@pytest.mark.parametrize("field", [
    "head_sha", "base_sha", "plan_sha256", "plan_json_sha256", "provider_lockfile_sha256",
])
def test_hex_fields_rejected_when_wrong_length(field):
    with pytest.raises(ValueError, match=field):
        build_metadata(_valid_input(**{field: "abc"}))


@pytest.mark.parametrize("field", [
    "head_sha", "base_sha",  # 40-hex SHA-1
])
def test_sha1_fields_must_be_lowercase_hex(field):
    with pytest.raises(ValueError, match=field):
        build_metadata(_valid_input(**{field: "G" * 40}))


@pytest.mark.parametrize("field", [
    "plan_sha256", "plan_json_sha256", "provider_lockfile_sha256",  # 64-hex SHA-256
])
def test_sha256_fields_must_be_lowercase_hex(field):
    with pytest.raises(ValueError, match=field):
        build_metadata(_valid_input(**{field: "G" * 64}))


def test_pr_number_must_be_positive():
    with pytest.raises(ValueError, match="pr_number"):
        build_metadata(_valid_input(pr_number=0))
    with pytest.raises(ValueError, match="pr_number"):
        build_metadata(_valid_input(pr_number=-1))


def test_repo_must_match_owner_slash_repo():
    with pytest.raises(ValueError, match="repo"):
        build_metadata(_valid_input(repo="adi-prasetyo"))
    with pytest.raises(ValueError, match="repo"):
        build_metadata(_valid_input(repo="adi-prasetyo/driftscribe/extra"))


def test_artifact_uri_plan_must_match_pr_head_and_run_dir():
    bad = _valid_input(artifact_uri_plan="gs://other-bucket/pr-1/aaa/run-1-1/plan.tfplan")
    with pytest.raises(ValueError, match="artifact_uri_plan"):
        build_metadata(bad)
    # Wrong run_id segment also fails — the path must reflect THIS dispatch's run.
    bad2 = _valid_input(
        artifact_uri_plan=f"gs://driftscribe-hack-2026-tofu-artifacts/pr-42/{_HEAD}/run-999-1/plan.tfplan",
    )
    with pytest.raises(ValueError, match="artifact_uri_plan"):
        build_metadata(bad2)


def test_artifact_uri_must_omit_or_include_run_attempt_correctly():
    # The path scheme is `run-<run_id>-<run_attempt>`: a path that drops the
    # attempt segment must be rejected.
    bad = _valid_input(
        artifact_uri_plan=f"gs://driftscribe-hack-2026-tofu-artifacts/pr-42/{_HEAD}/run-1234567890/plan.tfplan",
    )
    with pytest.raises(ValueError, match="artifact_uri_plan"):
        build_metadata(bad)


def test_generation_must_be_numeric_string():
    with pytest.raises(ValueError, match="generation_plan"):
        build_metadata(_valid_input(generation_plan="abc"))
    with pytest.raises(ValueError, match="generation_plan"):
        build_metadata(_valid_input(generation_plan=""))


def test_opentofu_version_must_be_semver_like():
    with pytest.raises(ValueError, match="opentofu_version"):
        build_metadata(_valid_input(opentofu_version=""))
    with pytest.raises(ValueError, match="opentofu_version"):
        build_metadata(_valid_input(opentofu_version="1.12"))  # only 2 segments


def test_pr_number_rejects_bool():
    """bool is an int subclass; pr_number=True must NOT pass validation."""
    with pytest.raises(ValueError, match="pr_number"):
        build_metadata(_valid_input(pr_number=True))
    with pytest.raises(ValueError, match="pr_number"):
        build_metadata(_valid_input(pr_number=False))
