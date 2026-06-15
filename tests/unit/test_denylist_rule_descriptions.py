"""RULE_DESCRIPTIONS ↔ enforcement drift pin.

The capability card serves operator-facing copy for every denylist rule.
The rule IDs exist only as string literals — the first argument of every
``Violation(...)`` construction in ``driftscribe_lib.iac_plan_denylist``.
This module extracts those literals via AST so a 15th rule cannot ship
without a description (and a deleted rule cannot leave a stale one).
"""
from __future__ import annotations

import ast
import inspect

import driftscribe_lib.iac_plan_denylist as denylist_mod
from driftscribe_lib.iac_plan_denylist import RULE_DESCRIPTIONS


def _emitted_rule_ids() -> set[str]:
    """Every first arg to Violation(...) — FAIL LOUDLY on any non-literal.

    Codex review (2026-06-10): an earlier draft silently skipped calls whose
    first arg wasn't a string literal, so ``Violation(rule_id, ...)`` (a
    dynamic 15th rule) could ship with no description and the pin would
    still pass. Every ``Violation(...)`` call site MUST pass a string
    literal (or keyword ``rule="..."`` literal) — anything else fails this
    scan, which is the correct outcome: rewrite the call site as a literal
    or extend this scanner deliberately.
    """
    tree = ast.parse(inspect.getsource(denylist_mod))
    ids: set[str] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Violation"
        ):
            continue
        first = node.args[0] if node.args else next(
            (kw.value for kw in node.keywords if kw.arg == "rule"), None
        )
        assert isinstance(first, ast.Constant) and isinstance(first.value, str), (
            f"Violation(...) at line {node.lineno} does not pass its rule id "
            f"as a string literal — the RULE_DESCRIPTIONS drift pin cannot "
            f"see it. Use a literal."
        )
        ids.add(first.value)
    return ids


def test_every_emitted_rule_has_a_description_and_no_stale_ones():
    emitted = _emitted_rule_ids()
    assert emitted, "AST scan found no Violation(...) literals — scan is broken"
    assert set(RULE_DESCRIPTIONS) == emitted


def test_there_are_exactly_nineteen_rules():
    # The docstring promises 19 rule IDs; pin it so the AST scan can't
    # silently degrade (e.g. a refactor wrapping Violation in a helper).
    assert len(RULE_DESCRIPTIONS) == 19


def test_descriptions_are_operator_grade():
    for rule_id, text in RULE_DESCRIPTIONS.items():
        assert text and text[0].isupper() and len(text) >= 20, rule_id
        # No raw jargon the audience can't parse:
        assert "tuple" not in text.lower(), rule_id
