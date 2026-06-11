# Cost Estimate Per Change (ClickOps Wave 4, item 13) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A deterministic, honesty-pinned "what will this cost per month" line on the IaC approval page and in the `load_iac_plan` chat tool, computed from the verified plan.json by a pure JPY list-price heuristic table — no external pricing API, no LLM, no new credentials.

**Architecture:** New pure lib `driftscribe_lib/iac_cost.py` walks `plan_json["resource_changes"]` directly (the summary lib's `ChangeEntry` deliberately drops full `after` attrs, but `IacPlanView._plan_json` keeps them), reusing the summary lib's audited verb classification and sensitivity-mask walkers via three new public aliases, and refusing whenever `summarize_plan` refuses (parity-by-construction). A second `cached_property` on `IacPlanView` (`cost_summary`, mirroring `change_summary`) feeds both render surfaces: the trust-gated summary card in `iac_approval.html` and a new `cost` block in `load_iac_plan_tool`. Cost is only ever shown where the plain-language summary is already shown AND the plan is not denylist-blocked — same integrity/trust gate, zero new gating surface.

**Tech Stack:** Python 3.12 (dataclasses, functools.cached_property), Jinja2 (strict-CSP server-side template), pytest. No frontend changes (approval page is server-rendered; chat renders the tool dict via the model).

**Codex plan review:** thread `019eb729-ec14-7290-af56-d411b6b24859`, round 1 **NO-GO** with 6 must-fixes, round 2 **GO** with 3 should-fixes (destroy-only headline, terminal-page live-verify correction, H4 wording) — ALL folded. Round-1 must-fixes folded below: (1) tool cost now gated on `not view.denylist_violations`; (2) never-partial parity-by-construction via a `summarize_plan` guard; (3) unknown bucket storage class → generic note, never a Standard price; (4) `monthly_jpy` numeric ⟺ `kind=="fixed"` (no usage zeros in the contract); (5) `cpu_idle=false` wording fixed (instance-based billing never described as "billed only for traffic"); (6) Secret Manager version costs reframed usage-style (first-6-free tier makes a fixed delta unknowable). Should-fixes folded: whole-walk `try/except`, path-exact `_read` (H11), multi-container summing, exact rate date in the disclaimer, prompt-pin test located at `tests/unit/test_explore_workload_loads.py`, expanded test list.

---

## Why a heuristic table, not Infracost (decision record)

- Infracost would put a third-party binary + an external pricing-API call (new
  secret, new egress) inside the trusted C2 plan-builder or the coordinator —
  the exact supply-chain surface this project keeps minimal.
- For this estate (buckets, Pub/Sub, Cloud Run, SAs/IAM) Infracost mostly
  emits $0.00 usage-dependent rows anyway.
- A deterministic in-repo table matches the approval page's existing trust
  framing: "generated mechanically from the integrity-checked plan file."

## Rates ledger (source of truth for every constant below)

Fetched 2026-06-11 from the **Cloud Billing Catalog API** (`cloudbilling.googleapis.com/v1/services/<id>/skus?currencyCode=JPY`), region asia-northeast1 (Tokyo). JPY figures embed Google's own USD→JPY list conversion (~¥159.4/$).

| SKU | JPY list price |
|---|---|
| Cloud Run Services **Min Instance CPU** (request-based) | ¥0.000398487 / vCPU-second |
| Cloud Run Services **Min Instance Memory** (request-based) | ¥0.000398487 / GiB-second |
| Cloud Run Services CPU (**instance-based**, `cpu_idle=false`) | ¥0.002869110 / vCPU-second |
| Cloud Run Services Memory (**instance-based**) | ¥0.000318790 / GiB-second |
| Cloud Run Requests | first 2M/month free, then ¥0.000063758 each |
| Standard Storage Tokyo | ¥3.666085 / GiB-month |
| Nearline Storage Tokyo | ¥2.550320 / GiB-month |
| Coldline Storage Tokyo | ¥0.956370 / GiB-month |
| Archive Storage Tokyo | ¥0.398488 / GiB-month |
| Pub/Sub Message Delivery Basic | first 10 GiB/month free, then ¥6,375.80 / TiB |
| Pub/Sub retained-ack / backlog | ¥43.04 / GiB-month |
| Secret Manager version replica storage | first 6 free, then ¥9.5637 / version-month |

Worked check: one always-warm Cloud Run instance (1 vCPU, 512 MiB, request-based) = `(1×0.000398487 + 0.5×0.000398487) × 2,628,000 s ≈ ¥1,571/month`. Min-instances 0 (the estate's default — `scaling: []`) = ¥0 idle.

## Honesty invariants (ledger)

| # | Invariant | Where enforced |
|---|---|---|
| H1 | Cost is NEVER shown for an unverifiable / integrity-failed / **denylist-blocked** plan | template: cost block lives inside the existing trust-gated card (which already suppresses on all three); tool: integrity early-returns fire first AND the cost block is explicitly gated on `not view.denylist_violations` (the item-12 summary IS shown for blocked plans — explaining a block is legitimate; *pricing* one implies viability, so cost is not) |
| H2 | Every numeric surface carries the heuristic disclaimer (region, exact rates-as-of date, "not a quote") | `COST_DISCLAIMER` constant rides `PlanCostEstimate.disclaimer`; template footer + tool `cost.disclaimer`; prompt rule pins relay |
| H3 | Adoption (import) = "no billing change — already billed" — never a scary number, never ¥0-as-savings | `_estimate_rc` import branch; adopt-only headline |
| H4 | Usage-based resources are framed "¥0 until used" / "free to exist, billed by use" — never presented as entirely free | per-type notes; `kind="usage"` always has `monthly_jpy=None` |
| H5 | Unknown resource types AND unrecognized attr values say "no estimate available" — never invent (e.g. an unknown storage class is never priced as Standard) | `kind="unknown"`; `_est_bucket.cls()` returns None for present-but-unrecognized classes; headline appends the unknown count |
| H6 | Sensitive-masked cost attrs → conservative fallback, never read through the mask | `_read` consults the sensitivity mask at every step; sensitive ⇒ `_SENSITIVE` ⇒ defaults/unknown |
| H7 | `monthly_jpy` is numeric ⟺ `kind == "fixed"` — usage/free/unknown are ALWAYS `None`; the headline's always-on figure sums only fixed deltas; usage components are named, not numbered | EntryCost contract + accumulation; pinned by test |
| H8 | Totals computed over ALL resource_changes pre-truncation (display capped at 40, `n_hidden` honest) | `estimate_plan_cost` walk vs `entries[:MAX_ENTRIES]` |
| H9 | `forget` = "no billing change — keeps running, keeps being billed" | `_estimate_rc` forget branch |
| H10 | Never-partial parity-by-construction: `estimate_plan_cost` returns `None` whenever `summarize_plan` returns `None` (the cost card can never contradict the summary card about plan validity); whole walk is `try/except → None` | guard at top of `estimate_plan_cost` |
| H11 | Path-exact sensitivity: an UNRELATED sensitive attr (e.g. a secret env var on a Cloud Run service) must NOT void the cost estimate — only blanket-`True` ancestors or the cost attr's own mask position do | `_read` treats only `m is True` as a blanket at ancestors; leaf reads only |

**Deliberate divergence (documented):** the summary card RENDERS deposed rows (with an explainer); the estimator SKIPS them — a deposed row is the delete half of a replace and the main row carries the whole delta. The template's per-entry join guards `not e.deposed`, so no cost line ever appears on a deposed row.

**Gating/autonomy interplay: none.** No new tool, no tier change, no Layer-0 change, no denylist/gate change (⇒ no tofu-editor rebake; coordinator rebake only). The arch-doc tool tables are untouched.

---

### Task 1: Public aliases in `iac_plan_summary` for verb + mask reuse

**Files:**
- Modify: `driftscribe_lib/iac_plan_summary.py` (after `_verb`, ~line 331; `__all__` at top)
- Test: `tests/unit/test_iac_plan_summary.py`

**Step 1: Write the failing test**

```python
def test_public_aliases_for_cost_lib():
    """Wave-4 item 13: iac_cost reuses the audited verb classification and
    sensitivity-mask walkers — public aliases, never a re-derivation."""
    from driftscribe_lib import iac_plan_summary as m

    assert m.classify_verb is m._verb
    assert m.mask_any is m._mask_any
    assert m.sub_mask is m._sub_mask
    for name in ("classify_verb", "mask_any", "sub_mask"):
        assert name in m.__all__
    # behavior smoke (the alias really is the audited function)
    assert m.classify_verb(("no-op",), True) == "import"
    assert m.classify_verb(("create",), False) == "create"
    assert m.classify_verb(("no-op",), False) is None
```

**Step 2: Run it** — `.venv/bin/pytest tests/unit/test_iac_plan_summary.py::test_public_aliases_for_cost_lib -v` → FAIL (no attribute `classify_verb`).

**Step 3: Implement** — in `iac_plan_summary.py`, directly below `_verb`'s definition:

```python
# Wave-4 item 13 (cost estimate): driftscribe_lib.iac_cost reuses the audited
# verb classification and the sensitivity-mask walkers. Public aliases so the
# cost lib never re-derives action-tuple or mask semantics.
classify_verb = _verb
mask_any = _mask_any
sub_mask = _sub_mask
```

Placement: `_mask_any`/`_sub_mask` are defined earlier in the file, so the alias block after `_verb` (~line 331) has all three names in scope. Add `"classify_verb", "mask_any", "sub_mask"` to `__all__`.

**Step 4: Run** the new test + the whole file: `.venv/bin/pytest tests/unit/test_iac_plan_summary.py -q` → all PASS (existing tests unmodified).

**Step 5: Commit** — `feat(lib): public aliases classify_verb/mask_any/sub_mask for cost lib reuse`

---

### Task 2: `driftscribe_lib/iac_cost.py` — the pure estimator

**Files:**
- Create: `driftscribe_lib/iac_cost.py`
- Test: `tests/unit/test_iac_cost.py` (Tasks 3–5)

**Complete target code:**

```python
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
    instance-based billing is never described as 'billed only for traffic'."""
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
```

Implementation notes:
- `classify_verb(("no-op",), importing=True)` → `"import"` exactly as the summary lib; `importing` = `change.importing is not None` is safe ONLY because the guard already rejected non-dict `importing` (`_build_entry` raises `_Malformed`).
- `summarize_plan` is called once more inside `estimate_plan_cost` (3 walks per request including `change_summary`) — plans are ≤ a few hundred rows; cached_property on the view means once per view lifetime anyway.

**Commit** (with Task 3's first tests): `feat(lib): iac_cost — heuristic JPY plan cost estimator`

---

### Task 3: Core estimator tests (parsers, Cloud Run math, walk semantics)

**Files:**
- Create: `tests/unit/test_iac_cost.py`

Use the same `_rc`/`_plan` builder shapes as `test_iac_plan_summary.py` (copy the tiny helpers — they are 15 lines; do NOT import another test module). The full target test file:

```python
"""Unit tests for driftscribe_lib.iac_cost (ClickOps Wave-4 item 13)."""
import json
import math
from pathlib import Path

import pytest

from driftscribe_lib.iac_cost import (
    COST_DISCLAIMER,
    _cpu_vcpus,
    _mem_gib,
    estimate_plan_cost,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "iac_plan_denylist"

_YEN_PER_WARM_UNIT = 1.5 * 0.000398487 * 2628000  # 1 vCPU + 0.5 GiB, request-based


def _rc(actions, *, rtype="google_storage_bucket", name="b", address=None,
        before=None, after=None, b_sens=False, a_sens=False,
        mode="managed", importing=None, **extra):
    rc = {
        "address": address or f"{rtype}.{name}",
        "type": rtype,
        "name": name,
        "mode": mode,
        "change": {
            "actions": list(actions),
            "before": before,
            "after": after,
            "before_sensitive": b_sens,
            "after_sensitive": a_sens,
        },
    }
    if importing is not None:
        rc["change"]["importing"] = importing
    rc.update(extra)
    return rc


def _plan(*rcs):
    return {"format_version": "1.2", "resource_changes": list(rcs)}


def _run_side(min_inst=0, cpu="1000m", memory="512Mi", cpu_idle=True,
              where="service", containers=None):
    scaling = [{"min_instance_count": min_inst}] if min_inst else []
    cont = containers if containers is not None else [{
        "resources": [{"limits": {"cpu": cpu, "memory": memory},
                       "cpu_idle": cpu_idle}],
    }]
    return {
        "location": "asia-northeast1",
        "scaling": scaling if where == "service" else [],
        "template": [{
            "scaling": scaling if where == "template" else [],
            "containers": cont,
        }],
    }


# ---- parsers ---------------------------------------------------------------

@pytest.mark.parametrize("v,want", [
    ("1000m", 1.0), ("250m", 0.25), ("2", 2.0), (1, 1.0), (2.0, 2.0),
    ("garbage", None), ("", None), (None, None), (True, None), (0, None),
])
def test_cpu_vcpus(v, want):
    assert _cpu_vcpus(v) == want


@pytest.mark.parametrize("v,want", [
    ("512Mi", 0.5), ("1Gi", 1.0), ("2Gi", 2.0), ("1024Ki", 1.0 / 1024),
    ("512", None), (512, None), (None, None), ("Mi", None),
])
def test_mem_gib(v, want):
    assert _mem_gib(v) == want


# ---- Cloud Run math ---------------------------------------------------------

def test_run_one_warm_instance_is_about_1571_yen():
    p = _plan(_rc(["create"], rtype="google_cloud_run_v2_service", name="svc",
                  after=_run_side(min_inst=1)))
    est = estimate_plan_cost(p)
    (ec,) = est.entries
    assert ec.kind == "fixed"
    assert math.isclose(ec.monthly_jpy, _YEN_PER_WARM_UNIT, rel_tol=1e-9)
    assert "¥1,571" in ec.note and "1 always-warm instance" in ec.note
    assert "¥1,571" in est.headline and est.headline.startswith("Adds about")


def test_run_scale_to_zero_is_usage_with_no_number():
    p = _plan(_rc(["create"], rtype="google_cloud_run_v2_service", name="svc",
                  after=_run_side(min_inst=0)))
    est = estimate_plan_cost(p)
    (ec,) = est.entries
    assert ec.kind == "usage" and ec.monthly_jpy is None  # H7: no usage zeros
    assert "scales to zero" in ec.note and "handling requests" in ec.note
    assert est.headline.startswith("Adds no always-on cost")
    assert est.monthly_fixed_jpy == 0.0


def test_run_scale_to_zero_instance_based_wording_is_honest():
    # Codex MF-5: cpu_idle=false is instance-based billing — never describe
    # it as "billed only while handling requests".
    p = _plan(_rc(["create"], rtype="google_cloud_run_v2_service", name="svc",
                  after=_run_side(min_inst=0, cpu_idle=False)))
    (ec,) = estimate_plan_cost(p).entries
    assert ec.kind == "usage" and ec.monthly_jpy is None
    assert "instance-based" in ec.note
    assert "handling requests" not in ec.note


def test_run_template_level_scaling_counts_too():
    p = _plan(_rc(["create"], rtype="google_cloud_run_v2_service", name="svc",
                  after=_run_side(min_inst=2, where="template")))
    (ec,) = estimate_plan_cost(p).entries
    assert ec.kind == "fixed"
    assert math.isclose(ec.monthly_jpy, 2 * _YEN_PER_WARM_UNIT, rel_tol=1e-9)
    assert "2 always-warm instances" in ec.note


def test_run_instance_based_billing_uses_instance_rates():
    p = _plan(_rc(["create"], rtype="google_cloud_run_v2_service", name="svc",
                  after=_run_side(min_inst=1, cpu_idle=False)))
    (ec,) = estimate_plan_cost(p).entries
    want = (1.0 * 0.002869110 + 0.5 * 0.000318790) * 2628000
    assert math.isclose(ec.monthly_jpy, want, rel_tol=1e-9)


def test_run_sidecar_containers_are_summed():
    # Codex SF-4: sidecars bill too.
    containers = [
        {"resources": [{"limits": {"cpu": "1000m", "memory": "512Mi"},
                        "cpu_idle": True}]},
        {"resources": [{"limits": {"cpu": "500m", "memory": "256Mi"},
                        "cpu_idle": True}]},
    ]
    p = _plan(_rc(["create"], rtype="google_cloud_run_v2_service", name="svc",
                  after=_run_side(min_inst=1, containers=containers)))
    (ec,) = estimate_plan_cost(p).entries
    want = ((1.0 + 0.5) * 0.000398487 + (0.5 + 0.25) * 0.000398487) * 2628000
    assert math.isclose(ec.monthly_jpy, want, rel_tol=1e-9)


def test_run_update_delta_min_instances_0_to_2():
    p = _plan(_rc(["update"], rtype="google_cloud_run_v2_service", name="svc",
                  before=_run_side(min_inst=0), after=_run_side(min_inst=2)))
    (ec,) = estimate_plan_cost(p).entries
    assert ec.kind == "fixed" and ec.monthly_jpy > 3000
    assert "changes by about ¥3,142/month" in ec.note and "up" in ec.note


def test_run_destroy_with_min_instances_is_negative():
    p = _plan(_rc(["delete"], rtype="google_cloud_run_v2_service", name="svc",
                  before=_run_side(min_inst=1)))
    est = estimate_plan_cost(p)
    (ec,) = est.entries
    assert ec.kind == "fixed" and ec.monthly_jpy < 0
    assert "stops being billed" in ec.note
    assert est.headline.startswith("Reduces always-on cost by about ¥1,571")


def test_run_sensitive_cost_attr_is_conservative_unknown():
    p = _plan(_rc(["create"], rtype="google_cloud_run_v2_service", name="svc",
                  after=_run_side(min_inst=3),
                  a_sens={"scaling": True, "template": True}))
    (ec,) = estimate_plan_cost(p).entries
    assert ec.kind == "unknown" and ec.monthly_jpy is None
    assert "sensitive" in ec.note


def test_run_unrelated_sensitive_attr_does_not_void_estimate():
    # H11 (Codex SF-2): a sensitive env var must not kill the cost estimate.
    after = _run_side(min_inst=1)
    after["template"][0]["containers"][0]["env"] = [
        {"name": "SECRET", "value": "hunter2"}]
    a_sens = {"template": [{"containers": [{"env": [{"value": True}]}]}]}
    p = _plan(_rc(["create"], rtype="google_cloud_run_v2_service", name="svc",
                  after=after, a_sens=a_sens))
    (ec,) = estimate_plan_cost(p).entries
    assert ec.kind == "fixed"
    assert math.isclose(ec.monthly_jpy, _YEN_PER_WARM_UNIT, rel_tol=1e-9)


# ---- other types ------------------------------------------------------------

def test_bucket_create_standard_note():
    p = _plan(_rc(["create"], after={"location": "ASIA-NORTHEAST1",
                                     "storage_class": "STANDARD"}))
    (ec,) = estimate_plan_cost(p).entries
    assert ec.kind == "usage" and ec.monthly_jpy is None
    assert "¥0/month while empty" in ec.note and "¥3.67/GiB-month" in ec.note


def test_bucket_unknown_storage_class_gets_no_rate():
    # Codex MF-3 / H5: never price an unrecognized class as Standard.
    p = _plan(_rc(["create"], after={"storage_class": "MULTI_REGIONAL"}))
    (ec,) = estimate_plan_cost(p).entries
    assert ec.kind == "usage" and ec.monthly_jpy is None
    assert "¥3.67" not in ec.note and "Standard" not in ec.note
    assert "billed per GiB stored" in ec.note


def test_bucket_missing_storage_class_defaults_to_standard():
    p = _plan(_rc(["create"], after={"location": "ASIA-NORTHEAST1"}))
    (ec,) = estimate_plan_cost(p).entries
    assert "¥3.67/GiB-month" in ec.note and "Standard" in ec.note


def test_bucket_storage_class_change_shows_both_rates():
    p = _plan(_rc(["update"],
                  before={"storage_class": "NEARLINE"},
                  after={"storage_class": "STANDARD"}))
    (ec,) = estimate_plan_cost(p).entries
    assert "¥2.55" in ec.note and "¥3.67" in ec.note
    assert "Nearline → Standard" in ec.note


def test_topic_and_sub_free_to_exist():
    p = _plan(
        _rc(["create"], rtype="google_pubsub_topic", name="t", after={}),
        _rc(["create"], rtype="google_pubsub_subscription", name="s",
            after={"retain_acked_messages": True}),
    )
    t, s = estimate_plan_cost(p).entries
    assert t.monthly_jpy is None and s.monthly_jpy is None
    assert "free to exist" in t.note and "10 GiB/month free" in t.note
    assert "¥43/GiB-month" in s.note


def test_secret_version_is_usage_not_fixed():
    # Codex MF-6: the project-level first-6-free tier makes a numeric fixed
    # delta unknowable — both directions stay non-numeric.
    p = _plan(
        _rc(["create"], rtype="google_secret_manager_secret_version",
            name="v", after={}),
        _rc(["delete"], rtype="google_secret_manager_secret_version",
            name="w", before={}),
    )
    est = estimate_plan_cost(p)
    a, b = est.entries
    assert a.kind == "usage" and a.monthly_jpy is None and "¥10/month" in a.note
    assert b.kind == "usage" and b.monthly_jpy is None
    assert est.monthly_fixed_jpy == 0.0
    assert "first 6 free" in a.note


def test_free_types_and_unknown_type():
    p = _plan(
        _rc(["create"], rtype="google_service_account", name="sa", after={}),
        _rc(["create"], rtype="google_bigtable_instance", name="bt", after={}),
    )
    est = estimate_plan_cost(p)
    sa, bt = est.entries
    assert sa.kind == "free" and sa.monthly_jpy is None
    assert bt.kind == "unknown" and bt.monthly_jpy is None
    assert "no estimate" in est.headline


# ---- verbs with billing-neutral semantics ------------------------------------

def test_import_is_no_billing_change():
    p = _plan(_rc(["no-op"], name="adopted", after={"name": "adopted"},
                  importing={"id": "adopted"}))
    est = estimate_plan_cost(p)
    (ec,) = est.entries
    assert ec.kind == "free" and ec.monthly_jpy is None
    assert "already being billed" in ec.note
    assert est.headline.startswith("Adopting costs nothing extra")


def test_forget_is_no_billing_change():
    p = _plan(_rc(["forget"], name="kept", before={}))
    (ec,) = estimate_plan_cost(p).entries
    assert "keeps being billed" in ec.note


def test_destroy_only_usage_plan_headline():
    # Codex round-2 SF-1: a destroy-only plan must not read "adds no cost
    # until it is used".
    p = _plan(_rc(["delete"], name="gone", before={"storage_class": "STANDARD"}))
    est = estimate_plan_cost(p)
    assert est.headline.startswith("Removes resources")
    assert "until it is used" not in est.headline


# ---- walk semantics ----------------------------------------------------------

def test_deposed_and_data_and_noop_rows_skipped():
    p = _plan(
        _rc(["delete"], name="old", deposed="abc123", before={}),
        _rc(["read"], name="d", mode="data"),
        _rc(["no-op"], name="same"),
        _rc(["create"], name="new", after={}),
    )
    est = estimate_plan_cost(p)
    assert [e.address for e in est.entries] == ["google_storage_bucket.new"]


def test_parity_with_summarize_plan_on_malformed_input():
    # H10: whenever the summary refuses, the cost refuses — same inputs.
    from driftscribe_lib.iac_plan_summary import summarize_plan

    bad_row = _plan(_rc(["create"], after={}))
    bad_row["resource_changes"].append(
        {"address": "", "type": "x", "change": {"actions": ["create"]}})
    data_mutation = _plan(_rc(["create"], name="dm", mode="data", after={}))
    bad_importing = _plan(_rc(["no-op"], name="i", importing="yes"))
    unknown_mode = _plan(_rc(["create"], name="m", mode="weird", after={}))
    for plan in (bad_row, data_mutation, bad_importing, unknown_mode,
                 "nope", {"resource_changes": "nope"}):
        assert summarize_plan(plan) is None
        assert estimate_plan_cost(plan) is None


def test_totals_pre_cap_and_n_hidden():
    rcs = [_rc(["create"], rtype="google_cloud_run_v2_service",
               name=f"s{i}", address=f"google_cloud_run_v2_service.s{i}",
               after=_run_side(min_inst=1)) for i in range(45)]
    est = estimate_plan_cost(_plan(*rcs))
    assert len(est.entries) == 40 and est.n_hidden == 5
    assert est.monthly_fixed_jpy == pytest.approx(45 * _YEN_PER_WARM_UNIT)


def test_by_address_and_disclaimer():
    p = _plan(_rc(["create"], after={}))
    est = estimate_plan_cost(p)
    assert est.by_address["google_storage_bucket.b"].kind == "usage"
    assert est.disclaimer == COST_DISCLAIMER
    assert "not a quote" in COST_DISCLAIMER and "Tokyo" in COST_DISCLAIMER
    assert "2026-06-11" in COST_DISCLAIMER


def test_monthly_jpy_numeric_iff_fixed():
    # H7 contract pin across a mixed plan.
    p = _plan(
        _rc(["create"], rtype="google_cloud_run_v2_service", name="warm",
            after=_run_side(min_inst=1)),
        _rc(["create"], rtype="google_cloud_run_v2_service", name="cold",
            after=_run_side(min_inst=0)),
        _rc(["create"], name="bucket", after={}),
        _rc(["create"], rtype="google_service_account", name="sa", after={}),
        _rc(["create"], rtype="google_bigtable_instance", name="bt", after={}),
    )
    for ec in estimate_plan_cost(p).entries:
        assert (ec.monthly_jpy is not None) == (ec.kind == "fixed")


def test_empty_plan_has_zero_entries_not_none():
    est = estimate_plan_cost({"format_version": "1.2", "resource_changes": []})
    assert est is not None and est.entries == ()


# ---- real provider fixtures ---------------------------------------------------

def test_real_run_import_fixture_is_adopt_framed():
    p = json.loads((FIXTURES / "real_import_run_pure_noop.json").read_text())
    est = estimate_plan_cost(p)
    assert est is not None
    (ec,) = est.entries
    assert ec.kind == "free" and "already being billed" in ec.note
    assert est.headline.startswith("Adopting costs nothing extra")


def test_real_bucket_storage_class_update_fixture():
    p = json.loads((FIXTURES / "real_import_bucket_storage_class_update.json").read_text())
    est = estimate_plan_cost(p)
    assert est is not None
    assert any(e.kind == "usage" for e in est.entries)
```

Notes for the implementer:
- The exact Cloud Run delta string in `test_run_update_delta_min_instances_0_to_2` is `¥3,142` (= 2 × 1,570.84 rounded).
- If a fixture-shaped expectation fails (e.g. the real fixtures carry an extra rc), adjust the ASSERTION to the fixture's actual content — never the lib — and note it in the commit message.

**Run:** `.venv/bin/pytest tests/unit/test_iac_cost.py -v` → all PASS. **Commit** with Task 2.

---

### Task 4: `IacPlanView.cost_summary` cached property

**Files:**
- Modify: `agent/iac_artifacts.py` (directly below `change_summary`, ~line 529)
- Test: `tests/unit/test_iac_artifacts.py`

**Step 1: Failing tests** (mirror the file's existing `change_summary` tests and view-construction helper):

```python
def test_cost_summary_none_without_plan_json():
    view = _make_minimal_view()
    assert view._plan_json is None
    assert view.cost_summary is None


def test_cost_summary_present_with_plan_json():
    view = _make_minimal_view()
    view._plan_json = {"format_version": "1.2", "resource_changes": []}
    cost = view.cost_summary
    assert cost is not None and cost.entries == ()
```

**Step 2–4: Implement + run**

```python
    @cached_property
    def cost_summary(self):
        """Heuristic monthly-cost estimate of the parsed plan (roadmap W4-13),
        or None. Same trust posture as ``change_summary``: advisory display
        only, derived from the integrity-checked plan.json, None when the plan
        never parsed (estimate_plan_cost itself refuses whenever
        summarize_plan refuses — parity-by-construction). Surfaces MUST only
        render it where the plain-language summary itself renders, and never
        for a denylist-blocked plan (cost implies viability)."""
        from driftscribe_lib.iac_cost import estimate_plan_cost

        if self._plan_json is None:
            return None
        return estimate_plan_cost(self._plan_json)
```

`.venv/bin/pytest tests/unit/test_iac_artifacts.py -q` → PASS.

**Step 5: Commit** — `feat(agent): IacPlanView.cost_summary cached property`

---

### Task 5: Approval-page rendering (trust-gated card only)

**Files:**
- Modify: `agent/templates/iac_approval.html`
- Test: `tests/unit/test_iac_approval_template.py`

**Step 1: Failing tests** (use the file's `_render`/`_summary`/`_view` helpers; the view stub gains a `cost_summary` attribute — `SimpleNamespace` entries). First check the existing `_view()` helper: if it constructs a stub without `cost_summary`, add `cost_summary=None` to its defaults so every existing test keeps passing unmodified.

```python
def _cost(headline="Adds no always-on cost — ¥0/month until it is used.",
          entries=(), n_hidden=0):
    return SimpleNamespace(
        headline=headline, entries=entries, n_hidden=n_hidden,
        by_address={e.address: e for e in entries},
        disclaimer=("Cost figures are heuristic estimates from Google Cloud "
                    "list prices (Tokyo region, fetched 2026-06-11) — not a "
                    "quote. Usage-based charges (storage, messages, requests, "
                    "network) depend entirely on how much you use."),
    )


def test_cost_headline_entry_and_disclaimer_render():
    v = _view()
    v.change_summary = _summary()
    ec = SimpleNamespace(address="google_storage_bucket.assets",
                         kind="usage", monthly_jpy=None,
                         note="¥0/month while empty — storage billed at about "
                              "¥3.67/GiB-month (Standard, Tokyo list price)")
    v.cost_summary = _cost(entries=(ec,))
    html = _render(view=v, show_summary=True)
    assert 'data-testid="cost-estimate"' in html
    assert "Adds no always-on cost" in html
    assert 'data-testid="cost-entry"' in html and "¥3.67/GiB-month" in html
    assert 'data-testid="cost-disclaimer"' in html and "not a quote" in html


def test_cost_absent_when_cost_summary_none():
    v = _view()
    v.change_summary = _summary()
    v.cost_summary = None
    html = _render(view=v, show_summary=True)
    assert 'data-testid="cost-estimate"' not in html
    assert 'data-testid="cost-disclaimer"' not in html


def test_cost_never_renders_outside_trust_gate():
    # unverifiable / integrity-fail / denylist-blocked all suppress the whole
    # card today — cost must die with it (H1).
    for break_it in (
        lambda v: setattr(v, "unverifiable", True),
        lambda v: setattr(v, "integrity_ok", False),
        lambda v: setattr(v, "denylist_violations", [("import-forbidden-v1", "x")]),
    ):
        v = _view()
        v.change_summary = _summary()
        v.cost_summary = _cost()
        break_it(v)
        html = _render(view=v, show_summary=True)
        assert 'data-testid="cost-estimate"' not in html


def test_cost_entry_skipped_for_deposed_row():
    # A deposed entry shares its address with the main row — the join must
    # not put a cost line on it. Build a summary with a deposed entry plus a
    # normal one at the same address; assert exactly ONE cost-entry div.
    ...  # use the file's entry-builder; count html.count('data-testid="cost-entry"') == 1
```

**Step 3: Template changes** — all inside the existing trust-gated card.

(a) After the blast-radius `{% endif %}` (current line ~144), before the preview-map link:

```jinja
            {% set cost = view.cost_summary | default(none) %}
            {% if cost is not none %}
            <p class="ds-note" data-testid="cost-estimate">
              {{ cost.headline }}
            </p>
            {% endif %}
```

(b) Inside the entries loop, after the address `</div>` (current line ~168), before the `attr_changes` block:

```jinja
                  {% if cost is not none and not e.deposed %}
                    {% set ec = cost.by_address.get(e.address) %}
                    {% if ec %}
                      <div class="ds-subtle" data-testid="cost-entry">Cost: {{ ec.note }}.</div>
                    {% endif %}
                  {% endif %}
```

(c) After the "generated mechanically" footer `</p>` (current line ~195):

```jinja
            {% if cost is not none %}
            <p class="ds-subtle" data-testid="cost-disclaimer">{{ cost.disclaimer }}</p>
            {% endif %}
```

No inline styles, no scripts — strict-CSP (`style-src 'self'`, no script-src) is untouched; only existing `ds-*` classes are used.

**Step 4: Run** `.venv/bin/pytest tests/unit/test_iac_approval_template.py -q` → PASS (every pre-existing test unmodified).

**Step 5: Commit** — `feat(ui): heuristic cost estimate on the IaC approval summary card`

---

### Task 6: `load_iac_plan_tool` cost block + docstring

**Files:**
- Modify: `agent/adk_tools.py` (`load_iac_plan_tool`, after the `cannot_touch` line ~1033)
- Test: `tests/unit/test_load_iac_plan_tool.py`

**Step 1: Failing tests** (use the file's `_make_view` helper + `_adk_tools_mod` patching pattern):

```python
def _plan_json_one_warm_service():
    return {
        "format_version": "1.2",
        "resource_changes": [{
            "address": "google_cloud_run_v2_service.svc",
            "type": "google_cloud_run_v2_service", "name": "svc",
            "mode": "managed",
            "change": {"actions": ["create"], "before": None,
                       "after": {"scaling": [{"min_instance_count": 1}],
                                 "template": [{"scaling": [], "containers": [
                                     {"resources": [{"limits": {"cpu": "1000m",
                                                                "memory": "512Mi"},
                                                     "cpu_idle": True}]}]}]},
                       "before_sensitive": False, "after_sensitive": False},
        }],
    }


def test_cost_block_present_and_rounded(monkeypatch):
    view = _make_view()  # verified, no violations
    view._plan_json = _plan_json_one_warm_service()
    monkeypatch.setattr(_adk_tools_mod, "load_plan_view_from_gcs",
                        lambda *a, **k: view)
    out = _adk_tools_mod.load_iac_plan_tool(7)
    cost = out["cost"]
    assert cost["monthly_always_on_change_jpy"] == 1571
    assert cost["headline"].startswith("Adds about ¥1,571/month")
    assert cost["entries"][0]["monthly_jpy"] == 1571
    assert cost["entries"][0]["kind"] == "fixed"
    assert "not a quote" in cost["disclaimer"]
    assert cost["n_hidden"] == 0


def test_cost_absent_when_summary_unavailable(monkeypatch):
    view = _make_view()
    view._plan_json = None  # change_summary → None, cost_summary → None
    monkeypatch.setattr(_adk_tools_mod, "load_plan_view_from_gcs",
                        lambda *a, **k: view)
    out = _adk_tools_mod.load_iac_plan_tool(7)
    assert out["summary"] is None
    assert "cost" not in out


def test_cost_absent_for_denylist_blocked_plan(monkeypatch):
    # Codex MF-1 / H1: the item-12 summary IS returned for blocked plans
    # (explaining a block is the point) — but cost is NOT (pricing a plan
    # implies viability, and the approval page hides its card anyway).
    view = _make_view(denylist_violations=[("import-forbidden-v1", "boom")])
    view._plan_json = _plan_json_one_warm_service()
    monkeypatch.setattr(_adk_tools_mod, "load_plan_view_from_gcs",
                        lambda *a, **k: view)
    out = _adk_tools_mod.load_iac_plan_tool(7)
    assert out["blocked"] is True
    assert out["summary"] is not None  # item-12 behavior unchanged
    assert "cost" not in out
```

(Exact rounding pin: `round(1.5 × 0.000398487 × 2,628,000) = round(1570.84) = 1571`. Adapt `_make_view(denylist_violations=...)` to the helper's actual signature — it may take the violations list directly or need attribute assignment after construction.)

**Step 3: Implement** — after `out["cannot_touch"] = BLAST_CANNOT_TOUCH_NOTE`:

```python
    # Cost rides ONLY alongside a present summary AND never for a blocked
    # plan: the summary explains what a blocked plan tried to do (item 12);
    # a price tag would frame it as a viable change (H1).
    if not view.denylist_violations:
        cost = view.cost_summary
        if cost is not None:
            out["cost"] = {
                "headline": cost.headline,
                "monthly_always_on_change_jpy": round(cost.monthly_fixed_jpy),
                "entries": [
                    {
                        "address": c.address,
                        "kind": c.kind,
                        "monthly_jpy": (
                            round(c.monthly_jpy) if c.monthly_jpy is not None else None
                        ),
                        "note": c.note,
                    }
                    for c in cost.entries
                ],
                "n_hidden": cost.n_hidden,
                "disclaimer": cost.disclaimer,
            }
    return out
```

(The early-returns for unverifiable / integrity-fail / summary-None all fire BEFORE this point, so the cost block automatically obeys the rest of H1 and only rides alongside a present summary.)

Docstring: extend the "verified →" bullet with `, and a heuristic ``cost`` block on clean plans only — JPY list-price estimate with disclaimer (never for denylist-blocked plans; a price implies viability)`.

**Step 4: Run** `.venv/bin/pytest tests/unit/test_load_iac_plan_tool.py -q` → PASS.

**Step 5: Commit** — `feat(agent): cost block in load_iac_plan_tool output (clean plans only)`

---

### Task 7: Explore prompt rule

**Files:**
- Modify: `workloads/explore/system_prompt.md` (beside the load_iac_plan rules added by item 12)
- Test: `tests/unit/test_explore_workload_loads.py` (the prompt-pin tests live HERE — not a guessed filename; extend the existing pins)

Add one rule:

```markdown
- When the operator asks what a change will COST, use the `cost` block from
  `load_iac_plan_tool` and relay its headline, per-resource notes, and
  disclaimer faithfully. It is a heuristic list-price estimate — present it as
  an estimate, never as a quote or a promise. If the block is absent, say no
  estimate is available; never invent figures. For adoptions, the honest answer
  is the headline's: adopting changes nothing about what they already pay.
```

Extend the existing prompt-pin test with a presence assertion (match the file's existing style — e.g. asserting key phrases like "never as a quote" and "never invent figures" appear in the explore prompt).

**Commit** — `feat(workloads): explore prompt rule for honest cost relay`

---

### Task 8: Full verification

1. `.venv/bin/ruff check --no-cache .` → clean.
2. `.venv/bin/pytest -q` → expect **2767 + new** passed (baseline 2767).
3. `cd frontend && npm run test:unit` → **490** (unchanged — no frontend edits).
4. Review the full diff hunk-by-hunk before PR.

---

### Task 9: Ship (per established workflow)

1. PR: `feat: cost estimate per change — heuristic JPY list-price line on the approval page + chat tool (ClickOps Wave-4 item 13)`.
2. CI watch (`gh pr checks N --watch`, background; plan-builder "skipping" expected).
3. Codex completed-work review on the SAME thread as the plan review (`019eb729-ec14-7290-af56-d411b6b24859`).
4. Squash-merge → coordinator rebake (`infra/cloudbuild.coordinator-update.yaml`, `_TAG=<short-sha>`) → find revision **by image digest** → `update-traffic --to-revisions=<rev>=100`. NO tofu-editor rebake (no gate/denylist change).
5. Live verify (Codex round-2 SF-2: PR #102's approval page is TERMINAL — the
   route suppresses `show_summary` on terminal outcome pages, so the cost card
   correctly does NOT render there; chat is the live surface for #102):
   - `/iac-approvals/102` — page renders unchanged, NO cost card (terminal
     suppression is the correct behavior, assert absence).
   - Real explore chat turn: "what will PR #102 cost me per month?" → exactly
     one `load_iac_plan_tool` call → reply relays the adopt headline
     ("Adopting costs nothing extra…") + disclaimer, no invented figures.
   - Negative: a PR with no artifact still answers honestly (no cost invention).
   - The pending-page card path is covered by the template unit tests; no live
     mutation is manufactured just to see it.
6. Memory + closing report.

## Mode × surface matrix

| Surface | Observe | Propose | Propose+Apply |
|---|---|---|---|
| Approval page cost card | renders (read path, unaffected by autonomy) | renders | renders |
| Chat `load_iac_plan` cost block | available (report tier, no new tool) | available | available |

## Live-verification fixture note

PR #102's plan is adopt-only ⇒ live cost surfaces will show the H3 adoption framing, not a number. The numeric path (¥1,571 Cloud Run math) is covered by unit tests + the worked-check ledger; no live mutation is needed to verify it.
