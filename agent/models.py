from enum import Enum
from pydantic import BaseModel

class ContractStatus(str, Enum):
    ABSENT = "absent"
    PRESENT_ALLOW_MANUAL = "present_allow_manual"
    PRESENT_DISALLOW_MANUAL = "present_disallow_manual"
    MATCH = "match"

class DecisionAction(str, Enum):
    DOCS_PR = "docs_pr"
    DRIFT_ISSUE = "drift_issue"
    ESCALATION = "escalation"
    NO_OP = "no_op"
    ROLLBACK = "rollback"

class EnvDiff(BaseModel):
    name: str
    expected: str | None = None
    live: str | None = None
    contract_status: ContractStatus
    debug_config_value: str | None = None
    recent_pr_match: str | None = None

class DecisionProposal(BaseModel):
    action: DecisionAction
    env_diffs: list[EnvDiff]
    target_docs_file: str | None = None
    target_docs_section: str | None = None
    # Revision to roll back TO (the last-known-good); required when
    # action == ROLLBACK. None for all other actions.
    target_revision: str | None = None
    rationale: str
    confidence: float
    requires_human_review: bool = False
    blocked_reason: str | None = None
