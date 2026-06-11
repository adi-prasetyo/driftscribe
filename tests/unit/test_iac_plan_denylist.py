"""Unit tests for tools.iac_plan_denylist (Phase C1 — design doc §5.2).

Table-driven over hand-authored plan-JSON fixtures under
``tests/fixtures/iac_plan_denylist/``. Each single-rule fixture exercises
exactly one rule; multi-rule aggregation has dedicated fixtures.
"""

import dataclasses
from pathlib import Path

import pytest

from tools import iac_plan_denylist  # noqa: F401
from tools.iac_plan_denylist import (
    ADOPTABLE_RESOURCE_TYPES,
    DenylistInput,
    Violation,
    evaluate,
    load_plan_json,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "iac_plan_denylist"


def _load(name: str) -> str:
    """Read a fixture file as text. Caller decides whether to json-parse."""
    return (FIXTURES / name).read_text(encoding="utf-8")


def _rules(violations: list[Violation]) -> list[str]:
    return [v.rule for v in violations]


def test_module_imports():
    assert iac_plan_denylist is not None


def test_violation_is_frozen_dataclass():
    v = Violation(rule="x", detail="y")
    assert dataclasses.is_dataclass(v)
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.rule = "z"  # type: ignore[misc]


def test_empty_plan_with_empty_resource_changes_passes():
    di = DenylistInput(plan={"format_version": "1.2", "resource_changes": []})
    assert evaluate(di) == []


# --- Task 3: load_plan_json + structural rules ---


@pytest.mark.parametrize(
    "fixture",
    [
        "unparseable_empty_file.json",
        "unparseable_not_object.json",
    ],
)
def test_load_plan_json_handles_unparseable(fixture):
    parsed, violation = load_plan_json(_load(fixture))
    assert parsed is None
    assert violation is not None
    assert violation.rule == "plan-json-unparseable"


def test_load_plan_json_happy_path_returns_dict_and_no_violation():
    parsed, violation = load_plan_json('{"format_version": "1.2", "resource_changes": []}')
    assert parsed == {"format_version": "1.2", "resource_changes": []}
    assert violation is None


@pytest.mark.parametrize(
    "fixture",
    [
        "missing_resource_changes.json",
        "resource_changes_not_list.json",
    ],
)
def test_missing_or_non_list_resource_changes_is_denied(fixture):
    parsed, _ = load_plan_json(_load(fixture))
    assert parsed is not None
    assert "plan-json-missing-resource-changes" in _rules(evaluate(DenylistInput(plan=parsed)))


@pytest.mark.parametrize(
    "fixture",
    [
        "resource_changes_entry_not_dict.json",
        "change_not_dict.json",
    ],
)
def test_entry_or_change_not_dict_is_malformed(fixture):
    parsed, _ = load_plan_json(_load(fixture))
    assert parsed is not None
    assert "plan-json-malformed-change" in _rules(evaluate(DenylistInput(plan=parsed)))


# --- Task 4: malformed-change (missing type/actions/non-string) + unknown-action ---


@pytest.mark.parametrize(
    "fixture",
    [
        "malformed_change_missing_actions.json",
        "malformed_change_missing_type.json",
        "actions_not_all_strings.json",
    ],
)
def test_malformed_type_or_actions_emits_malformed_change(fixture):
    parsed, _ = load_plan_json(_load(fixture))
    assert parsed is not None
    assert "plan-json-malformed-change" in _rules(evaluate(DenylistInput(plan=parsed)))


def test_unknown_action_vocabulary_is_denied():
    parsed, _ = load_plan_json(_load("unknown_action_vocabulary.json"))
    assert parsed is not None
    rules = _rules(evaluate(DenylistInput(plan=parsed)))
    assert "unknown-action-forbidden-v1" in rules
    # The structural rules MUST NOT fire — the plan is well-formed, just unknown.
    assert "plan-json-malformed-change" not in rules


# --- Task 5: delete / forget / replace hard-deny on unrelated resources ---


@pytest.mark.parametrize(
    "fixture",
    [
        "benign_no_op.json",
        "benign_payment_demo_update.json",
        "benign_create_unprotected_secret.json",
        "benign_create_unprotected_bucket.json",
        "read_action_is_pass.json",
    ],
)
def test_benign_fixtures_pass(fixture):
    parsed, _ = load_plan_json(_load(fixture))
    assert parsed is not None
    assert evaluate(DenylistInput(plan=parsed)) == [], fixture


def test_delete_unrelated_resource_is_hard_denied():
    parsed, _ = load_plan_json(_load("delete_unprotected_resource.json"))
    assert "delete-action-forbidden-v1" in _rules(evaluate(DenylistInput(plan=parsed)))


def test_forget_unrelated_resource_is_hard_denied():
    parsed, _ = load_plan_json(_load("forget_unprotected_resource.json"))
    assert "forget-action-forbidden-v1" in _rules(evaluate(DenylistInput(plan=parsed)))


@pytest.mark.parametrize(
    "fixture",
    [
        "replace_unprotected_resource.json",
        "replace_create_first_unprotected.json",
    ],
)
def test_replace_unrelated_resource_is_hard_denied(fixture):
    parsed, _ = load_plan_json(_load(fixture))
    assert "replace-action-forbidden-v1" in _rules(evaluate(DenylistInput(plan=parsed))), fixture


# --- Task 6a: control-plane Cloud Run service rule ---


@pytest.mark.parametrize(
    "fixture",
    [
        "control_plane_coordinator_update.json",
        "control_plane_reader_update.json",
        "control_plane_infra_reader_update.json",
        "control_plane_legacy_v1_service_update.json",
        "update_rename_away_from_protected.json",
    ],
)
def test_control_plane_service_update_is_denied(fixture):
    parsed, _ = load_plan_json(_load(fixture))
    assert "control-plane-service" in _rules(evaluate(DenylistInput(plan=parsed))), fixture


def test_control_plane_service_delete_via_before_emits_both_rules():
    """A delete fixture with identity ONLY in `before` must still emit
    control-plane-service (Codex Important #2 — before-side identity)
    alongside delete-action-forbidden-v1.
    """
    parsed, _ = load_plan_json(_load("control_plane_cloudrun_delete_via_before.json"))
    rules = set(_rules(evaluate(DenylistInput(plan=parsed))))
    assert {"control-plane-service", "delete-action-forbidden-v1"} <= rules


# --- Task 6b: control-plane SA rule (account_id + email-local-part) ---


def test_control_plane_sa_update_by_account_id_is_denied():
    parsed, _ = load_plan_json(_load("control_plane_sa_update_account_id.json"))
    assert "control-plane-sa" in _rules(evaluate(DenylistInput(plan=parsed)))


def test_control_plane_sa_update_by_email_local_part_is_denied():
    """When account_id is absent but email is present, the local-part
    extraction must still identify the SA (rollback-agent-sa from
    rollback-agent-sa@<proj>.iam.gserviceaccount.com).
    """
    parsed, _ = load_plan_json(_load("control_plane_sa_update_email_only.json"))
    assert "control-plane-sa" in _rules(evaluate(DenylistInput(plan=parsed)))


# --- Task 6c: control-plane bucket + bucket-object rules ---


@pytest.mark.parametrize(
    "fixture",
    [
        "control_plane_state_bucket_update.json",
        "control_plane_artifact_bucket_create.json",
        "control_plane_state_bucket_object_create.json",
        "control_plane_artifact_bucket_object_update.json",
    ],
)
def test_control_plane_bucket_or_object_change_is_denied(fixture):
    parsed, _ = load_plan_json(_load(fixture))
    assert "control-plane-bucket" in _rules(evaluate(DenylistInput(plan=parsed))), fixture


def test_unprotected_bucket_object_passes():
    parsed, _ = load_plan_json(_load("benign_unprotected_bucket_object.json"))
    assert evaluate(DenylistInput(plan=parsed)) == []


# --- Task 6d: control-plane secret (+ secret_version) + KMS rules ---


def test_control_plane_secret_update_is_denied():
    parsed, _ = load_plan_json(_load("control_plane_hmac_secret_update.json"))
    assert "control-plane-secret" in _rules(evaluate(DenylistInput(plan=parsed)))


def test_control_plane_secret_version_create_is_denied():
    """secret_version resources carry the parent secret_id inside a resource
    path like ``projects/<p>/secrets/<id>``. The denylist extracts it and
    matches against CONTROL_PLANE_SECRET_IDS.
    """
    parsed, _ = load_plan_json(_load("control_plane_secret_version_create.json"))
    assert "control-plane-secret" in _rules(evaluate(DenylistInput(plan=parsed)))


def test_control_plane_tofu_editor_github_pat_secret_create_is_denied():
    """D1-0 forward-compat: the Phase D ``tofu-editor`` worker reads its GitHub
    PAT from the ``tofu-editor-github-pat`` secret. That secret is registered on
    the denylist BEFORE the worker exists, so a create of a
    ``google_secret_manager_secret`` whose ``secret_id`` resolves to it must
    emit control-plane-secret.
    """
    parsed, _ = load_plan_json(_load("control_plane_tofu_editor_pat_secret_create.json"))
    assert "control-plane-secret" in _rules(evaluate(DenylistInput(plan=parsed)))


def test_unprotected_secret_version_passes():
    parsed, _ = load_plan_json(_load("benign_unprotected_secret_version.json"))
    assert evaluate(DenylistInput(plan=parsed)) == []


@pytest.mark.parametrize(
    "fixture",
    [
        "control_plane_kms_update.json",
        "control_plane_kms_keyring_update.json",
    ],
)
def test_control_plane_kms_change_is_denied(fixture):
    parsed, _ = load_plan_json(_load(fixture))
    assert "control-plane-kms" in _rules(evaluate(DenylistInput(plan=parsed))), fixture


def test_secret_version_with_unparseable_path_is_malformed():
    """A secret_version whose `name` / `secret` does not contain /secrets/<id>
    cannot be matched against the allowlist; defensive bias-to-deny via
    plan-json-malformed-change.
    """
    parsed, _ = load_plan_json(_load("malformed_protected_secret_version_no_path.json"))
    assert "plan-json-malformed-change" in _rules(evaluate(DenylistInput(plan=parsed)))


# --- Task 7: WIF + IAM hard-deny rules ---


@pytest.mark.parametrize(
    "fixture",
    [
        "wif_pool_update.json",
        "wif_provider_create.json",
    ],
)
def test_wif_change_emits_both_wif_and_iam_rules(fixture):
    """WIF pool/provider changes must dual-emit `wif-config-change` AND
    `iam-change-forbidden-v1` (Codex Blocker #4 — WIF types are IAM
    identities under the general v1 hard-deny).
    """
    parsed, _ = load_plan_json(_load(fixture))
    rules = set(_rules(evaluate(DenylistInput(plan=parsed))))
    assert {"wif-config-change", "iam-change-forbidden-v1"} <= rules, fixture


@pytest.mark.parametrize(
    "fixture",
    [
        "iam_project_binding_create.json",
        "iam_storage_binding_update.json",
        "iam_run_invoker_grant.json",
        "iam_folder_binding_create.json",
    ],
)
def test_any_iam_resource_change_is_denied_in_v1(fixture):
    """All four fixtures cover the `_iam_` substring rule (project_iam_binding,
    storage_bucket_iam_member, cloud_run_v2_service_iam_member,
    folder_iam_binding). The v1 floor hard-denies every IAM resource type.
    """
    parsed, _ = load_plan_json(_load(fixture))
    assert "iam-change-forbidden-v1" in _rules(evaluate(DenylistInput(plan=parsed))), fixture


# --- Task 8: multi-rule aggregation + bias-to-deny ---


def test_one_plan_can_fire_multiple_rules():
    """A delete on the coordinator SA must emit ALL THREE rules:
    control-plane-sa (identity match), iam-change-forbidden-v1
    (IAM resource type), and delete-action-forbidden-v1 (action floor).
    Regression catch for the no-`continue` fall-through in evaluate.
    """
    parsed, _ = load_plan_json(_load("multi_violations_sa_delete.json"))
    rules = set(_rules(evaluate(DenylistInput(plan=parsed))))
    assert {
        "control-plane-sa",
        "iam-change-forbidden-v1",
        "delete-action-forbidden-v1",
    } <= rules


def test_protected_type_with_no_identity_is_malformed():
    """A control-plane Cloud Run create with after={} (no name) cannot be
    matched against the allowlist; defensive bias-to-deny emits
    plan-json-malformed-change.
    """
    parsed, _ = load_plan_json(_load("malformed_protected_cloud_run_no_name.json"))
    assert "plan-json-malformed-change" in _rules(evaluate(DenylistInput(plan=parsed)))


# --- Task 1: ADOPTABLE_RESOURCE_TYPES ---


def test_adoptable_types_exact_set():
    assert ADOPTABLE_RESOURCE_TYPES == {
        "google_storage_bucket", "google_pubsub_topic",
        "google_pubsub_subscription", "google_cloud_run_v2_service",
    }


def test_adoptable_types_strict_subset_of_identity_templates():
    from driftscribe_lib.iac_hcl import _SUPPORTED_RESOURCE_ASSET_TYPES
    assert ADOPTABLE_RESOURCE_TYPES < set(_SUPPORTED_RESOURCE_ASSET_TYPES)
    assert "google_service_account" not in ADOPTABLE_RESOURCE_TYPES  # D2


# --- Constant-shape guards (catch accidental mutation of the allowlists) ---


def test_constants_are_frozensets_or_tuples():
    """The constants must be immutable so a downstream caller cannot
    accidentally extend the allowlist at runtime.
    """
    from tools.iac_plan_denylist import (
        ALL_KNOWN_TUPLES,
        CLOUD_RUN_SERVICE_TYPES,
        CONTROL_PLANE_BUCKET_SUFFIXES,
        CONTROL_PLANE_KMS_KEY_NAMES,
        CONTROL_PLANE_KMS_KEYRING_NAMES,
        CONTROL_PLANE_SA_ACCOUNT_IDS,
        CONTROL_PLANE_SECRET_IDS,
        CONTROL_PLANE_SERVICE_NAMES,
        IAM_EXTRA_TYPES,
        WIF_RESOURCE_TYPES,
    )

    for c in (
        ALL_KNOWN_TUPLES,
        CLOUD_RUN_SERVICE_TYPES,
        CONTROL_PLANE_KMS_KEY_NAMES,
        CONTROL_PLANE_KMS_KEYRING_NAMES,
        CONTROL_PLANE_SA_ACCOUNT_IDS,
        CONTROL_PLANE_SECRET_IDS,
        CONTROL_PLANE_SERVICE_NAMES,
        IAM_EXTRA_TYPES,
        WIF_RESOURCE_TYPES,
    ):
        assert isinstance(c, frozenset), c
    assert isinstance(CONTROL_PLANE_BUCKET_SUFFIXES, tuple)


# --- Phase 1 import floor (adopt/import design §4.1–§4.2, 2026-06-11) ---


def test_real_provider_import_fixture_is_denied_by_the_floor_alone():
    """THE §4.1/§8 anchor: a REAL `tofu show -json` artifact (live import of
    the c6e probe bucket, google provider 6.50.0) is denied by exactly the
    blanket floor — proving (a) the floor sees provider-real `importing`
    rows, (b) identity checks run on the row without false-firing
    control-plane rules on an unprotected bucket."""
    parsed, _ = load_plan_json(_load("real_import_bucket_pure_noop.json"))
    assert parsed is not None
    assert _rules(evaluate(DenylistInput(plan=parsed))) == ["import-forbidden-v1"]


def test_import_alongside_unrelated_noops_fires_the_floor_exactly_once():
    """The D1-wording regression (design §8): OpenTofu lists EVERY configured
    resource in resource_changes, so unrelated no-op rows accompany any real
    import — they must not add violations."""
    parsed, _ = load_plan_json(_load("import_alongside_unrelated_noops.json"))
    assert _rules(evaluate(DenylistInput(plan=parsed))) == ["import-forbidden-v1"]


def test_import_of_unprotected_type_is_still_denied():
    parsed, _ = load_plan_json(_load("import_unprotected_topic.json"))
    assert _rules(evaluate(DenylistInput(plan=parsed))) == ["import-forbidden-v1"]


@pytest.mark.parametrize(
    ("fixture", "extra_rules"),
    [
        ("import_control_plane_state_bucket.json", {"control-plane-bucket"}),
        ("import_control_plane_service.json", {"control-plane-service"}),
        ("import_control_plane_sa.json", {"control-plane-sa", "iam-change-forbidden-v1"}),
        ("import_control_plane_secret.json", {"control-plane-secret"}),
        ("import_control_plane_kms.json", {"control-plane-kms"}),
        ("import_wif_pool.json", {"wif-config-change", "iam-change-forbidden-v1"}),
    ],
)
def test_importing_control_plane_identities_fires_identity_rules(fixture, extra_rules):
    """§4.1: identity checks now run on importing entries even though a pure
    import plans as no-op — adopting DriftScribe into DriftScribe is
    impossible. Each fixture must fire the floor AND its identity rule(s)."""
    parsed, _ = load_plan_json(_load(fixture))
    rules = set(_rules(evaluate(DenylistInput(plan=parsed))))
    assert ({"import-forbidden-v1"} | extra_rules) <= rules, fixture


def test_malformed_importing_value_is_denied_and_malformed():
    """`importing` must be an object (docs) — a non-dict value is BOTH an
    import (floor fires) and structurally malformed (fail-closed)."""
    parsed, _ = load_plan_json(_load("import_malformed_importing_string.json"))
    rules = set(_rules(evaluate(DenylistInput(plan=parsed))))
    assert {"import-forbidden-v1", "plan-json-malformed-change"} <= rules


def test_sparse_protected_import_row_fails_closed():
    """§4.2: a protected-type importing row whose before/after both lack the
    identity field cannot be cleared against the control-plane sets —
    plan-json-malformed-change fires (bias-to-deny), not a silent pass."""
    parsed, _ = load_plan_json(_load("import_sparse_protected_no_identity.json"))
    rules = set(_rules(evaluate(DenylistInput(plan=parsed))))
    assert {"import-forbidden-v1", "plan-json-malformed-change"} <= rules


def test_importing_null_is_treated_as_absent():
    """`importing: null` is NOT an import (mirrors iac_plan_summary) — and an
    inert leftover import block produces no `importing` at all, so later
    unrelated plans stay clean (design §4.5)."""
    parsed, _ = load_plan_json(_load("importing_null_is_noop_pass.json"))
    assert evaluate(DenylistInput(plan=parsed)) == []


def test_importing_with_unknown_action_fires_both_rules():
    """The floor runs BEFORE the unknown-action continue — an importing row
    with an unaudited action tuple is visible as an import, not only as an
    unknown action (Codex round-1 Important #1)."""
    parsed, _ = load_plan_json(_load("import_unknown_action.json"))
    rules = set(_rules(evaluate(DenylistInput(plan=parsed))))
    assert {"import-forbidden-v1", "unknown-action-forbidden-v1"} <= rules


def test_plain_noop_on_control_plane_identity_still_passes():
    """REGRESSION PIN (Codex round-1 Important #2): widening the identity-check
    gate to `_is_mutation(actions) or importing is not None` must NOT start
    firing control-plane rules on plain no-op rows — every real plan lists
    unchanged resources as no-ops."""
    parsed, _ = load_plan_json(_load("noop_control_plane_service_pass.json"))
    assert evaluate(DenylistInput(plan=parsed)) == []
