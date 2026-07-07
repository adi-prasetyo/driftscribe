"""Pure graph builder: infra-reader inventory → operator-UI resource-map DTO.

No network. Takes the dict returned by :mod:`workers.infra_reader` ``/describe``
(built by :func:`driftscribe_lib.infra_inventory.build_inventory`) and reshapes
it into a typed, redaction-safe **graph DTO** that the Svelte ``InfraDiagram``
renders as a Mermaid resource map.

**Phase 1 is NODE-ONLY** (design §5 Phase 1): nodes are grouped by ``asset_type``
and each is flagged managed-in-IaC vs drift. ``edges`` is always ``[]`` — the
partial topology (subscription→topic, service→secret, …) is a Phase-4 follow-up
that needs the extended identity resolver. Calling it a "resource map", not a
"topology", until then is deliberate (Codex nit).

**Secret / sensitive asset types are surfaced COUNTS-ONLY** — never a per-resource
node, never a name. This is inherited from ``build_inventory`` (which omits
``sample`` for :data:`SENSITIVE_ASSET_TYPES`) AND re-enforced here defensively:
even if a malformed inventory carried a ``sample`` for a sensitive type, this
builder emits zero nodes for it (see :func:`build_graph` and its test).

Pure + total: a malformed/partial inventory must never raise — the operator
panel degrades gracefully instead. Any inventory carrying an ``error`` key
(``cloud_asset_unavailable`` from the worker's soft-fail, or a synthetic reason
the coordinator injects when the worker is unreachable) becomes a ``degraded``
DTO the UI renders as an "unavailable" note.
"""
from __future__ import annotations

from driftscribe_lib.iac_plan_denylist import (
    ADOPTABLE_RESOURCE_TYPES,
    CONTROL_PLANE_NODE_MATCHERS,
    is_control_plane_node,
)
from driftscribe_lib.iac_plan_summary import PlanSummary
from driftscribe_lib.infra_inventory import SENSITIVE_ASSET_TYPES

# Display-only mapping: tofu resource type -> CAI asset type, used to place a
# planned change in the live map's matching type group. The 5 identity-resolver
# pairs are mirrored verbatim (drift-pinned in tests); the rest are
# display-grouping additions. Unmapped types render in a "Planned changes"
# fallback group client-side — never guessed.
PLAN_RTYPE_TO_ASSET_TYPE: dict[str, str] = {
    "google_cloud_run_v2_service": "run.googleapis.com/Service",
    "google_storage_bucket": "storage.googleapis.com/Bucket",
    "google_pubsub_topic": "pubsub.googleapis.com/Topic",
    "google_pubsub_subscription": "pubsub.googleapis.com/Subscription",
    "google_service_account": "iam.googleapis.com/ServiceAccount",
    "google_secret_manager_secret": "secretmanager.googleapis.com/Secret",
    "google_secret_manager_secret_version": "secretmanager.googleapis.com/SecretVersion",
    "google_artifact_registry_repository": "artifactregistry.googleapis.com/Repository",
    "google_firestore_database": "firestore.googleapis.com/Database",
    "google_compute_network": "compute.googleapis.com/Network",
    "google_compute_subnetwork": "compute.googleapis.com/Subnetwork",
    "google_compute_firewall": "compute.googleapis.com/Firewall",
    "google_eventarc_trigger": "eventarc.googleapis.com/Trigger",
}

# Adopt-button UI (Phase 4, adopt design §6): the CAI asset types whose live
# resources the operator may adopt straight from the map. SINGLE SOURCE OF
# TRUTH is the denylist's ADOPTABLE_RESOURCE_TYPES (the v1 adoptable HCL-type
# allowlist) — COMPUTED here by mapping each through PLAN_RTYPE_TO_ASSET_TYPE,
# never hand-listed, so a denylist allowlist change propagates to the map
# affordance automatically. A drift-pin test asserts the resolved set is
# exactly the four. (PLAN_RTYPE_TO_ASSET_TYPE covers all four adoptable
# rtypes — the .get below would silently drop any that did not map, which a
# new adoptable type without a CAI mapping would expose; that is acceptable
# fail-quiet for the map, and the drift pin catches the divergence.)
ADOPTABLE_ASSET_TYPES: frozenset[str] = frozenset(
    PLAN_RTYPE_TO_ASSET_TYPE[rtype]
    for rtype in ADOPTABLE_RESOURCE_TYPES
    if rtype in PLAN_RTYPE_TO_ASSET_TYPE
)

# Guided adoption order (roadmap item 10): deterministic "what to adopt first"
# ranking over the adoptable types. rank 1 = start here. The simplest configs
# to recognize and review come first; the largest (a live service definition)
# last. HONESTY: every adoption is the same zero-change import behind the same
# approval gate — the order is about building operator confidence, NEVER about
# one type being safer to adopt (tests ban safety framing in the hints).
# Drift-pinned: keys == ADOPTABLE_ASSET_TYPES, ranks unique + contiguous (a new
# adoptable type cannot ship unranked).
ADOPTION_GUIDE: dict[str, tuple[int, str]] = {
    "storage.googleapis.com/Bucket": (
        1,
        "a simple leaf resource — the easiest place to build confidence",
    ),
    "pubsub.googleapis.com/Topic": (
        2,
        "small and quick to review — a name and a handful of settings",
    ),
    "pubsub.googleapis.com/Subscription": (
        3,
        "best adopted after its topic, so the pair reads naturally in IaC",
    ),
    "run.googleapis.com/Service": (
        4,
        "the largest config to review — most operators adopt these once comfortable",
    ),
}

# Plural display labels for the canonical order sentence (prompt surface).
# Same drift-pin as ADOPTION_GUIDE.
_ADOPTION_PLURAL_LABELS: dict[str, str] = {
    "storage.googleapis.com/Bucket": "Storage buckets",
    "pubsub.googleapis.com/Topic": "Pub/Sub topics",
    "pubsub.googleapis.com/Subscription": "Pub/Sub subscriptions",
    "run.googleapis.com/Service": "Cloud Run services",
}

# Canonical honesty sentence (Codex must-fix 2): pinned verbatim (whitespace-
# normalized) into both workload prompts; the SPA order note pins the same
# phrases in its vitest. Never weaken this without updating every surface.
ADOPTION_ORDER_HONESTY = (
    "Every adoption is the same zero-change import behind the same approval "
    "gate — the order is about building confidence, not safety."
)


def adoption_order_sentence() -> str:
    """Canonical adoption-order phrase, derived from ADOPTION_GUIDE rank order.

    The explore + provision system prompts carry this string verbatim (modulo
    line wrapping — the pin test whitespace-normalizes both sides), so
    reordering the guide without updating the prompts fails CI.
    """
    ordered = sorted(ADOPTION_GUIDE, key=lambda t: ADOPTION_GUIDE[t][0])
    return " → ".join(_ADOPTION_PLURAL_LABELS[t] for t in ordered)


# Canonical control-plane adoption refusal (same prompt-pin pattern as
# ADOPTION_ORDER_HONESTY — the static .md prompts duplicate it by hand and the
# pin test keeps the duplication safe). Unlike the order hints, this IS safety
# framing — accurately: it states what the always-on gate does, mirroring the
# capability card's rule descriptions. Precision (Codex 019eb932 MF1): the
# gate refuses CHANGES and IMPORTS — a plain no-op on a control-plane
# identity passes — so the copy says "change or import", never "touches".
ADOPTION_CONTROL_PLANE_NOTE = (
    "DriftScribe's own control-plane resources — its Cloud Run services and "
    "the -tofu-state / -tofu-artifacts buckets — cannot be adopted, and "
    "neither can buckets that a Google service auto-creates (Cloud Build, App "
    "Engine, Cloud Functions, or Cloud Run source deploys): the always-on "
    "denylist refuses any plan that would change or import them."
)

# Control-plane adopt suppression (2026-06-12, ranking-filter follow-up found
# live during the item-14 tour verify: the rank-1 "start here" suggestion was
# DriftScribe's OWN -tofu-artifacts bucket). The denylist refuses any plan
# that would CHANGE OR IMPORT a control-plane identity (evaluate runs the
# identity rules `if _is_mutation(actions) or importing is not None`; a plain
# no-op row on one passes) — so an Adopt button on such a node is a
# guaranteed dead end at C2 evaluation. Nodes
# matching a control-plane identity carry `control_plane: True` so every adopt
# surface (panel list, Start-here pick, tour step 4) suppresses the CTA with
# an honest note. The node itself stays on the map: it IS unmanaged drift, and
# hiding it would misreport the estate.
#
# PARITY-BY-CONSTRUCTION: the classifier lives in the denylist module (its OWN
# identity predicates/constants) and is shared verbatim by the inventory's
# aggregate control-plane count and this per-node flag, so the two surfaces
# cannot drift. A flagged bucket node corresponds to EITHER a control-plane
# bucket OR a service-managed-bucket denylist refusal — both suppress the same
# `control_plane` CTA flag. Pub/Sub has no identity rule, so its nodes are never
# flagged. test_infra_graph pins the parity by driving build_graph and evaluate
# with the same identity. Failure direction is safe: an unflagged protected name
# only shows a button whose plan C2 then blocks; a false positive cannot happen
# without the denylist also refusing that same identity.
#
# `_CONTROL_PLANE_NODE_MATCHERS` / `_is_control_plane_node` remain as module-local
# aliases for the shared denylist objects (back-compat for existing importers).
_CONTROL_PLANE_NODE_MATCHERS = CONTROL_PLANE_NODE_MATCHERS
_is_control_plane_node = is_control_plane_node


# Plan rtypes whose names/addresses must never reach the map. Mirrors the
# static gate's SECRET_MATERIAL_RESOURCE_TYPES (drift-pinned ⊇ at test time;
# no runtime import of tools/). The REGIONAL variants are deliberately unmapped
# above but must still redact — keying redaction only on the mapped asset type
# would leak a regional secret's block name through the "Planned changes"
# fallback path.
SENSITIVE_PLAN_RTYPES: frozenset[str] = frozenset({
    "google_secret_manager_secret",
    "google_secret_manager_secret_version",
    "google_secret_manager_regional_secret",
    "google_secret_manager_regional_secret_version",
})

_OVERLAY_VERBS = ("create", "update", "destroy", "replace", "import", "forget", "change")


def plan_overlay_unavailable(pr_number: int, reason: str) -> dict:
    """The not-available overlay DTO (same shape, empty payload)."""
    return {
        "pr_number": pr_number,
        "available": False,
        "reason": reason,
        "counts": {v: 0 for v in _OVERLAY_VERBS},
        "hidden": 0,
        "entries": [],
    }


def plan_overlay(pr_number: int, summary: PlanSummary) -> dict:
    """Reshape a PlanSummary into the redaction-safe map-overlay DTO.

    Sensitive parity with build_graph: a planned change whose rtype is in
    SENSITIVE_PLAN_RTYPES, or whose mapped asset type is in SENSITIVE_ASSET_TYPES,
    carries NO name, address, or location — block names routinely equal the
    secret_id, so the address would leak it.
    """
    entries: list[dict] = []
    for e in summary.entries:
        atype = PLAN_RTYPE_TO_ASSET_TYPE.get(e.rtype)
        # atype-None short-circuit is explicitness only (None is never in a frozenset).
        sensitive = e.rtype in SENSITIVE_PLAN_RTYPES or (
            atype is not None and atype in SENSITIVE_ASSET_TYPES
        )
        entries.append({
            "verb": e.verb,
            "rtype": e.rtype,
            "type_label": e.type_label,
            "name": "" if sensitive else e.resource_name,
            "address": "" if sensitive else e.address,
            "asset_type": atype,
            "sensitive": sensitive,
            "location": "" if sensitive else e.location,
        })
    return {
        "pr_number": pr_number,
        "available": True,
        "reason": None,
        "counts": {
            "create": summary.n_create,
            "update": summary.n_update,
            "destroy": summary.n_destroy,
            "replace": summary.n_replace,
            "import": summary.n_import,
            "forget": summary.n_forget,
            "change": summary.n_change,
        },
        "hidden": summary.n_hidden,
        "entries": entries,
    }


# Friendly display labels for the asset types we expect to see. Anything not
# listed falls back to a humanized form of the CAI type suffix
# (:func:`_humanize_asset_type`). Small + explicit on purpose — no fuzzy mapping.
_TYPE_LABELS: dict[str, str] = {
    "run.googleapis.com/Service": "Cloud Run service",
    "storage.googleapis.com/Bucket": "Storage bucket",
    "pubsub.googleapis.com/Topic": "Pub/Sub topic",
    "pubsub.googleapis.com/Subscription": "Pub/Sub subscription",
    "secretmanager.googleapis.com/Secret": "Secret",
    "secretmanager.googleapis.com/SecretVersion": "Secret version",
    "iam.googleapis.com/ServiceAccount": "Service account",
    "compute.googleapis.com/Network": "VPC network",
    "compute.googleapis.com/Subnetwork": "Subnet",
    "artifactregistry.googleapis.com/Repository": "Artifact Registry repo",
    "firestore.googleapis.com/Database": "Firestore database",
}

# Shown when the inventory carries no freshness caveat (degraded path) — keeps
# the UI honest about CAI's eventual consistency without depending on the worker.
_DEFAULT_CAVEAT = (
    "Best-available index from Cloud Asset Inventory — eventually consistent, "
    "may lag a recent apply, and does not cover every resource type."
)

# Bound any free-text detail we echo from the worker (mirrors worker_client's
# 500-char truncation) so a degraded reason can't blow up the payload.
_DETAIL_CAP = 500


def _as_int(value: object, default: int = 0) -> int:
    """Coerce to int, never raising — a malformed count degrades to ``default``."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _humanize_asset_type(asset_type: str) -> str:
    """Fallback label: take the CAI type suffix and space out CamelCase.

    ``secretmanager.googleapis.com/SecretVersion`` → ``"Secret Version"``.
    """
    tail = asset_type.rsplit("/", 1)[-1] if asset_type else ""
    chars: list[str] = []
    for i, ch in enumerate(tail):
        if ch.isupper() and i > 0 and not tail[i - 1].isupper():
            chars.append(" ")
        chars.append(ch)
    return "".join(chars) or "Resource"


def _label_for(asset_type: str) -> str:
    return _TYPE_LABELS.get(asset_type) or _humanize_asset_type(asset_type)


def _degraded(reason: str, *, project: object = None, detail: object = None) -> dict:
    """A graph DTO the UI renders as an "infrastructure unavailable" note."""
    return {
        "generated_at": None,
        "project": project if isinstance(project, str) else None,
        "caveat": _DEFAULT_CAVEAT,
        "iac_snapshot_sha": None,
        "degraded": True,
        "degraded_reason": str(reason),
        "detail": (str(detail)[:_DETAIL_CAP] if detail else None),
        "totals": {"resources": 0, "managed": 0, "drift": 0},
        "groups": [],
        "edges": [],
        "truncated": {},
    }


def build_graph(inventory: dict) -> dict:
    """Reshape an infra-reader inventory dict into the resource-map graph DTO.

    Returns a dict with::

        { generated_at, project, caveat, iac_snapshot_sha,
          degraded: bool, degraded_reason: str|None, detail?: str|None,
          totals: {resources, managed, drift},
          groups: [ { asset_type, label, count, managed, drift, drift_adoptable, sensitive,
                      adoptable,
                      nodes: [ {id, label, asset_type, managed, location, control_plane?} ],
                      truncated_in_group? } ],
          edges: [],                      # Phase 1 is node-only
          truncated: {...},
          declared_set_status?: "parse_error" }

    Degraded contract: a non-dict inventory, or one carrying an ``error`` key,
    yields ``degraded=True`` with empty groups (see :func:`_degraded`). Never
    raises on malformed input — the panel degrades instead of 500-ing.
    """
    if not isinstance(inventory, dict):
        return _degraded("malformed_inventory")
    if inventory.get("error"):
        return _degraded(
            inventory["error"],
            project=inventory.get("project"),
            detail=inventory.get("detail"),
        )

    by_type = inventory.get("by_type")
    if not isinstance(by_type, dict):
        by_type = {}

    groups: list[dict] = []
    # Sort by asset_type for stable node ids (g<gi>n<ni>) across calls; the ids
    # are render-only handles, but determinism keeps tests + diffs sane. Sort +
    # coerce via str() so a direct caller passing a non-string key can't break
    # the "never raises" contract (HTTP JSON keys are always strings anyway).
    for gi, (raw_atype, entry) in enumerate(
        sorted(by_type.items(), key=lambda kv: str(kv[0]))
    ):
        if not isinstance(entry, dict):
            continue
        atype = str(raw_atype)
        # Defense in depth: trust the inventory's `sensitive` flag, but ALSO
        # treat the known secret types as sensitive even if the flag is missing
        # — so a per-resource secret node can never be emitted from here.
        sensitive = bool(entry.get("sensitive")) or atype in SENSITIVE_ASSET_TYPES
        count = _as_int(entry.get("count"))
        managed = _as_int(entry.get("declared_in_iac"))
        drift = _as_int(entry.get("not_in_iac"))
        adoptable = atype in ADOPTABLE_ASSET_TYPES and not sensitive
        # Actionable drift = unmanaged resources this surface can actually adopt:
        # an adoptable type MINUS its control-plane / service-managed members
        # (which the UI shows as "system-managed", never as adoptable drift). A
        # non-adoptable or sensitive type has no actionable drift by definition.
        # If a stale inventory lacks the control-plane count, fall back to raw
        # drift — over-report rather than hide unmanaged resources.
        cp_drift = _as_int(entry.get("not_in_iac_control_plane"))
        drift_adoptable = max(0, drift - cp_drift) if adoptable else 0

        nodes: list[dict] = []
        truncated_in_group = 0
        if not sensitive:
            samples = entry.get("sample")
            if isinstance(samples, list):
                for ni, sample in enumerate(samples):
                    if not isinstance(sample, dict):
                        continue
                    location = sample.get("location")
                    # dict.get's default only fires on a MISSING key; a present
                    # name=None would otherwise stringify to the literal "None".
                    name = sample.get("name")
                    label = str(name) if name is not None else ""
                    node = {
                        "id": f"g{gi}n{ni}",
                        "label": label,
                        "asset_type": atype,
                        "managed": bool(sample.get("iac")),
                        "location": location if isinstance(location, str) else None,
                    }
                    # Subscription→topic passthrough (adopt-sub-topic-prefill):
                    # only-when-present + type-strict (same defensive stance as
                    # `location`), so every non-subscription node stays
                    # byte-identical and a malformed topic can't reach the client.
                    topic = sample.get("topic")
                    if isinstance(topic, str) and topic:
                        node["topic"] = topic
                    if _is_control_plane_node(atype, label):
                        # Only-when-true (truncated_in_group style) so every
                        # non-control-plane graph stays byte-identical.
                        node["control_plane"] = True
                    nodes.append(node)
            shown = len(nodes)
            if count > shown:
                truncated_in_group = count - shown

        group = {
            "asset_type": atype,
            "label": _label_for(atype),
            "count": count,
            "managed": managed,
            "drift": drift,
            "drift_adoptable": drift_adoptable,
            "sensitive": sensitive,
            # Adopt-button affordance (Phase 4): an adoptable type whose group is
            # NOT sensitive. Sensitive groups are counts-only (no node names) so
            # they can never carry an Adopt button regardless of their type.
            "adoptable": adoptable,
            "nodes": nodes,
        }
        if group["adoptable"]:
            # Guided adoption order (item 10). .get (not [...]) keeps the
            # "never raises" contract even if the guide/adoptable drift-pin
            # were somehow violated at runtime.
            guide = ADOPTION_GUIDE.get(atype)
            if guide:
                group["adopt_rank"], group["adopt_hint"] = guide
        if truncated_in_group:
            group["truncated_in_group"] = truncated_in_group
        groups.append(group)

    totals = {
        "resources": _as_int(
            inventory.get("total_resources"), sum(g["count"] for g in groups)
        ),
        "managed": _as_int(
            inventory.get("declared_in_iac"), sum(g["managed"] for g in groups)
        ),
        "drift": _as_int(
            inventory.get("not_in_iac"), sum(g["drift"] for g in groups)
        ),
    }

    out = {
        "generated_at": inventory.get("generated_at"),
        "project": inventory.get("project"),
        "caveat": inventory.get("freshness_caveat") or _DEFAULT_CAVEAT,
        "iac_snapshot_sha": inventory.get("iac_snapshot_sha"),
        "degraded": False,
        "degraded_reason": None,
        "totals": totals,
        "groups": groups,
        "edges": [],  # Phase 1 is node-only; Phase 4 derives the partial topology.
        "truncated": inventory.get("truncated") or {},
    }
    if inventory.get("declared_set_status"):
        out["declared_set_status"] = inventory["declared_set_status"]
    return out
