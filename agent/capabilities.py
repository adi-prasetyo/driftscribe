"""Capability catalog — the agent's safety cage, serialized from enforcement constants.

``build_capabilities()`` assembles the DTO returned by ``GET /capabilities``
(Task 3). Every field it returns is derived from the SAME constants that
the enforcement code imports — never from hand-written documentation that
could silently drift. The coupling is verified by the drift-pin tests in
``tests/unit/test_capabilities.py``:

- ``test_tool_descriptions_cover_exactly_the_tool_registry`` — every key
  in ``TOOL_REGISTRY`` has a description and no stale entries exist.
- ``test_worker_descriptions_cover_exactly_the_worker_registry`` — same
  for ``WORKER_REGISTRY``.
- ``test_rule_categories_cover_exactly_the_rule_descriptions`` — every
  rule in ``RULE_DESCRIPTIONS`` has a category assignment.
- ``test_every_approval_gated_action_has_a_human_gate`` — every
  ``ACTION_REGISTRY`` entry with ``requires_approval=True`` appears in
  ``HUMAN_GATES``.
- ``test_chat_only_coherence_with_main`` — ``observation_kind == "none"``
  in the manifests agrees with ``CHAT_ONLY_WORKLOAD_NAMES`` in main.py.

Import direction: this module imports FROM ``agent.workloads.registry``,
``agent.workloads.spec``, ``agent.fanout``, and
``driftscribe_lib.iac_plan_denylist``. It MUST NOT import ``agent.main``
AT MODULE LOAD — main imports us, so a top-level import would cycle.
``build_capabilities()`` does take a single deliberate function-scope
import of ``agent.main.AUTONOMOUS_TRIGGER_WORKLOADS`` at call time (when
main is already fully loaded), mirroring the test side's lazy
``from agent.main import CHAT_ONLY_WORKLOAD_NAMES`` — see the comment at
the import site for why the autonomy signal is owned in main.
"""
from __future__ import annotations

import dataclasses
from types import MappingProxyType
from typing import Final, Mapping, get_args

from driftscribe_lib.iac_plan_denylist import RULE_DESCRIPTIONS, ADOPTABLE_RESOURCE_TYPES

from agent.fanout import MUTATION_TOOL_NAMES
from agent.workloads.registry import ACTION_REGISTRY
from agent.workloads.registry import load_workload_spec
from agent.workloads.spec import WorkloadSpec


# --------------------------------------------------------------------------- #
# WORKLOAD_NAMES — closed set, derived from the Literal in WorkloadSpec.name
# --------------------------------------------------------------------------- #

WORKLOAD_NAMES: Final[tuple[str, ...]] = get_args(
    WorkloadSpec.model_fields["name"].annotation
)
"""The ordered workload names, derived from the ``WorkloadSpec.name`` Literal.

The Literal IS the enumeration — we never hand-list workload names here.
Order follows the Literal declaration in ``agent/workloads/spec.py``
(currently ``("drift", "upgrade", "explore", "provision")``).

Pinned by ``test_build_capabilities_shape``: ``[w["name"] for w in dto["workloads"]]``
must equal ``list(WORKLOAD_NAMES)``.
"""


# --------------------------------------------------------------------------- #
# TOOL_DESCRIPTIONS — one entry per TOOL_REGISTRY key
# --------------------------------------------------------------------------- #

TOOL_DESCRIPTIONS: Final[Mapping[str, str]] = MappingProxyType({
    "drift_read_live_env": (
        "Reads the live Cloud Run environment: deployed image, revision, "
        "environment variables, and service configuration."
    ),
    "read_project_inventory": (
        "Reads a read-only whole-project asset inventory via the infra-reader "
        "worker (Cloud Asset Viewer only, no write access)."
    ),
    "drift_patch_docs": (
        "Updates the ops-contract documentation to record the current observed "
        "state after a drift detection run."
    ),
    "drift_propose_rollback": (
        "Proposes a rollback; never executes one. It creates an approval that "
        "waits for an operator."
    ),
    "notify": (
        "Sends a notification via the notifier worker (counted as write-capable "
        "because it rides a sending credential)."
    ),
    "load_contract": (
        "Loads the ops-contract YAML (the declarative ground truth) so the agent "
        "can compare it with observed state."
    ),
    "search_recent_prs": (
        "Searches the target repo's recent pull requests (counted as write-capable "
        "because it rides a repo credential)."
    ),
    "load_iac_plan": (
        "Reads the latest verified plan artifact for a pending infrastructure "
        "PR and summarizes it in plain language. Read-only: cannot approve, "
        "reject, or apply anything."
    ),
    "upgrade_read_dependencies": (
        "Reads the target repo's dependency lockfile to identify outdated packages."
    ),
    "upgrade_propose_pr": (
        "Opens a dependency-upgrade pull request in the target repo."
    ),
    "upgrade_close_pr": (
        "Closes an upgrade PR this agent opened, only when it is safe to do so "
        "(driftscribe label, upgrade/ branch, correct base)."
    ),
    "upgrade_merge_pr": (
        "Merges an upgrade PR this agent opened, only after CI is green on the "
        "exact head commit. Fails closed."
    ),
    "search_developer_docs": (
        "Searches the developer knowledge base for documentation relevant to the "
        "current task."
    ),
    "retrieve_developer_doc": (
        "Retrieves a specific document from the developer knowledge base by ID."
    ),
    "provision_open_infra_pr": (
        "Authors OpenTofu files under iac/ and opens ONE pull request. Never "
        "applies anything; applying happens only through the gated "
        "approve-then-apply pipeline."
    ),
    "provision_propose_adoption": (
        "Adopt an existing resource into IaC management via a zero-change "
        "import PR. Renders the config deterministically; cannot modify live "
        "infrastructure."
    ),
    "get_session_state": (
        "Reserved; not implemented. No workload can use it."
    ),
    "set_session_state": (
        "Reserved; not implemented. No workload can use it."
    ),
})
"""Operator-facing description of every tool in ``TOOL_REGISTRY``.

Keyed by the EXACT symbolic names in ``TOOL_REGISTRY`` — the drift-pin test
``test_tool_descriptions_cover_exactly_the_tool_registry`` in
``tests/unit/test_capabilities.py`` asserts set equality so a new tool cannot
ship without a description and a deleted tool cannot leave a stale entry.

Descriptions for tools in ``MUTATION_TOOL_NAMES`` that are there for credential
containment rather than direct mutation (``notify``, ``search_recent_prs``) must
say so explicitly — the ``write_capable`` field in the DTO carries the
``MUTATION_TOOL_NAMES`` membership, but the description is where the nuance lives.
"""


# --------------------------------------------------------------------------- #
# WORKER_DESCRIPTIONS — one entry per WORKER_REGISTRY key
# --------------------------------------------------------------------------- #

WORKER_DESCRIPTIONS: Final[Mapping[str, str]] = MappingProxyType({
    "drift_reader": (
        "Reads the live Cloud Run service state for drift detection. "
        "Read-only by the scope of calls it makes."
    ),
    "drift_docs": (
        "Patches the ops-contract documentation to record observed state."
    ),
    "drift_rollback": (
        "Executes a Cloud Run rollback to a previous revision. Refuses "
        "anything without a valid operator approval token."
    ),
    "infra_reader": (
        "Reads the whole-project GCP asset inventory. Read-only by IAM "
        "(asset viewer only)."
    ),
    "notifier": (
        "Sends notifications (e.g. Slack or webhook). Carries a sending "
        "credential."
    ),
    "upgrade_reader": (
        "Reads the target repo's dependency lockfile. Read-only by the "
        "scope of calls it makes."
    ),
    "upgrade_docs": (
        "Opens and manages upgrade pull requests in the target repo."
    ),
    "tofu_editor": (
        "Writes iac/-only files and opens PRs; never touches live "
        "infrastructure."
    ),
})
"""Operator-facing description of every worker in ``WORKER_REGISTRY``.

Keyed by the EXACT symbolic names in ``WORKER_REGISTRY`` — the drift-pin test
``test_worker_descriptions_cover_exactly_the_worker_registry`` asserts set
equality. Required nuances: ``infra_reader`` must mention read-only IAM;
``tofu_editor`` must clarify it only writes files and opens PRs; ``drift_rollback``
must mention the operator approval token requirement.
"""


# --------------------------------------------------------------------------- #
# CATEGORY_ORDER and RULE_CATEGORIES — denylist rule taxonomy
# --------------------------------------------------------------------------- #

ADOPTABLE_TYPE_LABELS: Final[Mapping[str, str]] = MappingProxyType({
    "google_storage_bucket":      "Cloud Storage bucket",
    "google_pubsub_topic":        "Pub/Sub topic",
    "google_pubsub_subscription": "Pub/Sub subscription",
    "google_cloud_run_v2_service": "Cloud Run service",
})
"""Human-readable label for every adoptable resource type in ``ADOPTABLE_RESOURCE_TYPES``.

Drift-pinned by ``test_adoptable_type_labels_cover_exactly_the_allowlist``:
``set(ADOPTABLE_TYPE_LABELS) == set(ADOPTABLE_RESOURCE_TYPES)``.
"""


CATEGORY_ORDER: Final[tuple[str, ...]] = (
    "control-plane",
    "service-managed",
    "iam",
    "global-v1",
    "structural",
)
"""Rendering order for denylist rule categories (anxiety-first).

Used both to sort rules in the DTO (``(CATEGORY_ORDER.index(cat), rule_id)``)
and to group them in the frontend. Verified by ``test_build_capabilities_shape``
which asserts full sort stability.
"""

RULE_CATEGORIES: Final[Mapping[str, str]] = MappingProxyType({
    # Structural (malformed plan rejected outright):
    "plan-json-unparseable":              "structural",
    "plan-json-missing-resource-changes": "structural",
    "plan-json-malformed-change":         "structural",
    # Control-plane (DriftScribe's own infrastructure):
    "control-plane-service":  "control-plane",
    "control-plane-sa":       "control-plane",
    "control-plane-bucket":   "control-plane",
    "control-plane-secret":   "control-plane",
    "control-plane-kms":      "control-plane",
    # Service-managed (buckets other Google services auto-create):
    "service-managed-bucket": "service-managed",
    # IAM (access-control changes):
    "wif-config-change":          "iam",
    "iam-change-forbidden-v1":    "iam",
    # Global v1 floors (structural action bans + conditional admit rules):
    "import-with-changes-forbidden-v1": "global-v1",
    "import-type-not-adoptable-v1":     "global-v1",
    "import-mixed-plan-forbidden-v1":   "global-v1",
    "import-batch-forbidden-v1":        "global-v1",
    "delete-action-forbidden-v1":       "global-v1",
    "forget-action-forbidden-v1":       "global-v1",
    "replace-action-forbidden-v1":      "global-v1",
    "unknown-action-forbidden-v1":      "global-v1",
})
"""Category assignment for every rule ID in ``RULE_DESCRIPTIONS``.

Keyed by the EXACT rule IDs from ``RULE_DESCRIPTIONS`` — the drift-pin test
``test_rule_categories_cover_exactly_the_rule_descriptions`` asserts:
- ``set(RULE_CATEGORIES) == set(RULE_DESCRIPTIONS)`` (no stale/missing entries)
- ``set(RULE_CATEGORIES.values()) <= set(CATEGORY_ORDER)`` (no unknown categories)
"""


# --------------------------------------------------------------------------- #
# HUMAN_GATES — approval gates that require operator action
# --------------------------------------------------------------------------- #

# Inner dicts are frozen with MappingProxyType — same in-place-mutation
# protection convention as TOOL_REGISTRY / WORKER_REGISTRY / ACTION_REGISTRY
# (a caller holding a reference cannot poison later build_capabilities() calls).
HUMAN_GATES: Final[tuple[Mapping[str, str], ...]] = (
    MappingProxyType({
        "id": "iac_apply",
        "title": "IaC plan apply",
        "description": (
            "Before the apply worker runs ``tofu apply``, an operator must "
            "approve the exact stored plan via the approval page. The approval "
            "is bound to the specific plan by a plan-bound HMAC with a signed "
            "expiry window. Approving one plan cannot approve another."
        ),
        "route": "/iac-approvals/{pr_number}",
        "method": "POST",
    }),
    MappingProxyType({
        "id": "rollback",
        "title": "Rollback",
        "description": (
            "The rollback worker requires a valid operator approval token before "
            "it will execute any Cloud Run rollback. The approval is single-use "
            "with a 15-minute TTL and bound to the specific rollback request by "
            "HMAC. The worker re-verifies the token at execution time."
        ),
        "route": "/approvals/{approval_id}",
        "method": "POST",
    }),
)
"""The two human approval gates, in DTO-ready dict form.

``method`` is included so the frontend can pin the gate to the mutating POST
(not the GET form page). The drift-pin test
``test_every_approval_gated_action_has_a_human_gate`` verifies that every
``ACTION_REGISTRY`` entry with ``requires_approval=True`` has a corresponding
gate id here.

Gate descriptions must mention their specific approval mechanism:
- iac_apply: plan-bound HMAC + signed expiry window
- rollback: single-use + 15-minute TTL + worker-side re-verification
"""


# --------------------------------------------------------------------------- #
# build_capabilities() — assembles the DTO
# --------------------------------------------------------------------------- #

def build_capabilities() -> dict:
    """Assemble the capabilities DTO from the enforcement constants.

    Returns a plain ``dict`` / ``list`` tree (JSON-serializable by
    ``json.dumps`` without a custom encoder). Never reads worker-URL
    env vars — it uses :func:`agent.workloads.registry.load_workload_spec`
    (env-free symbol validation only) so it works in every deploy
    environment.

    Does not cache; the workload YAML is re-parsed on every call (static
    per deploy, four small files — deliberate).

    Ordering is deterministic:
    - Workloads in ``WORKLOAD_NAMES`` order (Literal declaration order).
    - Tools / workers / actions in manifest declaration order.
    - Denylist rules sorted by ``(CATEGORY_ORDER.index(category), rule_id)``.

    Pinned by ``tests/unit/test_capabilities.py::test_build_capabilities_shape``
    and ``test_build_capabilities_is_json_serializable_and_env_free``.
    """
    # The "autonomous" signal is the explicit trigger set owned in
    # agent.main (NOT derived from observation_kind, which is intent not a
    # wired trigger — only drift actually fires on its own). Imported lazily
    # here, mirroring tests/unit/test_capabilities.py::
    # test_chat_only_coherence_with_main, so this module never imports the
    # heavy FastAPI app at load time (no cycle: main imports capabilities).
    from agent.main import AUTONOMOUS_TRIGGER_WORKLOADS

    workloads = []
    for name in WORKLOAD_NAMES:
        spec = load_workload_spec(name)
        tools = [
            {
                "name": tool_name,
                "description": TOOL_DESCRIPTIONS[tool_name],
                "write_capable": tool_name in MUTATION_TOOL_NAMES,
            }
            for tool_name in spec.enabled_tool_names
        ]
        workers = [
            {
                "name": worker_name,
                "description": WORKER_DESCRIPTIONS[worker_name],
            }
            for worker_name in spec.worker_names
        ]
        actions = [
            dataclasses.asdict(ACTION_REGISTRY[action_name])
            for action_name in spec.action_names
        ]
        workloads.append({
            "name": spec.name,
            "display_name": spec.display_name,
            "descriptor": spec.descriptor,
            "description": spec.description,
            "autonomous": spec.name in AUTONOMOUS_TRIGGER_WORKLOADS,
            "tools": tools,
            "workers": workers,
            "actions": actions,
        })

    rules = sorted(
        [
            {
                "id": rule_id,
                "description": RULE_DESCRIPTIONS[rule_id],
                "category": RULE_CATEGORIES[rule_id],
            }
            for rule_id in RULE_DESCRIPTIONS
        ],
        key=lambda r: (CATEGORY_ORDER.index(r["category"]), r["id"]),
    )

    return {
        "version": 1,
        "provenance": (
            "Generated from the same constants the enforcement code imports, "
            "not hand-written documentation."
        ),
        "iam_note": (
            "Each worker runs as its own service account with least-privilege "
            "IAM, codified in infra/scripts/. The only identity that can change "
            "live infrastructure is the apply worker's service account, and "
            "only after an operator approves the exact plan."
        ),
        "workloads": workloads,
        "human_gates": [dict(g) for g in HUMAN_GATES],
        "denylist": {
            "summary": (
                "Before any apply, the plan is checked against a fail-closed "
                "denylist. A violation blocks the apply; operator approval "
                "cannot override it."
            ),
            "enforced_at": [
                "the trusted plan-builder CI, before a plan is ever stored",
                "the approval page, as an advisory check before you approve",
                "the tofu-apply worker, immediately before apply (final gate)",
            ],
            "rules": rules,
            "adoptable_resource_types": [
                {"type": t, "label": ADOPTABLE_TYPE_LABELS[t]}
                for t in sorted(ADOPTABLE_RESOURCE_TYPES)
            ],
        },
    }
