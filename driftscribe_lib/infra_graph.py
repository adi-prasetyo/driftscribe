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

from driftscribe_lib.infra_inventory import SENSITIVE_ASSET_TYPES

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
          groups: [ { asset_type, label, count, managed, drift, sensitive,
                      nodes: [ {id, label, asset_type, managed, location} ],
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
                    nodes.append(
                        {
                            "id": f"g{gi}n{ni}",
                            "label": str(name) if name is not None else "",
                            "asset_type": atype,
                            "managed": bool(sample.get("iac")),
                            "location": location if isinstance(location, str) else None,
                        }
                    )
            shown = len(nodes)
            if count > shown:
                truncated_in_group = count - shown

        group = {
            "asset_type": atype,
            "label": _label_for(atype),
            "count": count,
            "managed": managed,
            "drift": drift,
            "sensitive": sensitive,
            "nodes": nodes,
        }
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
