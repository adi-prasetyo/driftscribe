"""Lock the canonical location of the self-protection denylist in ``driftscribe_lib``.

Phase C4 promoted the denylist out of ``tools/`` (not an installed package, not
shipped in worker containers) into ``driftscribe_lib`` so the ``tofu-apply``
worker can import + re-run it at runtime. These tests guard against a regression
that silently moves the definition back, prove the ``tools`` shim re-exports the
SAME objects (one definition), and pin the C4 addition that the denylist now
protects the real ``driftscribe-tofu-apply`` service name.
"""
from __future__ import annotations

from driftscribe_lib import iac_plan_denylist as lib
from driftscribe_lib.iac_plan_denylist import (
    DenylistInput,
    Violation,
    evaluate,
    load_plan_json,
)


def _service_change(name: str, actions: list[str]) -> dict:
    """A minimal plan.json with one Cloud Run service resource_change."""
    return {
        "resource_changes": [
            {
                "address": f"google_cloud_run_v2_service.{name}",
                "type": "google_cloud_run_v2_service",
                "change": {"actions": actions, "before": {"name": name}, "after": {"name": name}},
            }
        ]
    }


def test_canonical_api_importable_from_lib() -> None:
    assert callable(lib.load_plan_json)
    assert callable(lib.evaluate)
    assert lib.__all__ == ["Violation", "DenylistInput", "load_plan_json", "evaluate"]


def test_load_plan_json_and_evaluate_behave() -> None:
    parsed, v = load_plan_json('{"resource_changes": []}')
    assert v is None
    assert evaluate(DenylistInput(plan=parsed)) == []

    bad, v = load_plan_json("not json")
    assert bad is None
    assert v is not None and v.rule == "plan-json-unparseable"


def test_benign_noop_passes() -> None:
    assert evaluate(DenylistInput(plan=_service_change("payment-demo", ["no-op"]))) == []


def test_control_plane_service_denied() -> None:
    violations = evaluate(DenylistInput(plan=_service_change("driftscribe-agent", ["update"])))
    assert any(v.rule == "control-plane-service" for v in violations)


def test_c4_protects_real_apply_worker_service_name() -> None:
    """C4 addition: the actual deployed name (driftscribe- convention), not just
    the C1 forward-compat ``tofu-apply`` placeholder, must trip control-plane-service
    — the apply worker re-runs THIS denylist on its own fetched plan, so
    self-management of the mutator is hard-denied."""
    for name in ("driftscribe-tofu-apply", "driftscribe-tofu-editor"):
        violations = evaluate(DenylistInput(plan=_service_change(name, ["update"])))
        assert any(v.rule == "control-plane-service" for v in violations), name
    assert "driftscribe-tofu-apply" in lib.CONTROL_PLANE_SERVICE_NAMES


def test_tools_shim_reexports_the_same_objects() -> None:
    """The ``tools`` CLI module must re-export the lib objects identically — not a
    divergent copy — so there is exactly one denylist definition."""
    from tools import iac_plan_denylist as shim

    assert shim.evaluate is evaluate
    assert shim.load_plan_json is load_plan_json
    assert shim.Violation is Violation
    assert shim.DenylistInput is DenylistInput
    # A representative constant must be the SAME object (one definition).
    assert shim.IAM_EXTRA_TYPES is lib.IAM_EXTRA_TYPES
    assert shim.CONTROL_PLANE_SERVICE_NAMES is lib.CONTROL_PLANE_SERVICE_NAMES
