"""Pure inventory-summary builder for the infra-reader worker.

No network. Takes normalized CAI resource records + the IaC declared-identity
set and produces the bounded, redaction-safe summary the worker returns
(design §4.5). Kept pure so it is fully unit-testable; the worker supplies the
real CAI page iterator and the declared set.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

from driftscribe_lib.iac_hcl import DeclaredIdentity
from driftscribe_lib.iac_plan_denylist import is_control_plane_node

# Asset types whose resource NAMES tend to carry sensitive data — surfaced as
# counts only (no sample, identity redacted in declared_not_found). Small,
# explicit, prefix-exact (Codex nit: no fuzzy matching).
SENSITIVE_ASSET_TYPES = frozenset({
    "secretmanager.googleapis.com/Secret",
    "secretmanager.googleapis.com/SecretVersion",
})

_FRESHNESS = (
    "CAI is eventually consistent and does not cover all resource types; "
    "this is a best-available index, not ground truth."
)
_SAMPLE_CAP = 10

# The one type whose sample may carry an enrichment-joined ``image``. Gating on
# it keeps the pure layer robust even if a future reader mis-set ``image`` on a
# non-run row (defense in depth — the reader only sets it on run services today).
_RUN_ASSET_TYPE = "run.googleapis.com/Service"


@dataclass(frozen=True)
class CaiResource:
    """The masked CAI fields we use (read_mask = name,assetType,location).

    ``topic`` and ``image`` are the exceptions: neither is part of the minimal
    read_mask; each is joined in by a scoped enrichment search the worker runs
    only when that asset type is present. ``topic`` is the subscription→topic
    edge (retained so the Provision crew can adopt a subscription without
    stalling to ask); ``image`` is a Cloud Run service's template container
    image (same reason for run-service adoption). Both are None for every other
    resource and for rows the enrichment couldn't read; the defaults keep every
    existing three-field construction valid.
    """
    name: str            # full //service/projects/.../X
    asset_type: str
    location: str
    topic: str | None = None
    image: str | None = None


def shorten_topic(topic: str, project: str) -> str:
    """Shorten ``projects/{project}/topics/{name}`` → ``{name}`` iff the project
    matches; any other shape passes through unchanged.

    A cross-project full path (``projects/OTHER/topics/x``) is still a valid
    input to ``propose_adoption_tool``, which normalizes/rejects at its own
    boundary — so we never silently shorten a foreign project's path. An
    already-short name or any unrecognized shape is returned as-is.
    """
    prefix = f"projects/{project}/topics/"
    return topic[len(prefix):] if topic.startswith(prefix) else topic


def normalize_cai_name(name: str) -> str:
    """Strip the ``//<service>/`` scheme prefix → comparable ``projects/.../X`` path."""
    if name.startswith("//"):
        # //run.googleapis.com/projects/... -> projects/...
        rest = name[2:]
        slash = rest.find("/")
        return rest[slash + 1:] if slash != -1 else rest
    return name


def _is_sensitive(asset_type: str) -> bool:
    return asset_type in SENSITIVE_ASSET_TYPES


def build_inventory(
    resources: list[CaiResource],
    declared: list[DeclaredIdentity],
    *,
    project: str,
    iac_snapshot_sha: str,
    declared_parse_ok: bool = True,
) -> dict:
    """Build the bounded summary dict. See design §4.5 for the shape."""
    # Type-aware match index: only declarations with BOTH a resolved identity
    # AND a known (supported) asset_type are matchable. Keying by
    # (asset_type, identity) prevents force-matching an unsupported import ID
    # against an unrelated live resource that happens to share a path suffix.
    matchable: dict[tuple[str, str], DeclaredIdentity] = {
        (d.asset_type, d.identity): d
        for d in declared
        if d.asset_type is not None and d.identity is not None
    }
    matched_keys: set[tuple[str, str]] = set()

    type_buckets: dict[str, dict] = defaultdict(
        lambda: {
            "count": 0,
            "declared_in_iac": 0,
            "not_in_iac": 0,
            "not_in_iac_control_plane": 0,
            "_samples": [],
        }
    )
    declared_total = 0
    for r in resources:
        norm = normalize_cai_name(r.name)
        key = (r.asset_type, norm)
        decl = matchable.get(key)
        bucket = type_buckets[r.asset_type]
        bucket["count"] += 1
        # The short display name is what the graph uses as the node label, so it
        # is also the string the shared control-plane classifier keys on — keeping
        # the aggregate count (here) and the per-node flag (infra_graph) in lockstep.
        display = norm.rsplit("/", 1)[-1] if norm else r.name
        # Computed once per row (hoisted out of the not-in-IaC branch) because it
        # now also gates image emission below, which applies to managed rows too.
        is_control_plane = is_control_plane_node(r.asset_type, display)
        if decl is not None:
            matched_keys.add(key)
            bucket["declared_in_iac"] += 1
            declared_total += 1
            conf = decl.confidence
            iac = True
        else:
            bucket["not_in_iac"] += 1
            conf = None
            iac = False
            # Count unmanaged control-plane / service-managed resources over EVERY
            # resource (not just the ≤10 sample) so the graph can derive actionable
            # (adoptable, non-control-plane) drift from a whole-estate figure.
            if is_control_plane:
                bucket["not_in_iac_control_plane"] += 1
        if len(bucket["_samples"]) < _SAMPLE_CAP:
            entry = {"name": display, "location": r.location, "iac": iac, "match_confidence": conf}
            # Only-when-present (matches control_plane / truncated_in_group style):
            # a subscription that carries an enrichment-joined topic gains the
            # field; every other sample stays byte-identical.
            if isinstance(r.topic, str) and r.topic:
                entry["topic"] = r.topic
            # Cloud Run service image, same only-when-present style — but NEVER
            # for a control-plane row: DriftScribe's own service images must not
            # enter the sample text, the anonymous-visible /infra/graph JSON, or
            # the L2 cache (the scoped CAI search can't filter by name, so the
            # join covers every service; suppression happens here at emission).
            # Type-gated to run services so a mislabeled non-run image can't leak.
            if (
                r.asset_type == _RUN_ASSET_TYPE
                and isinstance(r.image, str)
                and r.image
                and not is_control_plane
            ):
                entry["image"] = r.image
            bucket["_samples"].append(entry)

    by_type: dict[str, dict] = {}
    for atype, b in sorted(type_buckets.items()):
        sensitive = _is_sensitive(atype)
        entry = {
            "count": b["count"],
            "declared_in_iac": b["declared_in_iac"],
            "not_in_iac": b["not_in_iac"],
            "not_in_iac_control_plane": b["not_in_iac_control_plane"],
            "sensitive": sensitive,
        }
        if not sensitive:
            entry["sample"] = b["_samples"]
        by_type[atype] = entry

    # declared_not_found: every declared item with no live match, categorized by
    # WHY it didn't match. possible_causes is conditioned, not a blanket list.
    declared_not_found = []
    for decl in declared:
        if decl.asset_type is not None and decl.identity is not None:
            if (decl.asset_type, decl.identity) in matched_keys:
                continue
            causes = ["cai_lag", "not_yet_applied", "format_mismatch"]
        elif decl.identity is None:
            causes = ["identity_unresolved"]      # runtime-valued attrs
        else:  # has identity but unsupported asset_type → not matchable
            causes = ["asset_type_not_supported"]
        sensitive = decl.asset_type is not None and _is_sensitive(decl.asset_type)
        entry = {
            "address": decl.address or None,
            "asset_type": decl.asset_type,
            "source": decl.source,
            "confidence": decl.confidence,
            "possible_causes": causes,
        }
        if sensitive:
            entry["identity_redacted"] = True       # have identity, withhold it
        elif decl.identity is not None:
            entry["identity"] = decl.identity
        # else: identity is None (unresolved) — neither field; `address` carries it
        declared_not_found.append(entry)

    total = len(resources)
    out = {
        "project": project,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inventory_source": "cloud_asset_inventory",
        "freshness_caveat": _FRESHNESS,
        "iac_snapshot_sha": iac_snapshot_sha,
        "total_resources": total,
        "declared_in_iac": declared_total,
        "not_in_iac": total - declared_total,
        "by_type": by_type,
        "declared_not_found": declared_not_found,
        "truncated": {"per_type_sample": _SAMPLE_CAP},
    }
    if not declared_parse_ok:
        out["declared_set_status"] = "parse_error"
    return out
