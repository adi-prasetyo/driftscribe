"""Shared ADK event/part test doubles for `agent.adk_agent` unit tests.

Three test files (`test_adk_agent_thinking.py`, `test_adk_agent_event_logging.py`,
`test_adk_agent_usage_logging.py`) all stub the same shape of
``google.adk.events.Event`` + ``google.genai.types.Part``: just enough
to drive the event-loop in :mod:`agent.adk_agent`. The classes are
deliberately minimal — only the attributes the production code reads.
Each test file defines its own ``_stub_run`` async generator because
the yield sequence is what each test is actually pinning; the part /
event scaffolding around it is the same everywhere.
"""
from __future__ import annotations

from types import SimpleNamespace


class StubPart:
    """Minimal stand-in for ``google.genai.types.Part``.

    The production code only reads ``text``, ``function_call``, and
    ``thought``. Other attributes are intentionally absent — production
    uses ``getattr(part, "...", default)`` so missing attrs are safe.
    """

    def __init__(self, *, text=None, function_call=None, thought=False):
        self.text = text
        self.function_call = function_call
        self.thought = thought


class StubEvent:
    """Minimal stand-in for ``google.adk.events.Event``.

    Mirrors only the fields the event-loop in :mod:`agent.adk_agent`
    inspects: ``content.parts``, ``partial``, ``usage_metadata``, and
    the ``is_final_response()`` predicate.
    """

    def __init__(self, parts, *, partial=False, final=False, usage=None):
        self.content = SimpleNamespace(parts=parts)
        self.partial = partial
        self._final = final
        self.usage_metadata = usage

    def is_final_response(self):
        return self._final
