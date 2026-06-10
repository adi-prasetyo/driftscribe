"""Self-protection denylist for DriftScribe IaC plan.json (design doc §5.2).

Pure, deterministic, fail-closed policy over an OpenTofu
``tofu show -json plan.tfplan`` document. Refuses any non-no-op change
targeting the DriftScribe control plane (Cloud Run services + their SAs,
state/artifact buckets and their objects, control-plane secrets and their
versions, the plan-builder KMS key + key ring), any IAM/WIF change, and
any state-mutating action (``delete`` / ``forget`` / replace) — even on
unrelated resources, in v1.

This is the canonical, lib-owned definition (Phase C4 promoted it out of
``tools/`` — which is not an installed package and is not shipped in worker
containers — so the ``tofu-apply`` worker can import + re-run it at runtime
along the established ``tools -> lib`` dependency direction, exactly like the
C3 ``iac_plan_metadata`` promotion). It runs in three places: the
``tools.iac_plan_denylist`` CLI (re-export shim) for local-dev validation, the
trusted plan-builder CI workflow (wired in C2), and the ``tofu-apply`` worker
which re-runs the denylist against the same ``plan.json`` immediately before
``tofu apply`` (wired in C4). Each call site supplies the parsed plan dict (or
raw JSON text via :func:`load_plan_json`) and treats any non-empty result as
deny.

**v1 floor.** The rule set is intentionally over-inclusive: hard-deny
*all* IAM changes (even on unrelated resources), hard-deny *all*
``delete``/``forget``/replace actions. A positive allowlist is a later-
phase decision; the v1 false-positive trade-off (e.g. a clean IAM grant
on a payment-demo bucket is also denied) is accepted to keep the gate
defensible until the C3 human-approval flow lands.

**Rule IDs (14)**:

- ``plan-json-unparseable`` — bad JSON or top-level not an object.
- ``plan-json-missing-resource-changes`` — key missing OR not a list.
- ``plan-json-malformed-change`` — entry / change / type / actions are
  missing or wrong-typed; OR a protected resource type lacks an
  identity field in BOTH ``before`` and ``after`` (defensive bias-to-
  deny — see "Identity matching" below).
- ``control-plane-service`` — non-no-op change to a Cloud Run service
  in the protected set (v2 OR legacy v1 resource type).
- ``control-plane-sa`` — non-no-op change to a control-plane SA
  (matched on ``account_id`` OR the local part of ``email``).
- ``control-plane-bucket`` — non-no-op change to a ``-tofu-state`` /
  ``-tofu-artifacts`` bucket or an OBJECT inside one.
- ``control-plane-secret`` — non-no-op change to a protected secret
  (matched on ``secret_id``) or one of its versions (parent id
  extracted from the resource path).
- ``control-plane-kms`` — non-no-op change to the ``tofu-state`` crypto
  key or the ``driftscribe-tofu`` key ring.
- ``wif-config-change`` — non-no-op change to a WIF pool or provider
  (always dual-emits with iam-change-forbidden-v1).
- ``iam-change-forbidden-v1`` — non-no-op change to any IAM resource
  type (``startswith("google_") and "_iam_" in rtype`` OR membership
  in :data:`IAM_EXTRA_TYPES`).
- ``delete-action-forbidden-v1`` — ``actions == ["delete"]``.
- ``forget-action-forbidden-v1`` — ``actions == ["forget"]``.
- ``replace-action-forbidden-v1`` — ``actions in (["delete","create"],
  ["create","delete"])``.
- ``unknown-action-forbidden-v1`` — ``actions`` tuple not in the
  documented OpenTofu vocabulary.

**Identity matching.** Per-resource rules check identity from BOTH
``change.before`` and ``change.after`` — a rename AWAY from a protected
name still fires. A protected resource type with no identity in either
side emits ``plan-json-malformed-change`` (defensive bias-to-deny: an
unknown identity on a protected type could hide a protected suffix).

**C4 worker contract.** Callers from the ``tofu-apply`` worker MUST:

1. ``parsed, parse_v = load_plan_json(text)``
2. If ``parse_v is not None``: treat as deny (the raw artifact is
   either corrupt or not a JSON object).
3. ``violations = evaluate(DenylistInput(plan=parsed))``
4. If ``violations`` is non-empty: treat as deny (do NOT apply).

The library NEVER raises on policy or structural concerns. Genuine
programming errors (e.g. passing a non-string to an internal helper)
can still bubble up, but the C4 worker should surround the calls with
a broad except to convert any such bug into a deny anyway.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Mapping

__all__ = ["Violation", "DenylistInput", "load_plan_json", "evaluate", "RULE_DESCRIPTIONS"]


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
REPLACE_ACTION_TUPLES: frozenset[tuple[str, ...]] = frozenset(
    {
        ("delete", "create"),
        ("create", "delete"),
    }
)
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
CLOUD_RUN_SERVICE_TYPES: frozenset[str] = frozenset(
    {
        "google_cloud_run_v2_service",
        "google_cloud_run_service",  # legacy v1 — explicitly covered
    }
)

CONTROL_PLANE_SERVICE_NAMES: frozenset[str] = frozenset(
    {
        "driftscribe-agent",  # coordinator
        "driftscribe-reader",  # drift reader (distinct from infra-reader)
        "driftscribe-docs",
        "driftscribe-rollback",
        "driftscribe-notifier",
        "driftscribe-upgrade-reader",
        "driftscribe-upgrade-docs",
        "driftscribe-infra-reader",  # Phase B
        # C4: the apply worker's OWN service — the real deployed name follows
        # the driftscribe- convention (the bare tofu-apply below was a C1
        # forward-compat placeholder). The worker re-runs THIS denylist on its
        # own fetched plan, so self-management of the mutator is hard-denied.
        "driftscribe-tofu-apply",
        "driftscribe-tofu-editor",  # forward-compat (Phase D editor worker)
        # Forward-compat placeholders (additive-safe — kept as harmless aliases):
        "tofu-apply",
        "tofu-editor",
        "driftscribe-plan-builder",
    }
)

# google_service_account account_ids (or the local part of `email` when
# account_id is absent — `<aid>@<proj>.iam.gserviceaccount.com`). A SA whose
# google_service_account RC matches one of these emits BOTH `control-plane-sa`
# AND `iam-change-forbidden-v1` — intentional defense in depth: if a later
# phase relaxes the blanket IAM rule, the control-plane-sa rule remains.
CONTROL_PLANE_SA_ACCOUNT_IDS: frozenset[str] = frozenset(
    {
        "driftscribe-agent",  # coordinator SA
        "reader-agent-sa",
        "docs-agent-sa",
        "rollback-agent-sa",
        "notifier-agent-sa",
        "upgrade-reader-sa",
        "upgrade-docs-sa",
        "infra-reader-sa",  # Phase B
        "tofu-plan-builder",  # Phase A WIF CI SA
        "eventarc-trigger-sa",
        # Forward-compat:
        "tofu-apply-sa",
        "tofu-editor-sa",
    }
)

# A bucket whose name ends with either suffix is the OpenTofu state bucket or
# the plan-artifact bucket: any mutation to it (or to an OBJECT inside it —
# Codex Blocker #5) redirects the IaC backend or smuggles a payload into the
# trusted artifact store, so both must be denied even on a green PR.
CONTROL_PLANE_BUCKET_SUFFIXES: tuple[str, ...] = ("-tofu-state", "-tofu-artifacts")

# Operational secrets that the denylist protects. Per Codex Important #4 the
# v1 list is intentionally broader than just the design-mandated HMAC keys —
# bias toward "any operational secret should not be redirected by IaC without
# explicit design intent". plan-hmac-key is forward-compat for the C3 approval
# flow; the github-pat trio backs the worker GitHub clients.
CONTROL_PLANE_SECRET_IDS: frozenset[str] = frozenset(
    {
        "approval-hmac-key",
        "coordinator-shared-token",
        "plan-hmac-key",  # forward-compat (C3)
        "github-pat",
        "docs-agent-github-pat",
        "upgrade-reader-github-pat",
        "upgrade-docs-github-pat",
        "developer-knowledge-api-key",
        "driftscribe-webhook-url",
        "tofu-editor-github-pat",  # forward-compat (Phase D editor worker PAT)
    }
)

# KMS resources that back the OpenTofu state encryption. Names match the Phase A
# bootstrap key + its containing ring (setup_iac_backend.sh).
CONTROL_PLANE_KMS_KEY_NAMES: frozenset[str] = frozenset({"tofu-state"})
CONTROL_PLANE_KMS_KEYRING_NAMES: frozenset[str] = frozenset({"driftscribe-tofu"})

# WIF resource types — explicit set used by both the WIF rule (single-emit)
# AND the IAM rule (dual-emit). Listed once and reused so the two rule
# definitions never drift apart.
WIF_RESOURCE_TYPES: frozenset[str] = frozenset(
    {
        "google_iam_workload_identity_pool",
        "google_iam_workload_identity_pool_provider",
    }
)

# IAM-identity resource types that do NOT carry `_iam_` in their name and so
# wouldn't be caught by the substring rule. Per Codex Blocker #4 the general
# rule is "starts with google_ AND contains _iam_" (covers project_iam_binding,
# storage_bucket_iam_member, run_v2_service_iam_member, folder_iam_binding,
# kms_*_iam_*, etc.) PLUS this explicit extras set.
IAM_EXTRA_TYPES: frozenset[str] = (
    frozenset(
        {
            "google_service_account",
            "google_service_account_key",
            "google_project_iam_audit_config",
            "google_project_iam_custom_role",
            "google_organization_iam_custom_role",
        }
    )
    | WIF_RESOURCE_TYPES
)


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
    if (before_name in CONTROL_PLANE_SERVICE_NAMES) or (after_name in CONTROL_PLANE_SERVICE_NAMES):
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


def _is_protected_bucket_name(name: object) -> bool:
    """True iff ``name`` is a string ending with a control-plane bucket suffix."""
    return isinstance(name, str) and any(name.endswith(s) for s in CONTROL_PLANE_BUCKET_SUFFIXES)


def _check_control_plane_bucket(
    rc: dict,
    rtype: str,
    actions: tuple[str, ...],
    before: dict,
    after: dict,
    violations: list[Violation],
) -> None:
    """Emit control-plane-bucket on a non-no-op change to a protected bucket
    OR to an OBJECT inside a protected bucket.

    Writing an object INTO the -tofu-state bucket is functionally a state
    mutation; writing one INTO the -tofu-artifacts bucket smuggles into the
    trusted artifact store the plan-builder reads. Both are denied.
    """
    if rtype == "google_storage_bucket":
        before_name = before.get("name")
        after_name = after.get("name")
        if not isinstance(before_name, str) and not isinstance(after_name, str):
            violations.append(
                Violation(
                    "plan-json-malformed-change",
                    f"{rc.get('address', '<unknown>')}: google_storage_bucket has no name",
                )
            )
            return
        if _is_protected_bucket_name(before_name) or _is_protected_bucket_name(after_name):
            violations.append(
                Violation(
                    "control-plane-bucket",
                    f"{rc.get('address', '<unknown>')}: protected bucket "
                    f"{(after_name or before_name)!r} (actions={list(actions)})",
                )
            )
    elif rtype == "google_storage_bucket_object":
        before_bucket = before.get("bucket")
        after_bucket = after.get("bucket")
        if not isinstance(before_bucket, str) and not isinstance(after_bucket, str):
            violations.append(
                Violation(
                    "plan-json-malformed-change",
                    f"{rc.get('address', '<unknown>')}: google_storage_bucket_object has no bucket",
                )
            )
            return
        if _is_protected_bucket_name(before_bucket) or _is_protected_bucket_name(after_bucket):
            violations.append(
                Violation(
                    "control-plane-bucket",
                    f"{rc.get('address', '<unknown>')}: object in protected bucket "
                    f"{(after_bucket or before_bucket)!r} (actions={list(actions)})",
                )
            )


def _secret_id_from_version_path(value: object) -> str | None:
    """Extract a secret id from a secret_version resource path.

    OpenTofu emits secret_version ``secret`` / ``name`` attributes as
    ``projects/<p>/secrets/<id>`` or
    ``projects/<p>/secrets/<id>/versions/<n>``. We split on ``/secrets/``
    and take the next path segment up to the next ``/``. Returns ``None``
    if the input is not a string or does not contain that segment.
    """
    if not isinstance(value, str) or "/secrets/" not in value:
        return None
    rest = value.split("/secrets/", 1)[1]
    head = rest.split("/", 1)[0]
    return head or None


def _check_control_plane_secret(
    rc: dict,
    rtype: str,
    actions: tuple[str, ...],
    before: dict,
    after: dict,
    violations: list[Violation],
) -> None:
    """Emit control-plane-secret on protected secret OR secret_version changes.

    For ``google_secret_manager_secret`` the identity is ``secret_id``.
    For ``google_secret_manager_secret_version`` the identity is the
    parent secret id extracted from the resource path in ``secret`` or
    ``name``. Either before- or after-side match is sufficient.
    """
    if rtype == "google_secret_manager_secret":
        bid = before.get("secret_id")
        aid = after.get("secret_id")
        if not isinstance(bid, str) and not isinstance(aid, str):
            violations.append(
                Violation(
                    "plan-json-malformed-change",
                    f"{rc.get('address', '<unknown>')}: google_secret_manager_secret has no secret_id",
                )
            )
            return
        if (bid in CONTROL_PLANE_SECRET_IDS) or (aid in CONTROL_PLANE_SECRET_IDS):
            violations.append(
                Violation(
                    "control-plane-secret",
                    f"{rc.get('address', '<unknown>')}: secret "
                    f"{(aid or bid)!r} (actions={list(actions)})",
                )
            )
    elif rtype == "google_secret_manager_secret_version":
        before_id = _secret_id_from_version_path(
            before.get("secret")
        ) or _secret_id_from_version_path(before.get("name"))
        after_id = _secret_id_from_version_path(
            after.get("secret")
        ) or _secret_id_from_version_path(after.get("name"))
        if before_id is None and after_id is None:
            violations.append(
                Violation(
                    "plan-json-malformed-change",
                    f"{rc.get('address', '<unknown>')}: secret_version path has no /secrets/<id>",
                )
            )
            return
        if (before_id in CONTROL_PLANE_SECRET_IDS) or (after_id in CONTROL_PLANE_SECRET_IDS):
            violations.append(
                Violation(
                    "control-plane-secret",
                    f"{rc.get('address', '<unknown>')}: version of "
                    f"{(after_id or before_id)!r} (actions={list(actions)})",
                )
            )


def _check_control_plane_kms(
    rc: dict,
    rtype: str,
    actions: tuple[str, ...],
    before: dict,
    after: dict,
    violations: list[Violation],
) -> None:
    """Emit control-plane-kms on a protected KMS key or key-ring change.

    Both google_kms_crypto_key (name in CONTROL_PLANE_KMS_KEY_NAMES) and
    google_kms_key_ring (name in CONTROL_PLANE_KMS_KEYRING_NAMES) match;
    other resource types are ignored.
    """
    if rtype == "google_kms_crypto_key":
        protected_names = CONTROL_PLANE_KMS_KEY_NAMES
    elif rtype == "google_kms_key_ring":
        protected_names = CONTROL_PLANE_KMS_KEYRING_NAMES
    else:
        return
    before_name = before.get("name")
    after_name = after.get("name")
    if not isinstance(before_name, str) and not isinstance(after_name, str):
        violations.append(
            Violation(
                "plan-json-malformed-change",
                f"{rc.get('address', '<unknown>')}: {rtype} has no name",
            )
        )
        return
    if (before_name in protected_names) or (after_name in protected_names):
        violations.append(
            Violation(
                "control-plane-kms",
                f"{rc.get('address', '<unknown>')}: protected KMS resource "
                f"{(after_name or before_name)!r} (actions={list(actions)})",
            )
        )


def _is_iam_type(rtype: str) -> bool:
    """True iff ``rtype`` is an IAM-related resource type under v1 policy.

    The rule is intentionally over-inclusive (Codex Blocker #4): the
    substring check catches every ``google_<x>_iam_<member|binding|policy>``
    shape across project, service_account, storage_bucket, kms_*, secret,
    run_v2_service, pubsub_topic, folder, organization, etc., and the
    explicit IAM_EXTRA_TYPES set catches identity-side types that lack the
    ``_iam_`` substring (google_service_account, IAM custom roles, WIF).
    """
    if rtype in IAM_EXTRA_TYPES:
        return True
    return rtype.startswith("google_") and "_iam_" in rtype


def _check_wif(
    rc: dict,
    rtype: str,
    actions: tuple[str, ...],
    violations: list[Violation],
) -> None:
    """Emit wif-config-change for WIF pool / provider resources."""
    if rtype in WIF_RESOURCE_TYPES:
        violations.append(
            Violation(
                "wif-config-change",
                f"{rc.get('address', '<unknown>')}: WIF {rtype!r} (actions={list(actions)})",
            )
        )


def _check_iam(
    rc: dict,
    rtype: str,
    actions: tuple[str, ...],
    violations: list[Violation],
) -> None:
    """Emit iam-change-forbidden-v1 for ANY IAM-related resource type.

    This rule is the v1 floor — every IAM change is denied even on
    unrelated resources. A positive IAM allowlist is a later-phase
    decision (design §5.2). Intentional defense-in-depth: WIF and
    google_service_account types ALSO trip more specific rules; if a
    later phase relaxes this blanket rule, those specific rules remain.
    """
    if _is_iam_type(rtype):
        violations.append(
            Violation(
                "iam-change-forbidden-v1",
                f"{rc.get('address', '<unknown>')}: IAM {rtype!r} "
                f"(actions={list(actions)}) - v1 hard-deny",
            )
        )


def evaluate(di: DenylistInput) -> list[Violation]:
    """Return all violations (empty list = pass).

    Fail-closed: any **policy-relevant** structural surprise in the plan-
    JSON (missing/wrong-typed fields the rules rely on, or a protected
    resource type with no identity in either ``before`` or ``after``)
    yields a :class:`Violation` rather than an exception. The library
    never raises on policy concerns; only genuine programming errors
    bubble up. Plan-JSON fields the rules do not consume are not
    inspected and cannot cause a denial.
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
            _check_control_plane_bucket(rc, rtype, actions, before, after, violations)
            _check_control_plane_secret(rc, rtype, actions, before, after, violations)
            _check_control_plane_kms(rc, rtype, actions, before, after, violations)
            _check_wif(rc, rtype, actions, violations)
            _check_iam(rc, rtype, actions, violations)
    return violations


# Operator-facing descriptions for every rule ID this module can emit.
# Serialized by the coordinator's GET /capabilities (the operator UI's
# capability card). Keyed by the EXACT Violation(...) first-arg literals —
# tests/unit/test_denylist_rule_descriptions.py extracts those literals via
# AST and pins set equality, so adding/removing a rule without updating
# this mapping fails CI. Descriptions are plain language for operators who
# do not read HCL; keep them honest and specific.
RULE_DESCRIPTIONS: Final[Mapping[str, str]] = MappingProxyType({
    "plan-json-unparseable": (
        "The plan file is not valid JSON — rejected outright (fail-closed)."
    ),
    "plan-json-missing-resource-changes": (
        "The plan has no resource-changes list — rejected outright (fail-closed)."
    ),
    "plan-json-malformed-change": (
        "A change entry is malformed, or a protected resource hides its "
        "identity — rejected outright (fail-closed)."
    ),
    "control-plane-service": (
        "No change may touch DriftScribe's own Cloud Run services."
    ),
    "control-plane-sa": (
        "No change may touch DriftScribe's own service accounts."
    ),
    "control-plane-bucket": (
        "No change may touch the IaC state or artifact buckets, or any "
        "object inside them."
    ),
    "control-plane-secret": (
        "No change may touch DriftScribe's secrets (approval keys, GitHub "
        "token, …) or any of their versions."
    ),
    "control-plane-kms": (
        "No change may touch the state-encryption KMS key or its key ring."
    ),
    "wif-config-change": (
        "No change may touch Workload Identity Federation pools or providers."
    ),
    "iam-change-forbidden-v1": (
        "All IAM changes are refused — even on unrelated resources (v1 floor)."
    ),
    "delete-action-forbidden-v1": (
        "All deletes are refused — the agent cannot destroy any resource "
        "(v1 floor)."
    ),
    "forget-action-forbidden-v1": (
        "All state-forget actions are refused (v1 floor)."
    ),
    "replace-action-forbidden-v1": (
        "All replacements (destroy-and-recreate) are refused (v1 floor)."
    ),
    "unknown-action-forbidden-v1": (
        "Any action shape not in the audited OpenTofu vocabulary is refused "
        "(fail-closed against new verbs)."
    ),
})
