"""Golden-parity guard for the Task-1 parser refactor.

Captures tools.iac_static_gate's parse + block-iteration output on the real
committed iac/*.tf BEFORE primitives move to driftscribe_lib.iac_hcl, then
asserts the shared module reproduces it byte-for-byte. Protects the merged
Phase A gate from a refactor regression.
"""
from pathlib import Path

import pytest

IAC = Path(__file__).resolve().parents[2] / "iac"
TF_FILES = sorted(p.name for p in IAC.glob("*.tf"))


@pytest.mark.parametrize("fname", TF_FILES)
def test_shared_parse_matches_gate_parse(fname):
    from tools import iac_static_gate as gate
    from driftscribe_lib import iac_hcl

    content = (IAC / fname).read_text(encoding="utf-8")
    # Same parser, same result (the gate delegates to the shared parser).
    assert iac_hcl.parse_hcl(content) == gate._parse(fname, content)


def test_meta_key_and_unwrap_parity():
    from tools import iac_static_gate as gate
    from driftscribe_lib import iac_hcl

    for k in ("__is_block__", "__start_line__", "__inline_comments__"):
        assert iac_hcl.is_meta_key(k) is True
        assert gate._is_meta_key(k) is True
    assert iac_hcl.is_meta_key("google") is False
    assert iac_hcl.unwrap('"hashicorp/google"') == "hashicorp/google"


def test_gate_iter_typed_blocks_still_yields_2_tuples():
    """The gate's internal contract is (type, body); the shared module yields
    (type, name, body). Pin BOTH so the adapter can't silently change either."""
    from tools import iac_static_gate as gate
    from driftscribe_lib import iac_hcl

    src = 'resource "null_resource" "x" { triggers = {} }'
    parsed = iac_hcl.parse_hcl(src)
    gate_yield = list(gate._iter_typed_blocks(parsed, "resource"))
    shared_yield = list(iac_hcl.iter_typed_blocks(parsed, "resource"))
    assert all(len(t) == 2 for t in gate_yield)          # (type, body)
    assert all(len(t) == 3 for t in shared_yield)        # (type, name, body)
    assert gate_yield[0][0] == "null_resource"
    assert shared_yield[0][:2] == ("null_resource", "x")


def test_gate_policy_intact_after_refactor():
    """The refactor must not loosen the gate. Spot-check the high-value rules
    still fire (the policy stays in tools/iac_static_gate.py, not the shared
    module)."""
    from tools.iac_static_gate import GateInput, GateMode, evaluate

    bad = {
        "iac/x.tf": 'resource "null_resource" "x" {}\n'
                    'data "external" "y" { program = ["sh"] }',
    }
    rules = {v.rule for v in evaluate(
        GateInput(mode=GateMode.OPERATOR, changed_paths=("iac/x.tf",), hcl_files=bad)
    )}
    assert "arbitrary-execution" in rules
    assert "forbidden-data-source" in rules
