"""Heuristic monthly-cost estimate for a verified OpenTofu plan (roadmap W4-13).

Deterministic, pure, offline: a small JPY list-price table keyed by resource
type, applied to the plan's own ``before``/``after`` attributes. Never an API
call at render time, never an LLM, never a number for something it can't read.

Rates: Cloud Billing Catalog API list prices, ``currencyCode=JPY``,
asia-northeast1 (Tokyo), fetched 2026-06-11 (see the plan doc's rates ledger).
Heuristic BY DESIGN — :data:`COST_DISCLAIMER` rides every surface that shows a
number. The honest shape for this estate: most resources are usage-based
(¥0 until used); the one always-on number that matters is Cloud Run
min-instances, which IS computable from the plan.

Contract pins (the honesty ledger in the plan doc):
- ``EntryCost.monthly_jpy`` is numeric ⟺ ``kind == "fixed"`` (H7).
- ``estimate_plan_cost`` returns ``None`` whenever ``summarize_plan`` returns
  ``None`` — parity-by-construction (H10); whole walk fail-soft.
- Deposed rows are skipped (the main replace row carries the whole delta) —
  the ONE deliberate divergence from the summary walk, render-guarded in the
  template by ``not e.deposed``.
- Sensitivity is PATH-EXACT (H11): only a blanket-``True`` ancestor mask or
  the cost attr's own mask position blocks a read — a sensitive env var never
  voids a service's estimate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from driftscribe_lib.iac_plan_summary import (
    MAX_ENTRIES,
    classify_verb,
    mask_any,
    sub_mask,
    summarize_plan,
)

__all__ = [
    "COST_DISCLAIMER",
    "RATES_AS_OF",
    "EntryCost",
    "PlanCostEstimate",
    "estimate_plan_cost",
]

RATES_AS_OF = "2026-06-11"

COST_DISCLAIMER = (
    "Cost figures are heuristic estimates from Google Cloud list prices "
    f"(Tokyo region, fetched {RATES_AS_OF}) — not a quote. Usage-based "
    "charges (storage, messages, requests, network) depend entirely on how "
    "much you use."
)

# Google bills the month as 730 hours.
_SECONDS_PER_MONTH = 730 * 3600

# ---- JPY list prices (asia-northeast1, Catalog API, 2026-06-11) ----------- #
# Cloud Run services, request-based billing (cpu_idle=true, provider default):
_RUN_IDLE_CPU_JPY_VCPU_S = 0.000398487
_RUN_IDLE_MEM_JPY_GIB_S = 0.000398487
# Cloud Run services, instance-based billing (cpu_idle=false):
_RUN_INST_CPU_JPY_VCPU_S = 0.002869110
_RUN_INST_MEM_JPY_GIB_S = 0.000318790

_GCS_JPY_GIB_MONTH: dict[str, float] = {
    "STANDARD": 3.67,
    "NEARLINE": 2.55,
    "COLDLINE": 0.96,
    "ARCHIVE": 0.40,
}


@dataclass(frozen=True)
class EntryCost:
    """Cost verdict for one resource change.

    ``monthly_jpy`` is the SIGNED always-on delta this change adds (negative =
    removes) and is numeric ⟺ ``kind == "fixed"`` (H7) — usage/free/unknown
    entries carry their story in ``note`` only. ``kind`` ∈
    {"fixed", "usage", "free", "unknown"}.
    """

    address: str
    kind: str
    monthly_jpy: float | None
    note: str


@dataclass(frozen=True)
class PlanCostEstimate:
    """Whole-plan estimate. ``entries`` display-capped; totals pre-cap (H8)."""

    entries: tuple[EntryCost, ...]
    monthly_fixed_jpy: float
    n_usage: int
    n_free: int
    n_unknown: int
    n_hidden: int
    headline: str

    @property
    def disclaimer(self) -> str:
        return COST_DISCLAIMER

    @property
    def by_address(self) -> dict[str, EntryCost]:
        """Join key for the template's per-entry rendering (deposed rows are
        never in here — the estimator skips them)."""
        return {e.address: e for e in self.entries}


# --------------------------------------------------------------------------- #
# Small parsers + path-exact mask-aware reads.
# --------------------------------------------------------------------------- #

def _fmt_jpy(v: float) -> str:
    return f"¥{round(abs(v)):,}"


def _cpu_vcpus(v: Any) -> float | None:
    """"1000m" → 1.0; "2" → 2.0; numeric → float; unparseable → None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and v > 0:
        return float(v)
    if isinstance(v, str) and v.strip():
        s = v.strip()
        try:
            if s.endswith("m"):
                return int(s[:-1]) / 1000.0
            return float(s)
        except ValueError:
            return None
    return None


def _mem_gib(v: Any) -> float | None:
    """"512Mi" → 0.5; "1Gi" → 1.0; unparseable → None (provider uses Ki/Mi/Gi)."""
    units = {"Ki": 1.0 / (1024 * 1024), "Mi": 1.0 / 1024, "Gi": 1.0, "Ti": 1024.0}
    if isinstance(v, str) and len(v) > 2 and v[-2:] in units:
        try:
            return float(v[:-2]) * units[v[-2:]]
        except ValueError:
            return None
    return None


_SENSITIVE = object()  # sentinel: the mask says this position is sensitive


def _read(side: Any, mask: Any, *path: Any) -> Any:
    """Walk ``side`` along ``path`` (str keys / int indexes) with PATH-EXACT
    sensitivity (H11): an ancestor blocks only when its mask is the blanket
    ``True``; a sibling's sensitivity never voids this read. Returns the
    value, ``None`` when the path is absent, ``_SENSITIVE`` when the path
    itself is masked. Use for LEAF reads only — returning a container and
    then indexing into it manually would bypass the mask."""
    cur, m = side, mask
    for step in path:
        if m is True:
            return _SENSITIVE
        if isinstance(step, str):
            if not isinstance(cur, dict):
                return None
            cur = cur.get(step)
            m = sub_mask(m, step) if not isinstance(m, bool) else m
        else:
            if not isinstance(cur, list) or step >= len(cur):
                return None
            cur = cur[step]
            if isinstance(m, list):
                m = m[step] if step < len(m) else None
            elif not isinstance(m, bool):
                m = None
    if m is True:
        return _SENSITIVE
    if not isinstance(cur, (dict, list)) and mask_any(m):
        return _SENSITIVE
    return cur


# --------------------------------------------------------------------------- #
# Cloud Run.
# --------------------------------------------------------------------------- #

def _run_cpu_idle(side: Any, mask: Any) -> bool | None:
    """Billing mode from the serving container (heuristic: container 0).
    True = request-based (provider default), False = instance-based,
    None = the flag itself is sensitivity-masked."""
    if not isinstance(side, dict):
        return True
    v = _read(side, mask, "template", 0, "containers", 0, "resources", 0, "cpu_idle")
    if v is _SENSITIVE:
        return None
    return v is not False


def _run_container_count(side: Any) -> int:
    """How many containers to sum (sidecars bill too). Shape-only read —
    never extracts values, so no mask consultation is needed."""
    template = side.get("template")
    if isinstance(template, list) and template and isinstance(template[0], dict):
        containers = template[0].get("containers")
        if isinstance(containers, list) and containers:
            return len(containers)
    return 1


def _run_baseline(side: Any, mask: Any) -> float | None:
    """Always-on JPY/month for one Cloud Run service config side.

    0.0 when the service scales to zero (the estate default, ``scaling: []``);
    ``None`` when a cost-relevant attr is ITSELF sensitivity-masked (H6 —
    path-exact per H11, so unrelated sensitive attrs never void this).
    Missing values use the provider defaults: min 0, 1 vCPU / 512 MiB per
    container, ``cpu_idle=true`` (request-based billing)."""
    if not isinstance(side, dict):
        return 0.0
    mins: list[int] = []
    for path in (("scaling", 0, "min_instance_count"),
                 ("template", 0, "scaling", 0, "min_instance_count")):
        v = _read(side, mask, *path)
        if v is _SENSITIVE:
            return None
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            mins.append(v)
    min_inst = max(mins, default=0)
    if min_inst <= 0:
        return 0.0
    cpu_idle = _run_cpu_idle(side, mask)
    if cpu_idle is None:
        return None
    cpu_rate = _RUN_IDLE_CPU_JPY_VCPU_S if cpu_idle else _RUN_INST_CPU_JPY_VCPU_S
    mem_rate = _RUN_IDLE_MEM_JPY_GIB_S if cpu_idle else _RUN_INST_MEM_JPY_GIB_S
    per_second = 0.0
    for i in range(_run_container_count(side)):
        cpu = _read(side, mask, "template", 0, "containers", i,
                    "resources", 0, "limits", "cpu")
        mem = _read(side, mask, "template", 0, "containers", i,
                    "resources", 0, "limits", "memory")
        if cpu is _SENSITIVE or mem is _SENSITIVE:
            return None
        cpu_v = _cpu_vcpus(cpu)
        mem_v = _mem_gib(mem)
        per_second += (cpu_v if cpu_v is not None else 1.0) * cpu_rate
        per_second += (mem_v if mem_v is not None else 0.5) * mem_rate
    return min_inst * per_second * _SECONDS_PER_MONTH


def _run_min_inst(side: Any, mask: Any) -> int:
    """Display-only min-instance count (baseline already vetted sensitivity)."""
    best = 0
    if isinstance(side, dict):
        for path in (("scaling", 0, "min_instance_count"),
                     ("template", 0, "scaling", 0, "min_instance_count")):
            v = _read(side, mask, *path)
            if isinstance(v, int) and not isinstance(v, bool) and v > best:
                best = v
    return best


def _run_zero_note(cpu_idle: bool | None) -> str:
    """Scale-to-zero wording, honest about the billing mode (Codex MF-5):
    instance-based billing is never described as 'billed only while handling requests'."""
    if cpu_idle is False:
        return ("scales to zero when idle — instance-based billing: billed "
                "for the full time instances are running, ¥0/month only "
                "while fully idle")
    if cpu_idle is None:
        return "scales to zero when idle — ¥0/month while idle"
    return ("¥0/month while idle — scales to zero; billed only while "
            "handling requests")


def _est_run(address: str, verb: str, change: dict) -> EntryCost:
    before, b_mask = change.get("before"), change.get("before_sensitive")
    after, a_mask = change.get("after"), change.get("after_sensitive")
    b = _run_baseline(before, b_mask)
    a = _run_baseline(after, a_mask) if verb != "destroy" else 0.0
    if b is None or a is None:
        return EntryCost(
            address, "unknown", None,
            "a cost-relevant attribute is hidden as sensitive — no always-on estimate")
    delta = a - b
    if verb == "destroy":
        if b > 0.5:
            return EntryCost(
                address, "fixed", -b,
                f"stops being billed — removes about {_fmt_jpy(b)}/month "
                "of always-warm cost")
        return EntryCost(address, "usage", None, "stops being billed once destroyed")
    if a < 0.5 and b < 0.5:
        return EntryCost(address, "usage", None,
                         _run_zero_note(_run_cpu_idle(after, a_mask)))
    if abs(delta) < 0.5:
        return EntryCost(
            address, "fixed", 0.0,
            f"always-warm cost unchanged at about {_fmt_jpy(a)}/month")
    if verb == "create":
        n = _run_min_inst(after, a_mask)
        return EntryCost(
            address, "fixed", a,
            f"about {_fmt_jpy(a)}/month — {n} always-warm "
            f"instance{'s' if n != 1 else ''} kept running")
    return EntryCost(
        address, "fixed", delta,
        f"always-warm cost changes by about {_fmt_jpy(delta)}/month "
        f"({'up' if delta > 0 else 'down'} from about {_fmt_jpy(b)} "
        f"to about {_fmt_jpy(a)})")


# --------------------------------------------------------------------------- #
# Other estate types.
# --------------------------------------------------------------------------- #

def _est_bucket(address: str, verb: str, change: dict) -> EntryCost:
    if verb == "destroy":
        return EntryCost(
            address, "usage", None,
            "stops being billed — note: any data still in it is deleted with it")

    def cls(side: Any, mask: Any) -> str | None:
        """Known class; "STANDARD" when ABSENT (the provider default); None
        when masked OR present-but-unrecognized — an unknown class is never
        priced as Standard (Codex MF-3, H5)."""
        if not isinstance(side, dict):
            return "STANDARD"
        v = _read(side, mask, "storage_class")
        if v is _SENSITIVE:
            return None
        if v is None or v == "":
            return "STANDARD"
        return v if isinstance(v, str) and v in _GCS_JPY_GIB_MONTH else None

    b_cls = cls(change.get("before"), change.get("before_sensitive"))
    a_cls = cls(change.get("after"), change.get("after_sensitive"))
    if a_cls is None:
        return EntryCost(address, "usage", None,
                         "billed per GiB stored — ¥0/month while empty")
    rate = _GCS_JPY_GIB_MONTH[a_cls]
    if verb in ("update", "replace", "change") and b_cls and b_cls != a_cls:
        return EntryCost(
            address, "usage", None,
            f"storage rate changes from about ¥{_GCS_JPY_GIB_MONTH[b_cls]:.2f} "
            f"to about ¥{rate:.2f} per GiB-month ({b_cls.title()} → "
            f"{a_cls.title()}) — the monthly total depends on how much is stored")
    return EntryCost(
        address, "usage", None,
        f"¥0/month while empty — storage billed at about ¥{rate:.2f}/GiB-month "
        f"({a_cls.title()}, Tokyo list price)")


def _est_topic(address: str, verb: str, change: dict) -> EntryCost:
    if verb == "destroy":
        return EntryCost(address, "usage", None,
                         "stops being billed (it was free to exist)")
    return EntryCost(
        address, "usage", None,
        "free to exist — messages are billed by data volume "
        "(first 10 GiB/month free, then about ¥6,400/TiB)")


def _est_sub(address: str, verb: str, change: dict) -> EntryCost:
    if verb == "destroy":
        return EntryCost(address, "usage", None,
                         "stops being billed (it was free to exist)")
    note = ("free to exist — delivery is billed by data volume "
            "(first 10 GiB/month free, then about ¥6,400/TiB)")
    retain = _read(change.get("after"), change.get("after_sensitive"),
                   "retain_acked_messages")
    if retain is True:
        note += "; retained acknowledged messages add about ¥43/GiB-month while stored"
    return EntryCost(address, "usage", None, note)


def _est_secret(address: str, verb: str, change: dict) -> EntryCost:
    """Version storage is ¥9.56/version-month AFTER the project's first 6 free
    version-replicas — free-tier exhaustion is unknowable from the plan, so
    this is never a numeric fixed delta (Codex MF-6)."""
    if verb == "destroy":
        return EntryCost(
            address, "usage", None,
            "stops being billed — its stored versions stop accruing charges")
    return EntryCost(
        address, "usage", None,
        "about ¥10/month per stored version, after the project's first 6 free "
        "version-replicas — the total depends on versions your project already stores")


def _est_secret_version(address: str, verb: str, change: dict) -> EntryCost:
    if verb == "destroy":
        return EntryCost(
            address, "usage", None,
            "stops being billed — about ¥10/month less while it was stored "
            "(unless it fell within the project's first 6 free version-replicas)")
    return EntryCost(
        address, "usage", None,
        "about ¥10/month while stored — free if it falls within the project's "
        "first 6 free version-replicas")


_FREE_GENERIC = "free — this resource itself has no charge"

_FREE_TYPES: dict[str, str] = {
    "google_service_account": _FREE_GENERIC,
    "google_project_iam_member": _FREE_GENERIC,
    "google_project_iam_binding": _FREE_GENERIC,
    "google_project_iam_custom_role": _FREE_GENERIC,
    "google_cloud_run_v2_service_iam_member": _FREE_GENERIC,
    "google_compute_network": _FREE_GENERIC,
    "google_compute_subnetwork": _FREE_GENERIC,
    "google_compute_firewall": _FREE_GENERIC,
    "google_eventarc_trigger": (
        "free — the trigger itself has no charge (events it delivers ride "
        "Pub/Sub, billed by volume)"),
}

_USAGE_GENERIC: dict[str, str] = {
    "google_artifact_registry_repository": (
        "¥0/month while empty — billed for stored images (first 0.5 GiB free)"),
    "google_firestore_database": (
        "usage-based — billed per read/write/storage, with a permanent free tier"),
}

_TYPE_ESTIMATORS = {
    "google_cloud_run_v2_service": _est_run,
    "google_storage_bucket": _est_bucket,
    "google_pubsub_topic": _est_topic,
    "google_pubsub_subscription": _est_sub,
    "google_secret_manager_secret": _est_secret,
    "google_secret_manager_secret_version": _est_secret_version,
}


def _estimate_rc(address: str, rtype: str, verb: str, change: dict) -> EntryCost:
    if verb == "import":
        return EntryCost(
            address, "free", None,
            "no billing change — this resource already exists and is already "
            "being billed; adopting it does not change what you pay")
    if verb == "forget":
        return EntryCost(
            address, "free", None,
            "no billing change — the live resource keeps running (and keeps "
            "being billed) exactly as before")
    if rtype in _FREE_TYPES:
        return EntryCost(address, "free", None, _FREE_TYPES[rtype])
    if rtype in _USAGE_GENERIC:
        if verb == "destroy":
            return EntryCost(address, "usage", None,
                             "stops being billed once destroyed")
        return EntryCost(address, "usage", None, _USAGE_GENERIC[rtype])
    est = _TYPE_ESTIMATORS.get(rtype)
    if est is None:
        return EntryCost(address, "unknown", None,
                         "no cost estimate available for this resource type")
    return est(address, verb, change)


# --------------------------------------------------------------------------- #
# The public walk.
# --------------------------------------------------------------------------- #

def _headline(fixed: float, n_usage: int, n_unknown: int,
              all_import: bool, all_destroy: bool) -> str:
    if all_import:
        return ("Adopting costs nothing extra — these resources already exist "
                "and are already being billed.")
    if all_destroy and -0.5 <= fixed <= 0.5:
        # Codex round-2 SF-1: "adds no cost until used" is odd framing for a
        # plan that only removes things.
        return ("Removes resources — no computable always-on cost change; "
                "usage-based charges stop when the resources are gone.")
    if fixed > 0.5:
        base = f"Adds about {_fmt_jpy(fixed)}/month in always-on cost"
    elif fixed < -0.5:
        base = f"Reduces always-on cost by about {_fmt_jpy(fixed)}/month"
    else:
        base = "Adds no always-on cost — ¥0/month until it is used"
    if n_usage:
        base += ", plus usage-based charges that depend on how much you use"
    if n_unknown:
        base += (f" ({n_unknown} resource{'s have' if n_unknown != 1 else ' has'} "
                 "no estimate)")
    return base + "."


def estimate_plan_cost(plan_json: Any) -> PlanCostEstimate | None:
    """Whole-plan heuristic cost estimate, or ``None``.

    H10 parity-by-construction: refuses whenever ``summarize_plan`` refuses,
    so the cost card can never render (or contradict) where the summary card
    doesn't — the guard inherits ALL of the summary walk's never-partial
    validation (unknown mode, data-mode mutation rows, malformed importing /
    deposed / actions, …) without re-deriving any of it. Whole walk fail-soft
    (any unexpected exception → ``None`` — advisory display must never take
    the approval page down). After the guard, every skip below is one the
    summary walk PROVED well-formed; deposed rows are the one deliberate
    divergence (see module docstring)."""
    try:
        if summarize_plan(plan_json) is None:
            return None
        rcs = plan_json.get("resource_changes") or []
        out: list[EntryCost] = []
        fixed = 0.0
        n_usage = n_free = n_unknown = 0
        n_entries = 0
        all_import = True
        all_destroy = True
        for rc in rcs:
            if rc.get("mode") == "data":
                continue  # guard proved: a well-formed data READ (the only skippable kind)
            if rc.get("deposed"):
                continue  # delete half of a replace; the main row carries the delta
            change = rc["change"]
            verb = classify_verb(
                tuple(change["actions"]), change.get("importing") is not None
            )
            if verb is None:
                continue  # pure no-op/read without an import
            ec = _estimate_rc(rc["address"], rc["type"], verb, change)
            n_entries += 1
            if verb != "import":
                all_import = False
            if verb != "destroy":
                all_destroy = False
            if ec.monthly_jpy is not None:
                fixed += ec.monthly_jpy
            if ec.kind == "usage":
                n_usage += 1
            elif ec.kind == "free":
                n_free += 1
            elif ec.kind == "unknown":
                n_unknown += 1
            out.append(ec)
        return PlanCostEstimate(
            entries=tuple(out[:MAX_ENTRIES]),
            monthly_fixed_jpy=fixed,
            n_usage=n_usage,
            n_free=n_free,
            n_unknown=n_unknown,
            n_hidden=max(0, len(out) - MAX_ENTRIES),
            headline=_headline(fixed, n_usage, n_unknown,
                               all_import and n_entries > 0,
                               all_destroy and n_entries > 0),
        )
    except Exception:  # noqa: BLE001 — advisory display, fail-soft by contract
        return None
