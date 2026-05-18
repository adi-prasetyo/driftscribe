"""Section-targeted, idempotent runbook patcher.

Given the current runbook text + a list of EnvDiff + the OpsContract, return
the runbook text with each diff applied to its declared `## section`.

Behaviour:
- MATCH diffs are skipped (no change required).
- Secret-named vars are refused. ALL diffs are validated up front so a secret
  failure leaves the runbook unchanged (atomic).
- If the target section is missing, a stub is appended.
- Existing bullets for a var (matched by name token) are replaced; otherwise
  a new bullet is appended to the section body.
- Idempotent: re-applying the same diffs to the patched output is a no-op.
- Code fences (``` blocks) are masked during section lookup so a literal
  `## Heading` inside example code doesn't get treated as a real section.
"""

import re

from agent.contract import OpsContract
from agent.models import ContractStatus, EnvDiff
from agent.secret_guard import is_secret_name

_FENCE = re.compile(r"^(```|~~~)", re.MULTILINE)


def _mask_code_fences(text: str) -> str:
    """Return ``text`` with the contents of fenced code blocks replaced by
    same-length whitespace so regex section lookup ignores them. Original
    text indices are preserved so spans are still valid for slicing.
    """
    lines = text.split("\n")
    out_lines = []
    in_fence = False
    for line in lines:
        if _FENCE.match(line):
            in_fence = not in_fence
            out_lines.append(line)
        elif in_fence:
            # Replace with spaces so `##` lines inside fences don't match section
            out_lines.append(" " * len(line))
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


def _find_section(content: str, section: str) -> re.Match[str] | None:
    """Find the section in ``content`` using a fence-masked copy for matching.

    Returns the match object whose .start()/.end() are valid against the
    original ``content`` (because masking preserves byte/line offsets).
    """
    pattern = re.compile(
        rf"(##\s+{re.escape(section)}\s*\n)(.*?)(?=\n##\s+|\Z)",
        re.DOTALL,
    )
    masked = _mask_code_fences(content)
    masked_match = pattern.search(masked)
    if not masked_match:
        return None
    # Re-anchor against original content using the same span
    return pattern.search(content, masked_match.start())


def _sanitize_operator_note(note: str) -> str:
    """Collapse any newlines in the operator note to single-line form so the
    inserted bullet doesn't escape the list item."""
    return re.sub(r"\s*\r?\n\s*", " ", note).strip()


def _update_var_line(
    body: str, name: str, new_value: str, operator_note: str | None
) -> str:
    """Replace any existing bullet for ``name`` with one carrying the new value
    + note. If no existing line, append.
    """
    if operator_note:
        new_line = f"- `{name}={new_value}` — **Operator note:** {_sanitize_operator_note(operator_note)}"
    else:
        new_line = f"- `{name}={new_value}`"
    pattern = re.compile(rf"^- `{re.escape(name)}=.*?`.*$", re.MULTILINE)
    if pattern.search(body):
        return pattern.sub(new_line, body)
    if body.endswith("\n"):
        return body + new_line + "\n"
    return body + "\n" + new_line + "\n"


def patch_runbook(
    content: str, diffs: list[EnvDiff], contract: OpsContract
) -> str:
    """Apply per-diff updates to a runbook. Idempotent. Atomic — if any diff
    would be refused (e.g. secret-named var), no changes are applied.
    """
    # Atomic pre-check: refuse the whole patch if any diff is a secret name
    for diff in diffs:
        if diff.contract_status == ContractStatus.MATCH:
            continue
        if is_secret_name(diff.name):
            raise ValueError(
                f"refusing to write secret-named var {diff.name!r} into runbook"
            )

    for diff in diffs:
        if diff.contract_status == ContractStatus.MATCH:
            continue

        rule = contract.expected_env.get(diff.name)
        section = rule.docs.section if rule else "Runtime Configuration"
        operator_note = rule.operator_note if rule else None
        new_value = diff.live if diff.live is not None else ""

        m = _find_section(content, section)
        if not m:
            # Section missing — append a normalized stub
            content = content.rstrip() + f"\n\n## {section}\n\n"
            m = _find_section(content, section)
        assert m is not None  # we just guaranteed it
        header, body = m.group(1), m.group(2)
        new_body = _update_var_line(body, diff.name, new_value, operator_note)
        content = content[: m.start()] + header + new_body + content[m.end():]
    return content
