"""Classify a tofu ``plan.json``: does it CREATE a resource? (Phase C6).

Shared by the coordinator (``agent/main.py`` — to ROUTE create-class plans through the
merge-then-apply-from-main two-step flow) and the worker (``workers/tofu_apply`` — to
ENFORCE the ``iac/``-tree hash gate before admitting a create). It lives in
``driftscribe_lib`` because the coordinator image (``Dockerfile.agent``) copies
``driftscribe_lib/`` but NOT ``workers/``, and both consumers MUST agree on the
predicate. Pure; no I/O.

Fail-closed: any malformed/unexpected structure ⇒ ``True`` (route through the create
path's stricter gate, never the lenient C5 update path). On the WORKER this is strictly
safer — the create path additionally requires the sidecar hash gate, and
``resource_set_guard`` still refuses the malformed plan. On the COORDINATOR, the plan
view is already integrity-/parse-verified before classification, so a malformed plan
does not reach here in practice; the fail-closed default is a backstop.
"""
from __future__ import annotations

from typing import Any

__all__ = ["plan_has_create"]


def plan_has_create(plan_json: Any) -> bool:
    """True iff any managed ``resource_changes`` entry's ``actions`` contains
    ``"create"``.

    A ``replace`` — ``["delete", "create"]`` OR ``["create", "delete"]`` — counts
    (it creates). ``no-op``/``read`` are ignored. A missing/non-list
    ``resource_changes``, a non-dict entry, a non-dict ``change``, or a non-list
    ``actions`` ⇒ ``True`` (fail-closed). A ``module.*`` create also returns ``True``
    here (routing/gating only — the worker's ``resource_set_guard`` still REFUSES
    ``module.*`` regardless).

    An entry whose ``change`` carries a non-null ``importing`` value also returns
    ``True`` (imports are create-class — adopt/import design §4.3)."""
    if not isinstance(plan_json, dict):
        return True
    rcs = plan_json.get("resource_changes")
    if not isinstance(rcs, list):
        return True
    for rc in rcs:
        if not isinstance(rc, dict):
            return True
        change = rc.get("change")
        if not isinstance(change, dict):
            return True
        actions = change.get("actions")
        if not isinstance(actions, list):
            return True
        # Adopt/import design §4.3: an entry with `importing` present is
        # CREATE-CLASS regardless of its actions — the apply writes a NEW
        # address into state, and the lenient C5 path would leave state
        # without config on main (next plan from main proposes DELETING the
        # adopted resource). `importing: null` is treated as absent, same
        # semantics as iac_plan_summary.
        if change.get("importing") is not None:
            return True
        if actions in (["no-op"], ["read"]):
            continue
        if "create" in actions:
            return True
    return False
