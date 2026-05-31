"""Build + serialize the C2 ``iac-tree.json`` sidecar artifact (CLI + re-export).

The ``c6.v1`` sidecar schema lives in ``driftscribe_lib.iac_tree`` (Phase C6a-1). This
module is the ``python -m tools.iac_tree_sidecar`` CLI the plan-builder workflow uses
to emit the sidecar, reading the same ``META_*`` env vars as
``tools.iac_plan_metadata`` plus ``IAC_TREE_HASH`` (from ``tools.iac_tree_hash``).

Every sidecar field except ``iac_tree_hash`` equals the matching ``c2.v1`` metadata
field, so the worker can cross-check the (unsigned) sidecar against the HMAC-signed
metadata before trusting the hash. ``build_sidecar`` validates + raises ``ValueError``
on malformed input; the CLI converts that to a non-zero exit so the workflow fails
BEFORE the sidecar is uploaded.
"""
from __future__ import annotations

from driftscribe_lib.iac_tree import (  # noqa: F401  (re-export)
    SIDECAR_SCHEMA_VERSION,
    SidecarInput,
    build_sidecar,
    serialize_sidecar,
)


def _read_env(name: str, env: dict[str, str]) -> str:
    value = env.get(name)
    if value is None or value == "":
        raise SystemExit(f"missing required env var: {name}")
    return value


def _main(env: dict[str, str]) -> int:
    """CLI entrypoint: read META_* + IAC_TREE_HASH env, emit canonical sidecar JSON."""
    import sys as _sys

    try:
        pr_str = _read_env("META_PR_NUMBER", env)
        try:
            pr_number = int(pr_str)
        except ValueError:
            raise SystemExit(f"META_PR_NUMBER: must be int (got {pr_str!r})")
        inp = SidecarInput(
            repo=_read_env("META_REPO", env),
            pr_number=pr_number,
            head_sha=_read_env("META_HEAD_SHA", env),
            base_sha=_read_env("META_BASE_SHA", env),
            workflow_run_id=_read_env("META_WORKFLOW_RUN_ID", env),
            workflow_run_attempt=_read_env("META_WORKFLOW_RUN_ATTEMPT", env),
            plan_sha256=_read_env("META_PLAN_SHA256", env),
            plan_json_sha256=_read_env("META_PLAN_JSON_SHA256", env),
            iac_tree_hash=_read_env("IAC_TREE_HASH", env),
        )
    except SystemExit as e:
        print(str(e), file=_sys.stderr)
        return 2

    try:
        sidecar = build_sidecar(inp)
    except ValueError as e:
        print(str(e), file=_sys.stderr)
        return 1

    print(serialize_sidecar(sidecar), end="")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    import os
    import sys as _sys

    _sys.exit(_main(dict(os.environ)))
