"""Structure locks for the tofu-apply-sa Pub/Sub least-privilege cutover
(2026-06-08 follow-up — docs/plans/2026-06-08-tofu-apply-pubsub-least-privilege.md).

`tofu-apply-sa` is the SOLE MUTATOR. Phase 3 hand-granted it the broad predefined
`roles/pubsub.editor` ("temporary … custom create/update role is the tighter future
option" — Phase-3 execution plan). This cutover replaces it with a custom, minimal
project role `driftscribeTofuApplyPubsub` (topic+subscription CRU + attachSubscription
only) and codifies the grant in the bootstrap script. These are cheap text locks on
that posture, mirroring tests/unit/test_setup_c5f_hardening_script.py.

The role is INTENTIONALLY minimal for the resource shape currently declared in
iac/checkout_events.tf (one topic + one pull subscription, no IAM/schemas/snapshots).
If iac/ later adds tags/schemas/DLQ/snapshots/Pub-Sub-IAM, this role MUST expand and
these tests should be updated in lockstep.
"""
from __future__ import annotations

import re
from pathlib import Path

LIB = Path("infra/scripts/_setup_lib.sh")
IAC_BACKEND = Path("infra/scripts/setup_iac_backend.sh")

_ROLE_ID = "driftscribeTofuApplyPubsub"

# The EXACT permission set the custom role must carry (minimal CRU + attach).
_EXPECTED_PERMS = {
    "pubsub.topics.create",
    "pubsub.topics.get",
    "pubsub.topics.list",
    "pubsub.topics.update",
    "pubsub.topics.attachSubscription",
    "pubsub.subscriptions.create",
    "pubsub.subscriptions.get",
    "pubsub.subscriptions.list",
    "pubsub.subscriptions.update",
}

# Permissions that MUST NOT appear in the role (data-plane, IAM, delete, breadth).
_FORBIDDEN_PERMS = {
    "pubsub.topics.publish",
    "pubsub.subscriptions.consume",
    "pubsub.topics.delete",
    "pubsub.subscriptions.delete",
    "pubsub.topics.setIamPolicy",
    "pubsub.topics.getIamPolicy",
    "pubsub.subscriptions.setIamPolicy",
    "pubsub.subscriptions.getIamPolicy",
    "pubsub.topics.detachSubscription",
}


def _body(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _pubsub_role_perms() -> set[str]:
    """Extract the comma-separated permission CSV passed to the custom-role helper
    for driftscribeTofuApplyPubsub (the quoted arg starting at pubsub.topics.create).
    Isolating the CSV — not the whole script — keeps the forbidden-perm assertions
    immune to permission-like words in comments (e.g. run.developer's 'setIamPolicy')."""
    m = re.search(r'"(pubsub\.topics\.create[^"]*)"', _body(IAC_BACKEND))
    assert m, "no driftscribeTofuApplyPubsub permission CSV found in setup_iac_backend.sh"
    return set(m.group(1).split(","))


# --------------------------------------------------------------------------- #
# Shared lib helper exists and is idempotent (describe -> update | create).
# --------------------------------------------------------------------------- #


def test_lib_defines_custom_role_helper() -> None:
    body = _body(LIB)
    assert "create_or_update_custom_role_idempotent()" in body, (
        "missing helper: create_or_update_custom_role_idempotent"
    )


def test_custom_role_helper_is_idempotent_describe_then_create_or_update() -> None:
    # Re-runs must converge: describe the role, then either update its permissions
    # (exists) or create it (absent). All three gcloud verbs must be present.
    body = _body(LIB)
    for verb in ("gcloud iam roles describe", "gcloud iam roles create", "gcloud iam roles update"):
        assert verb in body, f"custom-role helper missing `{verb}`"


# --------------------------------------------------------------------------- #
# setup_iac_backend.sh creates + grants the minimal custom role.
# --------------------------------------------------------------------------- #


def test_apply_sa_gets_custom_pubsub_role() -> None:
    body = _body(IAC_BACKEND)
    # Created via the idempotent helper, by exact role id.
    assert "create_or_update_custom_role_idempotent" in body
    assert _ROLE_ID in body
    # Granted to the apply SA by its fully-qualified custom-role path.
    assert (
        f'grant_role_idempotent "$PROJECT" "serviceAccount:${{APPLY_SA}}" '
        f'"projects/${{PROJECT}}/roles/{_ROLE_ID}"'
    ) in body, "apply SA not granted the custom Pub/Sub role via grant_role_idempotent"


def test_custom_pubsub_role_has_exact_minimal_permission_set() -> None:
    assert _pubsub_role_perms() == _EXPECTED_PERMS


def test_custom_pubsub_role_includes_attachsubscription() -> None:
    # Under-scoping regression guard: creating a subscription bound to a topic
    # REQUIRES pubsub.topics.attachSubscription (GCP access-control). Dropping it
    # would 403 the next subscription-creating apply.
    assert "pubsub.topics.attachSubscription" in _pubsub_role_perms()


def test_custom_pubsub_role_excludes_dataplane_iam_and_delete() -> None:
    perms = _pubsub_role_perms()
    for forbidden in _FORBIDDEN_PERMS:
        assert forbidden not in perms, f"custom role must not grant {forbidden}"


# --------------------------------------------------------------------------- #
# pubsub.editor cutover: gated removal, never re-added.
# --------------------------------------------------------------------------- #


def test_pubsub_editor_removal_is_gated() -> None:
    # Mirrors the c5f datastore cutover convention: a default re-run only ADDS the
    # tight custom role; the broad roles/pubsub.editor removal is the deliberate
    # cutover, behind an explicit flag (default off → idempotent, never strands).
    body = _body(IAC_BACKEND)
    assert "roles/pubsub.editor" in body, "expected the pubsub.editor cutover removal"
    assert "SETUP_TOFU_APPLY_PUBSUB_CUSTOM" in body, (
        "the roles/pubsub.editor removal must be gated behind SETUP_TOFU_APPLY_PUBSUB_CUSTOM"
    )


def test_pubsub_editor_only_removed_never_added() -> None:
    # The script may reference roles/pubsub.editor only to REMOVE it; never an add.
    for line in _body(IAC_BACKEND).splitlines():
        if "roles/pubsub.editor" in line and "add-iam-policy-binding" in line:
            raise AssertionError(f"setup_iac_backend.sh must not add pubsub.editor: {line}")


def test_pubsub_api_is_enabled() -> None:
    # Codifying Pub/Sub support means codifying its API enablement too.
    assert "pubsub.googleapis.com" in _body(IAC_BACKEND)
