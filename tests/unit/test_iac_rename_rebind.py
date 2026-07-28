"""What the gates do with a RENAMED resource (identity drift), empirically pinned.

Background. DriftScribe separates two senses of "drift" (PR #194): Anchor's Cloud
Run *attribute* drift, where both reconciliation directions are just `apply`
because identity is stable, and the infra map's *identity* drift, where a live
object no longer answers to the address IaC declares. PR #244 shipped visibility
for the second kind ("declared in IaC, not found live") and explicitly deferred
reconciliation. This module pins WHY it is deferred, so the deferral rests on
measured behavior rather than on reasoning about the plan graph.

The fixtures under ``tests/fixtures/iac_rename_rebind/`` carry plan structures
observed from real OpenTofu 1.12.0 runs (the CI-pinned version, see
``.github/workflows/iac.yml``). Their provenance and the exact reproduction
procedure are documented in that directory's README; the raw observed plans are
kept alongside under ``raw/``.

Two independent gates are involved, and the distinction is the whole point:

* **C1** (:mod:`driftscribe_lib.iac_plan_denylist`) reads ``resource_changes``
  and NOTHING else. It never inspects ``resource_drift``.
* **The freshness gate** (:func:`workers.tofu_apply.tofu_runner.classify_refresh_drift`)
  reads ``resource_drift``, and runs only when the refresh-only plan reports drift.

A renamed resource whose old object is already gone produces a plan that is
*clean under C1* and is stopped only by the freshness gate. If a future change
relaxes the freshness gate without adding an identity-drift rule to C1, these
tests fail — which is exactly the regression they exist to catch.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from driftscribe_lib.iac_plan_denylist import DenylistInput, evaluate as c1_evaluate
from tools.iac_static_gate import GateInput, GateMode, evaluate as static_gate_evaluate
from workers.tofu_apply.tofu_runner import classify_refresh_drift

FIXTURES = Path(__file__).parent.parent / "fixtures" / "iac_rename_rebind"

RENAME_SHAPES = (
    "rename_old_gone_new_address_import",
    "rename_old_gone_same_address_import",
    "rename_old_live_new_address_import",
)


def _plan(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _rules(violations) -> list[str]:
    return sorted(v.rule for v in violations)


def _freshness_engaged(plan: dict) -> bool:
    """Model ``run_apply_sequence``: ``plan -refresh-only -detailed-exitcode``
    exits 2 only when drift exists, and ``classify_refresh_drift`` is invoked
    ONLY on exit 2. No drift ⇒ the gate never runs."""
    return bool(plan.get("resource_drift"))


def _freshness_verdict(plan: dict):
    """Classify what the *refresh-only* plan would show: drift, no config changes."""
    return classify_refresh_drift({**plan, "resource_changes": []})


# --------------------------------------------------------------------------
# The load-bearing case: a one-PR rebind is clean under C1.
# --------------------------------------------------------------------------


def test_rename_via_new_address_import_passes_c1() -> None:
    """Old object gone live; config drops its address and imports the renamed
    object at a NEW address. ``resource_changes`` is then a lone zero-change
    import — the exact shape C1 already admits for ordinary adoption.

    This is the finding that matters: C1 is NOT what stops a rename.
    """
    plan = _plan("rename_old_gone_new_address_import")
    changes = plan["resource_changes"]
    assert len(changes) == 1
    assert changes[0]["change"]["actions"] == ["no-op"]
    assert changes[0]["change"]["importing"] == {"id": "driftscribe-demo-assets-new"}

    assert c1_evaluate(DenylistInput(plan=plan)) == []


def test_rename_via_new_address_import_is_refused_by_freshness_gate() -> None:
    """The vanished old object surfaces under ``resource_drift`` as a delete,
    which the freshness gate classifies as material and refuses."""
    plan = _plan("rename_old_gone_new_address_import")
    assert _freshness_engaged(plan)

    verdict = _freshness_verdict(plan)
    assert verdict.benign is False
    assert "google_storage_bucket.x" in verdict.reason
    assert "['delete']" in verdict.reason
    assert "material" in verdict.reason


def test_c1_never_reads_resource_drift() -> None:
    """Direct statement of the separation: deleting the drift array cannot change
    a C1 verdict, because C1 only ever consumes ``resource_changes``."""
    plan = _plan("rename_old_gone_new_address_import")
    without_drift = {k: v for k, v in plan.items() if k != "resource_drift"}
    assert c1_evaluate(DenylistInput(plan=plan)) == c1_evaluate(DenylistInput(plan=without_drift)) == []


# --------------------------------------------------------------------------
# Engine behavior pins: what OpenTofu 1.12 actually does with these configs.
# --------------------------------------------------------------------------


def test_same_address_import_block_is_inert() -> None:
    """An ``import`` block whose target is ALREADY in prior state is silently
    skipped, so rewriting a resource's identity attribute in place and adding an
    import for the new object does not produce an adoption. Once refresh drops
    the vanished old object, the plan degrades to a bare ``create`` — which,
    applied, would collide with the already-existing renamed object.

    Observed identically on two unrelated providers (kreuzwerker/docker and
    hashicorp/tfcoremock), confirming it is core plan-graph ordering rather than
    provider behavior.
    """
    plan = _plan("rename_old_gone_same_address_import")
    changes = plan["resource_changes"]
    assert len(changes) == 1
    assert changes[0]["change"]["actions"] == ["create"]
    assert "importing" not in changes[0]["change"], (
        "an import block targeting an address already present in prior state "
        "must NOT produce an importing entry"
    )


def test_rename_while_old_object_still_live_is_blocked_by_c1() -> None:
    """When the old object has NOT actually vanished, dropping its address emits a
    real destroy, and C1 hard-denies on two independent floors."""
    plan = _plan("rename_old_live_new_address_import")
    assert _rules(c1_evaluate(DenylistInput(plan=plan))) == [
        "delete-action-forbidden-v1",
        "import-mixed-plan-forbidden-v1",
    ]


# --------------------------------------------------------------------------
# Positive control + the property that must hold across every rename shape.
# --------------------------------------------------------------------------


def test_ordinary_adoption_still_passes_both_gates() -> None:
    """Guards against over-tightening: the adopt flow that ships today is a
    zero-change import with no drift, so C1 passes and the freshness gate is
    never engaged."""
    plan = _plan("adopt_clean_import_no_drift")
    assert c1_evaluate(DenylistInput(plan=plan)) == []
    assert not _freshness_engaged(plan)


@pytest.mark.parametrize("fixture", RENAME_SHAPES)
def test_every_rename_shape_is_stopped_by_some_gate(fixture: str) -> None:
    """The safety property, stated once and independently of WHICH gate holds:
    no rename/rebind plan reaches a live apply. Deliberately does not assert
    which gate fires, so it keeps holding if the division of labor is
    redesigned — while the per-gate tests above pin today's actual division."""
    plan = _plan(fixture)
    blocked_by_c1 = bool(c1_evaluate(DenylistInput(plan=plan)))
    blocked_by_freshness = _freshness_engaged(plan) and not _freshness_verdict(plan).benign
    assert blocked_by_c1 or blocked_by_freshness, (
        f"{fixture} would reach a live apply — identity-drift reconciliation is "
        "deferred and must stay gated"
    )


# --------------------------------------------------------------------------
# The authoring gate is NOT the blocker.
# --------------------------------------------------------------------------


RENAME_PR_HCL = """\
resource "google_storage_bucket" "checkout_assets_renamed" {
  name     = "driftscribe-demo-assets-new"
  project  = var.project_id
  location = var.region

  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  labels = {
    purpose    = "checkout-demo-assets"
    managed-by = "driftscribe-iac"
  }
}

import {
  to = google_storage_bucket.checkout_assets_renamed
  id = "driftscribe-demo-assets-new"
}
"""


def test_static_gate_admits_the_rename_pr() -> None:
    """AGENT-mode static analysis passes this PR: it is a plain ``iac/*.tf`` edit
    with a well-formed, non-indexed import of an adoptable type whose target
    resource travels in the same changed-file set.

    Recorded so nobody mistakes the authoring layer for the safety boundary —
    a crew CAN write this PR. It is stopped later, at apply time.
    """
    violations = static_gate_evaluate(
        GateInput(
            mode=GateMode.AGENT,
            changed_paths=("iac/checkout_assets.tf",),
            hcl_files={"iac/checkout_assets.tf": RENAME_PR_HCL},
        )
    )
    assert violations == [], _rules(violations)
