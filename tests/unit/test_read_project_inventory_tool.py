"""Unit test for ``agent.adk_tools.read_project_inventory_tool``.

Mirrors ``test_read_live_env_tool_calls_reader_with_empty_payload`` in
``test_adk_tools.py``: the zero-arg coordinator wrapper must delegate to the
``infra_reader`` worker with an empty payload (the worker's ``DescribeRequest``
schema is ``extra="forbid"``), and must pass the worker's response through
unchanged.
"""
from __future__ import annotations

from unittest.mock import patch


def test_read_project_inventory_tool_calls_infra_reader_with_empty_payload():
    from agent.adk_tools import read_project_inventory_tool

    sentinel = {"counts_by_type": {"compute.Instance": 3}, "freshness_caveat": "CAI"}
    with patch("agent.adk_tools.worker_client.call") as m:
        m.return_value = sentinel
        out = read_project_inventory_tool()

    m.assert_called_once_with("infra_reader", {})
    assert out == sentinel
