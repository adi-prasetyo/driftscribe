"""Build + serialize the C2 plan-builder metadata.json artifact (CLI + re-export).

The ``c2.v1`` schema + validators now live in ``driftscribe_lib.iac_plan_metadata``
(promoted in Phase C3 so the lib-only worker containers + the C3 approval signer
can share one canonical definition along the ``tools -> lib`` dependency
direction). This module re-exports the public names for backward compatibility
and retains the ``python -m tools.iac_plan_metadata`` CLI used by the plan-builder
workflow (.github/workflows/iac.yml).

The metadata record is the input contract for the C3 plan-approval schema — DO
NOT rename a field without updating C3.

Validation: ``build_metadata`` validates input shapes and raises ``ValueError``
on malformed inputs. The CLI wrapper converts ValueError into a non-zero exit so
the workflow fails BEFORE the artifact gets uploaded.
"""
from __future__ import annotations

from driftscribe_lib.iac_plan_metadata import (  # noqa: F401  (re-export)
    METADATA_SCHEMA_VERSION,
    MetadataInput,
    build_metadata,
    serialize_metadata,
)


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
