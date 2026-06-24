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


# --------------------------------------------------------------------------- #
# Part A — _contained_prompt_path helper + wiring into resolve_workload_prompts
# --------------------------------------------------------------------------- #


def test_contained_prompt_path_allows_plain_filename(tmp_path):
    from agent.workloads.registry import _contained_prompt_path
    p = _contained_prompt_path(tmp_path, "system_prompt.md")
    assert p == (tmp_path / "system_prompt.md").resolve()


def test_contained_prompt_path_rejects_parent_escape(tmp_path):
    from agent.workloads.registry import _contained_prompt_path
    with pytest.raises(ValueError):
        _contained_prompt_path(tmp_path, "../escape.md")


def test_contained_prompt_path_rejects_absolute(tmp_path):
    from agent.workloads.registry import _contained_prompt_path
    with pytest.raises(ValueError):
        _contained_prompt_path(tmp_path, "/etc/passwd")


def test_resolve_rejects_manifest_path_escape(monkeypatch):
    """Wiring proof: a malicious manifest filename makes the resolver raise,
    not read outside the workload dir. Monkeypatch _parse_spec so we don't need
    a real malicious workload on disk."""
    import types
    from agent.workloads import registry
    fake = types.SimpleNamespace(
        name="drift",
        system_prompt_file="../../escape.md",
        chat_system_prompt_file=None,
    )
    monkeypatch.setattr(registry, "_parse_spec", lambda *a, **k: fake)
    with pytest.raises(ValueError):
        registry.resolve_workload_prompts("drift")
