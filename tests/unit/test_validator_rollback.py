"""Validator policy tests for `DecisionAction.ROLLBACK` (Phase 13.1).

The rollback action is the response to a hard contract violation (a var the
contract says must NOT be manually changed). It mints an approval token via
the Rollback Worker's `/propose` endpoint and surfaces an approval URL to a
human operator — so the validator's job is to refuse any proposal that would
skip the HITL gate or target a non-violation diff.

These tests pin the validator policy. They intentionally mirror the structure
of ``test_validator.py`` (same ``_contract()`` / ``_proposal()`` helpers).
"""

import pytest

from agent.contract import DocsRef, EnvVarRule, OpsContract
from agent.models import (
    ContractStatus,
    DecisionAction,
    DecisionProposal,
    EnvDiff,
)
from agent.validator import ValidationError, validate


def _contract():
    return OpsContract(
        service="payment-demo",
        environment="production",
        cloud_run_service="payment-demo",
        region="asia-northeast1",
        github_repo="theghostsquad00/driftscribe",
        expected_env={
            "PAYMENT_MODE": EnvVarRule(
                value="mock",
                docs=DocsRef(file="demo/docs/runbook.md", section="Runtime Configuration"),
                allow_manual_change=False,
            ),
            "FEATURE_X": EnvVarRule(
                value="false",
                docs=DocsRef(file="demo/docs/runbook.md", section="Feature Flags"),
                allow_manual_change=True,
                operator_note="Operator-safe",
            ),
        },
    )


def _rollback_proposal(
    *,
    diffs: list[EnvDiff] | None = None,
    target_revision: str | None = "payment-demo-00042-abc",
    requires_human_review: bool = True,
):
    if diffs is None:
        diffs = [
            EnvDiff(
                name="PAYMENT_MODE",
                expected="mock",
                live="live",
                contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,
            )
        ]
    return DecisionProposal(
        action=DecisionAction.ROLLBACK,
        env_diffs=diffs,
        target_revision=target_revision,
        rationale="Hard contract violation; revert to last-known-good revision.",
        confidence=0.95,
        requires_human_review=requires_human_review,
    )


def test_validator_passes_correct_rollback():
    p = _rollback_proposal()
    validate(p, _contract())  # must not raise


def test_validator_rejects_rollback_without_target_revision():
    p = _rollback_proposal(target_revision=None)
    with pytest.raises(ValidationError, match="target_revision"):
        validate(p, _contract())


def test_validator_rejects_rollback_with_empty_target_revision():
    p = _rollback_proposal(target_revision="")
    with pytest.raises(ValidationError, match="target_revision"):
        validate(p, _contract())


def test_validator_rejects_rollback_with_whitespace_target_revision():
    p = _rollback_proposal(target_revision="   ")
    with pytest.raises(ValidationError, match="target_revision"):
        validate(p, _contract())


def test_validator_rejects_rollback_without_human_review():
    p = _rollback_proposal(requires_human_review=False)
    with pytest.raises(ValidationError, match="requires_human_review"):
        validate(p, _contract())


def test_validator_rejects_rollback_with_any_present_allow_manual_diff():
    diffs = [
        EnvDiff(
            name="PAYMENT_MODE",
            expected="mock",
            live="live",
            contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,
        ),
        EnvDiff(
            name="FEATURE_X",
            expected="false",
            live="true",
            contract_status=ContractStatus.PRESENT_ALLOW_MANUAL,
        ),
    ]
    p = _rollback_proposal(diffs=diffs)
    with pytest.raises(ValidationError, match="present_allow_manual"):
        validate(p, _contract())


def test_validator_rejects_rollback_with_any_absent_diff():
    diffs = [
        EnvDiff(
            name="PAYMENT_MODE",
            expected="mock",
            live="live",
            contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,
        ),
        EnvDiff(
            name="UNKNOWN_VAR",
            expected=None,
            live="x",
            contract_status=ContractStatus.ABSENT,
        ),
    ]
    p = _rollback_proposal(diffs=diffs)
    with pytest.raises(ValidationError, match="absent"):
        validate(p, _contract())


def test_validator_rejects_rollback_with_path_traversal_target_revision():
    p = _rollback_proposal(target_revision="../etc/passwd")
    with pytest.raises(ValidationError, match="target_revision"):
        validate(p, _contract())


def test_validator_rejects_rollback_with_uppercase_target_revision():
    # Cloud Run revision names are lowercase only
    p = _rollback_proposal(target_revision="PAYMENT-DEMO-00042")
    with pytest.raises(ValidationError, match="target_revision"):
        validate(p, _contract())


def test_validator_rejects_rollback_with_shell_metachar_target_revision():
    p = _rollback_proposal(target_revision="payment-demo;rm -rf /")
    with pytest.raises(ValidationError, match="target_revision"):
        validate(p, _contract())


def test_validator_docs_pr_unaffected_by_new_target_revision_field():
    # Smoke test: docs_pr with target_revision=None (the new field's default)
    # still passes — the field is optional for non-rollback actions.
    p = DecisionProposal(
        action=DecisionAction.DOCS_PR,
        env_diffs=[
            EnvDiff(
                name="FEATURE_X",
                expected="false",
                live="true",
                contract_status=ContractStatus.PRESENT_ALLOW_MANUAL,
            )
        ],
        target_docs_file="demo/docs/runbook.md",
        target_docs_section="Feature Flags",
        target_revision=None,
        rationale="t",
        confidence=0.9,
    )
    validate(p, _contract())  # must not raise
