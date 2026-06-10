"""Operator pause flag — the ClickOps-audience kill switch (Wave 2 item 5).

Fail-closed contract: read_pause_state NEVER raises. Any storage error
returns paused=True/read_error=True so mutation entrypoints refuse while
the flag is unreadable. Read-only routes must not call this at all.

The split between this module and the two POST/GET routes in main.py is
deliberate: the fail-closed read logic has no FastAPI dependency and can
be unit-tested with a plain InMemoryStateStore or a throw-on-get stub
without spinning up the full application.
"""

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("driftscribe.agent.pause")

PAUSED_DETAIL = (
    "DriftScribe is paused — an operator suspended agent activity. "
    "Resume from the operator UI to allow changes again."
)
# Returned as the ``reason`` field when the Firestore read itself fails.
# Surfaced in the GET /pause response and in any 423 detail so the operator
# knows whether the system is paused-by-choice or paused-by-read-failure.
FAIL_CLOSED_REASON = "pause state could not be read — failing closed"


@dataclass(frozen=True)
class PauseState:
    """Immutable snapshot of the pause flag at a point in time.

    Frozen so it can be passed around without mutation risk between the
    read site and the several mutation gates that consume it per request.

    ``read_error=True`` means Firestore returned an exception; the system
    is treated as paused in that state (fail-closed by design).
    """

    paused: bool
    reason: str | None = None
    actor: str | None = None
    updated_at: Any = None  # datetime or DatetimeWithNanoseconds from Firestore
    read_error: bool = False


def read_pause_state(state: Any) -> PauseState:
    """Read the pause flag from the given StateStore; NEVER raises.

    Calling convention: pass ``get_state()`` (the coordinator's singleton).
    Any exception from the storage layer is caught, logged at WARNING with
    the full traceback, and converted to a fail-closed PauseState
    (paused=True, read_error=True). This ensures that a Firestore outage
    degrades mutations to "paused" rather than letting them proceed on a
    stale or unknown flag — which is the explicit requirement from the
    ClickOps Wave 2 roadmap.

    Absent document (None returned) means the feature was never toggled:
    the system predates the pause button and defaults to running.
    """
    try:
        doc = state.get_pause()
    except Exception:  # noqa: BLE001 — fail-closed by contract, never raise
        log.warning("pause_state_read_failed", exc_info=True)
        return PauseState(paused=True, reason=FAIL_CLOSED_REASON, read_error=True)
    if not doc:
        return PauseState(paused=False)
    return PauseState(
        paused=bool(doc.get("paused")),
        reason=doc.get("reason"),
        actor=doc.get("actor"),
        updated_at=doc.get("updated_at"),
    )
