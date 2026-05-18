from pathlib import Path
from agent.models import DecisionProposal, DecisionAction
from agent.contract import OpsContract
from agent.secret_guard import is_secret_name

class ValidationError(Exception):
    pass

def _validate_path(p: str | None) -> None:
    if p is None:
        return
    if p.startswith("/") or ".." in Path(p).parts:
        raise ValidationError(f"target docs path rejected (absolute or traversal): {p!r}")

def validate(proposal: DecisionProposal, contract: OpsContract) -> None:
    """Raise ValidationError if proposal violates safety rules."""

    # 1. Action must be a known enum
    if not isinstance(proposal.action, DecisionAction):
        try:
            DecisionAction(proposal.action)
        except ValueError as e:
            raise ValidationError(f"unknown action: {proposal.action!r}") from e

    # 2. Confidence must be in [0, 1] — guards against LLM hallucinations like 1.5
    if not 0.0 <= proposal.confidence <= 1.0:
        raise ValidationError(f"confidence out of range [0,1]: {proposal.confidence}")

    # 3. Non-NO_OP actions require at least one diff (no empty PRs/issues)
    if proposal.action != DecisionAction.NO_OP and not proposal.env_diffs:
        raise ValidationError(f"action {proposal.action.value} requires at least one env_diff")

    # 4. Path guards
    _validate_path(proposal.target_docs_file)

    # 5. Docs PR semantics
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
