"""ADK-side MCP toolset wrappers (Phase 17.B).

Each module under this package owns a single remote MCP server binding:

- :mod:`agent.mcp.developer_knowledge` — Google Developer Knowledge MCP
  (Streamable HTTP at ``developerknowledge.googleapis.com``). Exposes
  ``search_developer_docs`` + ``retrieve_developer_doc`` as ADK
  function-tool wrappers around the underlying ``search_documents`` and
  ``get_documents`` MCP tools. ``answer_query`` is deliberately filtered
  out — it's a second LLM-reasoning surface (Preview) we don't want.

The wrappers add the things the raw MCP toolset doesn't give us by
default: a wall-clock timeout, an in-process response cache, response
truncation, fail-closed error translation, and structured-log emission
that carries the per-request ``trace_id``.
"""
from agent.mcp.developer_knowledge import (
    MissingDeveloperKnowledgeApiKeyError,
    build_developer_knowledge_toolset,
    retrieve_developer_doc,
    search_developer_docs,
)

__all__ = [
    "MissingDeveloperKnowledgeApiKeyError",
    "build_developer_knowledge_toolset",
    "retrieve_developer_doc",
    "search_developer_docs",
]
