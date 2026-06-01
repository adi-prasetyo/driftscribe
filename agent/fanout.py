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
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from driftscribe_lib.iac_editor_policy import EditorPolicyError, validate_iac_path

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
