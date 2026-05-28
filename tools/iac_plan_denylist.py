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


# ---------------------------------------------------------------------------
# Control-plane identity constants. Names below were re-derived from
# infra/cloudbuild.yaml, infra/scripts/setup_prod_project.sh, and
# infra/scripts/setup_secrets.sh at the worktree's main HEAD; forward-compat
# entries (services/SAs not yet built — tofu-apply, tofu-editor,
# driftscribe-plan-builder, plus their SAs) are intentionally listed so the
# denylist already protects them by the time C2/C4 spin them up.
# ---------------------------------------------------------------------------

# Both the v2 and legacy v1 Cloud Run resource types are matched: the static
# gate doesn't ban the legacy type (Codex Important #3), so the denylist must
# guard it explicitly or an agent could redirect a control-plane service via
# the legacy resource without ever tripping the gate.
CLOUD_RUN_SERVICE_TYPES: frozenset[str] = frozenset({
    "google_cloud_run_v2_service",
    "google_cloud_run_service",  # legacy v1 — explicitly covered
})

CONTROL_PLANE_SERVICE_NAMES: frozenset[str] = frozenset({
    "driftscribe-agent",            # coordinator
    "driftscribe-reader",           # drift reader (distinct from infra-reader)
    "driftscribe-docs",
    "driftscribe-rollback",
    "driftscribe-notifier",
    "driftscribe-upgrade-reader",
    "driftscribe-upgrade-docs",
    "driftscribe-infra-reader",     # Phase B
    # Forward-compat (additive-safe — names listed before they exist):
    "tofu-apply",
    "tofu-editor",
    "driftscribe-plan-builder",
})

# google_service_account account_ids (or the local part of `email` when
# account_id is absent — `<aid>@<proj>.iam.gserviceaccount.com`). A SA whose
# google_service_account RC matches one of these emits BOTH `control-plane-sa`
# AND `iam-change-forbidden-v1` — intentional defense in depth: if a later
# phase relaxes the blanket IAM rule, the control-plane-sa rule remains.
CONTROL_PLANE_SA_ACCOUNT_IDS: frozenset[str] = frozenset({
    "driftscribe-agent",        # coordinator SA
    "reader-agent-sa",
    "docs-agent-sa",
    "rollback-agent-sa",
    "notifier-agent-sa",
    "upgrade-reader-sa",
    "upgrade-docs-sa",
    "infra-reader-sa",          # Phase B
    "tofu-plan-builder",        # Phase A WIF CI SA
    "eventarc-trigger-sa",
    # Forward-compat:
    "tofu-apply-sa",
    "tofu-editor-sa",
})


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


def _identity_dicts(rc: dict) -> tuple[dict, dict]:
    """Return ``(before_dict, after_dict)`` from an RC, each possibly empty.

    Both ``before`` and ``after`` may legitimately be ``null`` (create vs
    delete) or be partly null because attributes are computed. The helpers
    coerce non-dict values to empty dicts so the per-rule callers can use
    ``.get(key)`` without isinstance checks at every callsite.
    """
    change = rc.get("change") or {}
    before = change.get("before")
    after = change.get("after")
    return (
        before if isinstance(before, dict) else {},
        after if isinstance(after, dict) else {},
    )


def _check_control_plane_service(
    rc: dict,
    rtype: str,
    actions: tuple[str, ...],
    before: dict,
    after: dict,
    violations: list[Violation],
) -> None:
    """Emit control-plane-service if RC targets a protected Cloud Run service.

    Identity is matched against BOTH ``before.name`` and ``after.name`` so
    a rename AWAY from a protected name cannot escape the rule by leaving
    only ``after.name`` non-protected (Codex Important #2). If neither side
    has a string name, the resource type is protected but unidentifiable —
    defensive bias-to-deny via plan-json-malformed-change.
    """
    if rtype not in CLOUD_RUN_SERVICE_TYPES:
        return
    before_name = before.get("name") if isinstance(before, dict) else None
    after_name = after.get("name") if isinstance(after, dict) else None
    if not isinstance(before_name, str) and not isinstance(after_name, str):
        violations.append(
            Violation(
                "plan-json-malformed-change",
                f"{rc.get('address', '<unknown>')}: {rtype} has no name in before/after",
            )
        )
        return
    if (before_name in CONTROL_PLANE_SERVICE_NAMES) or (
        after_name in CONTROL_PLANE_SERVICE_NAMES
    ):
        violations.append(
            Violation(
                "control-plane-service",
                f"{rc.get('address', '<unknown>')}: Cloud Run service "
                f"{(after_name or before_name)!r} is control plane "
                f"(actions={list(actions)})",
            )
        )


def _sa_account_id(identity: dict) -> str | None:
    """Extract a SA's account_id from a before/after dict.

    Prefers the explicit ``account_id`` field; falls back to the local
    part of ``email`` (everything before ``@``) when ``account_id`` is
    absent — Codex Blocker #2 / Important #1: real plan.json sometimes
    carries only ``email`` after the SA is fully realized.
    """
    if not isinstance(identity, dict):
        return None
    aid = identity.get("account_id")
    if isinstance(aid, str):
        return aid
    email = identity.get("email")
    if isinstance(email, str) and "@" in email:
        return email.split("@", 1)[0]
    return None


def _check_control_plane_sa(
    rc: dict,
    rtype: str,
    actions: tuple[str, ...],
    before: dict,
    after: dict,
    violations: list[Violation],
) -> None:
    """Emit control-plane-sa if RC targets a protected service account.

    Matches on EITHER ``before`` or ``after`` identity (rename-away
    coverage, same as the service rule). If a google_service_account RC
    carries no account_id/email in either direction, defensive
    plan-json-malformed-change.

    NB: this rule fires alongside ``iam-change-forbidden-v1`` (added in
    Task 7) because google_service_account is also an IAM-identity type.
    """
    if rtype != "google_service_account":
        return
    bid = _sa_account_id(before)
    aid = _sa_account_id(after)
    if bid is None and aid is None:
        violations.append(
            Violation(
                "plan-json-malformed-change",
                f"{rc.get('address', '<unknown>')}: google_service_account has no account_id/email",
            )
        )
        return
    if (bid in CONTROL_PLANE_SA_ACCOUNT_IDS) or (aid in CONTROL_PLANE_SA_ACCOUNT_IDS):
        violations.append(
            Violation(
                "control-plane-sa",
                f"{rc.get('address', '<unknown>')}: SA "
                f"{(aid or bid)!r} is control plane (actions={list(actions)})",
            )
        )


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
    for rc, rtype, actions in _iter_resource_changes(di.plan):
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
        # Identity-based per-resource rules only run for mutations; a `read`
        # data-source on a control-plane name is a legitimate no-op
        # (see read_action_is_pass fixture).
        if _is_mutation(actions):
            before, after = _identity_dicts(rc)
            _check_control_plane_service(rc, rtype, actions, before, after, violations)
            _check_control_plane_sa(rc, rtype, actions, before, after, violations)
    return violations
