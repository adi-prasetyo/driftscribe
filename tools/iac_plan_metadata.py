"""Build + serialize the C2 plan-builder metadata.json artifact.

Pure-stdlib helper called by the plan-builder workflow. The metadata
record is the input contract for the C3 plan-approval schema (see
docs/plans/2026-05-28-infra-iac-phase-c2-plan-builder.md §3) — DO NOT
rename a field without updating C3.

Determinism: every public function in this module is a pure function of
its arguments. ``serialize_metadata`` round-trips byte-identically given
the same input (sort_keys + fixed indent + no trailing whitespace).

Validation: ``build_metadata`` validates input shapes and raises
``ValueError`` on malformed inputs. The CLI wrapper (in a later task)
converts ValueError into a non-zero exit so the workflow fails BEFORE
the artifact gets uploaded.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

METADATA_SCHEMA_VERSION = "c2.v1"

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_REPO  = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_DIGITS = re.compile(r"^[0-9]+$")
_SEMVER_3 = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?$")


@dataclass(frozen=True)
class MetadataInput:
    """Input to :func:`build_metadata`. All fields required."""
    repo: str
    pr_number: int
    head_sha: str
    base_sha: str
    workflow_run_id: str
    workflow_run_attempt: str
    artifact_uri_plan: str
    artifact_uri_json: str
    generation_plan: str
    generation_json: str
    plan_sha256: str
    plan_json_sha256: str
    opentofu_version: str
    provider_lockfile_sha256: str


_POSITIVE_DIGITS = re.compile(r"^[1-9][0-9]*$")


def _check(name: str, value: Any, predicate, message: str) -> None:
    if not predicate(value):
        raise ValueError(f"{name}: {message} (got {value!r})")


def build_metadata(inp: MetadataInput) -> dict[str, Any]:
    """Validate input + return the metadata dict.

    Raises ValueError on any malformed field. NEVER produces a partial /
    half-validated record — either every field is sane or no dict is
    returned at all.
    """
    _check("repo", inp.repo, lambda v: bool(_REPO.fullmatch(v)), "must be 'owner/repo'")
    _check("pr_number", inp.pr_number, lambda v: isinstance(v, int) and not isinstance(v, bool) and v > 0, "must be a positive int")
    _check("head_sha", inp.head_sha, lambda v: bool(_HEX40.fullmatch(v)), "must be 40 lowercase hex")
    _check("base_sha", inp.base_sha, lambda v: bool(_HEX40.fullmatch(v)), "must be 40 lowercase hex")
    _check("workflow_run_id", inp.workflow_run_id, lambda v: isinstance(v, str) and bool(_DIGITS.fullmatch(v)), "must be a numeric string")
    _check("workflow_run_attempt", inp.workflow_run_attempt, lambda v: isinstance(v, str) and bool(_POSITIVE_DIGITS.fullmatch(v)), "must be a positive numeric string (GHA run_attempt is 1-indexed)")
    _check("generation_plan", inp.generation_plan, lambda v: isinstance(v, str) and bool(_DIGITS.fullmatch(v)), "must be a numeric string")
    _check("generation_json", inp.generation_json, lambda v: isinstance(v, str) and bool(_DIGITS.fullmatch(v)), "must be a numeric string")
    _check("plan_sha256", inp.plan_sha256, lambda v: bool(_HEX64.fullmatch(v)), "must be 64 lowercase hex")
    _check("plan_json_sha256", inp.plan_json_sha256, lambda v: bool(_HEX64.fullmatch(v)), "must be 64 lowercase hex")
    _check("provider_lockfile_sha256", inp.provider_lockfile_sha256, lambda v: bool(_HEX64.fullmatch(v)), "must be 64 lowercase hex")
    _check("opentofu_version", inp.opentofu_version, lambda v: bool(_SEMVER_3.fullmatch(v)), "must be three-segment semver")

    run_dir = f"run-{inp.workflow_run_id}-{inp.workflow_run_attempt}"
    expected_prefix = f"gs://driftscribe-hack-2026-tofu-artifacts/pr-{inp.pr_number}/{inp.head_sha}/{run_dir}/"
    _check(
        "artifact_uri_plan", inp.artifact_uri_plan,
        lambda v: v == expected_prefix + "plan.tfplan",
        f"must be exactly {expected_prefix}plan.tfplan",
    )
    _check(
        "artifact_uri_json", inp.artifact_uri_json,
        lambda v: v == expected_prefix + "plan.json",
        f"must be exactly {expected_prefix}plan.json",
    )

    return {
        "schema_version": METADATA_SCHEMA_VERSION,
        "repo": inp.repo,
        "pr_number": inp.pr_number,
        "head_sha": inp.head_sha,
        "base_sha": inp.base_sha,
        "workflow_run_id": inp.workflow_run_id,
        "workflow_run_attempt": inp.workflow_run_attempt,
        "artifact_uri_plan": inp.artifact_uri_plan,
        "artifact_uri_json": inp.artifact_uri_json,
        "generation_plan": inp.generation_plan,
        "generation_json": inp.generation_json,
        "plan_sha256": inp.plan_sha256,
        "plan_json_sha256": inp.plan_json_sha256,
        "opentofu_version": inp.opentofu_version,
        "provider_lockfile_sha256": inp.provider_lockfile_sha256,
    }


def serialize_metadata(md: dict[str, Any]) -> str:
    """Stable canonical JSON encoding — sorted keys, 2-space indent, trailing newline."""
    return json.dumps(md, sort_keys=True, indent=2) + "\n"


def _read_env(name: str, env: dict[str, str]) -> str:
    value = env.get(name)
    if value is None or value == "":
        raise SystemExit(f"missing required env var: {name}")
    return value


def _main(env: dict[str, str]) -> int:
    """CLI entrypoint: read META_* env vars, emit canonical metadata JSON on stdout."""
    try:
        pr_str = _read_env("META_PR_NUMBER", env)
        try:
            pr_number = int(pr_str)
        except ValueError:
            raise SystemExit(f"META_PR_NUMBER: must be int (got {pr_str!r})")
        inp = MetadataInput(
            repo=_read_env("META_REPO", env),
            pr_number=pr_number,
            head_sha=_read_env("META_HEAD_SHA", env),
            base_sha=_read_env("META_BASE_SHA", env),
            workflow_run_id=_read_env("META_WORKFLOW_RUN_ID", env),
            workflow_run_attempt=_read_env("META_WORKFLOW_RUN_ATTEMPT", env),
            artifact_uri_plan=_read_env("META_ARTIFACT_URI_PLAN", env),
            artifact_uri_json=_read_env("META_ARTIFACT_URI_JSON", env),
            generation_plan=_read_env("META_GENERATION_PLAN", env),
            generation_json=_read_env("META_GENERATION_JSON", env),
            plan_sha256=_read_env("META_PLAN_SHA256", env),
            plan_json_sha256=_read_env("META_PLAN_JSON_SHA256", env),
            opentofu_version=_read_env("META_OPENTOFU_VERSION", env),
            provider_lockfile_sha256=_read_env("META_PROVIDER_LOCKFILE_SHA256", env),
        )
    except SystemExit as e:
        # missing env -> exit 2
        print(str(e), file=__import__("sys").stderr)
        return 2

    try:
        md = build_metadata(inp)
    except ValueError as e:
        print(str(e), file=__import__("sys").stderr)
        return 1

    print(serialize_metadata(md), end="")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    import os
    import sys as _sys
    _sys.exit(_main(dict(os.environ)))
