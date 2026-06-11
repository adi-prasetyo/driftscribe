"""Operator autonomy dial — global Observe / Propose / Propose+Apply (ClickOps item 11).

Fail-closed contract: read_autonomy_state NEVER raises. Any storage error or
malformed document returns mode="observe"/read_error=True — the MOST
restrictive mode — so mutation surfaces degrade to report-only while the
dial is unreadable. An ABSENT document means the dial was never touched:
the default is "propose_apply", the system's behavior before the dial
existed (roadmap item 11: "default = current behavior").

The two defaults point in different directions on purpose:
- absent doc  → propose_apply (operator never chose; keep current behavior)
- broken read → observe       (we cannot KNOW what the operator chose; the
  only honest stance is the most restrictive mode)

Mirrors agent/pause.py: no FastAPI dependency, unit-testable with a plain
InMemoryStateStore or a throw-on-get stub. The pause flag outranks the dial
everywhere — pause is the full stop; the dial chooses what runs when not
paused.
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

log = logging.getLogger("driftscribe.agent.autonomy")

# Ordered least → most permissive. Index = permissiveness rank.
AUTONOMY_MODES: tuple[str, ...] = ("observe", "propose", "propose_apply")

DEFAULT_MODE = "propose_apply"

# Tool tiers. "report" tools are available in every mode — observation and
# reporting are the dial's floor, not a privilege. "propose" tools may open
# PRs / issues / approval requests. "apply" tools change live state or merge
# to a branch that deploys. The per-tool assignment (TOOL_TIERS) lives in
# agent/workloads/registry.py next to TOOL_REGISTRY — the authority layer —
# and is drift-pinned to it.
TIER_NAMES: tuple[str, ...] = ("report", "propose", "apply")

_TIER_MIN_MODE: Mapping[str, str] = MappingProxyType({
    "report": "observe",
    "propose": "propose",
    "apply": "propose_apply",
})

MODE_LABELS: Mapping[str, str] = MappingProxyType({
    "observe": "Observe",
    "propose": "Propose",
    "propose_apply": "Propose + Apply",
})

# Returned as the ``reason`` field when the Firestore read itself fails —
# same surfacing convention as agent.pause.FAIL_CLOSED_REASON.
FAIL_CLOSED_REASON = "autonomy state could not be read — failing closed to Observe"


@dataclass(frozen=True)
class AutonomyState:
    """Immutable snapshot of the autonomy dial at a point in time.

    ``read_error=True`` means the storage read failed or the document was
    malformed; ``mode`` is then always ``"observe"`` (fail-closed).
    """

    mode: str
    reason: str | None = None
    actor: str | None = None
    updated_at: Any = None  # datetime or DatetimeWithNanoseconds from Firestore
    read_error: bool = False


def read_autonomy_state(state: Any) -> AutonomyState:
    """Read the autonomy dial from the given StateStore; NEVER raises.

    Calling convention matches agent.pause.read_pause_state: pass
    ``get_state()``. Storage exceptions and malformed documents (not a
    dict, ``mode`` missing or not one of AUTONOMY_MODES) both fail closed
    to Observe with ``read_error=True``. An absent document (None) returns
    the permissive DEFAULT_MODE — the operator never touched the dial.
    """
    try:
        doc = state.get_autonomy()
    except Exception:  # noqa: BLE001 — fail-closed by contract, never raise
        log.warning("autonomy_state_read_failed", exc_info=True)
        return AutonomyState(mode="observe", reason=FAIL_CLOSED_REASON, read_error=True)
    if doc is None:
        return AutonomyState(mode=DEFAULT_MODE)
    mode_val = doc.get("mode") if isinstance(doc, dict) else None
    if mode_val not in AUTONOMY_MODES:
        log.warning("autonomy_state_malformed", extra={"doc_type": type(doc).__name__})
        return AutonomyState(mode="observe", reason=FAIL_CLOSED_REASON, read_error=True)
    return AutonomyState(
        mode=mode_val,
        reason=doc.get("reason"),
        actor=doc.get("actor"),
        updated_at=doc.get("updated_at"),
    )


def mode_allows(mode: str, tier: str) -> bool:
    """True iff ``mode`` permits tools/actions of ``tier``.

    Unknown tier or unknown mode returns False — fail-closed, so a new
    tool cannot leak into a restricted mode by missing or typo-ing its
    tier assignment.
    """
    min_mode = _TIER_MIN_MODE.get(tier)
    if min_mode is None or mode not in AUTONOMY_MODES:
        return False
    return AUTONOMY_MODES.index(mode) >= AUTONOMY_MODES.index(min_mode)


def filter_tools_for_mode(
    tools: Mapping[str, Callable],
    tiers: Mapping[str, str],
    mode: str,
) -> dict[str, Callable]:
    """Return the subset of ``tools`` permitted under ``mode``, preserving
    order. A tool absent from ``tiers`` is treated as "apply" — the most
    restricted tier — so the filter fails closed on registry drift."""
    return {
        name: fn
        for name, fn in tools.items()
        if mode_allows(mode, tiers.get(name, "apply"))
    }


def autonomy_instruction_note(mode: str) -> str:
    """LLM instruction suffix used when the dial strips tools — informational
    UX only; enforcement already happened at the registry filter."""
    label = MODE_LABELS.get(mode, mode)
    return (
        f"NOTE: the operator has set the autonomy dial to {label}, so some "
        "tools that open pull requests or issues, create approvals, or apply "
        "changes are disabled in this conversation. If the operator asks for "
        "something you no longer have a tool for, say that plainly and point "
        "them at the Autonomy control in the operator UI — do not improvise "
        "another way to make the change."
    )


def autonomy_apply_blocked_detail(mode: str) -> str:
    """HTTP 409 detail for apply-gate refusals. Factual, names the fix."""
    label = MODE_LABELS.get(mode, mode)
    return (
        f"autonomy is set to {label} — applying changes is disabled. "
        "Raise the dial to Propose + Apply in the operator UI to enable this "
        "approval. The proposal itself remains valid and waiting."
    )
