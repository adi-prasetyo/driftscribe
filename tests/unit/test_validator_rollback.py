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


# ds-b3m: the validator's rollback branch now has TWO layers — the reported-diff
# loop these tests were written for, and an independent check against the env the
# Reader Worker actually observed. Every test below passes an ``live_env`` that
# matches the scenario it describes, so the new layer never fires and each test
# still proves exactly what it was written to prove. A rejection test that
# passed because the NEW layer refused would be a test proving nothing about its
# own subject, which is how a suite quietly stops testing.
#
# This is the shape of the default ``_rollback_proposal()``: a genuine hard
# violation on PAYMENT_MODE, with the operator-safe FEATURE_X sitting at its
# contract value.
_DRIFTED_LIVE_ENV = {"PAYMENT_MODE": "live", "FEATURE_X": "false"}


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
    validate(p, _contract(), live_env=_DRIFTED_LIVE_ENV)  # must not raise


def test_validator_rejects_rollback_without_target_revision():
    p = _rollback_proposal(target_revision=None)
    with pytest.raises(ValidationError, match="target_revision"):
        validate(p, _contract(), live_env=_DRIFTED_LIVE_ENV)


def test_validator_rejects_rollback_with_empty_target_revision():
    p = _rollback_proposal(target_revision="")
    with pytest.raises(ValidationError, match="target_revision"):
        validate(p, _contract(), live_env=_DRIFTED_LIVE_ENV)


def test_validator_rejects_rollback_with_whitespace_target_revision():
    p = _rollback_proposal(target_revision="   ")
    with pytest.raises(ValidationError, match="target_revision"):
        validate(p, _contract(), live_env=_DRIFTED_LIVE_ENV)


def test_validator_rejects_rollback_without_human_review():
    p = _rollback_proposal(requires_human_review=False)
    with pytest.raises(ValidationError, match="requires_human_review"):
        validate(p, _contract(), live_env=_DRIFTED_LIVE_ENV)


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
        validate(p, _contract(), live_env=_DRIFTED_LIVE_ENV)


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
        validate(p, _contract(), live_env=_DRIFTED_LIVE_ENV)


def test_validator_rejects_rollback_with_path_traversal_target_revision():
    p = _rollback_proposal(target_revision="../etc/passwd")
    with pytest.raises(ValidationError, match="target_revision"):
        validate(p, _contract(), live_env=_DRIFTED_LIVE_ENV)


def test_validator_rejects_rollback_with_uppercase_target_revision():
    # Cloud Run revision names are lowercase only
    p = _rollback_proposal(target_revision="PAYMENT-DEMO-00042")
    with pytest.raises(ValidationError, match="target_revision"):
        validate(p, _contract(), live_env=_DRIFTED_LIVE_ENV)


def test_validator_rejects_rollback_with_shell_metachar_target_revision():
    p = _rollback_proposal(target_revision="payment-demo;rm -rf /")
    with pytest.raises(ValidationError, match="target_revision"):
        validate(p, _contract(), live_env=_DRIFTED_LIVE_ENV)


def test_validator_rejects_rollback_when_llm_lies_about_contract_status():
    """Phase 15.3 (Codex carry-over from Phase 14): the rollback gate
    must re-derive ``contract_status`` from ``contract.expected_env``,
    NOT trust the value the LLM placed on the diff.

    In USE_ADK mode the proposal is constructed by Gemini. If the model
    labels an allow_manual_change=True var as ``present_disallow_manual``
    (either by hallucination or by deliberate jailbreak prompt), the gate
    must still reject it — because the actual contract says it's
    operator-safe, and rolling back operator-safe vars defeats the
    contract's own flexibility.

    Setup: FEATURE_X is configured with allow_manual_change=True in
    ``_contract()``. The proposal carries an EnvDiff for FEATURE_X with
    contract_status=PRESENT_DISALLOW_MANUAL (the lie). Without the fix,
    the validator's ``diff.contract_status != PRESENT_DISALLOW_MANUAL``
    check passes (because the LLM lied) and the rollback proceeds. With
    the fix, the validator re-derives status from the contract rule
    (PRESENT_ALLOW_MANUAL) and rejects.
    """
    diffs = [
        EnvDiff(
            name="FEATURE_X",  # contract says allow_manual_change=True
            expected="false",
            live="true",
            contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,  # LLM lies
        )
    ]
    p = _rollback_proposal(diffs=diffs)
    with pytest.raises(ValidationError, match="present_allow_manual"):
        validate(p, _contract(), live_env=_DRIFTED_LIVE_ENV)


def test_validator_rejects_rollback_when_llm_lies_about_status_for_unknown_var():
    """Companion to the previous test: if the LLM labels an UNKNOWN var
    (not in contract.expected_env) as ``present_disallow_manual``, the
    re-derivation produces ABSENT and the rollback is rejected. Pins the
    "unknown var" branch of the contract-derived status lookup.
    """
    diffs = [
        EnvDiff(
            name="UNDECLARED_VAR",  # not in contract
            expected=None,
            live="x",
            contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,  # LLM lies
        )
    ]
    p = _rollback_proposal(diffs=diffs)
    with pytest.raises(ValidationError, match="absent"):
        validate(p, _contract(), live_env=_DRIFTED_LIVE_ENV)


def test_validator_passes_rollback_when_a_nondrifted_allow_manual_var_rides_along():
    """ds-2f5 — REGRESSION TEST FOR A LIVE PROD OUTAGE.

    Reproduces the exact shape observed on prod 2026-07-29. ``payment-demo``
    was drifted to PAYMENT_MODE=live (allow_manual_change=False — a hard
    contract violation, the textbook rollback trigger). The model returned a
    FULL comparison array: the real violation PLUS the untouched
    operator-toggleable flag as a match.

    The gate rejected it 3/3 — deterministic, not model variance — so
    ``/recheck`` answered 502 and minted NO approval. The headline autonomous
    self-heal was silently dead.

    ``FEATURE_X`` here has live == expected == "false". It is not drifted, and
    rolling back would revert nothing about it, so it must not veto a rollback
    justified by a different var.

    Contrast with test_validator_rejects_rollback_with_any_present_allow_manual_diff
    below/above, which keeps FEATURE_X at expected="false" / live="true" — a
    REAL deviation on an operator-safe var, where rejecting is correct because
    the rollback WOULD revert the operator's own legitimate manual change.
    That distinction — deviation vs agreement — is the entire fix.
    """
    diffs = [
        EnvDiff(
            name="PAYMENT_MODE",
            expected="mock",
            live="live",  # real deviation, allow_manual_change=False
            contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,
        ),
        EnvDiff(
            name="FEATURE_X",
            expected="false",
            live="false",  # NOT drifted — nothing to revert
            contract_status=ContractStatus.MATCH,
        ),
    ]
    p = _rollback_proposal(diffs=diffs)
    validate(p, _contract(), live_env=_DRIFTED_LIVE_ENV)  # must not raise


def test_validator_rejects_rollback_when_every_diff_agrees():
    """ds-2f5 — the mandatory companion to skipping non-deviations.

    Skipping ``live == expected`` entries without also requiring a surviving
    violation would let an all-agreement diff array satisfy the loop
    VACUOUSLY and authorize a rollback backed by nothing. That is a gate
    abstaining when it cannot see its subject — precisely the failure this
    change exists to correct, and it would be strictly worse than the bug,
    because it fails open on a live traffic mutation.

    Note step 3 ("non-NO_OP requires at least one diff") does NOT cover this:
    the array here is non-empty, it just contains no deviation.

    Every var here derives PRESENT_DISALLOW_MANUAL, deliberately: that makes
    the new violation counter the ONLY thing standing between this proposal
    and approval. Had this test included an allow_manual var it would reject
    for that unrelated reason and prove nothing about the vacuous path.

    Consequently this also closes a PRE-EXISTING fail-open: before ds-2f5 the
    loop accepted this proposal outright, so a rollback citing only a
    NON-drifted PAYMENT_MODE — no violation anywhere — passed the safety gate
    and went on to mint a real approval.
    """
    diffs = [
        EnvDiff(
            name="PAYMENT_MODE",
            expected="mock",
            live="mock",
            contract_status=ContractStatus.MATCH,
        ),
    ]
    p = _rollback_proposal(diffs=diffs)
    with pytest.raises(ValidationError, match="no env_diff deviates from the contract"):
        validate(p, _contract(), live_env=_DRIFTED_LIVE_ENV)


def test_validator_rejects_rollback_when_expected_contradicts_the_contract():
    """ds-2f5 (Codex review) — a fabricated baseline must not manufacture a
    violation.

    ``diff.expected`` is model-authored. If deviation were measured against
    it, this proposal would authorize a rollback: "mock" != "bogus" looks like
    a hard PAYMENT_MODE violation. But live IS the contract value ("mock") —
    nothing has drifted, and there is nothing to revert.

    Measuring against ``rule.value`` instead makes the entry agreement, the
    violation count zero, and the proposal refused. This failed against the
    first draft of the ds-2f5 fix, which compared live to expected.
    """
    diffs = [
        EnvDiff(
            name="PAYMENT_MODE",
            expected="bogus",  # model invents a baseline the contract denies
            live="mock",  # ...but this IS the contract value
            contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,
        )
    ]
    p = _rollback_proposal(diffs=diffs)
    with pytest.raises(ValidationError, match="no env_diff deviates from the contract"):
        validate(p, _contract(), live_env=_DRIFTED_LIVE_ENV)


def test_validator_counts_a_declared_var_reported_as_wholly_absent():
    """A declared var reported with BOTH expected and live None. Comparing the
    model's own pair would call this agreement and skip it; against the
    contract, live=None != "mock" is a deleted required var — a real hard
    violation that must count."""
    diffs = [
        EnvDiff(
            name="PAYMENT_MODE",
            expected=None,
            live=None,
            contract_status=ContractStatus.MATCH,
        )
    ]
    p = _rollback_proposal(diffs=diffs)
    validate(p, _contract(), live_env=_DRIFTED_LIVE_ENV)  # must not raise


def test_validator_rejects_present_undeclared_var_reported_as_agreement():
    """An undeclared var that is PRESENT on the service still rejects, even
    when the model reports it as agreeing with itself.

    The contract governs no value for it, so presence is the only deviation
    there is to measure — and it derives ABSENT. Preserves the pre-existing
    rejection pinned by the llm_lies_about_status_for_unknown_var test, which
    a naive live==expected skip would have quietly dropped."""
    diffs = [
        EnvDiff(
            name="PAYMENT_MODE",
            expected="mock",
            live="live",  # real violation, would otherwise carry the rollback
            contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,
        ),
        EnvDiff(
            name="UNDECLARED_VAR",
            expected="x",
            live="x",  # self-consistent, but present and ungoverned
            contract_status=ContractStatus.MATCH,
        ),
    ]
    p = _rollback_proposal(diffs=diffs)
    with pytest.raises(ValidationError, match="absent"):
        validate(p, _contract(), live_env=_DRIFTED_LIVE_ENV)


def test_validator_rejects_conflicting_duplicates_of_a_hard_var():
    """ds-2f5 (Codex re-review) — THE case an earlier version of this test
    missed, and the reason a dedicated duplicate check exists.

    Grounding deviation in ``rule.value`` does NOT dissolve conflicting
    duplicates on its own. It does for an allow_manual var (the conflicting
    entry rejects — see the companion test below), but for a disallow_manual
    var EVERY non-contract value clears the gate:

        live="mock"  -> skipped, matches the contract
        live="live"  -> counted as a hard violation

    so the proposal passes while asserting two live values the service cannot
    both have. If reality is "mock", that authorizes a rollback with no
    violation behind it — a fail-open. Reproduced before the fix.
    """
    diffs = [
        EnvDiff(
            name="PAYMENT_MODE",
            expected="mock",
            live="mock",  # claims contract-compliant
            contract_status=ContractStatus.MATCH,
        ),
        EnvDiff(
            name="PAYMENT_MODE",  # ...and simultaneously claims hard drift
            expected="mock",
            live="live",
            contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,
        ),
    ]
    p = _rollback_proposal(diffs=diffs)
    with pytest.raises(ValidationError, match="conflicting live values"):
        validate(p, _contract(), live_env=_DRIFTED_LIVE_ENV)


def test_validator_rejects_conflicting_duplicates_of_a_soft_var():
    """Companion: the allow_manual case, which the contract grounding would
    have caught anyway. Kept so the duplicate rule is pinned for both
    contract classes rather than only the one that needed it."""
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
            live="false",
            contract_status=ContractStatus.MATCH,
        ),
        EnvDiff(
            name="FEATURE_X",  # same var, impossible second live value
            expected="true",
            live="true",
            contract_status=ContractStatus.MATCH,
        ),
    ]
    p = _rollback_proposal(diffs=diffs)
    with pytest.raises(ValidationError, match="conflicting live values"):
        validate(p, _contract(), live_env=_DRIFTED_LIVE_ENV)


def test_validator_allows_identical_duplicate_entries():
    """Only a CONFLICT is incoherent. An identical repeat says nothing new and
    must not turn a valid rollback into a 502 — the availability failure this
    whole change exists to remove."""
    diffs = [
        EnvDiff(
            name="PAYMENT_MODE",
            expected="mock",
            live="live",
            contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,
        ),
        EnvDiff(
            name="PAYMENT_MODE",
            expected="mock",
            live="live",
            contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,
        ),
    ]
    p = _rollback_proposal(diffs=diffs)
    validate(p, _contract(), live_env=_DRIFTED_LIVE_ENV)  # must not raise


def test_validator_compares_env_values_exactly_without_normalization():
    """Env values are opaque to the runtime: "mock " is a different
    configuration from "mock". The gate must not strip or normalize, so a
    whitespace-only difference on a disallow_manual var is a real violation."""
    diffs = [
        EnvDiff(
            name="PAYMENT_MODE",
            expected="mock",
            live="mock ",  # trailing space — genuinely different
            contract_status=ContractStatus.MATCH,
        )
    ]
    p = _rollback_proposal(diffs=diffs)
    # The observed env carries the same trailing space, so BOTH layers are
    # asked the normalization question, not just the reported-diff loop.
    validate(
        p,
        _contract(),
        live_env={"PAYMENT_MODE": "mock ", "FEATURE_X": "false"},
    )  # counts as a violation; must not raise


def test_validator_still_rejects_drifted_allow_manual_var_alongside_a_real_violation():
    """ds-2f5 must NOT widen the gate: the security property stays intact.

    A genuinely drifted operator-safe var still rejects even when a real hard
    violation is present in the same proposal, because the rollback would
    revert BOTH — including the manual change the contract explicitly permits.
    Only agreement entries are skipped, never deviations.
    """
    diffs = [
        EnvDiff(
            name="PAYMENT_MODE",
            expected="mock",
            live="live",  # real violation
            contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,
        ),
        EnvDiff(
            name="FEATURE_X",
            expected="false",
            live="true",  # REAL deviation on an operator-safe var
            contract_status=ContractStatus.PRESENT_ALLOW_MANUAL,
        ),
    ]
    p = _rollback_proposal(diffs=diffs)
    with pytest.raises(ValidationError, match="present_allow_manual"):
        validate(p, _contract(), live_env=_DRIFTED_LIVE_ENV)


def test_validator_treats_a_deleted_disallow_manual_var_as_a_deviation():
    """A contract var missing from live env (expected set, live None) is a
    deviation, not agreement — it must reach the gate and be allowed through
    as a hard violation rather than skipped by the ds-2f5 equality check."""
    diffs = [
        EnvDiff(
            name="PAYMENT_MODE",
            expected="mock",
            live=None,  # var deleted from the live service
            contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,
        )
    ]
    p = _rollback_proposal(diffs=diffs)
    # The observed env is missing the key too, so the ds-b3m layer sees the same
    # deletion the diff reports. An absent declared var is a deviation there as
    # well — ``.get`` returns None, which never equals the contract's str value.
    validate(p, _contract(), live_env={"FEATURE_X": "false"})  # must not raise


def test_validator_skips_absent_unknown_var_instead_of_rejecting_absent():
    """An undeclared var that is not present (live is None) is nothing at all,
    so it is skipped rather than rejected as ABSENT, and the rollback still
    stands on its real violation.

    Companion to test_validator_rejects_present_undeclared_var_reported_as_
    agreement: for an ungoverned var, PRESENCE is the deviation. Absent is
    skipped, present rejects."""
    diffs = [
        EnvDiff(
            name="PAYMENT_MODE",
            expected="mock",
            live="live",
            contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,
        ),
        EnvDiff(
            name="UNDECLARED_VAR",  # not in contract
            expected=None,
            live=None,  # ...and not present, so there is no deviation
            contract_status=ContractStatus.MATCH,
        ),
    ]
    p = _rollback_proposal(diffs=diffs)
    validate(p, _contract(), live_env=_DRIFTED_LIVE_ENV)  # must not raise


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
    validate(p, _contract(), live_env=_DRIFTED_LIVE_ENV)  # must not raise


# --------------------------------------------------------------------------- #
# ds-b3m — the gate must judge OBSERVED state, not only the model's own array.
#
# Everything above pins the reported-diff loop, which reads `proposal.env_diffs`.
# That array is authored by the LLM, so the loop cannot see an OMISSION: a model
# that wanted a drifted operator-safe var to escape simply left it out, and the
# rollback proceeded and reverted that var anyway. These pin the second layer,
# which reads the env the Reader Worker actually observed.
# --------------------------------------------------------------------------- #


def test_the_omitted_allow_manual_hole_is_NOT_closed_here():
    """A KNOWN, DELIBERATELY UNCLOSED GAP — pinned so it cannot be mistaken for
    a property this layer provides.

    The proposal reports only the real PAYMENT_MODE violation and says nothing
    about FEATURE_X, while the service really does have FEATURE_X flipped away
    from its contract value. The rollback is allowed.

    The obvious "fix" — veto when an operator-safe var deviates from the
    contract — is the WRONG PREDICATE, and grounding it in observed env would
    only make a wrong answer authoritative. Whether this rollback disturbs
    FEATURE_X depends on the TARGET REVISION's value, not the contract's: if the
    target also holds "true", the rollback preserves the operator's change and a
    contract-based veto would refuse it for nothing. That question is answered
    on the approval page (ds-uwc), which is also the only surface the chat
    rollback lane passes through — this validator has exactly one call site and
    the chat lane is not it.

    What ds-b3m does close is the same omission for HARD violations, which is
    the half this layer can answer correctly. See the tests below."""
    p = _rollback_proposal()  # default diffs: PAYMENT_MODE only
    validate(
        p,
        _contract(),
        live_env={"PAYMENT_MODE": "live", "FEATURE_X": "true"},
    )  # must not raise


def test_an_unreadable_allow_manual_var_does_not_block_a_real_violation():
    """Same boundary from the other side: FEATURE_X missing from the observed
    env (deleted, or Secret-Manager-backed and dropped by the reader) is not
    this layer's business either. Refusing here would take out every rollback on
    a service that keeps an operator-safe var in Secret Manager, in exchange for
    an answer that would still be the wrong predicate."""
    p = _rollback_proposal()
    validate(p, _contract(), live_env={"PAYMENT_MODE": "live"})  # must not raise


def test_validator_rejects_rollback_when_observed_env_shows_no_violation():
    """The model reports a hard violation the service does not actually have.

    The reported-diff loop believes it — the diff it was handed is well-formed
    and derives present_disallow_manual from the contract. Grounding is the only
    thing that catches a fabricated justification."""
    p = _rollback_proposal()
    with pytest.raises(ValidationError, match="no hard contract violation"):
        validate(
            p,
            _contract(),
            live_env={"PAYMENT_MODE": "mock", "FEATURE_X": "false"},
        )


def test_validator_accepts_a_rollback_the_model_UNDERreported():
    """Grounding cuts both ways: the model omits the violation entirely and
    reports an unrelated (undeclared, present) var instead. That proposal is
    refused by the reported-diff loop for its own reasons — but a proposal that
    merely under-describes a violation the service really has must not be
    refused BY THIS LAYER, which is the one thing this test isolates."""
    _authorize = __import__(
        "agent.validator", fromlist=["_authorize_rollback_from_live_env"]
    )._authorize_rollback_from_live_env
    # No raise: PAYMENT_MODE really is drifted, FEATURE_X really is at contract.
    _authorize(_contract(), {"PAYMENT_MODE": "live", "FEATURE_X": "false"})


def test_validator_refuses_rollback_when_no_observed_env_is_available():
    """`live_env=None` is the caller saying "the Reader Worker read failed, and
    the only env picture I hold is the one I rebuilt from this proposal's own
    diffs". A gate cannot corroborate a proposal against the proposal. It
    refuses rather than abstaining."""
    p = _rollback_proposal()
    with pytest.raises(ValidationError, match="no observed live env"):
        validate(p, _contract(), live_env=None)


def test_validator_refuses_rollback_when_live_env_is_omitted_entirely():
    """The default is None, so a call site that forgets the argument REFUSES a
    rollback rather than silently permitting one. Forgetting can only tighten
    this gate, never loosen it — which is why the parameter is defaulted at all
    instead of being required like `autonomy_mode`."""
    p = _rollback_proposal()
    with pytest.raises(ValidationError, match="no observed live env"):
        validate(p, _contract())


def test_observed_env_layer_ignores_undeclared_vars_entirely():
    """A real Cloud Run service carries plenty of vars the contract says nothing
    about (PORT, K_SERVICE, ...). Treating their presence as deviation — which
    is what the REPORTED-diff loop does for a var the model names — would refuse
    every rollback that ever ran. The contract governs what it declares."""
    _authorize = __import__(
        "agent.validator", fromlist=["_authorize_rollback_from_live_env"]
    )._authorize_rollback_from_live_env
    _authorize(
        _contract(),
        {
            "PAYMENT_MODE": "live",
            "FEATURE_X": "false",
            "PORT": "8080",
            "K_SERVICE": "payment-demo",
        },
    )


@pytest.mark.parametrize(
    "action",
    [DecisionAction.NO_OP, DecisionAction.DOCS_PR, DecisionAction.DRIFT_ISSUE],
)
def test_non_rollback_actions_are_unaffected_by_a_missing_live_env(action):
    """The observed-env requirement is scoped to ROLLBACK. Nothing else changes
    live state on this path, so nothing else needs grounding — and widening the
    requirement would take the whole /recheck pipeline down with the reader."""
    diffs = [
        EnvDiff(
            name="FEATURE_X",
            expected="false",
            live="true",
            contract_status=ContractStatus.PRESENT_ALLOW_MANUAL,
            recent_pr_match="#123 flip the flag",
        )
    ]
    kwargs = {}
    if action == DecisionAction.DOCS_PR:
        kwargs = {
            "target_docs_file": "demo/docs/runbook.md",
            "target_docs_section": "Feature Flags",
        }
    p = DecisionProposal(
        action=action,
        confidence=0.9,
        rationale="r",
        env_diffs=[] if action == DecisionAction.NO_OP else diffs,
        requires_human_review=False,
        **kwargs,
    )
    validate(p, _contract())  # live_env omitted entirely: must not raise
