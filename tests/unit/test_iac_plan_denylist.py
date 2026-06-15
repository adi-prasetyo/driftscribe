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


# --- service-managed-bucket: buckets OTHER Google services auto-create ---


@pytest.mark.parametrize(
    "fixture",
    [
        "service_managed_bucket_update.json",
        "import_service_managed_bucket.json",
    ],
)
def test_service_managed_bucket_change_or_import_is_denied(fixture):
    """A bucket a Google service auto-creates (e.g. <project>_cloudbuild) must
    fire service-managed-bucket on BOTH a change and a zero-change import —
    and must NOT be mislabelled control-plane-bucket (DriftScribe's own)."""
    parsed, _ = load_plan_json(_load(fixture))
    rules = _rules(evaluate(DenylistInput(plan=parsed)))
    assert "service-managed-bucket" in rules, fixture
    assert "control-plane-bucket" not in rules, fixture


def test_import_service_managed_bucket_fires_only_that_rule():
    """The import is a zero-change adopt of an adoptable type, so ONLY the
    service-managed-bucket identity rule fires (no import-type-not-adoptable,
    no control-plane-bucket)."""
    parsed, _ = load_plan_json(_load("import_service_managed_bucket.json"))
    assert set(_rules(evaluate(DenylistInput(plan=parsed)))) == {"service-managed-bucket"}


def test_is_service_managed_bucket_name_matches_known_families_only():
    """Bounded set: Cloud Build / App Engine + legacy GCR / Cloud Functions /
    Cloud Run source buckets match; operator buckets (incl. Google-ish
    near-misses) do NOT — the false-positive direction would wrongly block a
    legitimate adoption (Codex 019eca9c #1)."""
    from driftscribe_lib.iac_plan_denylist import is_service_managed_bucket_name as f

    assert f("driftscribe-hack-2026_cloudbuild")
    assert f("my-project.appspot.com")
    assert f("staging.my-project.appspot.com")
    assert f("us.artifacts.my-project.appspot.com")
    assert f("gcf-sources-123456-us-central1")
    assert f("gcf-v2-sources-123456-us-central1")
    assert f("gcf-v2-uploads-123456-us-central1")
    assert f("run-sources-my-project-us-central1")
    # Near-misses an operator might legitimately own — NOT blocked:
    assert not f("gcf-v2-assets")  # the tightened-prefix win
    assert not f("driftscribe-hack-2026-assets")
    assert not f("my-tofu-state")
    assert not f("cloudbuild-logs")  # `_cloudbuild` is a SUFFIX, not a token
    assert not f(None)
    assert not f(123)


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
        SERVICE_MANAGED_BUCKET_PREFIXES,
        SERVICE_MANAGED_BUCKET_SUFFIXES,
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
    for t in (
        CONTROL_PLANE_BUCKET_SUFFIXES,
        SERVICE_MANAGED_BUCKET_SUFFIXES,
        SERVICE_MANAGED_BUCKET_PREFIXES,
    ):
        assert isinstance(t, tuple), t


# --- Phase 2 import admission (adopt/import design §4.2, 2026-06-11) ---
# Phase 1 blanket import-forbidden-v1 replaced with conditional rules.


def test_real_provider_import_pure_noop_is_admitted():
    """THE §4.1/§8 anchor (Phase 2 re-pin): a REAL `tofu show -json` artifact
    (live import of the c6e probe bucket, google provider 6.50.0) is now
    ADMITTED — allowlisted type + zero-change + single import + unprotected.
    Identity checks still run (no control-plane rules fire). Proving the
    conditional rules pass exactly this fixture."""
    parsed, _ = load_plan_json(_load("real_import_bucket_pure_noop.json"))
    assert parsed is not None
    assert _rules(evaluate(DenylistInput(plan=parsed))) == []


def test_import_alongside_unrelated_noops_is_admitted():
    """The D1-wording regression (design §8): OpenTofu lists EVERY configured
    resource in resource_changes, so unrelated no-op rows accompany any real
    import — they must NOT count as 'other mutations' and must not add violations.
    Phase 2: admitted (zero-change + single + unprotected + allowlisted)."""
    parsed, _ = load_plan_json(_load("import_alongside_unrelated_noops.json"))
    assert _rules(evaluate(DenylistInput(plan=parsed))) == []


def test_import_of_allowlisted_topic_is_admitted():
    """Pub/Sub topic is in the D2 four-type allowlist — admitted."""
    parsed, _ = load_plan_json(_load("import_unprotected_topic.json"))
    assert _rules(evaluate(DenylistInput(plan=parsed))) == []


def test_real_provider_import_with_update_fires_with_changes_rule():
    """Provider-real fixture: bucket import WITH actions=update must fire
    import-with-changes-forbidden-v1 and ONLY that rule (no type rule, no
    batch rule)."""
    parsed, _ = load_plan_json(_load("real_import_bucket_with_update.json"))
    assert parsed is not None
    assert _rules(evaluate(DenylistInput(plan=parsed))) == ["import-with-changes-forbidden-v1"]


def test_import_type_not_adoptable_fires_type_rule():
    """google_compute_instance is not in D2 allowlist → import-type-not-adoptable-v1."""
    parsed, _ = load_plan_json(_load("import_type_not_adoptable.json"))
    assert _rules(evaluate(DenylistInput(plan=parsed))) == ["import-type-not-adoptable-v1"]


def test_import_mixed_with_update_fires_mixed_rule():
    """Bucket import no-op + unrelated topic update → import-mixed-plan-forbidden-v1."""
    parsed, _ = load_plan_json(_load("import_mixed_with_update.json"))
    assert _rules(evaluate(DenylistInput(plan=parsed))) == ["import-mixed-plan-forbidden-v1"]


def test_import_batch_two_fires_batch_rule():
    """Two bucket imports, both pure no-op → import-batch-forbidden-v1 (D3)."""
    parsed, _ = load_plan_json(_load("import_batch_two.json"))
    assert _rules(evaluate(DenylistInput(plan=parsed))) == ["import-batch-forbidden-v1"]


@pytest.mark.parametrize(
    ("fixture", "expected_rules"),
    [
        ("import_control_plane_state_bucket.json", {"control-plane-bucket"}),
        ("import_control_plane_service.json", {"control-plane-service"}),
        ("import_control_plane_sa.json", {"control-plane-sa", "iam-change-forbidden-v1", "import-type-not-adoptable-v1"}),
        ("import_control_plane_secret.json", {"control-plane-secret", "import-type-not-adoptable-v1"}),
        ("import_control_plane_kms.json", {"control-plane-kms", "import-type-not-adoptable-v1"}),
        ("import_wif_pool.json", {"wif-config-change", "iam-change-forbidden-v1", "import-type-not-adoptable-v1"}),
    ],
)
def test_importing_control_plane_identities_fires_identity_rules(fixture, expected_rules):
    """§4.1: identity checks run on importing entries even though a pure
    import plans as no-op — adopting DriftScribe into DriftScribe is
    impossible. Each fixture must fire exactly its expected rule(s)."""
    parsed, _ = load_plan_json(_load(fixture))
    rules = set(_rules(evaluate(DenylistInput(plan=parsed))))
    assert rules == expected_rules, fixture


def test_malformed_importing_value_emits_malformed_only():
    """`importing` must be an object — a non-dict value emits
    plan-json-malformed-change. The entry STILL counts as importing (visible
    to the batch/mixed accumulators); here no conditional rule happens to fire
    (no-op actions, adoptable type), so denial rests on the malformed rule."""
    parsed, _ = load_plan_json(_load("import_malformed_importing_string.json"))
    rules = _rules(evaluate(DenylistInput(plan=parsed)))
    assert "plan-json-malformed-change" in rules
    assert "import-forbidden-v1" not in rules


def test_sparse_protected_import_row_fails_closed():
    """§4.2: a protected-type importing row whose before/after both lack the
    identity field cannot be cleared against the control-plane sets —
    plan-json-malformed-change fires (bias-to-deny), not a silent pass."""
    parsed, _ = load_plan_json(_load("import_sparse_protected_no_identity.json"))
    rules = set(_rules(evaluate(DenylistInput(plan=parsed))))
    assert "plan-json-malformed-change" in rules
    assert "import-forbidden-v1" not in rules


def test_importing_null_is_treated_as_absent():
    """`importing: null` is NOT an import (mirrors iac_plan_summary) — and an
    inert leftover import block produces no `importing` at all, so later
    unrelated plans stay clean (design §4.5)."""
    parsed, _ = load_plan_json(_load("importing_null_is_noop_pass.json"))
    assert evaluate(DenylistInput(plan=parsed)) == []


def test_importing_with_unknown_action_fires_both_rules():
    """The import checks run BEFORE the unknown-action continue — an importing row
    with an unaudited action tuple is visible as an import, not only as an
    unknown action. Phase 2: fires import-with-changes-forbidden-v1 (non-no-op
    actions) AND unknown-action-forbidden-v1."""
    parsed, _ = load_plan_json(_load("import_unknown_action.json"))
    rules = set(_rules(evaluate(DenylistInput(plan=parsed))))
    assert {"import-with-changes-forbidden-v1", "unknown-action-forbidden-v1"} <= rules
    assert "import-forbidden-v1" not in rules


def test_plain_noop_on_control_plane_identity_still_passes():
    """REGRESSION PIN (Codex round-1 Important #2): widening the identity-check
    gate to `_is_mutation(actions) or importing is not None` must NOT start
    firing control-plane rules on plain no-op rows — every real plan lists
    unchanged resources as no-ops."""
    parsed, _ = load_plan_json(_load("noop_control_plane_service_pass.json"))
    assert evaluate(DenylistInput(plan=parsed)) == []


# --- Task 2 / Phase 3: real-fixture extension — all four adoptable types ---
# Provider-real plan.json artifacts from the 2026-06-11 adopt-fidelity probes
# (docs/plans/2026-06-11-adopt-recipe.md §0.2). Each fixture was produced by
# ``tofu show -json`` against a live GCP project, google provider 6.50.0,
# tofu 1.12.0, local backend — no hand-crafting.


@pytest.mark.parametrize(
    "fixture",
    [
        "real_import_bucket_pure_noop.json",   # existing bucket probe
        "real_import_topic_pure_noop.json",
        "real_import_sub_pure_noop.json",
        "real_import_run_pure_noop.json",
    ],
)
def test_real_provider_import_pure_noop_all_four_types_admitted(fixture):
    """All four adoptable types: a real tofu-show plan with pure no-op import
    must pass the denylist with zero violations."""
    parsed, _ = load_plan_json(_load(fixture))
    assert parsed is not None, f"{fixture} did not parse"
    assert _rules(evaluate(DenylistInput(plan=parsed))) == [], (
        f"{fixture}: expected no violations"
    )


def test_real_provider_import_bucket_nearline_fires_with_changes_rule():
    """Provider-real fixture: bucket import with NEARLINE storage class deviation
    (actions=[update]) must fire import-with-changes-forbidden-v1 and ONLY that rule.
    This is the 'can't cleanly adopt yet' honest failure case (§0.2 deviant probe)."""
    parsed, _ = load_plan_json(_load("real_import_bucket_storage_class_update.json"))
    assert parsed is not None
    assert _rules(evaluate(DenylistInput(plan=parsed))) == ["import-with-changes-forbidden-v1"]
