"""Item 10: adoption-order + honesty copy in the prompts is PINNED to the lib.

The prompts are static .md files (no interpolation), so the canonical
sentences are duplicated by hand — these pins make the duplication safe:
changing ADOPTION_GUIDE order or the honesty note without updating both
prompts (or vice versa) fails here. The .md files hard-wrap, so both sides
are whitespace-normalized before the substring check.
"""
from pathlib import Path

import pytest

from driftscribe_lib.adopt_recipe import FINAL_REFUSAL_MARKER
from driftscribe_lib.iac_plan_summary import PLAN_RESOURCE_NAME_NOTE
from driftscribe_lib.infra_graph import (
    ADOPTION_CONTROL_PLANE_NOTE,
    ADOPTION_ORDER_HONESTY,
    adoption_order_sentence,
)

WORKLOADS = Path(__file__).resolve().parents[2] / "workloads"


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


@pytest.mark.parametrize("workload", ["explore", "provision"])
def test_prompt_carries_the_canonical_order_sentence(workload):
    text = _normalized(WORKLOADS / workload / "system_prompt.md")
    assert " ".join(adoption_order_sentence().split()) in text


@pytest.mark.parametrize("workload", ["explore", "provision"])
def test_prompt_carries_the_honesty_note(workload):
    text = _normalized(WORKLOADS / workload / "system_prompt.md")
    assert " ".join(ADOPTION_ORDER_HONESTY.split()) in text


@pytest.mark.parametrize("workload", ["explore", "provision"])
def test_prompt_carries_the_control_plane_note(workload):
    text = _normalized(WORKLOADS / workload / "system_prompt.md")
    assert " ".join(ADOPTION_CONTROL_PLANE_NOTE.split()) in text


@pytest.mark.parametrize("workload", ["explore", "provision"])
def test_prompt_carries_the_resource_name_note(workload):
    """The crew must name a resource by its real cloud name, not the Terraform
    address/label (the adopt-probe-topic vs adopt_adopt_probe_topic slip). Pin
    the canonical sentence into both plan-narrating prompts."""
    text = _normalized(WORKLOADS / workload / "system_prompt.md")
    assert " ".join(PLAN_RESOURCE_NAME_NOTE.split()) in text


def test_provision_prompt_quotes_the_final_refusal_marker():
    """PR #108 papercut: the prompt's "rejected = parameter feedback,
    call again" bullet contradicted the control-plane refusals whose
    reason says do-not-retry. The bullet now quotes the exact marker
    sentence; this pin keeps the quote and the lib constant in sync.
    """
    text = _normalized(WORKLOADS / "provision" / "system_prompt.md")
    assert " ".join(FINAL_REFUSAL_MARKER.split()) in text


def test_provision_prompt_relays_tool_next_steps_not_a_hardcoded_checklist():
    """Provision must RELAY the tool's situation-aware ``next_steps`` rather than
    a hardcoded checklist that predates C2 auto-dispatch (PR #128).

    The old list told the operator to "Dispatch the C2 plan-builder workflow on
    this PR number" and dropped the "takes a minute or two to build — reload if
    it isn't there yet" hint that ``iac_pr_next_steps`` already emits. So a
    chat-opened adoption looked like a failure when the operator clicked through
    to /iac-approvals before the ~80s plan build finished (PR #168). The fix
    relays ``next_steps`` verbatim; this pin keeps the prompt from relapsing into
    its own checklist (here or in the fan-out transparency note).
    """
    text = _normalized(WORKLOADS / "provision" / "system_prompt.md")
    lower = text.lower()
    # Positive: relay next_steps + convey the wait/reload expectation.
    assert "Relay `next_steps`" in text
    assert "reload" in lower
    # Negative: the pre-auto-dispatch imperative checklist and its drift twins.
    assert "Dispatch the C2 plan-builder workflow on this PR number" not in text
    assert "EXACT next steps" not in text
    assert "C2 plan → approve" not in text
