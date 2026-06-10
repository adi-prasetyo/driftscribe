"""Self-protection denylist for OpenTofu plan.json — CLI + re-export shim.

The denylist policy + validators now live in ``driftscribe_lib.iac_plan_denylist``
(promoted in Phase C4 so the lib-only ``tofu-apply`` worker container can import +
re-run the denylist at runtime along the established ``tools -> lib`` dependency
direction — the same move C3 made for ``iac_plan_metadata``). This module
re-exports the public API + every module constant for backward compatibility,
and retains the ``python -m tools.iac_plan_denylist`` CLI used by the C2
plan-builder workflow (.github/workflows/iac.yml) and local-dev validation.

Exit-code contract (unchanged): 0 = pass, 1 = violations (incl. parse failure),
2 = usage / I/O error. ASCII-only output by design. NOTE: the C4 apply worker
does NOT scrape this stderr — per the lib docstring's C4 contract, in-process
callers MUST use ``load_plan_json()`` + ``evaluate()`` directly, treating both a
non-None parse Violation and any non-empty ``evaluate()`` result as deny.
"""

from __future__ import annotations

import argparse
import sys

from driftscribe_lib.iac_plan_denylist import (  # noqa: F401  (re-export)
    ALL_KNOWN_TUPLES,
    CLOUD_RUN_SERVICE_TYPES,
    CONTROL_PLANE_BUCKET_SUFFIXES,
    CONTROL_PLANE_KMS_KEY_NAMES,
    CONTROL_PLANE_KMS_KEYRING_NAMES,
    CONTROL_PLANE_SA_ACCOUNT_IDS,
    CONTROL_PLANE_SECRET_IDS,
    CONTROL_PLANE_SERVICE_NAMES,
    DELETE_ACTION_TUPLES,
    DenylistInput,
    FORGET_ACTION_TUPLES,
    IAM_EXTRA_TYPES,
    MUTATION_KNOWN_TUPLES,
    NO_OP_ACTION_TUPLES,
    REPLACE_ACTION_TUPLES,
    RULE_DESCRIPTIONS,
    Violation,
    WIF_RESOURCE_TYPES,
    evaluate,
    load_plan_json,
)


def _main(argv: list[str]) -> int:
    """CLI entrypoint: read a plan.json file, evaluate, print, exit.

    Returns 0 (pass), 1 (violations), or 2 (usage / I/O error). The
    library functions never raise on policy concerns — both
    ``load_plan_json`` and ``evaluate`` return :class:`Violation`
    records, which the CLI flattens to "DENY [rule] detail" lines on
    stderr.
    """
    if not argv:
        print("usage: python -m tools.iac_plan_denylist <plan.json>", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(
        prog="python -m tools.iac_plan_denylist",
        description=(
            "Self-protection denylist for OpenTofu plan.json (design doc 5.2). "
            "Exits 0 on pass, 1 on violations, 2 on usage/IO error."
        ),
    )
    parser.add_argument("plan_json", help="Path to plan.json")
    ns = parser.parse_args(argv)
    try:
        with open(ns.plan_json, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as e:
        print(f"error: cannot read {ns.plan_json}: {e}", file=sys.stderr)
        return 2
    parsed, parse_violation = load_plan_json(text)
    if parsed is None:
        assert parse_violation is not None  # narrowing for type checkers
        print(f"DENY [{parse_violation.rule}] {parse_violation.detail}", file=sys.stderr)
        return 1
    violations = evaluate(DenylistInput(plan=parsed))
    if not violations:
        print(f"OK: {ns.plan_json} - 0 violations")
        return 0
    for v in violations:
        print(f"DENY [{v.rule}] {v.detail}", file=sys.stderr)
    print(
        f"FAIL: {ns.plan_json} - {len(violations)} violation(s)",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":  # pragma: no cover (covered by CLI subprocess tests)
    sys.exit(_main(sys.argv[1:]))
