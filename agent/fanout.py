"""Pure decomposition/validation core for D5 parallel sub-agent fan-out.

PURE + offline: no agents, no async, no network. This module is the
foundation slice (Phase D5-1) of the coordinator's parallel fan-out: it
defines the :class:`SliceSpec` data model (one independent ``iac/`` file
slice), the fan-out module's own typed :class:`FanoutError`, and
:func:`validate_slice_specs` — the fail-closed gate the later orchestrator
runs *before* spawning any sub-agent.

Design notes:

- **Why a dedicated error type.** :class:`FanoutError` does NOT subclass
  :class:`driftscribe_lib.iac_editor_policy.EditorPolicyError`; it carries an
  explicit :class:`FanoutFailureKind`. The orchestrator (a later slice)
  branches fail-open vs fail-closed on ``kind`` — never on the HTTP status —
  so a 422 from a transient authoring hiccup and a 422 from a policy rejection
  stay distinguishable. The HTTP ``status`` is preserved only so the
  coordinator can map a surfaced error straight onto an ``HTTPException``.

- **Single source of truth for path policy.** Per-slice ``target_path``
  validation delegates to
  :func:`driftscribe_lib.iac_editor_policy.validate_iac_path` (the public
  promotion of the editor worker's path allowlist) and re-raises any
  :class:`EditorPolicyError` as a POLICY-kind :class:`FanoutError`. The
  library error never leaks out of this module, and the fan-out file gate
  cannot drift apart from the editor worker's gate.
"""
from __future__ import annotations

import enum
import json
import re
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from agent.autonomy import mode_allows
from driftscribe_lib.iac_editor_policy import (
    EditorPolicyError,
    validate_iac_path,
    validate_title_body,
)

# ``Runner`` is imported at module scope (NOT lazily like the other ADK
# symbols) for ONE reason: it is the mock seam for :func:`decompose`. Tests
# patch ``agent.fanout.Runner`` (mirroring how ``tests/unit/test_run_chat_stream
# .py`` patches ``agent.adk_agent.Runner``), which requires the name to be a
# real module attribute at patch time and to be the name ``decompose`` reads.
# A lazy in-function import would shadow the patch and the mock would never
# take effect. The rest of the ADK surface stays lazily imported inside the
# agent-building functions so the pure-decomposition core (SliceSpec /
# validate_slice_specs) callers don't pull ADK in.
from google.adk.runners import Runner

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime ADK import here
    from google.adk import Agent

# Fan-out fans the coordinator's authoring concurrency out to at most this many
# sub-agents. A small cap bounds the parallel surface (each slice spawns a
# sub-agent + opens against the same branch later) and keeps the merge step
# tractable. Deliberately well under iac_editor_policy.MAX_FILES (32): a slice
# is an authoring unit, not a single file.
MAX_SLICES = 8

# Sane upper bound on a slice goal so a malformed/oversized request can't
# amplify into an enormous sub-agent prompt. Codepoint length (these map to
# prompt text, not file bytes), mirroring iac_editor_policy's title/body caps.
MAX_GOAL_CHARS = 2000


class FanoutFailureKind(enum.Enum):
    """How a fan-out failure should be handled by the (later) orchestrator.

    The orchestrator branches fail-open vs fail-closed on this ``kind``, NOT on
    an HTTP status code — so the policy/non-policy distinction survives even
    when two failures share a status. String values keep logs/traces readable.

    - ``POLICY``: a fail-closed allowlist/shape rejection (bad/duplicate path,
      count bounds, foundation guard). Must abort the whole fan-out.
    - ``DECOMPOSE_NON_POLICY``: decomposition failed for a non-policy reason
      (e.g. the decomposer produced nothing usable) — distinct from POLICY so
      the orchestrator can choose a fall-back path.
    - ``AUTHORING``: a sub-agent authoring step failed.
    - ``EDITOR``: the downstream editor/PR step failed.
    """

    POLICY = "policy"
    DECOMPOSE_NON_POLICY = "decompose_non_policy"
    AUTHORING = "authoring"
    EDITOR = "editor"


class FanoutError(Exception):
    """The fan-out module's own error type.

    Carries the HTTP ``status`` (for the coordinator's ``HTTPException``
    mapping), a human-readable ``detail``, and the branch-able ``kind``. It is
    intentionally NOT a subclass of
    :class:`driftscribe_lib.iac_editor_policy.EditorPolicyError`: the library
    error is translated into this type at the boundary so the library error
    never leaks out and the orchestrator has the ``kind`` it needs.
    """

    def __init__(self, status: int, detail: str, *, kind: FanoutFailureKind) -> None:
        self.status = status
        self.detail = detail
        self.kind = kind
        super().__init__(detail)

    def __str__(self) -> str:
        return f"{self.kind.value} {self.status}: {self.detail}"


class SliceSpec(BaseModel):
    """One independent ``iac/`` authoring slice in a fan-out request.

    A slice is the unit a single sub-agent authors in parallel: a ``goal``
    (what to build, in prose), the ``target_path`` it is allowed to write
    (validated against the ``iac/`` allowlist by :func:`validate_slice_specs`,
    not here — path policy is a cross-slice concern), and optional
    ``doc_citations`` the decomposer attached for grounding.

    ``extra="forbid"`` so a stray/sneaky field (e.g. a second path) fails
    loudly rather than being silently dropped — same property as the
    coordinator's other strict models.
    """

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=MAX_GOAL_CHARS)
    target_path: str
    doc_citations: list[str] = Field(default_factory=list)

    @field_validator("goal")
    @classmethod
    def _goal_non_blank(cls, v: str) -> str:
        """Reject an all-whitespace goal at the model level (``min_length``
        alone would accept ``"   "``). Returns the value unchanged on success —
        no normalization, so the caller's text is preserved verbatim."""
        if not v.strip():
            raise ValueError("slice goal must be non-empty after strip")
        return v


def validate_slice_specs(specs: list[SliceSpec]) -> None:
    """Fail-closed validation of a fan-out slice set. Returns None on success.

    Enforces, in order:

    1. Count bounds — ``1 <= len(specs) <= MAX_SLICES`` (else POLICY 422).
    2. Per-slice ``target_path`` policy via
       :func:`driftscribe_lib.iac_editor_policy.validate_iac_path`; any
       :class:`EditorPolicyError` is translated to a POLICY-kind
       :class:`FanoutError` carrying the library's status/reason (the library
       error is never allowed to propagate).
    3. Disjoint ``target_path``s across slices — a duplicate is POLICY 422.

    Every rejection is :class:`FanoutFailureKind.POLICY`: this is the
    fail-closed gate the orchestrator runs before spawning any sub-agent, so a
    rejection here must abort the whole fan-out.
    """
    if len(specs) == 0:
        raise FanoutError(422, "no slices supplied", kind=FanoutFailureKind.POLICY)
    if len(specs) > MAX_SLICES:
        raise FanoutError(
            422,
            f"too many slices (> {MAX_SLICES})",
            kind=FanoutFailureKind.POLICY,
        )

    seen: set[str] = set()
    for spec in specs:
        try:
            validate_iac_path(spec.target_path)
        except EditorPolicyError as e:
            # Translate the library error — never let EditorPolicyError leak.
            raise FanoutError(
                e.status_code, e.reason, kind=FanoutFailureKind.POLICY
            ) from e
        if spec.target_path in seen:
            raise FanoutError(
                422,
                f"duplicate slice path: {spec.target_path}",
                kind=FanoutFailureKind.POLICY,
            )
        seen.add(spec.target_path)


def make_submit_slice_file(
    target_path: str, sink: dict
) -> Callable[..., dict]:
    """Build the authority-clean hand-back tool for ONE fan-out slice.

    Each slice sub-agent gets its own ``submit_slice_file`` closure. The
    slice's ``target_path`` is PINNED here, server-side — it is captured in
    the closure and is the ONLY source of the recorded path. The returned
    tool exposes ONLY ``content`` (+ optional ``citations``) to the LLM, so
    the model can never influence the path/repo/branch. This mirrors the
    authority-clean philosophy of
    :func:`agent.adk_tools.open_infra_pr_tool`, where the LLM supplies only
    the decision content and every routing field is derived server-side.

    The returned callable's ``__name__`` is ``submit_slice_file`` and its
    annotated signature is what google-adk turns into the tool's
    function-declaration; the docstring is the model-facing tool description.

    This tool only RECORDS into ``sink`` — it deliberately does NOT validate
    or reject the content. Empty/oversize content and path policy are enforced
    later by the fan-out barrier via ``validate_file_writes``; surfacing those
    rejections there (not here) keeps a single source of truth for file policy.
    """

    def submit_slice_file(
        content: str, citations: list[str] | None = None
    ) -> dict:
        """Submit the FULL file content you authored for your assigned path.

        Call this exactly once when your slice is complete. Pass the ENTIRE
        final file body in ``content`` (not a diff or a fragment) — it is
        written verbatim to the path this slice was assigned. You do NOT
        choose or pass the path: it is fixed for your slice. In
        ``citations`` (optional) list any developer-doc URLs or identifiers
        you relied on to author the file, for grounding/audit.

        Calling this again within the same slice OVERWRITES the previous
        submission (last write wins). Returns an acknowledgement
        ``{"status": "recorded", "path": <your assigned path>, "bytes": <UTF-8
        byte length of content>}``.
        """
        sink["file"] = {"path": target_path, "content": content}
        sink["citations"] = list(citations) if citations else []
        return {
            "status": "recorded",
            "path": target_path,
            "bytes": len(content.encode("utf-8")),
        }

    return submit_slice_file


# --------------------------------------------------------------------------- #
# D5-3: the constrained slice-author agent + provision read-tool resolution
# --------------------------------------------------------------------------- #
#
# Single source of truth for "which symbolic tool names are mutation tools".
# This set is the trust-boundary classifier used by
# :func:`resolve_provision_read_tools` to strip the editor/PR tool out of a
# slice sub-agent's tool set. It is the SAME canonical set the coordinator
# tool-inventory audit pins: ``tests/unit/test_coordinator_tool_inventory.py``
# imports it from here (so the audit and the runtime filter cannot drift
# apart) and asserts the read-only/mutation disjointness invariants on it.
# The names mirror every symbolic tool that either mutates a system OR rides
# a write-capable credential — see that test module for the per-name
# rationale (notify/search_recent_prs are here for credential containment,
# not because their code writes).
MUTATION_TOOL_NAMES: frozenset[str] = frozenset({
    "drift_patch_docs",
    "drift_propose_rollback",
    "upgrade_propose_pr",
    "upgrade_close_pr",
    "upgrade_merge_pr",
    "notify",
    "search_recent_prs",
    # The provision workload's IaC-authoring tool: it rides the tofu-editor's
    # write-capable GitHub PAT and opens a PR (a mutation). Its symbolic name
    # ``provision_open_infra_pr`` DIFFERS from its callable ``__name__``
    # (``open_infra_pr_tool``) — both are filtered below, see the
    # double-filter rationale on resolve_provision_read_tools.
    "provision_open_infra_pr",
    # Adopt tool: renders probe-proven zero-change import HCL and opens a PR
    # via the same tofu-editor path. Symbolic name ``provision_propose_adoption``
    # differs from callable ``__name__`` (``propose_adoption_tool``).
    "provision_propose_adoption",
})

# Belt-and-suspenders companion to MUTATION_TOOL_NAMES: the set of *callable*
# ``__name__``s that back a mutation tool. This matters because a symbolic
# name and its callable name can DIFFER (``provision_open_infra_pr`` resolves
# to ``open_infra_pr_tool``), so filtering on only the symbolic name would
# leak the editor callable into a slice agent if the YAML ever exposed it
# under a different symbolic name. Filtering on BOTH is the load-bearing
# trust check that keeps a slice sub-agent authority-clean (read + author
# only; no PR/apply/mutation).
MUTATION_CALLABLE_NAMES: frozenset[str] = frozenset({
    "open_infra_pr_tool",
    "propose_adoption_tool",
})


def resolve_provision_read_tools() -> dict[str, Callable]:
    """Resolve the ``provision`` workload's READ tools — every mutation
    tool stripped — as a ``{symbolic_name: callable}`` mapping.

    Loads :func:`agent.workloads.load_workload` ``("provision")`` and returns
    its ``.tools`` mapping with every mutation tool removed. A tool is dropped
    if EITHER its symbolic name is in :data:`MUTATION_TOOL_NAMES` OR its
    callable ``__name__`` is in :data:`MUTATION_CALLABLE_NAMES`. BOTH filters
    matter: the symbolic name (``provision_open_infra_pr``) and the callable
    name (``open_infra_pr_tool``) DIFFER, so filtering on only one would leak
    the editor tool into a slice sub-agent. This is the load-bearing trust
    check behind the fan-out boundary — sub-agents author HCL text only and
    must never be handed a PR/apply/mutation tool.

    The result preserves the workload's tool order (insertion order of the
    resolution mapping) for the surviving read tools, and is a plain mutable
    ``dict`` the caller owns (the workload's own ``tools`` is a read-only
    ``MappingProxyType`` view — we copy out of it, never mutate it).

    Note this imports ``load_workload`` lazily so the pure-decomposition core
    of this module stays import-light (no agent/registry pull-in for the
    callers that only use :class:`SliceSpec` / :func:`validate_slice_specs`).
    """
    from agent.workloads import load_workload

    resolution = load_workload("provision")
    return {
        symbolic: fn
        for symbolic, fn in resolution.tools.items()
        if symbolic not in MUTATION_TOOL_NAMES
        and getattr(fn, "__name__", "") not in MUTATION_CALLABLE_NAMES
    }


# Bound on the slugged path segment of a slice agent's name. ADK agent names
# must be identifier-safe and we keep them short so the (already-prefixed)
# name stays a sane Python identifier even for a deep/long target path.
_MAX_SLUG_LEN = 64


def _slug_target_path(target_path: str) -> str:
    """Turn a ``target_path`` into an identifier-safe slug.

    Replaces every char outside ``[A-Za-z0-9_]`` with ``_`` (so
    ``iac/bucket.tf`` -> ``iac_bucket_tf``; no hyphens, dots, or slashes),
    collapses leading/trailing underscores, and bounds the length. The result
    is used as the suffix of an ADK ``Agent.name``, which must be a valid
    Python identifier (letters/digits/underscores; NO hyphens/dots) — see the
    name comment in :func:`agent.adk_agent.build_chat_agent`.
    """
    slug = re.sub(r"[^A-Za-z0-9_]", "_", target_path).strip("_")
    if not slug:
        slug = "slice"
    return slug[:_MAX_SLUG_LEN]


# The constrained slice-author system prompt. The trust boundary is stated in
# the prompt itself (you have NO PR/apply/mutation tool; you must NOT attempt
# to open a PR) AND enforced structurally by the tool set built in
# :func:`build_slice_author_agent` (the editor tool is filtered out). Prompt
# discipline is defense-in-depth on top of the structural guarantee, never the
# only guard. ``{goal}`` and ``{target_path}`` are injected per slice.
_SLICE_AUTHOR_INSTRUCTION = """\
You are a DriftScribe slice-author sub-agent. You author EXACTLY ONE \
OpenTofu (HCL) file and nothing else.

Your assigned file (the ONLY path you may write): {target_path}
Your goal for that file: {goal}

Rules:
- Author the single file `{target_path}` to achieve the goal above. Keep the \
change MINIMAL and in-place: add only what the goal requires, and match the \
existing style/conventions of the surrounding `iac/` files.
- Before authoring, READ the current state with your read tools (live env, \
project inventory, the ops contract, and developer docs) so your file is \
grounded in what already exists.
- You have NO PR tool, NO apply tool, and NO mutation tool of any kind. You \
must NOT attempt to open a PR, merge, or apply anything. Authoring the file \
text is the entire extent of your authority — opening the pull request is \
done downstream, not by you.
- NEVER author providers, modules, provisioners, secrets, backend, or other \
foundation files: the downstream static gate rejects them and your slice \
would fail. Author only the ordinary resource/data/locals content the goal \
calls for, scoped to `{target_path}`.
- When the file is complete, call `submit_slice_file` with the FULL final \
file content (the entire file body, not a diff or fragment) and any \
developer-doc citations you relied on. You do not choose the path — it is \
pinned to `{target_path}` for you.
"""


def build_slice_author_agent(
    spec: SliceSpec, read_tools, sink: dict, slice_index: int
) -> Agent:
    """Construct the constrained slice-author ADK ``Agent`` for one slice.

    The agent gets the provision workload's READ tools plus this slice's
    pinned ``submit_slice_file`` hand-back tool (built via
    :func:`make_submit_slice_file`, which pins ``spec.target_path`` and writes
    into ``sink``) — and crucially NO editor / PR / mutation tool. That is the
    fan-out trust boundary: a sub-agent authors HCL text only; it cannot open
    a PR (the coordinator merges the slices into ONE PR downstream, behind the
    gated apply pipeline).

    ``read_tools`` is the ``{symbolic_name: callable}`` mapping returned by
    :func:`resolve_provision_read_tools` (it accepts the mapping; only the
    callable VALUES are handed to the agent). The agent's ``name`` is
    ``driftscribe_slice_<slice_index>_<slugged-target-path>`` — identifier-safe
    (it begins with the literal ``driftscribe_slice_<int>_`` and the slug
    replaces every non-``[A-Za-z0-9_]`` char with ``_``). The ``slice_index``
    (the slice's 0-based ordinal within the fan-out, guaranteed unique there)
    makes the name UNIQUE BY CONSTRUCTION: two DISJOINT paths can slug to the
    SAME suffix (e.g. ``iac/foo-bar.tf`` and ``iac/foo_bar.tf`` both slug to
    ``iac_foo_bar_tf``, and any two paths sharing the first ``_MAX_SLUG_LEN``
    slug chars collide after truncation), but their index prefixes differ — so
    ADK never sees a duplicate sub-agent name and the ``name_to_slice`` tagging
    map is never overwritten. The slug suffix is kept purely for human
    readability. Model/planner mirror :func:`agent.adk_agent.build_chat_agent`
    exactly (``gemini-2.5-flash`` + ``BuiltInPlanner(ThinkingConfig(
    include_thoughts=True))``).

    Constructing the ``Agent`` is offline (no network) — the ADK imports
    happen lazily here so the pure-decomposition core of this module stays
    import-light for callers that only need :class:`SliceSpec` /
    :func:`validate_slice_specs`.
    """
    from google.adk import Agent
    from google.adk.planners.built_in_planner import BuiltInPlanner
    from google.genai.types import ThinkingConfig

    read_tool_values = list(read_tools.values())
    submit_tool = make_submit_slice_file(spec.target_path, sink)

    return Agent(
        name=f"driftscribe_slice_{slice_index}_{_slug_target_path(spec.target_path)}",
        model="gemini-2.5-flash",
        instruction=_SLICE_AUTHOR_INSTRUCTION.format(
            goal=spec.goal,
            target_path=spec.target_path,
        ),
        tools=[*read_tool_values, submit_tool],
        # Mirror build_chat_agent: surface Gemini 2.5 Flash's thought
        # summaries (same planner/thinking config as the coordinator agents).
        planner=BuiltInPlanner(
            thinking_config=ThinkingConfig(include_thoughts=True),
        ),
    )


# --------------------------------------------------------------------------- #
# D5-4: decompose() — the structured plan agent (runs BEFORE parallel author)
# --------------------------------------------------------------------------- #


class DecomposeResult(BaseModel):
    """The validated output of :func:`decompose`.

    A plan = the list of INDEPENDENT one-file :class:`SliceSpec`s plus an
    overall PR ``pr_title`` and ``pr_body_intro`` (the intro prose the
    coordinator prepends to the merged PR body downstream). ``extra="forbid"``
    keeps the result model as strict as the rest of the coordinator's models.

    A result with ``len(slices) == 1`` is VALID and returned — the CALLER
    decides to route that single coupled/simple change to the legacy
    single-agent path. :func:`decompose` itself never collapses to a fallback.
    """

    model_config = ConfigDict(extra="forbid")

    slices: list[SliceSpec]
    pr_title: str = Field(min_length=1)
    pr_body_intro: str


def make_submit_plan(sink: dict) -> Callable[..., dict]:
    """Build the decompose agent's single hand-back tool.

    Mirrors :func:`make_submit_slice_file`: the returned closure RECORDS the
    model's raw plan into ``sink`` and returns an ack. The plan is passed as a
    single ``plan_json`` STRING param — a JSON object
    ``{"slices":[{"goal","target_path","doc_citations"?}],"pr_title",
    "pr_body_intro"}``. A flat string param is chosen deliberately: ADK builds
    the tool's function-declaration from the callable's type hints, and a
    primitive ``str`` maps cleanly to ``types.Type.STRING`` — unlike a
    deeply-nested ``list[dict]`` / ``list[SliceSpec]`` param, whose schema may
    not be expressible. The shape is proven to build offline in
    ``tests/unit/test_fanout_decompose.py`` (it wraps this callable in
    ``FunctionTool`` and asserts ``_get_declaration()`` succeeds).

    This tool deliberately does NOT validate or parse the plan — it only
    records the raw string. Parsing (``json.loads``) and validation
    (Pydantic + :func:`validate_slice_specs`) happen in :func:`decompose`, so
    there is a single place that decides the POLICY vs DECOMPOSE_NON_POLICY
    failure kind.
    """

    def submit_plan(plan_json: str) -> dict:
        """Submit your FULL decomposition plan, exactly once, as a JSON string.

        Call this once when you have decided the split. ``plan_json`` must be a
        JSON object of the form::

            {
              "slices": [
                {"goal": "...", "target_path": "iac/<file>.tf",
                 "doc_citations": ["...optional..."]}
              ],
              "pr_title": "...",
              "pr_body_intro": "..."
            }

        Each slice is ONE independent ``iac/`` file with NO cross-references to
        the others. Use 2+ slices ONLY for genuinely independent multi-file
        work; use EXACTLY ONE slice for a coupled/interdependent change or a
        simple single-file change. Never put two slices on the same
        ``target_path``. Returns an acknowledgement ``{"status": "recorded"}``.
        """
        sink["plan_json"] = plan_json
        return {"status": "recorded"}

    return submit_plan


# The decomposition system prompt. The model READs the live env / inventory /
# docs first to ground its split, then decides INDEPENDENT multi-slice vs a
# single coupled/simple change, then calls ``submit_plan`` exactly once. The
# "EXACTLY ONE slice for anything coupled or simple" rule is what lets the
# caller cleanly fall back to the legacy single-agent path on a 1-slice result.
_DECOMPOSE_INSTRUCTION = """\
You are the DriftScribe decomposition planner. Your ONE job is to turn the \
operator's infrastructure request into a plan of INDEPENDENT one-file \
authoring slices, then hand it back by calling `submit_plan` exactly once.

Process:
1. READ the current state first with your read tools (live env, project \
inventory, the ops contract, and developer docs) so your split is grounded in \
what already exists. You author NOTHING — you only plan.
2. Decide the shape of the work:
   - If it splits into MULTIPLE INDEPENDENT `iac/` files — files that have NO \
cross-references between them (no slice's file refers to a resource/local/ \
output another slice's file defines) — return 2 OR MORE slices, each with one \
`target_path` and a prose `goal` describing exactly what that one file should \
contain.
   - If the work is COUPLED or interdependent (the files would reference each \
other), OR it is a simple single-file change, return EXACTLY ONE slice. The \
caller routes a single-slice plan to the legacy single-agent path — so when \
in doubt, prefer ONE slice.
   - An ADOPTION request (bringing an existing live resource under IaC \
management, or importing) is NEVER decomposed: always return exactly ONE slice. \
The single-agent path holds the provision_propose_adoption tool, which renders \
the import HCL deterministically — slice sub-agents do not have that tool and \
must never author import blocks themselves.
3. Constraints on every slice (the downstream gate enforces these; violating \
them fails the whole fan-out):
   - Never put two slices on the SAME `target_path`.
   - Only ordinary `iac/*.tf` resource/data/locals files or `iac/*.md` docs. \
NEVER providers, modules, provisioners, backend, secrets, or any other \
foundation/provider/module/secret file.
4. Call `submit_plan` exactly ONCE with a JSON object: a `slices` list (each \
`{"goal", "target_path", "doc_citations"?}`), a `pr_title`, and a \
`pr_body_intro` (a short prose intro for the overall pull request). Do not \
call it more than once.
"""


async def decompose(
    prompt: str,
    *,
    read_tools: dict[str, Callable] | None = None,
    event_sink: Callable[[object], None] | None = None,
) -> DecomposeResult:
    """Run the ONE structured decomposition LLM call → a validated plan.

    Turns the operator's ``prompt`` into a :class:`DecomposeResult`: a list of
    INDEPENDENT one-file :class:`SliceSpec`s plus a PR title/intro. This is the
    stage that runs BEFORE the parallel authoring; the parallel author is a
    later slice.

    ``read_tools`` defaults to :func:`resolve_provision_read_tools` so the
    decomposer can READ the live env / inventory / docs to ground its split.
    Only the read-tool VALUES are handed to the agent (plus the ``submit_plan``
    hand-back tool) — never any mutation/PR tool.

    The agent runs in its OWN dedicated :class:`InMemorySessionService` session
    with a FRESH session id (never shared with any authoring session): this
    isolation is required so decompose chatter can't leak into later slice
    prompts. If ``event_sink`` is provided, each ADK event is forwarded to it
    (so a later orchestrator can buffer decompose events); otherwise events are
    simply consumed.

    Failure typing (load-bearing — the orchestrator branches on ``kind``):

    - The model never called ``submit_plan`` / the sink is empty / the recorded
      plan is malformed (not JSON, can't build the :class:`SliceSpec`s, missing
      ``pr_title``/``pr_body_intro``, a forbidden extra field, etc.) → a
      :class:`FanoutError` ``(502, kind=DECOMPOSE_NON_POLICY)``. The orchestrator
      FAILS OPEN to the single-agent path on this kind.
    - If :func:`validate_slice_specs` rejects the slice set (foundation path /
      duplicate path / non-iac / too many) it raises a POLICY-kind
      :class:`FanoutError`; that propagates UNCHANGED (kind stays POLICY → the
      orchestrator FAILS CLOSED).

    A 1-slice result is VALID and returned — the CALLER decides to fall back,
    not :func:`decompose`.

    The agent-building ADK imports are lazy (mirroring the rest of this module)
    so the pure core stays import-light for callers that only use
    :class:`SliceSpec` / :func:`validate_slice_specs`. ``Runner`` is the one
    exception — it is a module-level import on purpose (the mock seam; see the
    import comment).
    """
    from google.adk import Agent
    from google.adk.planners.built_in_planner import BuiltInPlanner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types
    from google.genai.types import ThinkingConfig

    if read_tools is None:
        read_tools = resolve_provision_read_tools()

    sink: dict = {}
    submit_plan = make_submit_plan(sink)
    agent = Agent(
        name="driftscribe_decompose",
        model="gemini-2.5-flash",
        instruction=_DECOMPOSE_INSTRUCTION,
        tools=[*read_tools.values(), submit_plan],
        # Mirror build_chat_agent: surface Gemini 2.5 Flash's thought summaries.
        planner=BuiltInPlanner(
            thinking_config=ThinkingConfig(include_thoughts=True),
        ),
    )

    # Own dedicated session — a FRESH id, never shared with any authoring
    # session, so decompose chatter can't leak into later slice prompts.
    session_service = InMemorySessionService()
    sid = str(uuid.uuid4())
    await session_service.create_session(
        app_name="driftscribe",
        user_id="driftscribe-runtime",
        session_id=sid,
    )
    runner = Runner(
        agent=agent,
        app_name="driftscribe",
        session_service=session_service,
    )
    msg = types.Content(role="user", parts=[types.Part(text=prompt)])

    async for event in runner.run_async(
        user_id="driftscribe-runtime",
        session_id=sid,
        new_message=msg,
    ):
        if event_sink is not None:
            event_sink(event)

    return _parse_plan_sink(sink)


def _parse_plan_sink(sink: dict) -> DecomposeResult:
    """Parse + validate the recorded plan into a :class:`DecomposeResult`.

    A missing/empty sink or any malformed plan is a NON-POLICY failure (raised
    as ``FanoutError(502, kind=DECOMPOSE_NON_POLICY)``); a slice-set policy
    rejection from :func:`validate_slice_specs` (kind POLICY) is allowed to
    propagate UNCHANGED. Split out from :func:`decompose` so the parse/validate
    path is testable without an agent run.
    """
    raw = sink.get("plan_json")
    if not raw:
        raise FanoutError(
            502,
            "decompose produced no plan (submit_plan was never called)",
            kind=FanoutFailureKind.DECOMPOSE_NON_POLICY,
        )
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError) as e:
        raise FanoutError(
            502,
            f"decompose plan was not valid JSON: {e}",
            kind=FanoutFailureKind.DECOMPOSE_NON_POLICY,
        ) from e
    if not isinstance(payload, dict):
        raise FanoutError(
            502,
            "decompose plan must be a JSON object",
            kind=FanoutFailureKind.DECOMPOSE_NON_POLICY,
        )

    raw_slices = payload.get("slices")
    if not isinstance(raw_slices, list):
        raise FanoutError(
            502,
            "decompose plan is missing a 'slices' list",
            kind=FanoutFailureKind.DECOMPOSE_NON_POLICY,
        )
    try:
        slices = [SliceSpec(**s) for s in raw_slices]
        title = payload["pr_title"]
        intro = payload["pr_body_intro"]
        result = DecomposeResult(
            slices=slices, pr_title=title, pr_body_intro=intro
        )
    except (ValidationError, KeyError, TypeError, ValueError) as e:
        # A malformed/missing field (bad slice, missing pr_title, forbidden
        # extra, etc.) is a NON-POLICY failure — the orchestrator fails OPEN.
        raise FanoutError(
            502,
            f"decompose plan was malformed: {e}",
            kind=FanoutFailureKind.DECOMPOSE_NON_POLICY,
        ) from e

    # Slice-set POLICY gate — a rejection here (kind=POLICY) propagates
    # UNCHANGED so the orchestrator fails CLOSED.
    validate_slice_specs(result.slices)
    return result


# --------------------------------------------------------------------------- #
# D5-5: author_slices_parallel() — parallel authoring + fail-closed barrier
# --------------------------------------------------------------------------- #
#
# The N slice-author sub-agents run IN PARALLEL via ADK ``ParallelAgent``
# (asyncio.TaskGroup under the hood). Then a DETERMINISTIC, FAIL-CLOSED barrier
# collects each slice's file-write from its own sink and merges them. The next
# slice (the orchestrator) makes the single editor call from this output.
#
# The fan-out's "name" is fixed below so the per-sub-agent isolation branch ADK
# stamps on each event (``<parallel.name>.<sub_agent.name>``, see
# ``parallel_agent._create_branch_ctx_for_sub_agent``) is predictable — the
# event-tagging loop maps a branch's sub-agent suffix back to the slice whose
# agent ``name`` it ends with.
_FANOUT_PARALLEL_NAME = "driftscribe_fanout"


class AuthorResult(BaseModel):
    """The validated output of :func:`author_slices_parallel`.

    ``files`` is the per-slice ``{"path", "content"}`` writes IN SLICE ORDER
    (the same order the orchestrator hands to the editor downstream).
    ``citations`` maps each slice's ``target_path`` to the citation list its
    sub-agent recorded (``[]`` if none). ``extra="forbid"`` keeps the result
    model as strict as the rest of the coordinator's models.
    """

    model_config = ConfigDict(extra="forbid")

    files: list[dict]
    citations: dict[str, list[str]]


def _describe_authoring_cause(exc: BaseException) -> str:
    """Render the most informative human cause of a parallel-author failure.

    ADK's :class:`ParallelAgent` runs each sub-agent inside an
    ``asyncio.TaskGroup``; when a sub-agent raises, the TaskGroup re-raises a
    wrapping :class:`ExceptionGroup` whose ``str()`` is the opaque
    ``"unhandled errors in a TaskGroup (N sub-exception(s))"`` — useless in a
    fail-closed reply (this was the cosmetic residual: the AUTHORING reply
    surfaced the wrapper, not the real error). Recurse through the group(s) and
    join the distinct leaf causes (order-preserving de-dup) so the surfaced
    detail names the actual error. A plain (non-group) exception renders as its
    ``str()``, falling back to the class name when the message is empty.
    """
    if isinstance(exc, BaseExceptionGroup):
        seen: dict[str, None] = {}
        for sub in exc.exceptions:
            seen.setdefault(_describe_authoring_cause(sub), None)
        return "; ".join(seen) or type(exc).__name__
    text = str(exc).strip()
    return text or type(exc).__name__


async def author_slices_parallel(
    specs: list[SliceSpec],
    *,
    read_tools: dict[str, Callable] | None = None,
    event_sink: Callable[[dict], None] | None = None,
) -> AuthorResult:
    """Run N slice-author sub-agents IN PARALLEL, then merge fail-closed.

    Each spec gets its OWN fresh per-slice ``sink`` and a constrained
    slice-author agent (:func:`build_slice_author_agent`, which has NO PR/apply/
    mutation tool). The N agents are wrapped in a single ADK
    :class:`~google.adk.agents.ParallelAgent` and run concurrently on a
    dedicated authoring session (fresh uuid, mirroring :func:`decompose` /
    :func:`agent.adk_agent.run_chat_stream`).

    ``read_tools`` defaults to :func:`resolve_provision_read_tools` so each
    sub-agent can READ the live env / inventory / docs to ground its file. Only
    read-tool VALUES are handed to each agent (plus its pinned
    ``submit_slice_file`` hand-back tool) — never any mutation/PR tool.

    Event handling (per slice): for each ADK event we emit the SAME
    ``llm_thought`` / ``tool_call`` / ``tool_result`` / ``llm_usage`` payloads
    the chat path emits (via :func:`agent.adk_agent._emit_event_logs` /
    :func:`agent.adk_agent._emit_llm_usage`), TAG each payload with the event's
    ``branch`` plus the matched slice's ``slice_id`` / ``target_path``, and
    forward the tagged payload to ``event_sink`` (if given). We DELIBERATELY do
    NOT collect any ``is_final_response()`` reply: each of the N sub-agents
    would otherwise emit its own natural-language final and corrupt the single
    coordinator reply timeline. Per-slice finals are suppressed; the file each
    slice produced comes from its tool sink, not its final text.

    Fail-closed contract (load-bearing):

    - If the run raises (a sub-agent errored → the ``ParallelAgent``'s
      TaskGroup cancels its siblings and raises through), ALL sink writes are
      DISCARDED and a :class:`FanoutError` ``(502, kind=AUTHORING)`` is raised
      — never a partial :class:`AuthorResult`.
    - :class:`asyncio.CancelledError` (outer request cancellation) is re-raised
      UNCHANGED — it is NOT converted into a :class:`FanoutError`. The narrow
      ``except CancelledError: raise`` sits BEFORE the broad ``except`` so an
      outer cancel can never be swallowed into an authoring failure.

    Barrier (only on a clean run), deterministic, in slice order:

    - Each slice must have produced a non-empty file (``sink["file"]`` present
      with content that is not empty/whitespace) — else
      :class:`FanoutError` ``(502, kind=AUTHORING)`` naming the offending path.
    - The merged writes (in slice order) are re-validated by
      :func:`driftscribe_lib.iac_editor_policy.validate_file_writes` (disjoint
      paths + per-file/total byte bounds). A rejection is translated to a
      :class:`FanoutError` carrying the library's status/reason
      (``kind=AUTHORING``) — the library error is NEVER allowed to leak.

    Returns an :class:`AuthorResult` with the files in slice order and a
    ``target_path → citations`` map.
    """
    import asyncio

    from google.adk.agents import ParallelAgent
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    from agent.adk_agent import _emit_event_logs, _emit_llm_usage

    if read_tools is None:
        read_tools = resolve_provision_read_tools()

    # Build one (spec, sink, agent) per slice, IN SLICE ORDER. Each sink is a
    # fresh dict captured by that slice's pinned submit_slice_file closure.
    pairs: list[tuple[SliceSpec, dict]] = []
    sub_agents = []
    # name -> (slice_id, target_path); used to map an event branch suffix back
    # to its slice. The branch is "<parallel.name>.<sub_agent.name>", so we
    # match the slice whose agent name the branch ENDS with.
    name_to_slice: dict[str, tuple[int, str]] = {}
    for idx, spec in enumerate(specs):
        sink: dict = {}
        # Pass idx so the agent name is unique-by-construction (idx prefix) —
        # two disjoint paths can slug identically, but their indices differ, so
        # no duplicate ADK name and no name_to_slice overwrite below.
        agent = build_slice_author_agent(spec, read_tools, sink, idx)
        pairs.append((spec, sink))
        sub_agents.append(agent)
        name_to_slice[agent.name] = (idx, spec.target_path)

    root = ParallelAgent(name=_FANOUT_PARALLEL_NAME, sub_agents=sub_agents)

    # Own dedicated authoring session — a FRESH id, never shared with the
    # decompose session, so authoring chatter stays isolated.
    session_service = InMemorySessionService()
    sid = str(uuid.uuid4())
    await session_service.create_session(
        app_name="driftscribe",
        user_id="driftscribe-runtime",
        session_id=sid,
    )
    runner = Runner(
        agent=root,
        app_name="driftscribe",
        session_service=session_service,
    )
    # The ParallelAgent fans the SAME user message to every sub-agent; each
    # sub-agent's own instruction (goal + pinned target_path) is what scopes it.
    msg = types.Content(
        role="user",
        parts=[types.Part(text="Author your assigned iac/ file slice now.")],
    )

    def _slice_tag_for_branch(branch: str | None) -> tuple[int, str] | None:
        """Map an event ``branch`` to its slice ``(slice_id, target_path)``.

        The branch is ``<parallel.name>.<sub_agent.name>``; the sub-agent name
        is the trailing dotted segment. Match the slice whose agent name equals
        that suffix (``endswith`` on a ``.<name>`` boundary, falling back to an
        exact tail-segment match) so a future nested branch prefix can't break
        the mapping."""
        if not branch:
            return None
        tail = branch.rsplit(".", 1)[-1]
        return name_to_slice.get(tail)

    try:
        async for event in runner.run_async(
            user_id="driftscribe-runtime",
            session_id=sid,
            new_message=msg,
        ):
            # Same partial-event dedup gate as the chat path: only merged
            # non-partial events are eligible to emit. NB: we deliberately run
            # NO is_final_response() reply-collection — per-slice finals are
            # suppressed (see docstring).
            if (
                event.content
                and event.content.parts
                and getattr(event, "partial", None) is not True
            ):
                branch = getattr(event, "branch", None)
                tag = _slice_tag_for_branch(branch)
                for payload in _emit_event_logs(event):
                    _tag_and_forward(payload, branch, tag, event_sink)
            usage_payload = _emit_llm_usage(event)
            if usage_payload is not None:
                branch = getattr(event, "branch", None)
                tag = _slice_tag_for_branch(branch)
                _tag_and_forward(usage_payload, branch, tag, event_sink)
    except asyncio.CancelledError:
        # Outer request cancellation — propagate UNCHANGED. This narrow re-raise
        # MUST stay before the broad except so a cancel is never swallowed into
        # an AUTHORING failure.
        raise
    except FanoutError:
        # Defensive: a FanoutError surfaced mid-run (e.g. from a future tool)
        # is already typed — let it propagate unchanged rather than re-wrap.
        raise
    except Exception as e:
        # A sub-agent errored → the ParallelAgent's TaskGroup cancelled its
        # siblings and raised through (as an ExceptionGroup). DISCARD every sink
        # write (we simply never read them) and surface a typed AUTHORING
        # failure naming the INNER cause — never the opaque TaskGroup wrapper
        # (see _describe_authoring_cause). The original exception is chained
        # via `from e` so the full group is still in the server-side traceback.
        raise FanoutError(
            502,
            f"slice authoring failed: {_describe_authoring_cause(e)}",
            kind=FanoutFailureKind.AUTHORING,
        ) from e

    return _merge_slice_sinks(pairs)


def _tag_and_forward(
    payload: dict,
    branch: str | None,
    tag: tuple[int, str] | None,
    event_sink: Callable[[dict], None] | None,
) -> None:
    """Tag a per-event payload with its slice provenance and forward it.

    Adds ``branch`` (the raw ADK isolation branch, or ``None``) and — when the
    branch mapped to a slice — ``slice_id`` / ``target_path``. The payload is a
    plain dict already redacted by the emit helper; we mutate it in place (it is
    not the durable log copy — the emit helper already logged its own) and hand
    it to ``event_sink`` if one was provided.
    """
    payload["branch"] = branch
    if tag is not None:
        payload["slice_id"] = tag[0]
        payload["target_path"] = tag[1]
    if event_sink is not None:
        event_sink(payload)


def _merge_slice_sinks(pairs: list[tuple[SliceSpec, dict]]) -> AuthorResult:
    """Deterministic, fail-closed barrier over the per-slice sinks (in order).

    Only called on a CLEAN run. Split out from :func:`author_slices_parallel`
    so the barrier is testable without an agent run. Enforces, in slice order:

    1. Every slice produced a non-empty file (``sink["file"]`` present, content
       not empty/whitespace) — else :class:`FanoutError` ``(502, AUTHORING)``
       naming the offending ``target_path``.
    2. The merged writes pass
       :func:`driftscribe_lib.iac_editor_policy.validate_file_writes` (disjoint
       paths + per-file/total byte bounds); an :class:`EditorPolicyError` is
       translated to a :class:`FanoutError` carrying the lib status/reason
       (``kind=AUTHORING``) — the lib error never leaks.

    Returns the :class:`AuthorResult` (files in slice order + path→citations).
    """
    from driftscribe_lib.iac_editor_policy import validate_file_writes

    from driftscribe_lib.adopt_recipe import find_import_violations

    writes: list[dict] = []
    citations: dict[str, list[str]] = {}
    for spec, sink in pairs:
        file_write = sink.get("file")
        content = (file_write or {}).get("content")
        if not file_write or not content or not content.strip():
            raise FanoutError(
                502,
                f"slice {spec.target_path} produced no file",
                kind=FanoutFailureKind.AUTHORING,
            )
        writes.append(file_write)
        citations[spec.target_path] = list(sink.get("citations", []))

    # Freehand-import guard (Phase 3 §1.10): reject any merged file set that
    # contains an import block. Slice sub-agents author HCL text only and must
    # never produce an import block — adoptions go through propose_adoption_tool
    # in the single-agent path (a 1-slice result delegates there). Fail-closed:
    # an unparseable .tf file also triggers a POLICY rejection so a broken HCL
    # can never silently sneak an import through.
    import_violations = find_import_violations(writes)
    if import_violations:
        raise FanoutError(
            422,
            (
                "Slice-authored files must not contain import blocks — use "
                "provision_propose_adoption for resource adoptions (import blocks "
                "are coordinator-side only; the fan-out editor path does not accept "
                f"them). Violation(s): {'; '.join(import_violations)}"
            ),
            kind=FanoutFailureKind.POLICY,
        )

    try:
        validate_file_writes(writes)
    except EditorPolicyError as e:
        # Translate the library error — never let EditorPolicyError leak.
        raise FanoutError(
            e.status_code, e.reason, kind=FanoutFailureKind.AUTHORING
        ) from e

    return AuthorResult(files=writes, citations=citations)


# --------------------------------------------------------------------------- #
# D5-6: run_provision_fanout_stream() — the streaming orchestrator
# --------------------------------------------------------------------------- #
#
# The orchestrator ties the engine (decompose → parallel author → barrier)
# into ONE streaming entrypoint and makes the SINGLE convergent editor call.
# It yields the SAME item shapes as agent.adk_agent.run_chat_stream so /chat
# (and the SSE timeline) treats a fan-out run exactly like a chat run.
#
# The agent/worker imports below are FUNCTION-SCOPED (lazy) — mirroring how
# author_slices_parallel imports `_emit_event_logs`/`_emit_llm_usage` — so this
# module keeps its pure, import-light core (callers that only need SliceSpec /
# validate_slice_specs never pull ADK/agent in, and there is no import cycle
# with agent.adk_agent, which in turn imports agent.adk_tools).


# The operator next-steps reminder after a fan-out PR opens is built by the
# SHARED helper agent.adk_tools.iac_pr_next_steps (lazy-imported in
# _compose_fanout_success_reply), so the single-agent and fan-out paths give the
# operator IDENTICAL instructions — now with the real pr_number substituted into
# the /iac-approvals/<N> approval link instead of a literal placeholder.


def _compose_fanout_pr_body(plan: DecomposeResult, author_result: AuthorResult) -> str:
    """Compose the merged PR body from the plan intro + a per-slice manifest.

    Pure + deterministic (split out so it is unit-testable without an agent
    run, mirroring :func:`_parse_plan_sink` / :func:`_merge_slice_sinks`). The
    body is the decomposer's ``pr_body_intro`` followed by one bullet per slice
    (in slice order): a backticked ``target_path`` — ``goal``, plus a citations
    sub-line whenever the slice's authoring sub-agent recorded any. The
    per-slice manifest makes the convergent PR self-describing (which file came
    from which slice goal, and what each file was grounded in).
    """
    lines = [plan.pr_body_intro, "", "## Authored files"]
    for spec in plan.slices:
        lines.append(f"- `{spec.target_path}` — {spec.goal}")
        cites = author_result.citations.get(spec.target_path) or []
        if cites:
            lines.append(f"  - citations: {', '.join(cites)}")
    return "\n".join(lines)


def _compose_success_reply(
    worker_result: dict, plan: DecomposeResult, author_result: AuthorResult,
    *, plan_builder_dispatched: bool = False
) -> str:
    """Compose the operator-facing success reply for a fan-out PR.

    Pure + deterministic (testable without an agent run). Summarizes the opened
    PR (number + url), lists the N authored paths (in slice order), and ends
    with the EXACT wording from the shared
    :func:`agent.adk_tools.iac_pr_next_steps` helper so the two authoring paths
    give identical next steps (the real pr_number is substituted into the
    /iac-approvals/<N> approval link).

    When ``plan_builder_dispatched=True``, the next-steps copy tells the operator
    the plan-builder has already been started for this PR.
    """
    # Lazy import keeps this module's pure SliceSpec core import-light (adk_tools
    # pulls worker_client/config); only the reply composer needs the helper.
    from agent.adk_tools import iac_pr_next_steps

    pr_number = worker_result.get("pr_number")
    pr_url = worker_result.get("pr_url")
    paths = ", ".join(f["path"] for f in author_result.files)
    return (
        f"Opened infrastructure PR #{pr_number} ({pr_url}) with {len(author_result.files)} "
        f"authored file(s): {paths}.\n\n{iac_pr_next_steps(pr_number, plan_builder_dispatched=plan_builder_dispatched)}"
    )


async def run_provision_fanout_stream(
    prompt: str,
    session_id: str | None = None,
    *,
    autonomy_mode: str,
    prior_turns: list[dict] | None = None,
) -> AsyncIterator[dict]:
    """Stream a parallel fan-out ``provision`` (IaC-authoring) run end to end.

    This is the D5-6 orchestrator: it runs :func:`decompose`, decides
    single-slice vs committed fan-out, runs :func:`author_slices_parallel`
    LIVE-streaming the N parallel authors, makes the ONE convergent editor call
    via the shared authority helper, and surfaces the operator's final.

    Yields the SAME item shapes as :func:`agent.adk_agent.run_chat_stream`::

        {"type": "event",  "event": <payload + seq/insert_id/timestamp>}
        ...and finally exactly one...
        {"type": "result", "reply": str, "tool_calls": list, "session_id": str,
         "iac_pr"?: {"pr_number": int, "pr_url": str}}

    ``iac_pr`` is present ONLY on a confirmed first-authoring infra-PR success
    (the SPA reads it for the clickable approval CTA); omitted otherwise.

    Every yielded event flows through ONE monotonic ``seq`` counter for the
    whole committed run (so the SSE timeline keeps stable, contiguous ordering
    keys across the buffered decompose events, the live parallel-author events,
    and the trailing ``final_response``).

    Branching contract (the fail-open vs fail-closed decision keys on
    :class:`FanoutFailureKind`, never on a status):

    - **POLICY** decompose failure → FAIL CLOSED: surface the violation as the
      operator's final, NO editor call, ``tool_calls == []``.
    - **DECOMPOSE_NON_POLICY** (or any non-policy) decompose failure → FAIL
      OPEN: delegate the whole stream to the legacy single-agent
      :func:`run_chat_stream` (``workload="provision"``); its own ``seq``
      restarts at 1, which is correct for a fresh delegated run.
    - **single-slice** plan (``len == 1``) → delegate to the legacy
      single-agent path (a coupled/simple change the fan-out wasn't built for).
    - **committed** (``N >= 2``) → fan out: flush buffered decompose events,
      author in parallel (live), then make the ONE editor call.
    - **AUTHORING** failure → FAIL CLOSED: surface, NO editor call, ``[]``.
    - editor :class:`WorkerClientError` → surface the worker error, NO
      fabricated PR; ``tool_calls == ["open_infra_pr"]`` (the call WAS attempted).

    ``tool_calls`` semantics: it reflects operator-facing MUTATION INVOCATION
    only — exactly one synthetic ``"open_infra_pr"`` entry whenever the editor
    call was ATTEMPTED (success OR a WorkerClientError), and ``[]`` when no
    editor call was attempted (any fail-closed branch). It deliberately does
    NOT include the internal ``submit_slice_file`` / ``submit_plan`` tool calls
    the sub-agents make — those are engine internals, not operator mutations.

    The ADK/agent/worker imports are FUNCTION-SCOPED (lazy) to keep this
    module's pure import-light core (and to avoid an import cycle with
    ``agent.adk_agent``); ``decompose`` / ``author_slices_parallel`` are
    module globals (patchable at the seam by tests).
    """
    import asyncio
    import contextlib

    from agent import worker_client
    from agent.adk_agent import (
        _emit_event_logs,
        _emit_final_response,
        _emit_llm_usage,
        run_chat_stream,
    )
    from agent.adk_tools import derive_iac_pr_authority

    sid = session_id or str(uuid.uuid4())
    read_tools = resolve_provision_read_tools()
    seq = 0

    def _stream(payload: dict) -> dict:
        # ONE monotonic seq for the whole committed run — augment the
        # already-redacted, already-emitted payload with the SSE-only ordering
        # metadata (mirrors run_chat_stream's _stream exactly).
        nonlocal seq
        seq += 1
        return {
            **payload,
            "seq": seq,
            "insert_id": f"stream-{seq}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    if autonomy_mode == "observe":
        # Autonomy dial (ClickOps item 11, Codex must-fix 1): authoring an
        # infra PR is propose-class work and the whole fan-out exists to make
        # that ONE coordinator-direct editor call — which never passes through
        # Layer-0 tool filtering. In Observe, delegate the ENTIRE stream to the
        # single-agent path at entry (same delegation machinery as the
        # single-slice and non-policy branches below). The single agent is
        # Layer-0-filtered, so it still answers provision questions but has no
        # authoring tool; its instruction note points at the dial. The
        # delegated run's seq restarts at 1 — documented contract.
        async for item in run_chat_stream(
            prompt, sid, workload="provision", autonomy_mode=autonomy_mode,
            prior_turns=prior_turns,
        ):
            yield item
        return

    # 1+2. Decompose into a BUFFER (events are not yielded yet — we don't know
    # whether this run commits, single-slice-delegates, or fails).
    decompose_buffer: list = []
    try:
        plan = await decompose(
            prompt, read_tools=read_tools, event_sink=decompose_buffer.append
        )
    except FanoutError as e:
        if e.kind is FanoutFailureKind.POLICY:
            # FAIL CLOSED: a policy rejection (foundation/duplicate/non-iac
            # path, count bound). Editor NOT called; decompose_buffer discarded.
            reply = f"Could not author the infrastructure change: {e.detail}"
            yield {"type": "event", "event": _stream(_emit_final_response(reply))}
            yield {
                "type": "result",
                "reply": reply,
                "tool_calls": [],
                "session_id": sid,
            }
            return
        # FAIL OPEN (DECOMPOSE_NON_POLICY etc.): discard the buffer, delegate to
        # the legacy single-agent path. Its own seq restarts at 1 — correct for
        # a fresh delegated run.
        async for item in run_chat_stream(
            prompt, sid, workload="provision", autonomy_mode=autonomy_mode,
            prior_turns=prior_turns,
        ):
            yield item
        return

    # 3. Single-slice plan → a coupled/simple change. Discard the buffer and
    # delegate to the legacy single-agent path (same as the non-policy branch).
    if len(plan.slices) == 1:
        async for item in run_chat_stream(
            prompt, sid, workload="provision", autonomy_mode=autonomy_mode,
            prior_turns=prior_turns,
        ):
            yield item
        return

    # 4. Committed (N >= 2): flush the buffered decompose events through _stream,
    # tagged phase="decompose". Mirror run_chat_stream's emit gate EXACTLY.
    # (_emit_event_logs never emits final_response, so decompose's own
    # natural-language final is naturally suppressed — correct.)
    for ev in decompose_buffer:
        if ev.content and ev.content.parts and getattr(ev, "partial", None) is not True:
            for payload in _emit_event_logs(ev):
                payload["phase"] = "decompose"
                yield {"type": "event", "event": _stream(payload)}
        usage = _emit_llm_usage(ev)
        if usage is not None:
            usage["phase"] = "decompose"
            yield {"type": "event", "event": _stream(usage)}

    # 5. Author in parallel, LIVE-streaming via a queue bridge so the N parallel
    # authors appear incrementally in the SSE timeline under the SAME seq.
    # The author sink (event_sink) is a synchronous Callable[[dict],None] called
    # from the author coroutine; put_nowait bridges it onto an unbounded queue
    # this generator drains. The author payloads are already tagged
    # (branch/slice_id/target_path) by author_slices_parallel's _tag_and_forward.
    queue: asyncio.Queue = asyncio.Queue()
    _SENTINEL = object()

    async def _run_author() -> AuthorResult:
        try:
            return await author_slices_parallel(
                plan.slices, read_tools=read_tools, event_sink=queue.put_nowait
            )
        finally:
            # Always unblock the drain loop, even on failure/cancel.
            queue.put_nowait(_SENTINEL)

    author_task = asyncio.create_task(_run_author())
    try:
        while True:
            item = await queue.get()
            if item is _SENTINEL:
                break
            yield {"type": "event", "event": _stream(item)}
        author_result = await author_task  # re-raises FanoutError / CancelledError
    except FanoutError as e:
        # Authoring failed CLOSED → surface, NO PR. (CancelledError is NOT
        # caught here — it propagates to the caller unchanged.)
        reply = f"Could not author the infrastructure change: {e.detail}"
        yield {"type": "event", "event": _stream(_emit_final_response(reply))}
        yield {
            "type": "result",
            "reply": reply,
            "tool_calls": [],
            "session_id": sid,
        }
        return
    finally:
        # If we leave this block via an exception or an early generator close
        # (the SSE consumer stops draining mid-author), don't orphan the author
        # task: cancel it AND reap it. Awaiting here is safe — a `finally` in an
        # async generator may await as long as it does not `yield`. Reaping
        # retrieves any pending task exception so it can't surface later as an
        # unretrieved-task warning; CancelledError (and any already-surfaced
        # FanoutError/authoring error we deliberately don't re-handle here) is
        # suppressed since the outcome was already decided above.
        if not author_task.done():
            author_task.cancel()
        with contextlib.suppress(BaseException):
            await author_task

    # 6. Derive authority via the SHARED helper (no independent derivation here
    # — same helper open_infra_pr_tool uses).
    authority = derive_iac_pr_authority(plan.pr_title)

    # 7. Compose the body + validate title/body BEFORE the call (fail closed on
    # an overflow without attempting the editor call).
    body = _compose_fanout_pr_body(plan, author_result)
    try:
        validate_title_body(plan.pr_title, body)
    except EditorPolicyError as e:
        reply = f"Could not author the infrastructure change: {e.reason}"
        yield {"type": "event", "event": _stream(_emit_final_response(reply))}
        yield {
            "type": "result",
            "reply": reply,
            "tool_calls": [],
            "session_id": sid,
        }
        return

    # Pre-editor-call guard (defense in depth, ClickOps item 11). Unreachable
    # when the entry delegation works (Observe never gets here), but pinned so
    # a future branch reorder cannot reopen the bypass Codex flagged. The
    # editor call is propose-class; refuse it whenever the dial does not allow
    # proposals.
    if not mode_allows(autonomy_mode, "propose"):
        reply = (
            "The infrastructure PR was not opened — the autonomy dial does "
            "not allow proposals in this mode. Raise the dial in the "
            "operator UI and ask again."
        )
        yield {"type": "event", "event": _stream(_emit_final_response(reply))}
        yield {
            "type": "result",
            "reply": reply,
            "tool_calls": [],
            "session_id": sid,
        }
        return

    # 8. The SINGLE convergent editor call — off the event loop (synchronous
    # httpx), positional args ONLY (call_open_infra_pr pins base="main"
    # internally; there is NO base parameter, passing one would crash).
    try:
        result = await asyncio.to_thread(
            worker_client.call_open_infra_pr,
            authority.target_repo,
            authority.branch,
            plan.pr_title,
            body,
            author_result.files,
            dispatch_plan_builder=(autonomy_mode == "propose_apply"),
        )
    except worker_client.WorkerClientError as e:
        # The call WAS attempted → tool_calls records the synthetic open_infra_pr.
        # Surface the worker error; do NOT fabricate a PR number/url.
        reply = (
            f"The infrastructure PR could not be opened (editor worker error "
            f"{e.status_code}): {e.body or e}"
        )
        yield {"type": "event", "event": _stream(_emit_final_response(reply))}
        yield {
            "type": "result",
            "reply": reply,
            "tool_calls": ["open_infra_pr"],
            "session_id": sid,
        }
        return

    # 9. Success — but ONLY if the worker returned a CONFIRMED PR: a positive
    # non-bool pr_number AND a non-empty pr_url. The gate is the shared
    # ``iac_pr_pointer`` helper (the same one that builds the SPA approval CTA),
    # so a malformed 200 — no pr_number, ``pr_number=True``, an empty pr_url, etc.
    # — fails closed instead of surfacing a fabricated "PR #None"/"#True"/"(...)".
    # The call WAS made → tool_calls records open_infra_pr; no PR is fabricated.
    from agent.adk_tools import iac_pr_pointer, notify_iac_pr_pending

    iac_pr = iac_pr_pointer(result)
    if iac_pr is None:
        reply = (
            "The infrastructure PR could not be confirmed: the editor worker "
            f"returned no usable PR number/url (response status {result.get('status')!r})."
        )
        yield {"type": "event", "event": _stream(_emit_final_response(reply))}
        yield {
            "type": "result",
            "reply": reply,
            "tool_calls": ["open_infra_pr"],
            "session_id": sid,
        }
        return

    # Best-effort notification — runs off the event loop (notify_iac_pr_pending
    # calls the synchronous httpx-based worker_client.call; use to_thread to
    # match the surrounding async/sync boundary pattern). The helper never raises.
    # Bounded by worker_client's _HTTPX_TIMEOUT (30 s); acceptable for an
    # advisory side-channel.
    await asyncio.to_thread(
        notify_iac_pr_pending,
        iac_pr["pr_number"],
        iac_pr["pr_url"],
        plan.pr_title,
        plan_builder_dispatched=result.get("plan_builder_dispatched", False),
    )

    # Surface the opened PR + the exact operator next steps. The terminal item
    # carries the structured ``iac_pr`` pointer so the SPA can render a clickable
    # first-authoring "Review & approve" CTA (the reply text alone is not a link).
    reply = _compose_success_reply(result, plan, author_result, plan_builder_dispatched=result.get("plan_builder_dispatched", False))
    yield {"type": "event", "event": _stream(_emit_final_response(reply))}
    yield {
        "type": "result",
        "reply": reply,
        "tool_calls": ["open_infra_pr"],
        "session_id": sid,
        "iac_pr": iac_pr,
    }
