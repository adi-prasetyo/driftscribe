# agent/github_actions.py — re-export shim. See driftscribe_lib.github
# for the implementation. Phase 11.2 moved it there for worker reuse.
from driftscribe_lib.github import (
    get_repo,
    open_docs_pr,
    open_drift_issue,
    open_escalation_issue,
)

__all__ = ["get_repo", "open_drift_issue", "open_escalation_issue", "open_docs_pr"]
