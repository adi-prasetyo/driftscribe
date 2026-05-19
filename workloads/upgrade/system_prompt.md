PLACEHOLDER — populated in Phase 17.C.

The upgrade workload's full system prompt lands with its first
implementation. This file exists so the registry's path resolution
succeeds when (eventually) the upgrade tools are no longer None.

Today, `load_workload("upgrade")` fails with
`ReservedToolNotImplementedError` at the tool-resolution step
(`upgrade_read_dependencies`, `upgrade_propose_pr`, `get_session_state`,
`set_session_state` are reserved-but-not-implemented in the registry).
That failure happens AFTER this prompt file is read but before the
resolution is returned, so the placeholder content here is loaded into
memory but never reaches the LLM until 17.C flips the reserved entries.

Before proposing an upgrade PR, call `search_developer_docs` for
migration guides on the bumped package. Cite the resulting document
URL in the PR body so the reviewer can audit which canonical guidance
the proposed wording references.
