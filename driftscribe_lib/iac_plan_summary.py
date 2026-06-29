"""Deterministic plain-language summary of a tofu ``plan.json`` (roadmap W1-1).

Renders the C2 plan artifact as structured CREATE/UPDATE/DESTROY/REPLACE
entries for the ``/iac-approvals/{pr_number}`` page, so an operator who cannot
read HCL can still see exactly what an apply will do. Pure; no I/O; walks
``resource_changes`` with the same shape assumptions as ``iac_plan_classify``.

Never partial: any entry this module cannot fully understand makes
``summarize_plan`` return ``None`` (the page falls back to the raw ``tofu
show`` text) — a summary that silently dropped an unparseable change could
under-report a destroy, which is worse than no summary. Display truncation
(the MAX_* caps) is the one exception: counts are computed over ALL entries
first, so the destroy/replace warning stays truthful when the list is capped.

Sensitive values: ``before_sensitive`` / ``after_sensitive`` masks are honored
at every nesting level — a sensitive attribute renders as a ``(sensitive)``
marker and its value NEVER appears in any output string (parity with ``tofu
show`` masking).
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from driftscribe_lib.iac_plan_denylist import (
    DELETE_ACTION_TUPLES,
    FORGET_ACTION_TUPLES,
    NO_OP_ACTION_TUPLES,
    REPLACE_ACTION_TUPLES,
)

__all__ = [
    "MAX_ATTRS_PER_ENTRY",
    "MAX_ENTRIES",
    "AttrChange",
    "BLAST_CANNOT_TOUCH_NOTE",
    "PLAN_RESOURCE_NAME_NOTE",
    "ChangeEntry",
    "PlanSummary",
    "blast_radius_phrase",
    "classify_verb",
    "mask_any",
    "sub_mask",
    "summarize_plan",
]

# One-line operator-facing summary of the denylist cage, rendered next to
# the per-plan blast radius on the approval page. HONESTY CONTRACT: this
# sentence may claim ONLY what driftscribe_lib/iac_plan_denylist.py
# enforces. test_blast_cannot_touch_note_matches_rule_set pins the exact
# RULE_DESCRIPTIONS key set — any denylist rule change fails that test and
# forces a re-review of this copy.
BLAST_CANNOT_TOUCH_NOTE = (
    "It cannot touch DriftScribe's own control plane (its services, "
    "service accounts, state/artifact buckets, secrets, or encryption "
    "keys), cannot change IAM anywhere, cannot delete, replace, or "
    "un-manage any resource, and can adopt (import) an existing resource "
    "only one at a time, from a small allowlist of types, and only when "
    "nothing would be modified — denylist-enforced, re-checked by the "
    "apply worker before apply."
)

# Crew-facing guidance for naming a resource when narrating a plan. Each
# `load_iac_plan_tool` entry carries the Terraform `address`/`name` identifiers
# AND the resource's real cloud `resource_name`; they DIFFER for an adoption
# (the import HCL labels the resource `adopt_<sanitized>`, while the live name
# has no such prefix), so a crew that echoes the Terraform label misnames the
# resource. Hand-duplicated into the explore + provision prompts; pinned by
# test_prompt_carries_the_resource_name_note so the copy can't drift.
PLAN_RESOURCE_NAME_NOTE = (
    "When you name a resource to the operator, prefer its real cloud name (a "
    "plan entry's resource_name) over the Terraform address or label (e.g. "
    "google_pubsub_topic.adopt_adopt_probe_topic). An adoption prefixes the "
    "Terraform label with adopt_, so the live name (adopt-probe-topic) and the "
    "Terraform label (adopt_adopt_probe_topic) are different things. If "
    "resource_name is empty (an unknown or masked name), say the real name "
    "isn't available rather than passing off the Terraform label as the name; "
    "mention the Terraform address only if the operator asks."
)

MAX_ENTRIES = 40           # resource rows rendered (counts stay total)
MAX_ATTRS_PER_ENTRY = 25   # attribute-diff rows per resource
MAX_DEPTH = 8              # nesting depth before a wholesale "(nested change)"
MAX_VALUE_CHARS = 120      # display clamp per rendered value

_SENSITIVE = "(sensitive)"
_UNKNOWN_AFTER = "(known after apply)"

# Human labels for the resource types DriftScribe authors today + the common
# neighbors an operator will meet first. Fallback: strip ``google_``, spaces.
_TYPE_LABELS = {
    "google_storage_bucket": "Cloud Storage bucket",
    "google_pubsub_topic": "Pub/Sub topic",
    "google_pubsub_subscription": "Pub/Sub subscription",
    "google_cloud_run_v2_service": "Cloud Run service",
    "google_service_account": "service account",
    "google_secret_manager_secret": "Secret Manager secret",
    "google_secret_manager_secret_version": "Secret Manager secret version",
    "google_eventarc_trigger": "Eventarc trigger",
    "google_artifact_registry_repository": "Artifact Registry repository",
    "google_project_iam_member": "project IAM member binding",
    "google_project_iam_binding": "project IAM binding",
    "google_project_iam_custom_role": "custom IAM role",
    "google_cloud_run_v2_service_iam_member": "Cloud Run IAM member binding",
    "google_firestore_database": "Firestore database",
    "google_compute_network": "VPC network",
    "google_compute_subnetwork": "VPC subnetwork",
    "google_compute_firewall": "firewall rule",
}


class _Malformed(Exception):
    """Internal: an entry the summary cannot fully understand (=> whole-plan None)."""


@dataclass(frozen=True)
class AttrChange:
    """One attribute-level diff row. ``before``/``after`` are DISPLAY strings —
    already masked, stringified, and clamped; the template renders them as-is."""

    path: str
    before: str
    after: str
    sensitive: bool = False
    unknown: bool = False


@dataclass(frozen=True)
class ChangeEntry:
    verb: str  # create | update | destroy | replace | import | forget | change
    rtype: str
    type_label: str
    name: str
    address: str
    location: str = ""        # creates only: after.location/region when scalar+non-sensitive
    imported: bool = False    # change.importing present (OpenTofu import block)
    deposed: str = ""         # rc.deposed key — this row targets a DEPOSED object,
                              # not the current one (address+deposed is the unique key)
    action_reason: str = ""   # rc.action_reason, prettified ("replace because cannot update")
    attr_changes: tuple[AttrChange, ...] = ()
    attrs_truncated: bool = False
    resource_name: str = ""   # real GCP resource name (mask-aware; "" if unknown)


@dataclass(frozen=True)
class PlanSummary:
    entries: tuple[ChangeEntry, ...]
    n_create: int = 0
    n_update: int = 0
    n_destroy: int = 0
    n_replace: int = 0
    n_import: int = 0
    n_forget: int = 0  # state mutation (leaves management) — never green
    n_change: int = 0  # unclassifiable action combos — never green
    n_hidden: int = 0  # entries beyond MAX_ENTRIES (counts above include them)
    # Per-type counts aggregated over ALL entries pre-truncation (same pre-
    # truncation guarantee as verb counts), sorted by (-count, type_label) for
    # deterministic rendering. Populated by summarize_plan; default () for tests
    # that construct PlanSummary directly without a full plan walk.
    type_counts: tuple[tuple[str, int], ...] = ()

    @property
    def destructive(self) -> bool:
        return (self.n_destroy + self.n_replace) > 0

    @property
    def all_accounted_safe(self) -> bool:
        """True iff the green 'nothing destroyed/replaced' line may render:
        no destroys, no replaces, no forgets, no unclassified combos."""
        return (self.n_destroy + self.n_replace + self.n_forget + self.n_change) == 0

    @property
    def adopt_only(self) -> bool:
        """True iff the plan does NOTHING except import (adopt) resources —
        drives the approval page's calm adoption framing. Counts are full-plan
        (not display-capped), so n_hidden does not weaken the claim."""
        return self.n_import > 0 and (
            self.n_create + self.n_update + self.n_destroy
            + self.n_replace + self.n_forget + self.n_change
        ) == 0


# --------------------------------------------------------------------------- #
# Masks. tofu's *_sensitive / after_unknown mirror the value structure with
# ``true`` at flagged positions (or ``true`` for a whole subtree).
# --------------------------------------------------------------------------- #

def _sub_mask(mask: Any, key: Any) -> Any:
    """The mask for ``key`` under ``mask`` (True propagates to all children)."""
    if mask is True:
        return True
    if isinstance(mask, dict) and isinstance(key, str):
        return mask.get(key, False)
    if isinstance(mask, list) and isinstance(key, int):
        return mask[key] if 0 <= key < len(mask) else False
    return False


# Purely recursive; pathological depth raises RecursionError, which
# summarize_plan converts to the None fallback (never-partial).
def _mask_any(mask: Any) -> bool:
    """True iff ANY position under ``mask`` is flagged."""
    if mask is True:
        return True
    if isinstance(mask, dict):
        return any(_mask_any(v) for v in mask.values())
    if isinstance(mask, list):
        return any(_mask_any(v) for v in mask)
    return False


def _render_value(v: Any) -> str:
    s = json.dumps(v, ensure_ascii=False, sort_keys=True) if isinstance(
        v, (dict, list)
    ) else json.dumps(v, ensure_ascii=False)
    return s if len(s) <= MAX_VALUE_CHARS else s[: MAX_VALUE_CHARS - 1] + "…"


def _guarded(v: Any, mask: Any) -> str:
    """Render ``v`` for display, masked wholesale if ANY of it is sensitive."""
    return _SENSITIVE if _mask_any(mask) else _render_value(v)


_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


def _join(path: str, key: str) -> str:
    if _IDENT.fullmatch(key):
        return f"{path}.{key}" if path else key
    quoted = f'["{key}"]'
    return f"{path}{quoted}" if path else quoted


# --------------------------------------------------------------------------- #
# Attribute diff — bounded recursive walk of change.before vs change.after.
# --------------------------------------------------------------------------- #

def _diff(
    before: Any,
    after: Any,
    b_sens: Any,
    a_sens: Any,
    unknown: Any,
    path: str,
    depth: int,
    out: list[AttrChange],
) -> bool:
    """Append diff rows under this node. Returns False iff the attr budget ran out.

    Order matters: the sensitivity check precedes the unknown check and all
    descent, so a sensitive subtree is NEVER stringified.
    """
    if len(out) >= MAX_ATTRS_PER_ENTRY:
        return False
    label = path or "(value)"
    if b_sens is True or a_sens is True:
        if before != after or _mask_any(unknown):
            out.append(AttrChange(label, _SENSITIVE, _SENSITIVE, sensitive=True))
        return True
    if unknown is True:
        out.append(
            AttrChange(
                label,
                _guarded(before, b_sens),
                _UNKNOWN_AFTER,
                sensitive=_mask_any(b_sens),
                unknown=True,
            )
        )
        return True
    if depth >= MAX_DEPTH:
        if before != after or _mask_any(unknown):
            out.append(
                AttrChange(
                    label,
                    _guarded(before, b_sens),
                    _guarded(after, a_sens),
                    sensitive=_mask_any(b_sens) or _mask_any(a_sens),
                )
            )
        return True
    if isinstance(before, dict) and isinstance(after, dict):
        for k in sorted(set(before) | set(after)):
            if not _diff(
                before.get(k),
                after.get(k),
                _sub_mask(b_sens, k),
                _sub_mask(a_sens, k),
                _sub_mask(unknown, k),
                _join(path, k),
                depth + 1,
                out,
            ):
                return False
        return True
    if isinstance(before, list) and isinstance(after, list):
        if len(before) != len(after):
            # Item-level alignment of unequal lists is guesswork — summarize
            # counts only (no values => no leak either).
            out.append(AttrChange(label, f"({len(before)} item(s))", f"({len(after)} item(s))"))
            return True
        for i, (b, a) in enumerate(zip(before, after)):
            if not _diff(
                b,
                a,
                _sub_mask(b_sens, i),
                _sub_mask(a_sens, i),
                _sub_mask(unknown, i),
                f"{path}[{i}]" if path else f"[{i}]",
                depth + 1,
                out,
            ):
                return False
        return True
    if before != after:
        out.append(
            AttrChange(
                label,
                _guarded(before, b_sens),
                _guarded(after, a_sens),
                sensitive=_mask_any(b_sens) or _mask_any(a_sens),
            )
        )
    return True


# --------------------------------------------------------------------------- #
# Entry building + the public summarize_plan.
# --------------------------------------------------------------------------- #

def _verb(actions: tuple[str, ...], importing: bool) -> str | None:
    """Verb for an EXACT actions tuple (None = pure no-op/read, skip the row).

    Exact-tuple matching against the denylist's audited vocabulary — never
    set-membership, so an unaudited future combo (e.g. ``["create","read"]``)
    classifies as visible-amber "change", not as a benign create.
    """
    if actions in NO_OP_ACTION_TUPLES:
        return "import" if importing else None
    if actions in REPLACE_ACTION_TUPLES:
        return "replace"
    if actions in DELETE_ACTION_TUPLES:
        return "destroy"
    if actions == ("create",):
        return "create"
    if actions == ("update",):
        return "update"
    if actions in FORGET_ACTION_TUPLES:
        return "forget"
    return "change"


# Wave-4 item 13 (cost estimate): driftscribe_lib.iac_cost reuses the audited
# verb classification and the sensitivity-mask walkers. Public aliases so the
# cost lib never re-derives action-tuple or mask semantics.
classify_verb = _verb
mask_any = _mask_any
sub_mask = _sub_mask


def _require_str(v: Any, what: str) -> str:
    if not isinstance(v, str) or not v:
        raise _Malformed(what)
    return v


def _type_label(rtype: str) -> str:
    if rtype in _TYPE_LABELS:
        return _TYPE_LABELS[rtype]
    stripped = rtype.removeprefix("google_").replace("_", " ").strip()
    return stripped or rtype


def _extract_name(side: Any, mask: Any) -> str:
    """change.<side>["name"] for display — only when scalar, non-empty, and
    its mask position is not sensitive (same discipline as ``location``)."""
    if isinstance(side, dict):
        v = side.get("name")
        if isinstance(v, str) and v and not _mask_any(_sub_mask(mask, "name")):
            return v
    return ""


def _resource_name(verb: str, change: dict) -> str:
    """Real GCP resource name for an entry, extracted mask-aware.

    create / import → after["name"]; destroy / replace → before["name"] only;
    update / change / forget → before["name"] falling back to after["name"].
    Returns "" whenever the candidate is missing, non-str, empty, or sensitive.
    """
    before = _extract_name(change.get("before"), change.get("before_sensitive"))
    after = _extract_name(change.get("after"), change.get("after_sensitive"))
    if verb in ("create", "import"):
        return after
    if verb in ("destroy", "replace"):
        return before
    return before or after  # update / change / forget


def _build_entry(rc: Any) -> ChangeEntry | None:
    """One ChangeEntry, None for skippable rows, _Malformed for anything else.

    EVERY row is fully validated BEFORE any skip decision — a malformed data
    row or an unknown ``mode`` voids the whole summary (never-partial holds
    unconditionally), only a WELL-FORMED data read is skipped.
    """
    if not isinstance(rc, dict):
        raise _Malformed("resource_changes entry is not an object")
    address = _require_str(rc.get("address"), "address")
    rtype = _require_str(rc.get("type"), "type")
    name = _require_str(rc.get("name"), "name")
    change = rc.get("change")
    if not isinstance(change, dict):
        raise _Malformed("change")
    raw_actions = change.get("actions")
    if not isinstance(raw_actions, list) or not all(
        isinstance(a, str) for a in raw_actions
    ):
        raise _Malformed("actions")
    actions = tuple(raw_actions)
    imp = change.get("importing")
    if imp is not None and not isinstance(imp, dict):
        raise _Malformed("importing")  # docs define an object with ``id``
    importing = imp is not None
    deposed = rc.get("deposed")
    if deposed is not None and not isinstance(deposed, str):
        raise _Malformed("deposed")

    mode = rc.get("mode")
    if mode == "data":
        if actions in NO_OP_ACTION_TUPLES and not importing:
            return None  # a well-formed data READ is the only skippable row
        # A data row claiming mutation/import actions is outside audited
        # semantics — silently hiding it would break never-partial.
        raise _Malformed("data-mode row with non-read actions")
    if mode != "managed":
        raise _Malformed("mode")  # unknown/missing mode — refuse to summarize

    verb = _verb(actions, importing)
    if verb is None:
        return None  # pure no-op/read without an import

    b_sens = change.get("before_sensitive")
    a_sens = change.get("after_sensitive")
    after = change.get("after")

    location = ""
    if verb == "create" and isinstance(after, dict):
        for k in ("location", "region"):
            v = after.get(k)
            if isinstance(v, str) and v and not _mask_any(_sub_mask(a_sens, k)):
                location = v
                break

    attr_changes: list[AttrChange] = []
    attrs_truncated = False
    if verb in ("update", "replace", "change"):
        attrs_truncated = not _diff(
            change.get("before"),
            after,
            b_sens,
            a_sens,
            change.get("after_unknown"),
            "",
            0,
            attr_changes,
        )

    reason = rc.get("action_reason")
    return ChangeEntry(
        verb=verb,
        rtype=rtype,
        type_label=_type_label(rtype),
        name=name,
        address=address,
        location=location,
        imported=importing,
        deposed=deposed or "",
        action_reason=reason.replace("_", " ") if isinstance(reason, str) else "",
        attr_changes=tuple(attr_changes),
        attrs_truncated=attrs_truncated,
        resource_name=_resource_name(verb, change),
    )


def summarize_plan(plan_json: Any) -> PlanSummary | None:
    """Total function: a PlanSummary, or None when no faithful summary exists.

    None on a non-dict plan, a non-list ``resource_changes``, ANY entry that
    fails to parse, or any unexpected exception (advisory display must never
    take the approval page down). An empty/no-op plan is a PlanSummary with
    zero entries — distinct from None so the page can say "no changes".
    """
    try:
        if not isinstance(plan_json, dict):
            return None
        rcs = plan_json.get("resource_changes")
        if rcs is None:
            rcs = []  # tofu omits the key entirely for a truly empty plan
        if not isinstance(rcs, list):
            return None
        entries: list[ChangeEntry] = []
        for rc in rcs:
            e = _build_entry(rc)
            if e is not None:
                entries.append(e)
    except _Malformed:
        return None
    except Exception:  # noqa: BLE001 — advisory display: any surprise => no summary
        return None
    counts = Counter(e.verb for e in entries)
    type_counter = Counter(e.type_label for e in entries)
    type_counts = tuple(
        (label, n)
        for label, n in sorted(type_counter.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    shown = tuple(entries[:MAX_ENTRIES])
    return PlanSummary(
        entries=shown,
        n_create=counts["create"],
        n_update=counts["update"],
        n_destroy=counts["destroy"],
        n_replace=counts["replace"],
        n_import=counts["import"],
        n_forget=counts["forget"],
        n_change=counts["change"],
        n_hidden=len(entries) - len(shown),
        type_counts=type_counts,
    )


# --------------------------------------------------------------------------- #
# Blast-radius helpers (ClickOps Wave 2 item 8)
# --------------------------------------------------------------------------- #

def _pluralize(label: str) -> str:
    """Pluralize a type label's final word.

    Rules applied in order:
    - +'es' after s/x/z/ch/sh sibilants (e.g. 'address' → 'addresses')
    - ies after consonant+y (e.g. 'repository' → 'repositories')
    - +'s' otherwise (e.g. 'bucket' → 'buckets')

    Covers every _TYPE_LABELS value and the google_-strip fallback; no
    irregular plurals needed for the current resource-type vocabulary.
    """
    if label.endswith(("s", "x", "z", "ch", "sh")):
        return label + "es"
    if label.endswith("y") and len(label) >= 2 and label[-2] not in "aeiou":
        return label[:-1] + "ies"
    return label + "s"


def blast_radius_phrase(summary: PlanSummary) -> str:
    """'1 Pub/Sub topic, 2 Cloud Storage buckets' — the can-affect-at-most
    half of the blast-radius line. '' for an empty plan (the empty card
    already says 'no changes'; the line is suppressed there)."""
    if not summary.type_counts:
        return ""
    return ", ".join(
        f"{n} {label}" if n == 1 else f"{n} {_pluralize(label)}"
        for label, n in summary.type_counts
    )
