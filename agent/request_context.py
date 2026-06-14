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
