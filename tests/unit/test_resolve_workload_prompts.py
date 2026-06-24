"""Unit tests for the env-free prompt resolver behind GET /workloads/{name}/prompts."""
import pytest

from agent.workloads.registry import UnknownWorkloadError, resolve_workload_prompts


def test_drift_has_distinct_chat_prompt():
    p = resolve_workload_prompts("drift")
    assert p.chat_prompt_distinct is True
    assert p.chat_prompt is not None
    assert p.recheck_prompt.strip()
    assert p.chat_prompt.strip()
    # The two drift prompts are genuinely different files.
    assert p.recheck_prompt != p.chat_prompt


def test_upgrade_has_distinct_chat_prompt():
    p = resolve_workload_prompts("upgrade")
    assert p.chat_prompt_distinct is True
    assert p.chat_prompt is not None


@pytest.mark.parametrize("name", ["explore", "provision"])
def test_single_prompt_workloads_have_no_chat_override(name):
    p = resolve_workload_prompts(name)
    assert p.chat_prompt_distinct is False
    assert p.chat_prompt is None
    assert p.recheck_prompt.strip()


def test_unknown_workload_raises():
    with pytest.raises(UnknownWorkloadError):
        resolve_workload_prompts("nope")
