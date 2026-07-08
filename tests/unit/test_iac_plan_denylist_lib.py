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
    assert lib.__all__ == [
        "Violation",
        "DenylistInput",
        "load_plan_json",
        "evaluate",
        "RULE_DESCRIPTIONS",
        "ADOPTABLE_RESOURCE_TYPES",
        "CONTROL_PLANE_NODE_MATCHERS",
        "is_control_plane_node",
    ]


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


class TestIsControlPlaneNode:
    """`is_control_plane_node` is the single source of truth shared by the infra
    inventory (aggregate control-plane drift count) and the graph (per-node
    control_plane flag). It classifies a live resource by (asset_type, short
    name) — the same short name both surfaces use as the node label."""

    def test_own_worker_service_is_control_plane(self) -> None:
        assert lib.is_control_plane_node("run.googleapis.com/Service", "driftscribe-agent") is True

    def test_adoptable_probe_service_is_not_control_plane(self) -> None:
        assert lib.is_control_plane_node("run.googleapis.com/Service", "adopt-probe-svc") is False

    def test_tofu_state_bucket_is_control_plane(self) -> None:
        assert (
            lib.is_control_plane_node(
                "storage.googleapis.com/Bucket", "driftscribe-hack-2026-tofu-state"
            )
            is True
        )

    def test_service_managed_staging_bucket_is_control_plane(self) -> None:
        assert (
            lib.is_control_plane_node("storage.googleapis.com/Bucket", "run-sources-1234-asia")
            is True
        )

    def test_ordinary_bucket_is_not_control_plane(self) -> None:
        assert (
            lib.is_control_plane_node(
                "storage.googleapis.com/Bucket", "driftscribe-hack-2026-assets"
            )
            is False
        )

    def test_non_matcher_type_never_flagged_even_on_name_collision(self) -> None:
        # No control-plane identity rule for Pub/Sub: a topic named like a worker
        # service is adoptable, not control-plane.
        assert lib.is_control_plane_node("pubsub.googleapis.com/Topic", "driftscribe-agent") is False

    def test_empty_or_nonstr_name_is_safe(self) -> None:
        assert lib.is_control_plane_node("run.googleapis.com/Service", "") is False
        assert lib.is_control_plane_node("run.googleapis.com/Service", None) is False


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
    assert shim.ADOPTABLE_RESOURCE_TYPES is lib.ADOPTABLE_RESOURCE_TYPES
    # service-managed-bucket additions — pin identity so a local re-definition
    # in tools/ (not a re-export) is caught, not just a type mismatch.
    assert shim.SERVICE_MANAGED_BUCKET_SUFFIXES is lib.SERVICE_MANAGED_BUCKET_SUFFIXES
    assert shim.SERVICE_MANAGED_BUCKET_PREFIXES is lib.SERVICE_MANAGED_BUCKET_PREFIXES
    assert shim.is_service_managed_bucket_name is lib.is_service_managed_bucket_name
    assert shim.SERVICE_MANAGED_PUBSUB_PREFIXES is lib.SERVICE_MANAGED_PUBSUB_PREFIXES
    assert shim.is_service_managed_pubsub_name is lib.is_service_managed_pubsub_name
    assert shim.is_control_plane_node is lib.is_control_plane_node
    assert shim.CONTROL_PLANE_NODE_MATCHERS is lib.CONTROL_PLANE_NODE_MATCHERS
