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
from collections.abc import Callable
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from driftscribe_lib.iac_editor_policy import EditorPolicyError, validate_iac_path

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
    spec: SliceSpec, read_tools, sink: dict
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
    ``driftscribe_slice_<slugged-target-path>`` — identifier-safe (the slug
    replaces every non-``[A-Za-z0-9_]`` char with ``_``), so two slices for
    two different paths get two different, valid ADK names. Model/planner
    mirror :func:`agent.adk_agent.build_chat_agent` exactly (``gemini-2.5-
    flash`` + ``BuiltInPlanner(ThinkingConfig(include_thoughts=True))``).

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
        name=f"driftscribe_slice_{_slug_target_path(spec.target_path)}",
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
