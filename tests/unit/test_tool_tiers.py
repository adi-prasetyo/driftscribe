"""Drift-pins for TOOL_TIERS — every tool has a tier; tiers cohere with
MUTATION_TOOL_NAMES (the existing trust-boundary classifier in fanout.py)."""
from agent.autonomy import TIER_NAMES
from agent.fanout import MUTATION_TOOL_NAMES
from agent.workloads.registry import TOOL_REGISTRY, TOOL_TIERS


def test_tool_tiers_cover_exactly_the_tool_registry():
    assert set(TOOL_TIERS) == set(TOOL_REGISTRY)


def test_tool_tiers_values_are_valid():
    assert set(TOOL_TIERS.values()) <= set(TIER_NAMES)


def test_every_propose_or_apply_tool_is_a_known_mutation_tool():
    # The dial's tier ladder must be at least as strict as the existing
    # mutation classifier: anything we let past Observe-stripping must
    # already be flagged write-capable there.
    elevated = {n for n, t in TOOL_TIERS.items() if t != "report"}
    assert elevated <= MUTATION_TOOL_NAMES


def test_report_tier_mutation_names_are_exactly_the_credential_containment_pair():
    # Two report-tier tools sit in MUTATION_TOOL_NAMES for DIFFERENT reasons:
    # notify IS a side effect (it posts a webhook) and is intentionally
    # allowed in Observe because it is the report-delivery channel — Observe
    # means "report only", not "silent". search_recent_prs is read-only and
    # is there purely for credential containment. This pin makes adding a
    # third such exception an explicit, reviewed decision.
    report_but_mutation = {
        n for n, t in TOOL_TIERS.items() if t == "report"
    } & MUTATION_TOOL_NAMES
    assert report_but_mutation == {"notify", "search_recent_prs"}


def test_apply_tier_is_exactly_merge():
    assert {n for n, t in TOOL_TIERS.items() if t == "apply"} == {"upgrade_merge_pr"}
