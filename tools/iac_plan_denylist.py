"""Self-protection denylist for DriftScribe IaC plan.json (design doc §5.2).

Pure, deterministic, fail-closed policy over an OpenTofu
``tofu show -json plan.tfplan`` document. Refuses any non-no-op change
targeting the DriftScribe control plane (Cloud Run services + their SAs,
state/artifact buckets and their objects, control-plane secrets and their
versions, the plan-builder KMS key + key ring), any IAM/WIF change, and
any state-mutating action (``delete`` / ``forget`` / replace) — even on
unrelated resources, in v1.

Task 3 added :func:`load_plan_json` + the three structural rules
(``plan-json-unparseable``, ``plan-json-missing-resource-changes``,
``plan-json-malformed-change``). Task 4 adds the OpenTofu action
vocabulary check (``unknown-action-forbidden-v1``) and the
:func:`_iter_resource_changes` helper that subsequent rule evaluators
share. Per-resource identity rules land in subsequent tasks.
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


# OpenTofu action vocabulary, verified against the JSON-output-format docs.
# Anything not in here is "unknown" and emits ``unknown-action-forbidden-v1``
# (defense-in-depth against a future OpenTofu version emitting a new action
# shape we haven't audited).
NO_OP_ACTION_TUPLES: frozenset[tuple[str, ...]] = frozenset({("no-op",), ("read",)})
DELETE_ACTION_TUPLES: frozenset[tuple[str, ...]] = frozenset({("delete",)})
FORGET_ACTION_TUPLES: frozenset[tuple[str, ...]] = frozenset({("forget",)})
REPLACE_ACTION_TUPLES: frozenset[tuple[str, ...]] = frozenset({
    ("delete", "create"),
    ("create", "delete"),
})
MUTATION_KNOWN_TUPLES: frozenset[tuple[str, ...]] = (
    frozenset({("create",), ("update",)})
    | DELETE_ACTION_TUPLES
    | FORGET_ACTION_TUPLES
    | REPLACE_ACTION_TUPLES
)
ALL_KNOWN_TUPLES: frozenset[tuple[str, ...]] = NO_OP_ACTION_TUPLES | MUTATION_KNOWN_TUPLES


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


def _iter_resource_changes(plan: dict):
    """Yield ``(rc, rtype, actions_tuple_or_None)`` per resource_changes entry.

    ``actions_tuple_or_None is None`` signals "malformed entry" — the
    caller emits ``plan-json-malformed-change`` and skips downstream
    per-rule checks for that entry. A well-formed entry yields its action
    tuple even if that tuple is not in the OpenTofu vocabulary (the
    unknown-action rule handles that case in the main loop).

    This helper centralizes the malformed-detection logic so the main
    loop and the future per-rule evaluators share the same idea of
    "well-formed-enough to inspect".
    """
    for rc in plan.get("resource_changes", []) or []:
        if not isinstance(rc, dict):
            yield rc, None, None
            continue
        rtype = rc.get("type")
        change = rc.get("change") if isinstance(rc.get("change"), dict) else None
        if change is None or not isinstance(rtype, str):
            yield rc, rtype if isinstance(rtype, str) else None, None
            continue
        actions = change.get("actions")
        if not isinstance(actions, list) or not all(isinstance(a, str) for a in actions):
            yield rc, rtype, None
            continue
        yield rc, rtype, tuple(actions)


def _is_mutation(actions: tuple[str, ...]) -> bool:
    """True iff ``actions`` represents a state mutation (not no-op/read)."""
    return actions not in NO_OP_ACTION_TUPLES


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
    for rc, _rtype, actions in _iter_resource_changes(di.plan):
        address = rc.get("address", "<unknown>") if isinstance(rc, dict) else "<unknown>"
        if actions is None:
            # Either the entry is not a dict, the change is not a dict, the
            # type is missing/non-str, or actions is missing/non-list/has a
            # non-string element — all collapse to one structural rule.
            detail = (
                f"resource_changes entry is {type(rc).__name__}, expected object"
                if not isinstance(rc, dict)
                else f"{address}: missing or malformed type / change / change.actions"
            )
            violations.append(Violation("plan-json-malformed-change", detail))
            continue
        if actions not in ALL_KNOWN_TUPLES:
            violations.append(
                Violation(
                    "unknown-action-forbidden-v1",
                    f"{address}: action {list(actions)!r} not in OpenTofu vocabulary",
                )
            )
            continue
        # delete / forget / replace are v1 hard-deny floors regardless of
        # which resource they target. Fall through (do NOT continue) so the
        # identity-based rules added in later tasks can ALSO fire on the
        # same RC — multi_violations_sa_delete is the regression fixture.
        if actions in DELETE_ACTION_TUPLES:
            violations.append(
                Violation(
                    "delete-action-forbidden-v1",
                    f"{address}: action {list(actions)!r} forbidden in v1",
                )
            )
        if actions in FORGET_ACTION_TUPLES:
            violations.append(
                Violation(
                    "forget-action-forbidden-v1",
                    f"{address}: action {list(actions)!r} forbidden in v1",
                )
            )
        if actions in REPLACE_ACTION_TUPLES:
            violations.append(
                Violation(
                    "replace-action-forbidden-v1",
                    f"{address}: action {list(actions)!r} (replace) forbidden in v1",
                )
            )
    return violations
