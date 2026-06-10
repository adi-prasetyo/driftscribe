# Plain-Language Plan Summary Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Render the IaC approval page's integrity-checked `plan.json` as a deterministic plain-language change summary (CREATE/UPDATE/DESTROY/REPLACE badges, human resource labels, attribute-level before→after diffs, and a prominent "no resources will be destroyed" / "⚠ N will be destroyed" line) so an operator who cannot read HCL can still understand exactly what an apply will do.

**Architecture:** A new pure library `driftscribe_lib/iac_plan_summary.py` walks `resource_changes` (same conventions as `iac_plan_classify`) into frozen dataclasses; `agent/iac_artifacts.IacPlanView` exposes it as a `cached_property` over the `_plan_json` it already parses; `agent/templates/iac_approval.html` gains one new card between the integrity card and the C2-metadata card. No new endpoints, no new auth surface, no LLM involvement — the summary is mechanical, which is the whole trust story.

**Tech Stack:** Python 3.12 (stdlib only in the lib), Jinja2 (Starlette `Jinja2Templates`, autoescape on), design-system CSS in `frontend/src/styles/base.css` (strict CSP: classes only, no inline styles), pytest.

**Context:** Wave 1 item 1 of [`2026-06-10-clickops-audience-roadmap.md`](2026-06-10-clickops-audience-roadmap.md).

---

## Design invariants (read before coding)

1. **Never a partial summary.** If ANY `resource_changes` entry cannot be fully
   understood, `summarize_plan` returns `None` and the page falls back to the
   raw `tofu show` block. A summary that silently drops an entry it failed to
   parse could under-report a destroy — worse than no summary at all.
2. **Truncation is explicit, counts are total.** The `MAX_*` display caps trim
   the rendered *list*, but all counts (and therefore the destroy warning) are
   computed over the FULL entry set before truncation, and the page says
   "…and N more".
3. **Sensitive values never appear.** `before_sensitive` / `after_sensitive`
   masks are honored at every nesting level — a sensitive attribute renders as
   a `(sensitive)` marker, matching `tofu show`'s own masking. Tests assert the
   raw secret string appears NOWHERE in the lib output or rendered HTML.
4. **Fail-soft at the page, always-200.** Summary generation can never raise
   out of the GET route (`summarize_plan` is total: catches everything →
   `None`).
5. **Render only for a trustworthy artifact** (Codex must-fix). The card
   renders ONLY when the route classified the page as non-error — the GET
   passes `show_summary = reason_severity != "error" and not
   resolved_decision`, which covers unverifiable, integrity mismatch,
   denylist violations, AND the artifact-vs-PR consistency check that only
   the route can compute — and,
   belt-and-braces in the template, `integrity_ok` is true and the denylist is
   clean. `load_plan_view` populates `_plan_json` even on a digest MISMATCH,
   so without this gate the card (and its "integrity-checked" footer) would
   render for a tampered artifact, and a green "no destroys" line could sit
   under a red hard-stop. Outcome pages never show the card: the POST result
   renders don't pass `show_summary` (`| default(false)`), and the GET's own
   terminal-outcome renders (already applied+merged / terminal failed —
   `resolved_decision` set while `reason_severity` stays `""`) are excluded
   by the flag's `and not resolved_decision` term.
6. **The green reassurance line is conservative.** "No resources will be
   destroyed or replaced" renders ONLY when destroy = replace = forget =
   unclassified ("change") = 0. Unknown action combos get an amber "review
   the raw plan" note instead of a green all-clear.
7. **Action vocabulary is shared, exact-tuple, fail-visible** (Codex
   must-fix). Verbs are classified by EXACT actions tuple against the
   denylist's audited vocabulary (`iac_plan_denylist.NO_OP_ACTION_TUPLES`,
   `DELETE_ACTION_TUPLES`, `FORGET_ACTION_TUPLES`, `REPLACE_ACTION_TUPLES`) —
   set-membership would let a hypothetical future `["create","read"]` read as
   a plain create. `["forget"]` is a real OpenTofu state mutation (the
   resource leaves state; the live resource is untouched) and gets its own
   verb + explainer; `deposed` objects are labeled so a deposed-cleanup delete
   doesn't read as destroying the current object.
8. **Deterministic, not LLM.** The card footer says so explicitly — that line
   is the audience-facing trust claim, keep it.

> Reality check: the C1 denylist currently hard-blocks delete / replace /
> forget plans outright (v1 policy), so a destructive plan can't reach an
> approvable page today — the destroy-warning path is defense-in-depth for
> when that policy relaxes, and the truthful-labeling rules above must hold
> regardless.

## Out of scope (YAGNI — see roadmap)

Before/after diagram overlay (Wave 2), blast-radius line (Wave 2), cost
estimates (Wave 4), i18n/Japanese copy, summarizing on the SPA decisions rail,
and any change to the POST/approve path or the tofu-apply worker.

---

### Task 0: Branch

```bash
cd /home/adi/driftscribe && git checkout main && git pull && git checkout -b feat/plan-summary
```

(Or a worktree via superpowers:using-git-worktrees if executing in parallel
with other work.)

### Task 1: Lib skeleton — verb classification + basic entries

**Files:**
- Create: `driftscribe_lib/iac_plan_summary.py`
- Test: `tests/unit/test_iac_plan_summary.py`

**Step 1: Write the failing tests**

```python
"""Unit tests for driftscribe_lib.iac_plan_summary (ClickOps roadmap W1-1).

The summary is ADVISORY DISPLAY ONLY, but its failure modes are safety-shaped:
- never a partial summary (any unparseable entry => None, not a shorter list);
- sensitive values must never appear in any output string;
- counts are computed over ALL entries, truncation trims only the display list.
"""
from __future__ import annotations

from driftscribe_lib.iac_plan_summary import (
    MAX_ATTRS_PER_ENTRY,
    MAX_ENTRIES,
    summarize_plan,
)


def _rc(actions, *, rtype="google_storage_bucket", name="b", address=None,
        before=None, after=None, b_sens=False, a_sens=False, unknown=False,
        mode="managed", **extra):
    rc = {
        "address": address or f"{rtype}.{name}",
        "mode": mode,
        "type": rtype,
        "name": name,
        "change": {
            "actions": actions,
            "before": before,
            "after": after,
            "before_sensitive": b_sens,
            "after_sensitive": a_sens,
            "after_unknown": unknown,
        },
    }
    rc.update(extra)
    return rc


def _plan(*rcs):
    return {"format_version": "1.2", "resource_changes": list(rcs)}


def test_create_entry():
    s = summarize_plan(_plan(_rc(["create"], after={"name": "b", "location": "ASIA-NORTHEAST1"})))
    assert s is not None
    assert s.n_create == 1 and not s.destructive
    e = s.entries[0]
    assert e.verb == "create"
    assert e.type_label == "Cloud Storage bucket"
    assert e.name == "b"
    assert e.address == "google_storage_bucket.b"
    assert e.location == "ASIA-NORTHEAST1"
    assert e.attr_changes == ()


def test_update_destroy_replace_classification():
    s = summarize_plan(_plan(
        _rc(["update"], name="u", before={"x": 1}, after={"x": 2}),
        _rc(["delete"], name="d"),
        _rc(["delete", "create"], name="r1"),
        _rc(["create", "delete"], name="r2"),
    ))
    assert (s.n_update, s.n_destroy, s.n_replace) == (1, 1, 2)
    assert s.destructive
    assert [e.verb for e in s.entries] == ["update", "destroy", "replace", "replace"]


def test_noop_read_and_data_reads_are_skipped():
    s = summarize_plan(_plan(
        _rc(["no-op"], name="n"),
        _rc(["read"], name="rd"),
        _rc(["read"], name="dm", mode="data"),
    ))
    assert s is not None and s.entries == ()
    assert (s.n_create, s.n_update, s.n_destroy, s.n_replace) == (0, 0, 0, 0)


def test_data_row_with_mutation_actions_voids_summary():
    # A data row is only skippable as a well-formed READ — one claiming
    # mutation actions is outside audited semantics and must not be hidden.
    good = _rc(["create"], name="ok")
    assert summarize_plan(_plan(good, _rc(["create"], name="dm", mode="data"))) is None
    assert summarize_plan(_plan(good, _rc(["forget"], name="df", mode="data"))) is None


def test_forget_is_its_own_verb_never_green():
    # ["forget"] = OpenTofu "removed" block: the resource LEAVES state, the
    # live resource is untouched. Real state mutation — own verb, never green.
    s = summarize_plan(_plan(_rc(["forget"], name="f")))
    assert s.n_forget == 1 and s.entries[0].verb == "forget"
    assert not s.destructive and not s.all_accounted_safe


def test_unknown_action_combo_is_visible_not_green():
    # Exact-tuple matching: an unaudited combo must NOT classify as a benign
    # create — it shows as amber "change" and suppresses the green line.
    s = summarize_plan(_plan(_rc(["create", "read"], name="weird")))
    assert s.n_change == 1 and s.n_create == 0
    assert s.entries[0].verb == "change"
    assert not s.destructive and not s.all_accounted_safe


def test_malformed_data_row_or_unknown_mode_voids_summary():
    # Never-partial holds for EVERY row: only a WELL-FORMED data read is
    # skipped; a malformed data row or an unknown/missing mode => None.
    good = _rc(["create"], name="ok")
    bad_data = _rc("not-a-list", name="d", mode="data")
    assert summarize_plan(_plan(good, bad_data)) is None
    unknown_mode = _rc(["create"], name="m", mode="mystery")
    assert summarize_plan(_plan(good, unknown_mode)) is None
    no_mode = _rc(["create"], name="nm")
    del no_mode["mode"]
    assert summarize_plan(_plan(good, no_mode)) is None


def test_deposed_row_is_labeled():
    rc = _rc(["delete"], name="b")
    rc["deposed"] = "byebye01"
    s = summarize_plan(_plan(rc))
    e = s.entries[0]
    assert e.verb == "destroy" and e.deposed == "byebye01"


def test_truthy_non_dict_importing_voids_summary():
    rc = _rc(["no-op"], name="b")
    rc["change"]["importing"] = "yes"
    assert summarize_plan(_plan(rc)) is None


def test_unknown_type_label_falls_back_to_readable():
    s = summarize_plan(_plan(_rc(["create"], rtype="google_dataproc_cluster", name="c")))
    assert s.entries[0].type_label == "dataproc cluster"


def test_missing_resource_changes_key_is_empty_plan():
    s = summarize_plan({"format_version": "1.2"})
    assert s is not None and s.entries == ()


def test_malformed_entry_voids_whole_summary():
    # NEVER a partial summary: one bad entry => None, even with good siblings.
    good = _rc(["create"], name="ok")
    for bad in (
        "not-a-dict",
        {"address": "a.b", "mode": "managed", "type": "t", "name": "n"},  # no change
        _rc("not-a-list", name="x"),
        {**_rc(["create"], name="y"), "address": ""},
    ):
        assert summarize_plan(_plan(good, bad)) is None


def test_non_dict_plan_is_none():
    assert summarize_plan(None) is None
    assert summarize_plan([]) is None
    assert summarize_plan({"resource_changes": "nope"}) is None
```

**Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_iac_plan_summary.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'driftscribe_lib.iac_plan_summary'`

**Step 3: Write the implementation**

`driftscribe_lib/iac_plan_summary.py`:

```python
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

__all__ = ["AttrChange", "ChangeEntry", "PlanSummary", "summarize_plan"]

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

    @property
    def destructive(self) -> bool:
        return (self.n_destroy + self.n_replace) > 0

    @property
    def all_accounted_safe(self) -> bool:
        """True iff the green 'nothing destroyed/replaced' line may render:
        no destroys, no replaces, no forgets, no unclassified combos."""
        return (self.n_destroy + self.n_replace + self.n_forget + self.n_change) == 0


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
                f"{label if path else ''}[{i}]" if path else f"[{i}]",
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


def _require_str(v: Any, what: str) -> str:
    if not isinstance(v, str) or not v:
        raise _Malformed(what)
    return v


def _type_label(rtype: str) -> str:
    if rtype in _TYPE_LABELS:
        return _TYPE_LABELS[rtype]
    stripped = rtype.removeprefix("google_").replace("_", " ").strip()
    return stripped or rtype


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
    )
```

**Step 4: Run tests**

Run: `uv run pytest tests/unit/test_iac_plan_summary.py -q`
Expected: PASS (all tests in the file)

**Step 5: Commit**

```bash
git add driftscribe_lib/iac_plan_summary.py tests/unit/test_iac_plan_summary.py
git commit -m "feat(lib): iac_plan_summary — classify resource_changes into plain-language entries"
```

### Task 2: Attribute diff — nested paths, lists, clamping

**Files:**
- Modify: `tests/unit/test_iac_plan_summary.py` (append)
- (implementation already in place from Task 1 — these tests pin its behavior)

**Step 1: Write the tests**

```python
def _one(s):
    assert s is not None and len(s.entries) == 1
    return s.entries[0]


def test_update_scalar_diff_with_dotted_path():
    e = _one(summarize_plan(_plan(_rc(
        ["update"], rtype="google_cloud_run_v2_service", name="svc",
        before={"template": {"max_instance_request_concurrency": 80}},
        after={"template": {"max_instance_request_concurrency": 200}},
    ))))
    assert e.type_label == "Cloud Run service"
    (a,) = e.attr_changes
    assert a.path == "template.max_instance_request_concurrency"
    assert (a.before, a.after) == ("80", "200")


def test_update_list_index_path_and_added_key():
    e = _one(summarize_plan(_plan(_rc(
        ["update"], name="svc",
        before={"env": [{"name": "FOO", "value": "1"}]},
        after={"env": [{"name": "FOO", "value": "2"}], "labels": {"team": "ops"}},
    ))))
    paths = {a.path: (a.before, a.after) for a in e.attr_changes}
    assert paths["env[0].value"] == ('"1"', '"2"')
    assert paths["labels"] == ("null", '{"team":"ops"}') or "labels.team" in paths


def test_unequal_list_lengths_summarized_as_counts():
    e = _one(summarize_plan(_plan(_rc(
        ["update"], name="svc",
        before={"env": [1]}, after={"env": [1, 2, 3]},
    ))))
    (a,) = e.attr_changes
    assert a.path == "env"
    assert (a.before, a.after) == ("(1 item(s))", "(3 item(s))")


def test_long_value_clamped():
    e = _one(summarize_plan(_plan(_rc(
        ["update"], name="b", before={"v": "x" * 500}, after={"v": "y"},
    ))))
    (a,) = e.attr_changes
    assert len(a.before) <= 120 and a.before.endswith("…")


def test_unknown_after_renders_known_after_apply():
    e = _one(summarize_plan(_plan(_rc(
        ["update"], name="b",
        before={"etag": "abc"}, after={"etag": None},
        unknown={"etag": True},
    ))))
    (a,) = e.attr_changes
    assert a.unknown and a.after == "(known after apply)" and a.before == '"abc"'


def test_attr_budget_truncates_with_flag():
    before = {f"k{i:03d}": i for i in range(40)}
    after = {f"k{i:03d}": i + 1 for i in range(40)}
    e = _one(summarize_plan(_plan(_rc(["update"], name="b", before=before, after=after))))
    assert e.attrs_truncated
    assert len(e.attr_changes) == MAX_ATTRS_PER_ENTRY
```

> Note on `test_update_list_index_path_and_added_key`: a key absent on one side
> diffs as `null → value` at the parent path (dict-vs-None is the scalar
> fallback arm). Either rendering in the assertion is acceptable — pin
> whichever the implementation produces and keep it.

**Step 2: Run** — `uv run pytest tests/unit/test_iac_plan_summary.py -q`
Expected: PASS if Task 1's implementation is faithful; fix the implementation
(NOT the invariants) where it isn't. Adjust the `labels` assertion to the
actual (stable) rendering.

**Step 3: Commit**

```bash
git add tests/unit/test_iac_plan_summary.py
git commit -m "test(lib): pin iac_plan_summary diff paths, list handling, clamps"
```

### Task 3: Sensitivity masking (the critical one)

**Files:**
- Modify: `tests/unit/test_iac_plan_summary.py` (append)

**Step 1: Write the tests**

```python
SECRET = "hunter2-super-secret"


def _assert_secret_nowhere(s):
    for e in s.entries:
        for a in e.attr_changes:
            assert SECRET not in a.before and SECRET not in a.after
            assert SECRET not in a.path
        assert SECRET not in e.location


def test_sensitive_leaf_masked_both_sides():
    e = _one(summarize_plan(_plan(_rc(
        ["update"], name="b",
        before={"password": SECRET, "x": 1},
        after={"password": "rotated-" + SECRET, "x": 1},
        b_sens={"password": True}, a_sens={"password": True},
    ))))
    (a,) = e.attr_changes
    assert a.sensitive and a.path == "password"
    assert (a.before, a.after) == ("(sensitive)", "(sensitive)")


def test_sensitive_subtree_never_descended():
    s = summarize_plan(_plan(_rc(
        ["update"], name="b",
        before={"conn": {"user": "u", "pass": SECRET}},
        after={"conn": {"user": "u2", "pass": SECRET}},
        b_sens={"conn": True}, a_sens={"conn": True},
    )))
    e = _one(s)
    (a,) = e.attr_changes
    assert a.path == "conn" and a.sensitive
    _assert_secret_nowhere(s)


def test_unknown_with_nested_sensitive_before_does_not_leak():
    # after_unknown=True at the node, before contains a sensitive leaf:
    # the before display must be masked wholesale, not json-dumped.
    s = summarize_plan(_plan(_rc(
        ["update"], name="b",
        before={"cfg": {"token": SECRET}}, after={"cfg": None},
        b_sens={"cfg": {"token": True}}, unknown={"cfg": True},
    )))
    e = _one(s)
    (a,) = e.attr_changes
    assert a.path == "cfg" and a.unknown
    assert a.before == "(sensitive)" and a.after == "(known after apply)"
    _assert_secret_nowhere(s)


def test_sensitive_unchanged_emits_nothing():
    s = summarize_plan(_plan(_rc(
        ["update"], name="b",
        before={"password": SECRET, "x": 1}, after={"password": SECRET, "x": 2},
        b_sens={"password": True}, a_sens={"password": True},
    )))
    e = _one(s)
    assert [a.path for a in e.attr_changes] == ["x"]
    _assert_secret_nowhere(s)


def test_sensitive_location_not_surfaced_on_create():
    e = _one(summarize_plan(_plan(_rc(
        ["create"], name="b", after={"location": SECRET},
        a_sens={"location": True},
    ))))
    assert e.location == ""


def test_max_depth_wholesale_respects_sensitivity():
    deep_b = deep_a = None
    deep_b = {"l1": {"l2": {"l3": {"l4": {"l5": {"l6": {"l7": {"l8": {"l9": SECRET}}}}}}}}}
    deep_a = {"l1": {"l2": {"l3": {"l4": {"l5": {"l6": {"l7": {"l8": {"l9": "other"}}}}}}}}}
    sens = {"l1": {"l2": {"l3": {"l4": {"l5": {"l6": {"l7": {"l8": {"l9": True}}}}}}}}}
    s = summarize_plan(_plan(_rc(
        ["update"], name="b", before=deep_b, after=deep_a,
        b_sens=sens, a_sens=sens,
    )))
    _assert_secret_nowhere(_one(s) and s)
```

**Step 2: Run** — `uv run pytest tests/unit/test_iac_plan_summary.py -q`
Expected: PASS (the Task 1 implementation already orders sensitivity before
unknown before descent; if any of these fail, fix `_diff` — these tests are
the spec).

**Step 3: Commit**

```bash
git add tests/unit/test_iac_plan_summary.py
git commit -m "test(lib): sensitive values never appear in plan-summary output"
```

### Task 4: Truncation counts + import recognition

**Files:**
- Modify: `tests/unit/test_iac_plan_summary.py` (append)

**Step 1: Write the tests**

```python
def test_entry_cap_truncates_display_but_not_counts():
    rcs = [_rc(["create"], name=f"c{i}") for i in range(45)] + [_rc(["delete"], name="d")]
    s = summarize_plan(_plan(*rcs))
    assert len(s.entries) == MAX_ENTRIES
    assert s.n_hidden == 46 - MAX_ENTRIES
    # The destroy is beyond the display cap but MUST still be counted/warned.
    assert s.n_destroy == 1 and s.destructive


def test_import_only_change_is_import_verb_not_skipped():
    e = _one(summarize_plan(_plan(_rc(
        ["no-op"], name="adopted", importing_extra=True,
        change_extra={"importing": {"id": "projects/p/buckets/adopted"}},
    ) if False else {
        "address": "google_storage_bucket.adopted",
        "mode": "managed",
        "type": "google_storage_bucket",
        "name": "adopted",
        "change": {
            "actions": ["no-op"],
            "before": {"name": "adopted"},
            "after": {"name": "adopted"},
            "before_sensitive": False,
            "after_sensitive": False,
            "after_unknown": False,
            "importing": {"id": "projects/p/buckets/adopted"},
        },
    })))
    assert e.verb == "import" and e.imported
    assert e.attr_changes == ()


def test_import_plus_update_keeps_update_verb_with_imported_flag():
    rc = _rc(["update"], name="adopted", before={"x": 1}, after={"x": 2})
    rc["change"]["importing"] = {"id": "x"}
    s = summarize_plan(_plan(rc))
    e = _one(s)
    assert e.verb == "update" and e.imported
    assert s.n_update == 1 and s.n_import == 0


def test_action_reason_prettified():
    rc = _rc(["delete", "create"], name="r")
    rc["action_reason"] = "replace_because_cannot_update"
    e = _one(summarize_plan(_plan(rc)))
    assert e.action_reason == "replace because cannot update"
```

(Clean up the first import test to build the dict directly — the inline
conditional above is illustrative of the shape, not style to copy.)

**Step 2: Run** — `uv run pytest tests/unit/test_iac_plan_summary.py -q` → PASS

**Step 3: Commit**

```bash
git add tests/unit/test_iac_plan_summary.py
git commit -m "test(lib): plan-summary truncation counts, import recognition, action_reason"
```

### Task 5: Surface on IacPlanView

**Files:**
- Modify: `agent/iac_artifacts.py` (the `IacPlanView` dataclass, around line 388)
- Test: `tests/unit/test_iac_plan_summary.py` (append) or a new small block in the existing iac_artifacts test module if one exists for `IacPlanView`

**Step 1: Write the failing test**

```python
def test_iac_plan_view_change_summary_property():
    from agent.iac_artifacts import IacPlanView

    v = IacPlanView()
    v._plan_json = _plan(_rc(["create"], name="b"))
    s = v.change_summary
    assert s is not None and s.n_create == 1
    assert v.change_summary is s  # cached

    v2 = IacPlanView()  # _plan_json stays None (unparsed / unverifiable)
    assert v2.change_summary is None
```

**Step 2: Run** — Expected: FAIL with `AttributeError: 'IacPlanView' object has no attribute 'change_summary'`

**Step 3: Implement** — add to `IacPlanView` (next to the `has_create` property; import `cached_property` from `functools` at the top of the module):

```python
    @cached_property
    def change_summary(self):
        """Plain-language summary of the parsed plan (roadmap W1-1), or None.

        None when the plan never parsed (unverifiable / denylist parse error)
        or when summarize_plan cannot produce a FAITHFUL summary — the template
        then falls back to the raw tofu-show block. Advisory display only; the
        worker's authoritative checks are unaffected.
        """
        from driftscribe_lib.iac_plan_summary import summarize_plan

        if self._plan_json is None:
            return None
        return summarize_plan(self._plan_json)
```

(`IacPlanView` is a plain `@dataclass` — no slots — so `cached_property` works;
it is not a dataclass *field*, so construction sites are untouched.)

**Step 4: Run** — `uv run pytest tests/unit/test_iac_plan_summary.py -q` → PASS

**Step 5: Commit**

```bash
git add agent/iac_artifacts.py tests/unit/test_iac_plan_summary.py
git commit -m "feat(agent): expose change_summary on IacPlanView (cached, fail-soft)"
```

### Task 6: Route flag + template — the "What this change does" card

**Files:**
- Modify: `agent/main.py` — `iac_approval_get` ctx (around line 2371): add
  `"show_summary": reason_severity != "error" and not resolved_decision`.
  The `resolved_decision` guard matters (Codex round 2): the GET itself
  renders terminal OUTCOME pages (already applied+merged, terminal failed)
  with `reason_severity` still `""` — those are outcome pages too and must
  not carry the card. The POST handler is NOT touched — its renders never
  pass `show_summary`, so the card never appears there (`| default(false)`).
- Modify: `agent/templates/iac_approval.html` (insert between the integrity/denylist card and the `C2 plan artifact` h2, i.e. after current line 68)
- Test: `tests/unit/test_iac_approval_template.py` (extend)

**Step 1: Write the failing tests** (append). Two helper updates first: add
`change_summary=None` to the existing `_view()` stub so it matches the real
view's attribute surface, and add `"show_summary": True` to the `_render`
base context (the GET passes it for every non-error page; gating tests below
override it).

```python
from driftscribe_lib.iac_plan_summary import AttrChange, ChangeEntry, PlanSummary


def _summary(**kw):
    base = dict(
        entries=(
            ChangeEntry(
                verb="create", rtype="google_storage_bucket",
                type_label="Cloud Storage bucket", name="assets",
                address="google_storage_bucket.assets", location="asia-northeast1",
            ),
        ),
        n_create=1,
    )
    base.update(kw)
    return PlanSummary(**base)


def test_change_summary_card_renders_entry_and_green_note():
    view = _view()
    view.change_summary = _summary()
    html = _render(view=view)
    assert 'data-testid="change-summary"' in html
    assert "Cloud Storage bucket" in html and "assets" in html
    assert "asia-northeast1" in html
    assert 'data-testid="no-destroy-note"' in html
    assert "ds-verb--create" in html


def test_destroy_warning_replaces_green_note():
    view = _view()
    view.change_summary = _summary(
        entries=(ChangeEntry(
            verb="destroy", rtype="google_pubsub_topic", type_label="Pub/Sub topic",
            name="orders", address="google_pubsub_topic.orders",
        ),),
        n_create=0, n_destroy=1,
    )
    html = _render(view=view)
    assert 'data-testid="destroy-warning"' in html
    assert 'data-testid="no-destroy-note"' not in html
    assert "ds-verb--destroy" in html


def test_unclassified_change_blocks_green_note():
    view = _view()
    view.change_summary = _summary(
        entries=(ChangeEntry(
            verb="change", rtype="google_x", type_label="x",
            name="n", address="google_x.n",
        ),),
        n_create=0, n_change=1,
    )
    html = _render(view=view)
    assert 'data-testid="no-destroy-note"' not in html
    assert 'data-testid="destroy-warning"' not in html
    assert "cannot classify" in html


def test_attr_diff_rows_and_sensitive_marker():
    view = _view()
    view.change_summary = _summary(
        entries=(ChangeEntry(
            verb="update", rtype="google_cloud_run_v2_service",
            type_label="Cloud Run service", name="svc",
            address="google_cloud_run_v2_service.svc",
            attr_changes=(
                AttrChange("template.env[0].value", '"1"', '"2"'),
                AttrChange("password", "(sensitive)", "(sensitive)", sensitive=True),
            ),
        ),),
        n_create=0, n_update=1,
    )
    html = _render(view=view)
    assert "template.env[0].value" in html
    assert "sensitive value changed" in html


def test_summary_values_are_html_escaped():
    view = _view()
    view.change_summary = _summary(
        entries=(ChangeEntry(
            verb="update", rtype="google_storage_bucket",
            type_label="Cloud Storage bucket", name="b",
            address="google_storage_bucket.b",
            attr_changes=(AttrChange("label", '"<script>alert(1)</script>"', '"x"'),),
        ),),
        n_create=0, n_update=1,
    )
    html = _render(view=view)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_no_summary_renders_fallback_note():
    html = _render()  # stub has change_summary=None
    assert 'data-testid="summary-unavailable"' in html
    assert 'data-testid="change-summary"' not in html


def test_empty_summary_renders_no_changes_card():
    view = _view()
    view.change_summary = PlanSummary(entries=())
    html = _render(view=view)
    assert 'data-testid="change-summary-empty"' in html
    assert "does not modify any infrastructure" in html


def test_unverifiable_view_suppresses_summary_section():
    view = _view()
    view.unverifiable = True
    view.change_summary = _summary()
    html = _render(view=view, show_summary=False,
                   reason_blocked="artifact unverifiable", reason_severity="error")
    assert 'data-testid="change-summary"' not in html
    assert 'data-testid="summary-unavailable"' not in html


def test_integrity_mismatch_suppresses_summary_even_if_route_flag_leaks():
    # Belt-and-braces: _plan_json is populated even on a digest MISMATCH, so
    # the template independently requires integrity_ok — a card claiming
    # "integrity-checked" must never render for a tampered artifact.
    view = _view()
    view.integrity_ok = False
    view.change_summary = _summary()
    html = _render(view=view, show_summary=True)
    assert 'data-testid="change-summary"' not in html


def test_denylist_violation_suppresses_summary():
    # No green "nothing destroyed" reassurance under a red denylist hard-stop.
    view = _view()
    view.denylist_violations = [("delete-action-forbidden-v1", "x")]
    view.change_summary = _summary()
    html = _render(view=view, show_summary=True)
    assert 'data-testid="change-summary"' not in html
    assert 'data-testid="no-destroy-note"' not in html


def test_missing_show_summary_defaults_to_no_card():
    # The POST outcome renders never pass show_summary => no card there.
    view = _view()
    view.change_summary = _summary()
    html = _render(view=view, show_summary=None)  # then ALSO test with the key removed
    # build a context without the key:
    base = {"pr_number": 42, "view": view, "form_token": None,
            "can_approve": False, "reason_blocked": "", "reason_severity": ""}
    from agent.main import _TEMPLATES
    html = _TEMPLATES.env.get_template("iac_approval.html").render(base)
    assert 'data-testid="change-summary"' not in html


def test_forget_entry_has_explainer_and_no_green_line():
    view = _view()
    view.change_summary = _summary(
        entries=(ChangeEntry(
            verb="forget", rtype="google_storage_bucket",
            type_label="Cloud Storage bucket", name="b",
            address="google_storage_bucket.b",
        ),),
        n_create=0, n_forget=1,
    )
    html = _render(view=view)
    assert "ds-verb--forget" in html
    assert "stops being managed" in html  # explainer: live resource NOT deleted
    assert 'data-testid="no-destroy-note"' not in html


def test_deposed_marker_rendered():
    view = _view()
    view.change_summary = _summary(
        entries=(ChangeEntry(
            verb="destroy", rtype="google_storage_bucket",
            type_label="Cloud Storage bucket", name="b",
            address="google_storage_bucket.b", deposed="byebye01",
        ),),
        n_create=0, n_destroy=1,
    )
    html = _render(view=view)
    assert "leftover copy" in html  # deposed ≠ the current object


def test_hidden_entries_note_and_truncated_attrs_note():
    view = _view()
    view.change_summary = _summary(n_hidden=3)
    html = _render(view=view)
    assert "3 more resource change(s)" in html
```

**Step 2: Run** — `uv run pytest tests/unit/test_iac_approval_template.py -q`
Expected: new tests FAIL (`data-testid="change-summary"` absent); the six
pre-existing tests must still PASS.

**Step 3: Implement** — insert into `iac_approval.html` after the closing
`</div>` of the integrity/denylist card (current line 68), before the
`C2 plan artifact` `<h2>`:

```jinja
      {# Render ONLY for a trustworthy artifact: the route's show_summary flag
         (false/absent on error-class pages AND on all POST outcome renders)
         plus belt-and-braces re-checks of the view's own verdict — _plan_json
         survives an integrity MISMATCH, and this card must never vouch for a
         tampered artifact or show green reassurance under a red hard-stop. #}
      {% if (show_summary | default(false)) and not view.unverifiable
            and view.integrity_ok and not view.denylist_violations %}
        {% set s = view.change_summary | default(none) %}
        <h2 class="ds-h2">What this change does</h2>
        {% if s is none %}
          <div class="ds-note" data-testid="summary-unavailable">
            No structured summary is available for this plan — review the raw
            <code class="ds-code">tofu show</code> output below before approving.
          </div>
        {% elif not s.entries %}
          <div class="ds-card" data-testid="change-summary-empty">
            <p class="ds-ok">No changes — applying this plan does not modify any infrastructure.</p>
          </div>
        {% else %}
          <div class="ds-card" data-testid="change-summary">
            <p class="ds-summary-counts">
              {% if s.n_create %}<span class="ds-verb ds-verb--create">{{ s.n_create }} create</span>{% endif %}
              {% if s.n_update %}<span class="ds-verb ds-verb--update">{{ s.n_update }} update</span>{% endif %}
              {% if s.n_replace %}<span class="ds-verb ds-verb--replace">{{ s.n_replace }} replace</span>{% endif %}
              {% if s.n_destroy %}<span class="ds-verb ds-verb--destroy">{{ s.n_destroy }} destroy</span>{% endif %}
              {% if s.n_import %}<span class="ds-verb ds-verb--import">{{ s.n_import }} import</span>{% endif %}
              {% if s.n_forget %}<span class="ds-verb ds-verb--forget">{{ s.n_forget }} forget</span>{% endif %}
              {% if s.n_change %}<span class="ds-verb ds-verb--change">{{ s.n_change }} other</span>{% endif %}
            </p>
            {% if s.destructive %}
              <div class="ds-blocked" data-testid="destroy-warning">
                ⚠ {{ s.n_destroy + s.n_replace }} resource(s) will be destroyed or replaced.
                Check the list below carefully before approving.
              </div>
            {% elif not s.all_accounted_safe %}
              <div class="ds-note">
                This plan contains state changes or change types the summary
                cannot fully classify — review the raw plan output below before
                approving.
              </div>
            {% else %}
              <p class="ds-ok" data-testid="no-destroy-note">
                No resources will be destroyed or replaced by this plan.
              </p>
            {% endif %}
            <ul class="ds-summary-list">
              {% for e in s.entries %}
                <li class="ds-summary-row">
                  <span class="ds-verb ds-verb--{{ e.verb }}">{{ e.verb }}</span>
                  {{ e.type_label }} <code class="ds-code">{{ e.name }}</code>
                  {% if e.location %}<span class="ds-subtle">in {{ e.location }}</span>{% endif %}
                  {% if e.deposed %}
                    <span class="ds-subtle">— a leftover copy from an earlier replace
                    (deposed <code class="ds-code">{{ e.deposed }}</code>), not the current resource</span>
                  {% endif %}
                  {% if e.imported %}
                    <span class="ds-subtle">— imported into management: the live resource is
                    not changed, it is recorded in OpenTofu state</span>
                  {% endif %}
                  {% if e.verb == "forget" %}
                    <span class="ds-subtle">— removed from OpenTofu state: the live resource
                    is NOT deleted, it just stops being managed</span>
                  {% endif %}
                  <div class="ds-subtle">
                    <code class="ds-code">{{ e.address }}</code>{% if e.action_reason %} — {{ e.action_reason }}{% endif %}
                  </div>
                  {% if e.attr_changes %}
                    <ul class="ds-attr-list">
                      {% for a in e.attr_changes %}
                        <li>
                          <code class="ds-code">{{ a.path }}</code>:
                          {% if a.sensitive %}
                            <span class="ds-subtle">(sensitive value changed — hidden)</span>
                          {% else %}
                            <code class="ds-code">{{ a.before }}</code> → <code class="ds-code">{{ a.after }}</code>
                          {% endif %}
                        </li>
                      {% endfor %}
                      {% if e.attrs_truncated %}
                        <li class="ds-subtle">…more attribute changes — see the raw plan output below.</li>
                      {% endif %}
                    </ul>
                  {% endif %}
                </li>
              {% endfor %}
            </ul>
            {% if s.n_hidden %}
              <p class="ds-subtle">…and {{ s.n_hidden }} more resource change(s) — see the raw plan output below.</p>
            {% endif %}
            <p class="ds-subtle">
              This summary is generated mechanically from the integrity-checked
              plan file — not written by the AI.
            </p>
          </div>
        {% endif %}
      {% endif %}
```

(`| default(none)` keeps any stub/legacy context without the attribute on the
fallback path instead of crashing the render.)

**Step 4: Run** — `uv run pytest tests/unit/test_iac_approval_template.py -q` → ALL PASS

**Step 5: Commit**

```bash
git add agent/main.py agent/templates/iac_approval.html tests/unit/test_iac_approval_template.py
git commit -m "feat(ui): plain-language change summary card on the IaC approval page"
```

### Task 7: CSS — ds-verb badges + summary lists

**Files:**
- Modify: `frontend/src/styles/base.css` (append a new section; reuse `tokens.css` vars only — `--ds-ok-*`, `--ds-warn-*`, `--ds-danger-*`, `--ds-sp-*`, `--ds-fs-1`, `--ds-fw-semibold`, `--ds-tracking-caps`, `--ds-radius-sm`)
- Regenerates: `agent/static/` hashed bundle (committed)

**Step 1: Append the CSS**

```css
/* --- IaC approval: plain-language change summary --------------------------- */
.ds-verb {
  display: inline-block;
  padding: 0 var(--ds-sp-2);
  border: 1px solid;
  border-radius: var(--ds-radius-sm);
  font-size: var(--ds-fs-1);
  font-weight: var(--ds-fw-semibold);
  text-transform: uppercase;
  letter-spacing: var(--ds-tracking-caps);
}
.ds-verb--create,
.ds-verb--import {
  background: var(--ds-ok-surface);
  color: var(--ds-ok-ink);
  border-color: var(--ds-ok-border);
}
.ds-verb--update,
.ds-verb--forget,
.ds-verb--change {
  background: var(--ds-warn-surface);
  color: var(--ds-warn-ink);
  border-color: var(--ds-warn-border);
}
.ds-verb--destroy,
.ds-verb--replace {
  background: var(--ds-danger-surface);
  color: var(--ds-danger-ink);
  border-color: var(--ds-danger-border);
}
.ds-summary-counts {
  display: flex;
  flex-wrap: wrap;
  gap: var(--ds-sp-2);
  margin: 0 0 var(--ds-sp-3);
}
.ds-summary-list {
  list-style: none;
  margin: var(--ds-sp-4) 0 0;
  padding: 0;
  display: grid;
  gap: var(--ds-sp-4);
}
.ds-attr-list {
  list-style: none;
  margin: var(--ds-sp-2) 0 0;
  padding-left: var(--ds-sp-5);
  display: grid;
  gap: var(--ds-sp-1);
  font-size: var(--ds-fs-1);
}
```

**Step 2: Rebuild + sanity-check the bundle**

```bash
make ui
git status --short agent/static
```

Expected: the hashed CSS (and possibly JS) filenames change; `grep ds-verb agent/static/*.css` finds the new classes.

**Step 3: Run the frontend checks**

```bash
cd frontend && npm run check && npm run test:unit
```

Expected: PASS (CSS-only change; the Svelte component tests are unaffected).

**Step 4: Commit**

```bash
git add frontend/src/styles/base.css agent/static
git commit -m "feat(ui): ds-verb badge + summary-list styles for the approval page"
```

### Task 8: Integration — GET route end-to-end

**Files:**
- Modify: `tests/integration/test_iac_approval_get.py` (extend, using its existing `_view(**overrides)` helper and route-level seams)

**Step 1: Write the failing test**

```python
def test_get_renders_change_summary_from_plan_json(client_and_seams):
    # Follow the module's existing fixture/monkeypatch pattern: resolve a view
    # whose _plan_json carries one create + one update, then GET the page.
    view = _view()
    view._plan_json = {
        "format_version": "1.2",
        "resource_changes": [
            {
                "address": "google_pubsub_topic.orders",
                "mode": "managed",
                "type": "google_pubsub_topic",
                "name": "orders",
                "change": {
                    "actions": ["create"],
                    "before": None,
                    "after": {"name": "orders"},
                    "before_sensitive": False,
                    "after_sensitive": False,
                    "after_unknown": {"id": True},
                },
            },
        ],
    }
    # ...monkeypatch agent.main._resolve_iac_plan -> (ref, view) as the
    # neighboring tests do...
    resp = client.get("/iac-approvals/42")
    assert resp.status_code == 200
    assert 'data-testid="change-summary"' in resp.text
    assert "Pub/Sub topic" in resp.text
    assert 'data-testid="no-destroy-note"' in resp.text


def test_get_still_200_with_summary_fallback(client_and_seams):
    # _plan_json None (the load left it unset) => fallback note, page healthy.
    resp = client.get("/iac-approvals/42")
    assert resp.status_code == 200
    assert 'data-testid="summary-unavailable"' in resp.text


def test_get_integrity_mismatch_renders_no_summary_card(client_and_seams):
    # Route-level proof of the trust gate: integrity_ok=False classifies as
    # reason_severity="error" => show_summary False => no card, no green line —
    # even though _plan_json is populated and parseable.
    view = _view()
    view.integrity_ok = False
    view._plan_json = {...same one-create plan as above...}
    resp = client.get("/iac-approvals/42")
    assert resp.status_code == 200
    assert 'data-testid="change-summary"' not in resp.text
    assert 'data-testid="no-destroy-note"' not in resp.text
    assert "MISMATCH" in resp.text  # the existing red verdict still renders


def test_get_terminal_outcome_pages_render_no_summary_card(client_and_seams):
    # The GET itself renders terminal OUTCOME pages (resolved_decision set,
    # reason_severity "") — already applied+merged and terminal failed. Those
    # are outcome pages, not review pages: no card. Follow the module's
    # existing find_decision_for_event seam to install each decision pointer.
    for status, merge_state in (("applied", "merged"), ("failed_state_suspect", None)):
        ...install decision pointer with apply_status=status (+merge_state)...
        view = _view()
        view._plan_json = {...same one-create plan as above...}
        resp = client.get("/iac-approvals/42")
        assert resp.status_code == 200
        assert 'data-testid="change-summary"' not in resp.text
```

Adapt both to the file's actual fixture names/seams (it patches
`agent.main.get_repo` + `agent.main.iac_artifacts.*` and builds real
`IacPlanView`s via `_view`) — the assertions above are the contract.

**Step 2: Run** — `uv run pytest tests/integration/test_iac_approval_get.py -q`
Expected: the two new tests FAIL before the template change is merged into the
branch's working tree, PASS after (they will pass immediately if Tasks 5–6 are
already committed — that's fine, they're regression pins, run them anyway).

**Step 3: Commit**

```bash
git add tests/integration/test_iac_approval_get.py
git commit -m "test(integration): change-summary card renders through GET /iac-approvals"
```

### Task 9: Full verification + PR

**Step 1: Full test suite**

```bash
uv run pytest -q
```

Expected: all green (~2150+ tests; baseline was 2140 + the ~30 added here).

**Step 2: Frontend suite** (already run in Task 7; re-run if anything changed since)

```bash
cd frontend && npm run check && npm run test:unit && cd ..
```

**Step 3: Visual smoke (optional but recommended)**

Render the template standalone with a fixture summary (the template unit tests
already cover structure; eyeball spacing):

```bash
uv run python - <<'EOF'
from agent.main import _TEMPLATES
from agent.iac_artifacts import IacPlanView
v = IacPlanView()
v._plan_json = {"resource_changes": [{"address": "google_storage_bucket.a", "mode": "managed",
  "type": "google_storage_bucket", "name": "a", "change": {"actions": ["create"], "before": None,
  "after": {"location": "asia-northeast1"}, "before_sensitive": False, "after_sensitive": False,
  "after_unknown": False}}]}
html = _TEMPLATES.env.get_template("iac_approval.html").render(
    pr_number=99, view=v, form_token=None, can_approve=False,
    reason_blocked="", reason_severity="pending")
open("/tmp/iac_preview.html", "w").write(html)
EOF
```

Open `/tmp/iac_preview.html` (link the built CSS manually if needed).

**Step 4: PR**

```bash
git push -u origin feat/plan-summary
gh pr create --title "feat(ui): plain-language change summary on the IaC approval page" --body "..."
```

PR body: link this plan + the roadmap doc; call out the design invariants
(never-partial, counts-before-truncation, sensitivity parity with tofu show);
note coordinator-rebake-only deploy.

**Step 5: Codex completed-work review** (per the global instruction — reply on
the planning thread), fold must-fixes, merge on SHIP + CI green.

### Task 10: Deploy

Coordinator rebake per `docs/runbooks/deploy.md`; **traffic is pinned** —
after the Cloud Build deploy, run
`gcloud run services update-traffic driftscribe-agent --to-revisions=<new>=100`
(see memory: coordinator deploy traffic pinning). Verify live:
`/iac-approvals/<recent PR>` renders the summary card on a real C2 artifact;
keep the previous revision as rollback target.

---

## Risks & mitigations

- **A wrong summary is worse than no summary.** Mitigated by: never-partial
  (`None` on anything unparseable, including malformed data rows and unknown
  modes), exact-tuple action classification shared with the denylist
  vocabulary, conservative green line (suppressed for forget/unclassified),
  counts computed pre-truncation, and the raw `tofu show` block staying on the
  page unchanged.
- **Vouching for a bad artifact.** `_plan_json` survives an integrity
  MISMATCH, so the card is double-gated: the route's `show_summary`
  (`reason_severity != "error" and not resolved_decision`, which also covers
  the artifact-vs-PR consistency check only the route can compute and the
  GET's terminal-outcome renders) AND the template's own `integrity_ok` +
  clean-denylist re-check. POST outcome pages never pass the flag → no card.
- **Secret leakage via plan values.** The plan JSON contains real values where
  `tofu show` masks them; the lib enforces the same masks (`*_sensitive`),
  ordered before any stringification, with tests asserting the secret string
  appears nowhere. The page already displays `tofu show` text, so non-sensitive
  values are not a new exposure class.
- **Template/stub drift.** The template reads one new view attribute; the
  `| default(none)` guard + updating the test stub keep legacy render contexts
  on the fallback path instead of 500s (the GET must stay always-200).
- **Giant plans.** Caps (40 entries / 25 attrs / depth 8 / 120 chars) bound
  the page size; `tofu show` text on the same page is already the larger
  payload.
