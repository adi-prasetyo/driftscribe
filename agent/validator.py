import re
from pathlib import Path
from agent.models import ContractStatus, DecisionProposal, DecisionAction
from agent.contract import OpsContract
from agent.secret_guard import is_secret_name

class ValidationError(Exception):
    pass

# Cloud Run revision-name regex. Mirrors the canonical definition in
# ``workers/rollback/main.py`` (``_REVISION_NAME``). Inlined rather than
# imported because the rollback worker is a separate deployable package
# (own pyproject.toml, separate container) and the agent must not take a
# build-time dependency on a worker. If the worker's regex ever loosens
# or tightens, update this constant in lockstep.
_REVISION_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}[a-z0-9]$")

# Actions the drift workload may emit. Pinned here (not derived from
# ACTION_REGISTRY) so a future upgrade-only action added to the registry
# cannot silently widen drift's surface. Codex 2026-05-20 follow-up:
# without this allowlist, an LLM under workload=drift that returns
# ``upgrade_pr`` would pass the bare-enum check at step 1, then hit
# ``_render_for()`` with no renderer and produce a 500-shaped failure.
# The fix is a hard "wrong workload" rejection at the validator.
_DRIFT_ACTIONS: frozenset[DecisionAction] = frozenset({
    DecisionAction.NO_OP,
    DecisionAction.DOCS_PR,
    DecisionAction.DRIFT_ISSUE,
    DecisionAction.ROLLBACK,
    DecisionAction.ESCALATION,
})


def _validate_path(p: str | None) -> None:
    if p is None:
        return
    if p.startswith("/") or ".." in Path(p).parts:
        raise ValidationError(f"target docs path rejected (absolute or traversal): {p!r}")

def _authorize_rollback_from_live_env(
    contract: OpsContract, live_env: dict[str, str] | None
) -> None:
    """Raise ValidationError unless OBSERVED state justifies a rollback (ds-b3m).

    The per-diff loop in :func:`validate` judges the evidence the MODEL reported.
    This judges the evidence we went and looked at ourselves, and it is the half
    that can see an OMISSION: before this, a proposal whose diff array contained
    no real violation at all — fabricated, or describing a service state that had
    since changed — satisfied the gate as long as each diff it DID list derived
    ``present_disallow_manual`` from the contract. The honest statement of the
    old guarantee was "of the deviations REPORTED, all are hard violations",
    which is not what a safety gate should be promising. This layer requires
    that a hard violation actually EXISTS on the service.

    The omission it closes is therefore of a HARD VIOLATION, not of an
    ``allow_manual`` change — see the scope note below, and note that an earlier
    version of this docstring claimed the latter while the code did the former.

    ``live_env is None`` means we have NO ground truth and the rollback is
    refused. That is not a formality. ``agent/main.py`` has two sources for this
    value, and the second one — the ADK path's fallback when the Reader Worker
    call fails — RECONSTRUCTS it from ``proposal.env_diffs``. Feeding that back
    in here would make this function launder the model's own array into
    "observed state" and re-derive exactly the answer it exists to check
    independently. The caller passes ``None`` for a reconstruction, and a gate
    that cannot see its subject fails rather than abstains.

    SCOPE — this answers ONE question: *is there a hard contract violation for
    this rollback to revert?* It counts ``allow_manual_change=False`` vars whose
    observed value is not the contract's. It says nothing about
    ``allow_manual_change=True`` vars, and the reason is the whole reason this
    function is scoped the way it is.

    The tempting rule — "an operator-safe var that deviates from the contract
    vetoes the rollback" — asks current-vs-contract, and the real question is
    current-vs-TARGET:

    - current ``true``, target ``true``: the rollback PRESERVES the operator's
      value, and a current-vs-contract veto would refuse it anyway. A false
      refusal, and the ds-2f5 availability shape all over again.
    - current ``false``, target ``true``: the rollback CHANGES it, and
      current-vs-contract sees nothing wrong.

    A first draft of ds-b3m had that veto here; it was wrong in both rows. The
    blast-radius question needs the target revision's env, which this function
    does not have and which the /recheck path does not fetch — so it is answered
    on the approval page (ds-uwc), the one surface BOTH rollback lanes share.
    The chat lane never reaches this function at all.

    The pre-existing reported-diff loop in :func:`validate` still refuses a
    rollback whose proposal REPORTS a deviating ``allow_manual`` var. That is
    left exactly as it was: not widened, not narrowed. Being blunt about what
    that leaves: a model can still change the OUTCOME by omitting such a diff —
    reporting it refuses, omitting it proceeds — and grounding that rule here
    would not fix it, because the grounded predicate would be the wrong one.
    ds-uwc SURFACES the consequence on the approval page; it does not by itself
    close the asymmetry. Closing it means replacing the current-vs-contract veto
    with a target-aware rule once target state is available to a gate, which is
    follow-up work, not something ds-uwc delivers. See
    ``test_the_omitted_allow_manual_hole_is_NOT_closed_here``.

    ``live_env`` is deliberately NOT treated as a complete picture, because it
    is not one: ``_extract_env_from_containers`` (driftscribe_lib/cloud_run.py)
    SKIPS Secret-Manager-backed entries and flattens every container
    last-one-wins. A declared var missing from it is therefore UNREADABLE, not
    violated, and is not counted — see the loop.

    Undeclared vars are ignored entirely. The contract governs what it declares;
    a real Cloud Run service carries plenty of vars it says nothing about, and
    treating their presence as deviation would refuse every rollback ever. The
    model LYING about an undeclared var is still caught — by the reported-diff
    loop in :func:`validate`, which keeps that rule.
    """
    if live_env is None:
        raise ValidationError(
            "rollback rejected: no observed live env available to justify it. "
            "The Reader Worker read failed, so the only env picture we hold is "
            "the one reconstructed from the proposal's own diffs — which cannot "
            "independently corroborate the proposal. Retry once the reader is "
            "reachable."
        )

    violations = 0
    for name, rule in contract.expected_env.items():
        if rule.allow_manual_change:
            continue  # not this function's question — see the docstring
        if name not in live_env:
            # UNREADABLE, not "violated". The reader omits a var both when it is
            # genuinely deleted and when it is Secret-Manager-backed
            # (``_extract_env_from_containers`` skips ``value_source`` entries),
            # and the contract's schema never says a declared var must be an
            # inline literal — so a secret-backed PAYMENT_MODE that resolves to
            # "mock" is compliant and reads here as missing.
            #
            # A draft of this counted a missing key as a violation, reasoning
            # that both branches of the ambiguity are "not observably at the
            # declared value". That phrase is true and the conclusion does not
            # follow: not observably compliant is not the same as observably
            # non-compliant, and THIS predicate AUTHORIZES a traffic shift, so
            # absence of proof must never become the proof. It was also flatly
            # inconsistent with the malformed-payload rule one layer up, which
            # refuses to coerce a bad reader response to ``{}`` precisely
            # because an empty env would manufacture violations — an omitted
            # secret-backed entry manufactures exactly the same one.
            continue
        # Values compare EXACTLY — no strip, no case-folding, no Unicode
        # normalization — because env values are opaque bytes to the runtime,
        # so "mock " and "mock" really are different configurations.
        if live_env[name] != rule.value:
            violations += 1

    if violations == 0:
        raise ValidationError(
            "rollback rejected: observed live env shows every "
            "allow_manual_change=false var already at its contract value, so "
            "there is no hard contract violation to revert"
        )


def validate(
    proposal: DecisionProposal,
    contract: OpsContract,
    *,
    live_env: dict[str, str] | None = None,
) -> None:
    """Raise ValidationError if proposal violates safety rules.

    ``live_env`` is the env the Reader Worker actually observed on the service,
    or ``None`` when no independent observation is available. It is consulted
    ONLY on the rollback path — see :func:`_authorize_rollback_from_live_env`,
    which explains why ``None`` refuses a rollback rather than falling back to
    the proposal. Keyword-only and defaulted so the many non-rollback callers
    (and every DOCS_PR / NO_OP test) are unaffected.
    """

    # 1. Action must be a known enum
    if not isinstance(proposal.action, DecisionAction):
        try:
            DecisionAction(proposal.action)
        except ValueError as e:
            raise ValidationError(f"unknown action: {proposal.action!r}") from e

    # 1a. Action must belong to the drift action allowlist. This validator
    #     is drift-only — an upgrade-flavored action arriving here means
    #     either (a) the wrong workload reached this code path, or (b) the
    #     LLM under workload=drift hallucinated an upgrade action. Either
    #     way the failure mode without this guard is a 500 at the renderer
    #     (no docs/rollback/issue renderer for upgrade_pr). Codex
    #     2026-05-20 follow-up.
    if proposal.action not in _DRIFT_ACTIONS:
        raise ValidationError(
            f"action {proposal.action.value!r} is not in the drift "
            f"workload's action set; got from drift validator. Expected "
            f"one of: {sorted(a.value for a in _DRIFT_ACTIONS)}"
        )

    # 2. Confidence must be in [0, 1] — guards against LLM hallucinations like 1.5
    if not 0.0 <= proposal.confidence <= 1.0:
        raise ValidationError(f"confidence out of range [0,1]: {proposal.confidence}")

    # 3. Non-NO_OP actions require at least one diff (no empty PRs/issues)
    if proposal.action != DecisionAction.NO_OP and not proposal.env_diffs:
        raise ValidationError(f"action {proposal.action.value} requires at least one env_diff")

    # 4. Path guards
    _validate_path(proposal.target_docs_file)

    # 5. Rollback semantics — HITL is mandatory and the action only applies
    #    to hard contract violations (vars marked allow_manual_change=False,
    #    surfaced as ContractStatus.PRESENT_DISALLOW_MANUAL). The rollback
    #    worker's `/propose` schema validates target_revision a second time
    #    at the API boundary; rejecting it here gives a faster, more
    #    explanatory failure.
    if proposal.action == DecisionAction.ROLLBACK:
        rev = proposal.target_revision
        if rev is None or not rev.strip():
            raise ValidationError("rollback requires target_revision")
        if not _REVISION_NAME.match(rev):
            raise ValidationError(
                f"rollback target_revision {rev!r} does not match Cloud Run "
                f"revision-name regex (see workers/rollback/main.py::_REVISION_NAME)"
            )
        if not proposal.requires_human_review:
            raise ValidationError("rollback requires requires_human_review=true")
        # Phase 15.3: re-derive contract_status from contract.expected_env
        # rather than trusting diff.contract_status (Codex carry-over from
        # Phase 14). In USE_ADK mode the diff is constructed by Gemini; a
        # hallucinated or prompt-injected label could otherwise bypass the
        # gate by claiming an operator-safe var is present_disallow_manual.
        # The contract YAML is the source of truth — re-derive from it.
        #
        # ds-2f5: the gate applies only to diffs that are an actual DEVIATION.
        # A diff whose live value already matches what the contract requires
        # reports agreement, not a change: it supplies no violation that could
        # justify a rollback, so it has no standing to veto one.
        #
        # DEVIATION IS MEASURED AGAINST ``rule.value``, NEVER ``diff.expected``
        # (Codex review of this change). ``diff.expected`` is model-authored,
        # so believing it lets a fabricated baseline manufacture a violation:
        #   EnvDiff(name="PAYMENT_MODE", expected="bogus", live="mock")
        # would count as a hard violation even though ``live`` IS the contract
        # value and nothing has drifted. The first draft of this fix compared
        # live/expected and had exactly that hole — the same wrong-subject
        # mistake it was written to correct, one level in. The contract is
        # already the source of truth for the STATUS re-derivation above; it is
        # the source of truth for the VALUE too, so ``diff.expected`` is now
        # consulted for nothing on a declared var.
        #
        # Undeclared vars have no contract value to compare against, so the
        # only meaningful question is presence: a var the contract does not
        # govern that is PRESENT on the service is a deviation (and derives
        # ABSENT, so it rejects — preserving the pre-existing behavior pinned
        # by test_validator_rejects_rollback_when_llm_lies_about_status_for_
        # unknown_var); absent-and-undeclared is simply nothing at all.
        #
        # Contradictory duplicates are rejected explicitly. An earlier draft of
        # this comment claimed grounding in ``rule.value`` handled them "for
        # free"; that is TRUE ONLY for allow_manual vars, where the conflicting
        # entry rejects. For a disallow_manual var every non-contract value
        # CLEARS the gate, so:
        #     EnvDiff("PAYMENT_MODE", live="mock")   -> skipped, matches
        #     EnvDiff("PAYMENT_MODE", live="live")   -> counted as a violation
        # passes while asserting two live values the service cannot both have.
        # If reality is "mock", that authorizes a rollback with no violation
        # behind it. Caught by Codex re-review; the test that was supposed to
        # cover this used the allow_manual var and so proved nothing.
        #
        # Identical repeats are harmless and stay allowed — only a CONFLICT is
        # incoherent evidence.
        seen_live: dict[str, str | None] = {}
        for diff in proposal.env_diffs:
            if diff.name in seen_live and seen_live[diff.name] != diff.live:
                raise ValidationError(
                    f"rollback rejected: conflicting live values reported for "
                    f"{diff.name!r} ({seen_live[diff.name]!r} and {diff.live!r}); "
                    f"a var cannot hold two values at once, so this evidence "
                    f"cannot support a rollback"
                )
            seen_live[diff.name] = diff.live
        #
        # NOT ADOPTED from that review: rejecting when ``diff.expected !=
        # rule.value`` as "contradictory evidence". We hold the authoritative
        # value, so a model that merely miscopied the baseline is harmless —
        # and turning a cosmetic mismatch into a hard refusal would rebuild the
        # availability bug this change exists to remove, keyed on a field that
        # now influences nothing.
        #
        # Stated precisely, because the looser phrasing is wrong: this does
        # NOT claim the rollback leaves that var untouched. A rollback reverts
        # the target revision's ENTIRE env, and nothing here or in the worker
        # ever inspects that revision's config (ds-uwc). The claim is only
        # that an agreeing var is not evidence OF drift, which is all this
        # gate is deciding.
        #
        # This was not academic. The model returns a FULL comparison array
        # (every var it inspected, matches included), so on prod a genuine
        # PAYMENT_MODE violation was rejected 3/3 because the untouched,
        # operator-toggleable FEATURE_NEW_CHECKOUT rode along as a match —
        # 502, no approval minted, the autonomous self-heal silently dead.
        # The gate was asking "is every var the model MENTIONED a hard
        # violation" when it means "is every var this rollback would REVERT
        # one".
        #
        # Values are compared EXACTLY — no strip(), no case-folding, no Unicode
        # normalization. Env values are opaque bytes to the runtime, so
        # "mock " and "mock" really are different configurations and must not
        # be quietly reconciled here.
        #
        # No new bypass, stated explicitly: a model that wanted to hide a
        # drifted operator-safe var from this gate can already OMIT the diff
        # entirely. Given a proposal whose diff array is model-authored, the
        # gate's honest guarantee is "of the deviations REPORTED, all are hard
        # violations" — skipping non-deviations does not weaken it. Closing
        # the omission hole needs the reader's real live env passed in so
        # deviations are derived from ground truth instead of the proposal
        # (ds-b3m), together with the target revision's env (ds-uwc).
        violations = 0
        for diff in proposal.env_diffs:
            rule = contract.expected_env.get(diff.name)
            if rule is None:
                # Undeclared var: no contract value exists, so presence is the
                # only deviation there is to measure.
                if diff.live is None:
                    continue
                derived_status = ContractStatus.ABSENT
            else:
                if diff.live == rule.value:
                    # Agreement with the CONTRACT: not evidence of drift.
                    continue
                derived_status = (
                    ContractStatus.PRESENT_ALLOW_MANUAL
                    if rule.allow_manual_change
                    else ContractStatus.PRESENT_DISALLOW_MANUAL
                )
            if derived_status != ContractStatus.PRESENT_DISALLOW_MANUAL:
                raise ValidationError(
                    f"rollback rejected: diff {diff.name!r} contract-derived "
                    f"status is {derived_status.value} (rollback only for "
                    f"present_disallow_manual; ignoring LLM-proposed "
                    f"contract_status={diff.contract_status.value})"
                )
            violations += 1
        # MANDATORY companion to the skip above, not a nicety. Without it an
        # all-agreement diff array satisfies the loop vacuously and authorizes
        # a rollback backed by no violation at all — a gate that abstains when
        # it cannot see its subject, which is the exact failure this fix is
        # correcting. A safety gate that finds nothing must refuse.
        if violations == 0:
            raise ValidationError(
                "rollback rejected: no env_diff deviates from the contract "
                "(every declared var was already at its contract value), so "
                "there is no hard contract violation to revert"
            )
        # ds-b3m: everything above judged the evidence the MODEL supplied. This
        # judges what we observed ourselves, and it is the half that can catch
        # an OMISSION — the reported-diff loop cannot, by construction. Both
        # layers run; neither replaces the other. The reported diffs are NOT
        # merely decorative and could not be dropped in favour of this: they
        # feed ``scrub_rationale_text`` (agent/main.py), which is what keeps a
        # secret out of the ``reason`` the rollback worker renders on the
        # operator approval page, and they are the decision record's audit
        # evidence.
        _authorize_rollback_from_live_env(contract, live_env)

    # 6. Docs PR semantics
    if proposal.action == DecisionAction.DOCS_PR:
        # target_docs_file and target_docs_section must be set (else the patcher
        # would produce literal "None" headings, and we wouldn't know what file
        # to update)
        if not proposal.target_docs_file:
            raise ValidationError("docs_pr requires target_docs_file")
        if not proposal.target_docs_section:
            raise ValidationError("docs_pr requires target_docs_section")
        for diff in proposal.env_diffs:
            # Secret-leak guard runs first — never document a secret-like name,
            # regardless of contract presence.
            if is_secret_name(diff.name):
                raise ValidationError(
                    f"refusing docs_pr that would document secret-like var {diff.name!r}"
                )

            rule = contract.expected_env.get(diff.name)
            if rule is None:
                if not diff.recent_pr_match:
                    raise ValidationError(
                        f"docs_pr for unknown var {diff.name!r} requires recent_pr_match evidence"
                    )
            elif not rule.allow_manual_change:
                raise ValidationError(
                    f"docs_pr for {diff.name!r} rejected: contract says allow_manual_change=False"
                )

        # Target file + section must match contract for known vars
        # (pinned so the LLM can't redirect a sanctioned change into README.md or similar)
        for diff in proposal.env_diffs:
            rule = contract.expected_env.get(diff.name)
            if rule and proposal.target_docs_file and rule.docs.file != proposal.target_docs_file:
                raise ValidationError(
                    f"target_docs_file {proposal.target_docs_file!r} does not match "
                    f"contract docs file {rule.docs.file!r} for {diff.name!r}"
                )
            if rule and proposal.target_docs_section and rule.docs.section != proposal.target_docs_section:
                raise ValidationError(
                    f"target_docs_section {proposal.target_docs_section!r} does not match "
                    f"contract section {rule.docs.section!r} for {diff.name!r}"
                )
