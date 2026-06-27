"""Phase D5-3: the constrained slice-author ADK Agent factory.

These tests pin the *trust boundary* of the parallel sub-agent fan-out:
each slice sub-agent gets the provision workload's READ tools plus its own
content-only ``submit_slice_file`` hand-back tool — and crucially NO
editor / PR / mutation tool. A sub-agent authors HCL text only; it cannot
open a PR. The merge-into-one-PR step is the coordinator's job, behind the
gated apply pipeline.

Two units under test (both in :mod:`agent.fanout`):

- :func:`resolve_provision_read_tools` — loads the ``provision`` workload
  and returns its tools with EVERY mutation tool stripped. The load-bearing
  check is that BOTH the symbolic name (``provision_open_infra_pr``) AND the
  callable ``__name__`` (``open_infra_pr_tool``) are filtered — they DIFFER,
  so filtering on only one would leak the editor tool into a slice agent.
- :func:`build_slice_author_agent` — wraps those read tools + the slice's
  pinned ``submit_slice_file`` into an ADK ``Agent`` with an identifier-safe
  name and a constrained system prompt.

Constructing an ADK ``Agent`` is OFFLINE (no network), so these tests build
real agents and introspect them.

How ADK tool names are introspected: ``Agent.tools`` holds the raw entries
the factory passed in. In this codebase those entries are plain Python
callables (see ``build_chat_agent`` / ``build_agent`` in
:mod:`agent.adk_agent`, whose tool-order pins read ``[t.__name__ for t in
agent.tools]``). So ``entry.__name__`` is the tool's callable name. We mirror
that exact introspection via :func:`_tool_name` below, falling back to a
wrapped ``.func``/``.__wrapped__`` if a future ADK release ever boxes a bare
callable into a FunctionTool.
"""
from __future__ import annotations

import re

import pytest

from agent.fanout import (
    MUTATION_TOOL_NAMES,
    SliceSpec,
    build_slice_author_agent,
    resolve_provision_read_tools,
)

# The provision workload resolves three worker URLs at load time:
# drift_reader (READER_URL), infra_reader (INFRA_READER_URL), tofu_editor
# (TOFU_EDITOR_URL). Mirror tests/unit/test_provision_workload.py's fixture.
_READ_TOOL_SYMBOLIC_NAMES = frozenset({
    "drift_read_live_env",
    "read_project_inventory",
    "load_contract",
    "search_developer_docs",
    "retrieve_developer_doc",
})

# The callable __name__ that backs the provision mutation tool. It DIFFERS
# from the symbolic name ``provision_open_infra_pr`` — that gap is the whole
# point of the double filter.
_MUTATION_CALLABLE_NAME = "open_infra_pr_tool"


@pytest.fixture
def provision_workload_env(monkeypatch):
    """Set the worker URL env vars the provision workload resolves at load
    time, and clear the workload cache on setup + teardown. Mirrors the
    fixture in tests/unit/test_provision_workload.py."""
    monkeypatch.setenv("READER_URL", "https://reader.test")
    monkeypatch.setenv("INFRA_READER_URL", "https://infra-reader.test")
    monkeypatch.setenv("TOFU_EDITOR_URL", "https://tofu-editor.test")
    import agent.workloads.registry as registry_mod
    registry_mod._WORKLOAD_CACHE.clear()
    yield
    registry_mod._WORKLOAD_CACHE.clear()


def _tool_name(entry) -> str:
    """Best-effort callable name for an ``Agent.tools`` entry.

    In this codebase the entries are bare callables, so ``entry.__name__``
    is the answer (this mirrors ``build_chat_agent``'s tool-order pin in
    tests/unit/test_coordinator_tool_inventory.py). The fallbacks cover a
    hypothetical future ADK release that boxes a callable in a FunctionTool
    wrapper exposing ``.func`` / ``.__wrapped__``."""
    if hasattr(entry, "__name__"):
        return entry.__name__
    for attr in ("func", "__wrapped__"):
        inner = getattr(entry, attr, None)
        if inner is not None and hasattr(inner, "__name__"):
            return inner.__name__
    return repr(entry)


def _slice_spec(path: str = "iac/bucket.tf", goal: str = "create a GCS bucket") -> SliceSpec:
    return SliceSpec(goal=goal, target_path=path)


# --------------------------------------------------------------------------- #
# resolve_provision_read_tools
# --------------------------------------------------------------------------- #


def test_resolve_provision_read_tools_returns_the_five_read_tools(
    provision_workload_env,
):
    read_tools = resolve_provision_read_tools()
    symbolic = set(read_tools.keys())
    assert symbolic == set(_READ_TOOL_SYMBOLIC_NAMES), (
        f"expected exactly the 5 provision read tools, got {sorted(symbolic)}"
    )


def test_resolve_provision_read_tools_excludes_read_conversations(
    provision_workload_env,
):
    """``read_conversations`` is read-only but deliberately kept OUT of the
    fan-out sub-agents: it returns untrusted cross-crew text and the fan-out
    decompose/slice prompts don't carry the injection guard (Codex review). It
    stays on the operator-facing provision CHAT agent, which does."""
    assert "read_conversations" not in resolve_provision_read_tools()


def test_resolve_provision_read_tools_excludes_the_mutation_tool(
    provision_workload_env,
):
    """The editor tool must NOT leak — neither by symbolic name nor by
    callable name."""
    read_tools = resolve_provision_read_tools()
    assert "provision_open_infra_pr" not in read_tools
    callable_names = {fn.__name__ for fn in read_tools.values()}
    assert _MUTATION_CALLABLE_NAME not in callable_names


def test_resolve_provision_read_tools_double_filter_regression(
    provision_workload_env,
):
    """Regression-proof: NONE of the resolved tools' callable ``__name__``s
    is ``open_infra_pr_tool``, AND none of the symbolic keys is in
    ``MUTATION_TOOL_NAMES``. This is the load-bearing trust check — the
    symbolic name and the callable name DIFFER, so a single-sided filter
    would leak the editor tool."""
    read_tools = resolve_provision_read_tools()

    leaked_symbolic = set(read_tools.keys()) & set(MUTATION_TOOL_NAMES)
    assert not leaked_symbolic, (
        f"mutation symbolic tool(s) leaked into the read set: {leaked_symbolic}"
    )
    for fn in read_tools.values():
        assert fn.__name__ != _MUTATION_CALLABLE_NAME, (
            f"the editor callable {_MUTATION_CALLABLE_NAME} leaked into the "
            f"read set despite its symbolic name being filtered"
        )


def test_mutation_tool_names_carries_provision_open_infra_pr():
    """The canonical mutation set must classify the provision editor tool —
    that is what makes the symbolic-name filter strip it."""
    assert "provision_open_infra_pr" in MUTATION_TOOL_NAMES


# --------------------------------------------------------------------------- #
# build_slice_author_agent
# --------------------------------------------------------------------------- #


def test_build_slice_author_agent_name_is_identifier_safe(provision_workload_env):
    read_tools = resolve_provision_read_tools()
    sink: dict = {}
    spec = _slice_spec("iac/bucket.tf")
    agent = build_slice_author_agent(spec, read_tools, sink, 0)

    assert re.match(r"^[A-Za-z0-9_]+$", agent.name), (
        f"agent name {agent.name!r} is not identifier-safe"
    )
    # The slugged path (iac/bucket.tf -> iac_bucket_tf) appears in the name.
    assert "iac_bucket_tf" in agent.name


def test_build_slice_author_agent_tool_set(provision_workload_env):
    """The agent carries submit_slice_file + the read tools, and EXCLUDES
    the editor callable / anything in the mutation set."""
    read_tools = resolve_provision_read_tools()
    sink: dict = {}
    spec = _slice_spec("iac/bucket.tf")
    agent = build_slice_author_agent(spec, read_tools, sink, 0)

    tool_names = {_tool_name(t) for t in agent.tools}
    assert "submit_slice_file" in tool_names
    # Every read tool callable is present.
    for fn in read_tools.values():
        assert fn.__name__ in tool_names
    # The editor callable is absent.
    assert _MUTATION_CALLABLE_NAME not in tool_names


def test_build_slice_author_agent_instruction_is_constrained(provision_workload_env):
    read_tools = resolve_provision_read_tools()
    sink: dict = {}
    spec = _slice_spec("iac/storage.tf", goal="add a versioning block to the bucket")
    agent = build_slice_author_agent(spec, read_tools, sink, 0)

    instr = agent.instruction
    # Pinned path + goal are injected.
    assert "iac/storage.tf" in instr
    assert "add a versioning block to the bucket" in instr
    # The no-PR trust boundary is stated explicitly.
    lowered = instr.lower()
    assert "submit_slice_file" in instr
    assert "pr" in lowered  # mentions PR (in the "do NOT open a PR" sense)
    assert "not" in lowered  # negation present
    # A crude but real check that the prompt forbids opening a PR / mutation.
    assert ("not open a pr" in lowered) or ("no" in lowered and "mutation" in lowered)


def test_two_slices_get_distinct_names_and_isolated_sinks(provision_workload_env):
    """Two specs -> two DIFFERENT agent names, and each agent's submit tool
    writes to its OWN sink (isolation)."""
    read_tools = resolve_provision_read_tools()
    sink_a: dict = {}
    sink_b: dict = {}
    spec_a = _slice_spec("iac/bucket.tf", goal="create bucket")
    spec_b = _slice_spec("iac/network.tf", goal="create vpc")

    agent_a = build_slice_author_agent(spec_a, read_tools, sink_a, 0)
    agent_b = build_slice_author_agent(spec_b, read_tools, sink_b, 1)

    assert agent_a.name != agent_b.name

    submit_a = next(t for t in agent_a.tools if _tool_name(t) == "submit_slice_file")
    submit_b = next(t for t in agent_b.tools if _tool_name(t) == "submit_slice_file")

    submit_a(content="resource A {}")
    submit_b(content="resource B {}")

    # Each sink recorded its own slice's path + content — no cross-talk.
    assert sink_a["file"]["path"] == "iac/bucket.tf"
    assert sink_a["file"]["content"] == "resource A {}"
    assert sink_b["file"]["path"] == "iac/network.tf"
    assert sink_b["file"]["content"] == "resource B {}"


# --------------------------------------------------------------------------- #
# Name uniqueness by construction (slice-index prefix)
# --------------------------------------------------------------------------- #


def test_slug_colliding_disjoint_paths_get_distinct_names(provision_workload_env):
    """Two VALID, DISJOINT paths that SLUG to the SAME string must still get
    distinct agent names — the slice-index prefix guarantees uniqueness.

    ``iac/foo-bar.tf`` and ``iac/foo_bar.tf`` both slug to ``iac_foo_bar_tf``
    (the hyphen and the underscore both become ``_``). Without the idx prefix
    both agents would be named ``driftscribe_slice_iac_foo_bar_tf`` — a
    duplicate ADK name that collapses their ParallelAgent branches and corrupts
    the ``name_to_slice`` tagging map."""
    read_tools = resolve_provision_read_tools()
    agent_0 = build_slice_author_agent(
        _slice_spec("iac/foo-bar.tf"), read_tools, {}, 0
    )
    agent_1 = build_slice_author_agent(
        _slice_spec("iac/foo_bar.tf"), read_tools, {}, 1
    )

    assert agent_0.name != agent_1.name
    # Both names remain valid Python identifiers.
    assert agent_0.name.isidentifier()
    assert agent_1.name.isidentifier()


def test_long_paths_sharing_first_64_slug_chars_get_distinct_names(
    provision_workload_env,
):
    """Two paths whose slugs share the first ``_MAX_SLUG_LEN`` (64) chars (so
    the slug TRUNCATION collides) still get distinct names via the idx prefix."""
    common = "x" * 70  # > 64, so the slugs are identical after truncation
    read_tools = resolve_provision_read_tools()
    agent_0 = build_slice_author_agent(
        _slice_spec(f"iac/{common}a.tf"), read_tools, {}, 0
    )
    agent_1 = build_slice_author_agent(
        _slice_spec(f"iac/{common}b.tf"), read_tools, {}, 1
    )

    assert agent_0.name != agent_1.name
    assert agent_0.name.isidentifier()
    assert agent_1.name.isidentifier()


def test_slice_index_appears_in_name_and_name_is_identifier(provision_workload_env):
    """The slice index is embedded in the name (so it is unique-by-construction)
    and the resulting name is a valid Python identifier."""
    read_tools = resolve_provision_read_tools()
    agent = build_slice_author_agent(_slice_spec("iac/bucket.tf"), read_tools, {}, 7)
    assert agent.name == "driftscribe_slice_7_iac_bucket_tf"
    assert agent.name.isidentifier()
