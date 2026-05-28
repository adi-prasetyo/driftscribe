"""Tests for tools.iac_plan_artifact_upload — two-step uploader.

Step 1: upload_plan_and_json() — uploads plan.tfplan + plan.json,
returns (generation_plan, generation_json).
Step 2: upload_metadata() — uploads metadata.json, returns
generation_metadata.

The workflow calls Step 1, builds final metadata.json with the returned
generations, then calls Step 2. NO placeholder metadata is ever written.
"""

from __future__ import annotations

import json
import pathlib
from unittest.mock import MagicMock

import pytest

from tools.iac_plan_artifact_upload import (
    MetadataUploadInput,
    PlanJsonUploadInput,
    PlanJsonUploadResult,
    upload_metadata,
    upload_plan_and_json,
)


def _make_mock_bucket(generations: dict[str, int]) -> MagicMock:
    """Build a google-cloud-storage Bucket mock whose blob().upload_from_filename()
    populates blob.generation from `generations[blob_name]`."""
    bucket = MagicMock()

    def blob_factory(name: str) -> MagicMock:
        blob = MagicMock()
        blob.name = name

        def _do_upload(*_a, **_kw):
            blob.generation = generations[name]

        blob.upload_from_filename.side_effect = _do_upload
        return blob

    bucket.blob.side_effect = blob_factory
    return bucket


_HEAD = "a" * 40
_PREFIX = f"pr-42/{_HEAD}/run-1234567890-1"


# --- Step 1 tests ---------------------------------------------------------


def test_plan_and_json_returns_two_generations(tmp_path: pathlib.Path) -> None:
    plan = tmp_path / "plan.tfplan"
    plan.write_bytes(b"\x00binary plan\x00")
    pjson = tmp_path / "plan.json"
    pjson.write_text(json.dumps({"resource_changes": []}))

    bucket = _make_mock_bucket({
        f"{_PREFIX}/plan.tfplan": 1700000000000000,
        f"{_PREFIX}/plan.json":   1700000000000001,
    })

    result = upload_plan_and_json(PlanJsonUploadInput(
        bucket=bucket, object_prefix=_PREFIX,
        local_plan=plan, local_plan_json=pjson,
    ))

    assert isinstance(result, PlanJsonUploadResult)
    assert result.generation_plan == "1700000000000000"
    assert result.generation_json == "1700000000000001"


def test_plan_and_json_upload_order(tmp_path: pathlib.Path) -> None:
    """plan.tfplan first, then plan.json — deterministic for debugging."""
    plan = tmp_path / "plan.tfplan"; plan.write_bytes(b"x")
    pjson = tmp_path / "plan.json"; pjson.write_text("{}")
    bucket = _make_mock_bucket({
        f"{_PREFIX}/plan.tfplan": 1, f"{_PREFIX}/plan.json": 2,
    })
    upload_plan_and_json(PlanJsonUploadInput(
        bucket=bucket, object_prefix=_PREFIX,
        local_plan=plan, local_plan_json=pjson,
    ))
    names_in_order = [c.args[0] for c in bucket.blob.call_args_list]
    assert names_in_order == [f"{_PREFIX}/plan.tfplan", f"{_PREFIX}/plan.json"]


def test_plan_and_json_fails_if_local_file_missing(tmp_path: pathlib.Path) -> None:
    bucket = _make_mock_bucket({})
    with pytest.raises(FileNotFoundError):
        upload_plan_and_json(PlanJsonUploadInput(
            bucket=bucket, object_prefix=_PREFIX,
            local_plan=tmp_path / "nope.tfplan",
            local_plan_json=tmp_path / "nope.json",
        ))


def test_plan_and_json_rejects_unsafe_object_prefix(tmp_path: pathlib.Path) -> None:
    bucket = _make_mock_bucket({})
    # path traversal
    with pytest.raises(ValueError, match="object_prefix"):
        upload_plan_and_json(PlanJsonUploadInput(
            bucket=bucket, object_prefix="../escape/pr-42/aaa/run-1-1",
            local_plan=tmp_path, local_plan_json=tmp_path,
        ))
    # trailing slash
    with pytest.raises(ValueError, match="object_prefix"):
        upload_plan_and_json(PlanJsonUploadInput(
            bucket=bucket, object_prefix="pr-42/aaa/run-1-1/",
            local_plan=tmp_path, local_plan_json=tmp_path,
        ))
    # missing run segment
    with pytest.raises(ValueError, match="object_prefix"):
        upload_plan_and_json(PlanJsonUploadInput(
            bucket=bucket, object_prefix=f"pr-42/{_HEAD}",
            local_plan=tmp_path, local_plan_json=tmp_path,
        ))


# --- Step 2 tests ---------------------------------------------------------


def test_metadata_returns_one_generation(tmp_path: pathlib.Path) -> None:
    meta = tmp_path / "metadata.json"
    meta.write_text('{"schema_version":"c2.v1"}\n')
    bucket = _make_mock_bucket({f"{_PREFIX}/metadata.json": 1700000000000002})
    gen = upload_metadata(MetadataUploadInput(
        bucket=bucket, object_prefix=_PREFIX, local_metadata=meta,
    ))
    assert gen == "1700000000000002"


def test_metadata_uses_correct_blob_path(tmp_path: pathlib.Path) -> None:
    meta = tmp_path / "metadata.json"; meta.write_text("{}")
    bucket = _make_mock_bucket({f"{_PREFIX}/metadata.json": 1})
    upload_metadata(MetadataUploadInput(
        bucket=bucket, object_prefix=_PREFIX, local_metadata=meta,
    ))
    bucket.blob.assert_called_once_with(f"{_PREFIX}/metadata.json")


def test_metadata_fails_if_local_file_missing(tmp_path: pathlib.Path) -> None:
    bucket = _make_mock_bucket({})
    with pytest.raises(FileNotFoundError):
        upload_metadata(MetadataUploadInput(
            bucket=bucket, object_prefix=_PREFIX,
            local_metadata=tmp_path / "nope.json",
        ))


def test_metadata_rejects_unsafe_object_prefix(tmp_path: pathlib.Path) -> None:
    bucket = _make_mock_bucket({})
    with pytest.raises(ValueError, match="object_prefix"):
        upload_metadata(MetadataUploadInput(
            bucket=bucket, object_prefix="../escape", local_metadata=tmp_path,
        ))
