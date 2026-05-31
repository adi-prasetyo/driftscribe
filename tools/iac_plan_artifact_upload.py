"""Upload plan artifacts to the C2 artifact bucket via google-cloud-storage SDK.

Two-step API:
- :func:`upload_plan_and_json` — uploads plan.tfplan + plan.json, returns
  their generations. Called by the workflow BEFORE metadata is built.
- :func:`upload_metadata` — uploads the final metadata.json, returns its
  generation. Called AFTER metadata is rebuilt with the real plan/json
  generations.

Why two steps and not one: metadata.json's content depends on the
generations of plan.tfplan + plan.json (Codex rev-2 Important 2 — never
upload a placeholder metadata file). The two-step API keeps both calls
testable without the workflow needing inline Python.

Failure semantics: if any upload raises mid-sequence, no metadata.json is
ever written. C4 keys off metadata.json's presence, so an orphan plan.tfplan
or plan.json is harmless — the bucket lifecycle rule deletes it after the
retention window. Cleanup is NOT this module's responsibility.

Why the SDK and not `gcloud storage cp`: ``Blob.upload_from_filename()``
populates ``Blob.generation`` from the upload response in-band, so we
do not need ``storage.objects.get`` (the CI SA's IAM stays at
``roles/storage.objectCreator``).

Path-pattern enforcement note: GCS IAM cannot condition on object name for
``storage.objects.create`` via predefined roles, so the bucket-scoped
``roles/storage.objectCreator`` granted in
``infra/scripts/setup_iac_backend.sh`` (block 5d) does not constrain object
paths. The ``pr-<N>/<head_sha>/run-<id>-<attempt>`` path scheme is enforced
HERE, by the ``_check_prefix`` regex on every upload. Renaming this regex
without updating the bootstrap script comment is a regression.
"""
from __future__ import annotations

import os
import pathlib
import re
from dataclasses import dataclass
from typing import Any

_OBJECT_PREFIX_RE = re.compile(r"^pr-[1-9][0-9]*/[0-9a-f]{40}/run-[1-9][0-9]*-[1-9][0-9]*$")


def _check_prefix(prefix: str) -> None:
    if not _OBJECT_PREFIX_RE.fullmatch(prefix):
        raise ValueError(
            f"object_prefix: must match 'pr-<N>/<head_sha>/run-<id>-<attempt>' "
            f"(got {prefix!r})"
        )


def _upload_one(bucket: Any, blob_name: str, local: pathlib.Path) -> str:
    if not local.exists():
        raise FileNotFoundError(str(local))
    blob = bucket.blob(blob_name)
    blob.upload_from_filename(str(local))
    gen = getattr(blob, "generation", None)
    if gen is None:
        raise RuntimeError(f"upload of {blob_name} returned no generation (SDK contract violation)")
    return str(gen)


# --- Step 1: plan.tfplan + plan.json ------------------------------------


@dataclass(frozen=True)
class PlanJsonUploadInput:
    bucket: Any
    object_prefix: str
    local_plan: pathlib.Path
    local_plan_json: pathlib.Path


@dataclass(frozen=True)
class PlanJsonUploadResult:
    generation_plan: str
    generation_json: str


def upload_plan_and_json(inp: PlanJsonUploadInput) -> PlanJsonUploadResult:
    _check_prefix(inp.object_prefix)
    gen_plan = _upload_one(inp.bucket, f"{inp.object_prefix}/plan.tfplan", inp.local_plan)
    gen_json = _upload_one(inp.bucket, f"{inp.object_prefix}/plan.json",   inp.local_plan_json)
    return PlanJsonUploadResult(generation_plan=gen_plan, generation_json=gen_json)


# --- Step 2: metadata.json -----------------------------------------------


@dataclass(frozen=True)
class MetadataUploadInput:
    bucket: Any
    object_prefix: str
    local_metadata: pathlib.Path


def upload_metadata(inp: MetadataUploadInput) -> str:
    _check_prefix(inp.object_prefix)
    return _upload_one(inp.bucket, f"{inp.object_prefix}/metadata.json", inp.local_metadata)


# --- Step 3 (C6): iac-tree.json sidecar ----------------------------------


@dataclass(frozen=True)
class IacTreeUploadInput:
    bucket: Any
    object_prefix: str
    local_iac_tree: pathlib.Path


def upload_iac_tree(inp: IacTreeUploadInput) -> str:
    """Upload the C6 sidecar ``iac-tree.json`` into the SAME run dir as the c2.v1
    triplet, returning its generation. Uploaded AFTER metadata.json (the denylist /
    integrity gates run against plan.json; the sidecar carries no policy of its own —
    it only binds the iac/-tree hash the worker re-derives + cross-checks)."""
    _check_prefix(inp.object_prefix)
    return _upload_one(inp.bucket, f"{inp.object_prefix}/iac-tree.json", inp.local_iac_tree)


# --- CLI -----------------------------------------------------------------

def _main(argv: list[str]) -> int:
    import argparse
    import sys as _sys
    parser = argparse.ArgumentParser(prog="iac_plan_artifact_upload")
    parser.add_argument("--mode", required=True, choices=["plan-and-json", "metadata", "iac-tree"])
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--object-prefix", required=True)
    parser.add_argument("--local-plan", type=pathlib.Path)
    parser.add_argument("--local-plan-json", type=pathlib.Path)
    parser.add_argument("--local-metadata", type=pathlib.Path)
    parser.add_argument("--local-iac-tree", type=pathlib.Path)
    ns = parser.parse_args(argv)

    # Defer the SDK import so unit tests do not require google-cloud-storage.
    from google.cloud import storage  # type: ignore
    # storage.Client() resolves the project EAGERLY in __init__ and raises
    # EnvironmentError if it can't — even though uploading to an existing
    # bucket never needs a project. Under WIF the ADC carries no project, so
    # we pass one explicitly: GOOGLE_CLOUD_PROJECT (exported by the auth
    # action) when present, else the hardcoded project. This keeps the tool
    # correct independent of the workflow's ambient env (e.g. ad-hoc operator
    # runs, or a future switch to direct WIF without SA impersonation).
    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or "driftscribe-hack-2026"
    client = storage.Client(project=project)
    bucket = client.bucket(ns.bucket)

    try:
        if ns.mode == "plan-and-json":
            if ns.local_plan is None or ns.local_plan_json is None:
                raise ValueError("--mode plan-and-json requires --local-plan and --local-plan-json")
            r = upload_plan_and_json(PlanJsonUploadInput(
                bucket=bucket, object_prefix=ns.object_prefix,
                local_plan=ns.local_plan, local_plan_json=ns.local_plan_json,
            ))
            print(f"GEN_PLAN={r.generation_plan}")
            print(f"GEN_JSON={r.generation_json}")
        elif ns.mode == "metadata":
            if ns.local_metadata is None:
                raise ValueError("--mode metadata requires --local-metadata")
            gen = upload_metadata(MetadataUploadInput(
                bucket=bucket, object_prefix=ns.object_prefix,
                local_metadata=ns.local_metadata,
            ))
            print(f"GEN_METADATA={gen}")
        else:  # mode == "iac-tree"
            if ns.local_iac_tree is None:
                raise ValueError("--mode iac-tree requires --local-iac-tree")
            gen = upload_iac_tree(IacTreeUploadInput(
                bucket=bucket, object_prefix=ns.object_prefix,
                local_iac_tree=ns.local_iac_tree,
            ))
            print(f"GEN_IAC_TREE={gen}")
    except (ValueError, FileNotFoundError) as e:
        print(str(e), file=_sys.stderr)
        return 1

    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    import sys as _sys
    _sys.exit(_main(_sys.argv[1:]))
