"""Self-protection denylist for DriftScribe IaC plan.json (design doc §5.2).

Pure, deterministic, fail-closed policy over an OpenTofu
``tofu show -json plan.tfplan`` document. Refuses any non-no-op change
targeting the DriftScribe control plane (Cloud Run services + their SAs,
state/artifact buckets and their objects, control-plane secrets and their
versions, the plan-builder KMS key + key ring), any IAM/WIF change, and
any state-mutating action (``delete`` / ``forget`` / replace) — even on
unrelated resources, in v1.

Task 3 adds :func:`load_plan_json` + the three structural rules
(``plan-json-unparseable``, ``plan-json-missing-resource-changes``,
``plan-json-malformed-change``); per-resource and per-action rules land
in subsequent tasks.
"""
from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Violation:
    """A single denylist violation.

    ``rule`` is a short machine identifier (one of the 14 rule IDs listed
    in the C1 plan §3); ``detail`` is a human-readable message that names
    the offending resource address + action tuple.
    """

    rule: str
    detail: str


@dataclass(frozen=True)
class DenylistInput:
    """Input to :func:`evaluate` — the parsed plan.json dict."""

    plan: dict


def load_plan_json(text: str) -> tuple[dict | None, Violation | None]:
    """Parse a plan.json document.

    Returns ``(parsed_dict, None)`` on success or
    ``(None, Violation("plan-json-unparseable", ...))`` on failure (bad
    JSON, or top-level not an object). Never raises — the producer (CI
    plan-builder in C2 or the apply worker in C4) hands raw bytes to this
    helper and expects a parse-side Violation to be treated as deny.
    """
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        return None, Violation("plan-json-unparseable", f"json decode error: {e}")
    if not isinstance(parsed, dict):
        return None, Violation(
            "plan-json-unparseable",
            f"root is {type(parsed).__name__}, expected object",
        )
    return parsed, None


def evaluate(di: DenylistInput) -> list[Violation]:
    """Return all violations (empty list = pass).

    Fail-closed: any structural surprise in the plan-JSON yields a
    :class:`Violation` rather than an exception. The library never raises
    on policy concerns; only genuine programming errors bubble up.
    """
    violations: list[Violation] = []
    rcs = di.plan.get("resource_changes")
    if not isinstance(rcs, list):
        violations.append(
            Violation(
                "plan-json-missing-resource-changes",
                f"resource_changes is {type(rcs).__name__}, expected list",
            )
        )
        return violations
    for rc in rcs:
        if not isinstance(rc, dict):
            violations.append(
                Violation(
                    "plan-json-malformed-change",
                    f"resource_changes entry is {type(rc).__name__}, expected object",
                )
            )
            continue
        change = rc.get("change")
        if not isinstance(change, dict):
            address = rc.get("address", "<unknown>")
            violations.append(
                Violation(
                    "plan-json-malformed-change",
                    f"{address}: change is {type(change).__name__}, expected object",
                )
            )
            continue
    return violations
