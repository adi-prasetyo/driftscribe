"""Request-scoped autonomy-mode contextvar for DriftScribe.

Provides a fail-closed default (``"observe"``) so any code path that does NOT
explicitly bind a mode never triggers auto-dispatch. Use :func:`autonomy_mode_scope`
(a Token-based context manager) to bind the mode for the duration of an agent run
and reset it on exit — so a reused event-loop task or worker thread never inherits
a stale ``propose_apply`` from a prior run.
"""
import contextvars
from contextlib import contextmanager

_autonomy_mode: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_autonomy_mode", default="observe"
)


def get_current_autonomy_mode() -> str:
    """Return the current request's autonomy mode. Defaults to ``"observe"``."""
    return _autonomy_mode.get()


@contextmanager
def autonomy_mode_scope(mode: str):
    """Bind the request's autonomy mode for the duration of the ``with`` block,
    then reset — so a reused event-loop task / worker thread can never inherit
    a stale ``propose_apply`` (which would wrongly auto-dispatch)."""
    token = _autonomy_mode.set(mode)
    try:
        yield
    finally:
        _autonomy_mode.reset(token)


_demo_anonymous: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "current_demo_anonymous", default=False
)


def is_demo_anonymous() -> bool:
    """True when the in-flight request is a marked anonymous demo caller.

    Read by tools (e.g. :func:`agent.adk_tools.propose_rollback_tool`) that must
    withhold live credentials from the model for anonymous callers during the
    public demo window (audit C1).

    Default ``False`` mirrors :func:`agent.main._is_demo_anonymous` (absence of
    the ``X-DriftScribe-Demo-Anonymous`` marker == trusted operator). NOTE: for
    the withholding this default is fail-OPEN, NOT fail-closed — an anonymous
    request that never bound the flag would be treated as an operator and could
    receive the token. That is safe ONLY because every ``/chat`` entrypoint
    computes the flag from the request and threads it in explicitly (SSE + JSON),
    and the sole credential-returning tool (``propose_rollback_tool``) is
    reachable only via that path. Do NOT rely on the default to protect a new
    credential surface — bind the flag at the request boundary."""
    return _demo_anonymous.get()


@contextmanager
def demo_anonymous_scope(flag: bool):
    """Bind the request's demo-anonymous flag for the duration of the ``with``
    block, then reset — mirroring :func:`autonomy_mode_scope` so a reused
    event-loop task / worker thread never inherits a stale flag."""
    token = _demo_anonymous.set(flag)
    try:
        yield
    finally:
        _demo_anonymous.reset(token)
