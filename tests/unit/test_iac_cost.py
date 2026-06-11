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
