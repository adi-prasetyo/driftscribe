"""Regression guard: workload prompts must name each tool by its LLM-facing
name (the registered callable's ``__name__``), never the YAML *capability*
name.

Why this exists — a live, on-camera failure (2026-07-03, prod rev
``00142-5lv``; see ``docs/plans/2026-07-03-tool-name-grounding-mismatch.md``):

The ADK runner registers every tool under its Python function ``__name__``
(e.g. ``read_team_log_tool``), because
:func:`agent.adk_agent.build_agent` / :func:`~agent.adk_agent.build_chat_agent`
hand ADK ``list(workload.tools.values())`` — the raw callables — with no name
override. The workload manifests, by contrast, *enable* tools by their
symbolic **capability** name (e.g. ``read_team_log``) in
``enabled_tool_names``. Those two names differ for most tools (the capability
name is the authority layer; the ``_tool`` suffix is the callable's name).

When a system prompt describes a tool using the capability name, it teaches
the model a function name that does not exist at the ADK layer; a faithful
call emits that name and the runner fails the turn with "Tool not found".
Reasoning depth (PR #196) cannot fix a prompt that actively teaches a
nonexistent name — this is a grounding gap, so we pin it structurally.

The registry is the single source of truth for both sides of the check, so a
new tool or a renamed callable updates the guard automatically. This is the
audit in ``docs/plans/2026-07-03-tool-name-grounding-mismatch.md`` made
permanent.
"""
from __future__ import annotations

import re

import pytest

from agent.workloads.registry import (
    TOOL_REGISTRY,
    load_workload_spec,
    resolve_workload_prompts,
)

# The four deployed workloads (crews). Each ships one or two prompt files
# (recheck ``system_prompt.md`` and, when distinct, ``chat_system_prompt.md``).
_WORKLOADS = ["drift", "upgrade", "explore", "provision"]

# Every LLM-facing tool name the coordinator can expose to a model — the
# ``__name__`` of each resolved callable. This is exactly the name space the
# model may legitimately emit in a function call (MCP tools included; they
# carry no ``_tool`` suffix, so the naming is deliberately not uniform).
_VALID_LLM_TOOL_NAMES = frozenset(
    fn.__name__ for fn in TOOL_REGISTRY.values() if fn is not None
)


def _prompt_text(name: str) -> str:
    """Concatenate a workload's served prompt file(s) — the exact text the
    model reads. ``resolve_workload_prompts`` is env-free (never reads worker
    URL env vars), so this needs no worker-env fixture."""
    prompts = resolve_workload_prompts(name)
    parts = [prompts.recheck_prompt]
    if prompts.chat_prompt is not None:
        parts.append(prompts.chat_prompt)
    return "\n".join(parts)


def _bare_token(name: str) -> re.Pattern:
    """Match ``name`` as a complete, UNQUALIFIED snake_case token.

    The trailing ``(?![a-z0-9_])`` excludes the ``_tool``-suffixed form
    (``read_team_log`` will not match inside ``read_team_log_tool``). The
    leading ``(?<![a-z0-9_.])`` also excludes a dotted-qualified reference
    (``agent.contract.load_contract``): a qualified Python path is never what
    the model emits as an ADK ``function_call`` name, so it is not a bare
    tool-name teach — only an unqualified token is."""
    return re.compile(rf"(?<![a-z0-9_.]){re.escape(name)}(?![a-z0-9_])")


def _bare_teaches(text: str, cap: str) -> int:
    """Count occurrences where the text appears to teach the bare capability
    name ``cap`` as a tool.

    A compound (underscored) capability name is never natural prose, so any
    standalone occurrence is a tool-teach. A single-word capability name
    (only ``notify`` today) IS ordinary English, so it counts only when
    immediately followed by ``(`` — i.e. written as a call/signature
    ``notify(...)`` — never when used as the verb "notify the operator". This
    keeps the guard from false-positiving on prose while still catching a bare
    single-word tool teach.
    """
    if "_" in cap:
        return len(_bare_token(cap).findall(text))
    return len(re.findall(rf"(?<![a-z0-9_.]){re.escape(cap)}\(", text))


# Every capability name whose LLM-facing callable name DIFFERS from it — the
# aliases that must never appear bare in ANY model-facing text (prompt,
# docstring, dynamic instruction, or tool result). Built from the registry so
# a new tool or a renamed callable updates the guard automatically.
_ALIAS_TO_REAL = {
    cap: fn.__name__
    for cap, fn in TOOL_REGISTRY.items()
    if fn is not None and fn.__name__ != cap
}


def _bare_alias_offenders(text: str) -> dict[str, tuple[str, int]]:
    """``{capability_name: (real_name, count)}`` for every alias this text
    teaches bare. Shared by the docstring and dynamic-instruction guards."""
    out: dict[str, tuple[str, int]] = {}
    for cap, real in _ALIAS_TO_REAL.items():
        n = _bare_teaches(text, cap)
        if n:
            out[cap] = (real, n)
    return out


@pytest.mark.parametrize("workload", _WORKLOADS)
def test_prompt_never_teaches_a_bare_capability_name(workload):
    """No prompt may reference an enabled tool by its YAML capability name
    when that differs from the registered callable name — the bare name is
    not a real tool at the ADK layer, so a call using it fails "Tool not
    found". Direct regression pin for the 2026-07-03 incident."""
    text = _prompt_text(workload)
    caps = load_workload_spec(workload).enabled_tool_names

    offenders = {}
    for cap in caps:
        fn = TOOL_REGISTRY.get(cap)
        if fn is None or fn.__name__ == cap:
            continue  # reserved, or capability name already == LLM-facing name
        n = _bare_teaches(text, cap)
        if n:
            offenders[cap] = (fn.__name__, n)

    assert not offenders, (
        f"{workload} prompt teaches capability name(s) that do not exist at "
        f"the ADK layer — the model would emit them and fail 'Tool not "
        f"found'. Rename each to its registered callable name:\n"
        + "\n".join(
            f"  {cap!r} (x{n}) -> {real!r}"
            for cap, (real, n) in sorted(offenders.items())
        )
    )


@pytest.mark.parametrize("workload", _WORKLOADS)
def test_prompt_suffixed_tool_tokens_are_real_callables(workload):
    """Every ``*_tool`` token a prompt mentions must be a registered callable
    name. Complements the bare-name check by catching a mistyped or invented
    suffixed form (e.g. ``read_team_logs_tool``) that the bare-name check,
    which only knows the capability names, would miss."""
    text = _prompt_text(workload)
    suffixed = set(
        re.findall(r"(?<![a-z0-9_])([a-z0-9_]+_tool)(?![a-z0-9_])", text)
    )
    unknown = sorted(t for t in suffixed if t not in _VALID_LLM_TOOL_NAMES)
    assert not unknown, (
        f"{workload} prompt mentions *_tool token(s) that are not registered "
        f"callables: {unknown}. Known: {sorted(_VALID_LLM_TOOL_NAMES)}"
    )


def test_registered_tool_docstrings_use_llm_facing_names():
    """ADK sends each tool's docstring to the model as the tool's *description*.
    A bare capability name in a docstring therefore teaches the same
    nonexistent name the workload prompts did — same "Tool not found" risk.
    Scan every registered callable's ``__doc__`` against all aliases (a
    docstring may reference sibling tools, not just itself)."""
    problems: dict[str, dict[str, tuple[str, int]]] = {}
    for fn in TOOL_REGISTRY.values():
        if fn is None or not fn.__doc__:
            continue
        offenders = _bare_alias_offenders(fn.__doc__)
        if offenders:
            problems[fn.__name__] = offenders

    assert not problems, (
        "Tool docstring(s) — sent to the model as the tool description — name "
        "a capability that does not exist at the ADK layer:\n"
        + "\n".join(
            f"  {tool}.__doc__ teaches "
            + ", ".join(
                f"{cap!r}->{real!r} (x{n})" for cap, (real, n) in offs.items()
            )
            for tool, offs in sorted(problems.items())
        )
    )


def test_dynamic_model_facing_instructions_use_llm_facing_names():
    """Instruction/header strings composed at request time and handed to (or
    prepended to) an ADK agent are as model-facing as the prompt files. Guard
    the ones that reference tools: the cross-crew conversations breadcrumb, the
    fan-out decompose / slice-author instructions, and the docstrings of the
    dynamically-built fan-out hand-back tools (``submit_plan`` /
    ``submit_slice_file`` — created by factories, so not in TOOL_REGISTRY, but
    still ADK tool descriptions the model reads)."""
    from agent.adk_tools import _BREADCRUMB_HEADER
    from agent.fanout import (
        _DECOMPOSE_INSTRUCTION,
        _SLICE_AUTHOR_INSTRUCTION,
        make_submit_plan,
        make_submit_slice_file,
    )

    sources = {
        "adk_tools._BREADCRUMB_HEADER": _BREADCRUMB_HEADER,
        "fanout._DECOMPOSE_INSTRUCTION": _DECOMPOSE_INSTRUCTION,
        "fanout._SLICE_AUTHOR_INSTRUCTION": _SLICE_AUTHOR_INSTRUCTION,
        # Hand-back tools built per fan-out; their docstrings are static.
        "fanout.submit_plan.__doc__": make_submit_plan({}).__doc__ or "",
        "fanout.submit_slice_file.__doc__": (
            make_submit_slice_file("iac/x.tf", {}).__doc__ or ""
        ),
    }
    problems = {
        name: offs
        for name, text in sources.items()
        if (offs := _bare_alias_offenders(text))
    }
    assert not problems, (
        "Model-facing instruction constant(s) name a capability that does not "
        "exist at the ADK layer:\n"
        + "\n".join(
            f"  {name} teaches "
            + ", ".join(
                f"{cap!r}->{real!r} (x{n})" for cap, (real, n) in offs.items()
            )
            for name, offs in sorted(problems.items())
        )
    )


def test_open_infra_pr_freehand_reject_reason_names_real_tools():
    """The freehand-import rejection from ``open_infra_pr_tool`` is returned to
    the model as feedback and directs it to the adoption tool. That name must
    be the real callable — a bare capability name here reproduces the on-camera
    "Tool not found" on the provision authoring RETRY. The guard path returns
    before any GitHub call, so this makes zero worker calls."""
    from agent.adk_tools import open_infra_pr_tool

    result = open_infra_pr_tool(
        files=[
            {
                "path": "iac/adopt.tf",
                "content": (
                    'import {\n  to = google_storage_bucket.x\n'
                    '  id = "x"\n}\n'
                ),
            }
        ],
        title="freehand adopt",
        body="should be rejected by the freehand-import guard",
    )
    assert result["status"] == "rejected", result
    reason = result["reason"]
    assert "open_infra_pr_tool" in reason
    assert "propose_adoption_tool" in reason
    assert "provision_open_infra_pr" not in reason
    assert "provision_propose_adoption" not in reason
