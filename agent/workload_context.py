"""ContextVar plumbing for the *calling* workload (Phase 17.B.4 follow-up).

The Developer Knowledge MCP wrapper emits a structured log line per call
with ``mcp_server="developer_knowledge"`` (the MCP target identity). Once
both the drift and upgrade workloads share that wrapper, distinguishing
latency / failure / quota usage by *caller* becomes load-bearing for
the operator's dashboards — the MCP server identity alone isn't enough.

This module owns the ContextVar that carries the *caller's* workload
identity (``"drift"``, ``"upgrade"``) through the async call tree from
the request handler down to the MCP wrapper. It deliberately mirrors
the trace-id pattern in :mod:`driftscribe_lib.logging`:

- A module-level :class:`ContextVar` with a string default — ``"unknown"``
  rather than ``""`` because the value flows straight into a structured
  log field where the empty string would render as missing data, and
  ``"unknown"`` is a clearer "no scope was set" sentinel for log
  consumers.
- :func:`set_workload` returns a :class:`Token` the caller passes to
  :func:`reset_workload` in a ``try/finally``. This is what
  guarantees a long-lived coordinator handling concurrent requests
  doesn't leak one request's workload into a sibling request — every
  coroutine sees its own ContextVar snapshot per :pep:`567` semantics.

Why this lives at the package level (``agent/workload_context.py``)
rather than inside :mod:`agent.workloads` (which is the obvious home
for a workload-related ContextVar) is purely a circular-import dodge:
the MCP wrapper in :mod:`agent.mcp.developer_knowledge` reads the
ContextVar, but :mod:`agent.workloads.registry` already imports the
MCP wrappers to wire them into ``TOOL_REGISTRY``. Importing the
ContextVar via ``agent.workloads`` would trigger that package's
``__init__.py``, which in turn imports ``registry``, which imports
the MCP wrappers — and we'd be inside a partially-initialized
``developer_knowledge`` module. Keeping the ContextVar at the
package-root level breaks the cycle without forcing a lazy import or
splitting :mod:`agent.workloads.registry` further.

Logically the ContextVar belongs to the workload subsystem; physically
it lives one level up. The trade-off is documented here so a future
reader doesn't try to "tidy" the module location and re-trigger the
cycle.
"""
from __future__ import annotations

from contextvars import ContextVar, Token

__all__ = [
    "current_workload",
    "reset_workload",
    "set_workload",
]


# Default of ``"unknown"`` (not ``""`` or ``None``) is deliberate — see
# the module docstring. Background tasks, unit tests that don't enter a
# request scope, and any other call that hits the MCP wrapper without
# first calling :func:`set_workload` will surface as
# ``workload="unknown"`` in the structured log, which is a clearer
# operator-facing sentinel than an empty string or a missing field.
_WORKLOAD: ContextVar[str] = ContextVar("workload", default="unknown")


def current_workload() -> str:
    """Return the workload bound to the current context.

    Returns ``"unknown"`` if no :func:`set_workload` call has bound a
    value in the current task's ContextVar copy. The MCP wrapper's
    structured log reads this value directly; the empty-string case
    cannot occur because the ContextVar's ``default`` is ``"unknown"``.
    """
    return _WORKLOAD.get()


def set_workload(name: str) -> Token[str]:
    """Bind ``name`` to the current context. Returns a Token for reset.

    Caller contract (matches :func:`driftscribe_lib.logging.set_trace_id`):
    pair every ``set_workload`` with a ``reset_workload`` in a
    ``try/finally`` so the ContextVar restores even on exception. The
    request handler frames in :mod:`agent.main` are the canonical
    binding points — see :func:`agent.main.chat` and
    :func:`agent.main._do_recheck`.
    """
    return _WORKLOAD.set(name)


def reset_workload(token: Token[str]) -> None:
    """Restore the previous workload binding using ``token``."""
    _WORKLOAD.reset(token)
