import re
from pathlib import Path
from agent.models import DecisionProposal, DecisionAction
from agent.contract import OpsContract

class ValidationError(Exception):
    pass

_SECRET_NAME_PATTERN = re.compile(
    r"(SECRET|TOKEN|KEY|PASSWORD|PASSWD|CRED|PRIVATE)",
    re.IGNORECASE,
)

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

    # 2. Path guards
    _validate_path(proposal.target_docs_file)

    # 3. Docs PR semantics
    if proposal.action == DecisionAction.DOCS_PR:
        for diff in proposal.env_diffs:
            # Secret-leak guard runs first — never document a secret-like name,
            # regardless of contract presence.
            if _SECRET_NAME_PATTERN.search(diff.name):
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

        # Target section must match contract for known vars
        for diff in proposal.env_diffs:
            rule = contract.expected_env.get(diff.name)
            if rule and proposal.target_docs_section and rule.docs.section != proposal.target_docs_section:
                raise ValidationError(
                    f"target_docs_section {proposal.target_docs_section!r} does not match "
                    f"contract section {rule.docs.section!r} for {diff.name!r}"
                )
