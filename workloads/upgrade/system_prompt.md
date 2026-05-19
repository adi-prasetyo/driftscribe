PLACEHOLDER — populated in Phase 17.C.

The upgrade workload's full system prompt lands with its first
implementation. This file exists so the registry's path resolution
succeeds when (eventually) the upgrade tools are no longer None.

Today, `load_workload("upgrade")` fails with `UnknownToolError` at the
tool-resolution step (upgrade_* tools are reserved-but-not-implemented
in the registry) — well before this prompt file is read. So the
placeholder content here has no runtime effect.
