"""Section-targeted, idempotent runbook patcher.

Given the current runbook text + a list of EnvDiff + the OpsContract, return
the runbook text with each diff applied to its declared `## section`.

Behaviour:
- MATCH diffs are skipped (no change required).
- Secret-named vars are refused (defense-in-depth alongside the validator).
- If the target section is missing, a stub is appended.
- Existing bullets for a var (matched by name token) are replaced; otherwise
  a new bullet is appended to the section body.
- Idempotent: re-applying the same diffs to the patched output is a no-op.
"""

import re

from agent.contract import OpsContract
from agent.models import ContractStatus, EnvDiff
from agent.secret_guard import is_secret_name


def _section_pattern(section: str) -> re.Pattern[str]:
    """Match `## Section` and capture its body until the next `##` or EOF."""
    return re.compile(
        rf"(##\s+{re.escape(section)}\s*\n)(.*?)(?=\n##\s+|\Z)",
        re.DOTALL,
    )


def _update_var_line(
    body: str, name: str, new_value: str, operator_note: str | None
) -> str:
    """Replace any existing bullet for `name` with one carrying the new value
    + note. If no existing line, append.
    """
    note = f" **Operator note:** {operator_note}" if operator_note else ""
    new_line = f"- `{name}={new_value}` —{note}"
    pattern = re.compile(rf"^- `{re.escape(name)}=.*?`.*$", re.MULTILINE)
    if pattern.search(body):
        return pattern.sub(new_line, body)
    if body.endswith("\n"):
        return body + new_line + "\n"
    return body + "\n" + new_line + "\n"


def patch_runbook(
    content: str, diffs: list[EnvDiff], contract: OpsContract
) -> str:
    """Apply per-diff updates to a runbook. Idempotent.

    Refuses to write a value for any secret-named var — defense-in-depth even
    though the validator should have already blocked the proposal.
    """
    for diff in diffs:
        # Skip MATCH (no change required)
        if diff.contract_status == ContractStatus.MATCH:
            continue

        # Refuse to write secret-named vars
        if is_secret_name(diff.name):
            raise ValueError(
                f"refusing to write secret-named var {diff.name!r} into runbook"
            )

        rule = contract.expected_env.get(diff.name)
        section = rule.docs.section if rule else "Runtime Configuration"
        operator_note = rule.operator_note if rule else None
        new_value = diff.live if diff.live is not None else ""

        pat = _section_pattern(section)
        m = pat.search(content)
        if not m:
            # Section missing — append a stub
            stub = "" if content.endswith("\n") else "\n"
            content += f"{stub}\n## {section}\n\n"
            m = pat.search(content)
        assert m is not None  # we just guaranteed it
        header, body = m.group(1), m.group(2)
        new_body = _update_var_line(body, diff.name, new_value, operator_note)
        content = content[: m.start()] + header + new_body + content[m.end() :]
    return content
