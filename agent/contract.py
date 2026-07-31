import hashlib
import logging
from pathlib import Path
from typing import Dict, Any
import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

class DocsRef(BaseModel):
    file: str
    section: str

    @field_validator("file")
    @classmethod
    def no_path_traversal(cls, v: str) -> str:
        # Reject absolute paths and ".." segments
        if v.startswith("/") or ".." in Path(v).parts:
            raise ValueError(f"invalid docs.file path (no absolute paths or '..'): {v!r}")
        return v

class EnvVarRule(BaseModel):
    value: str
    docs: DocsRef
    allow_manual_change: bool = False
    operator_note: str | None = None

    @field_validator("value", mode="before")
    @classmethod
    def normalise_scalar(cls, v: Any) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float, str)):
            return str(v)
        raise ValueError(
            f"value must be a string, bool, or number (got {type(v).__name__}); "
            "Cloud Run env values are always strings — quote your YAML scalar"
        )

    @model_validator(mode="after")
    def operator_note_required_when_manual(self) -> "EnvVarRule":
        if self.allow_manual_change and not self.operator_note:
            raise ValueError(
                "operator_note is required when allow_manual_change=true "
                "(operators need to know what flipping this does)"
            )
        if self.operator_note and ("\n" in self.operator_note or "\r" in self.operator_note):
            raise ValueError(
                "operator_note must be a single line (no embedded newlines); "
                "the patcher renders it inline in a markdown bullet"
            )
        return self

class OpsContract(BaseModel):
    service: str
    environment: str
    cloud_run_service: str
    region: str
    github_repo: str
    expected_env: Dict[str, EnvVarRule] = Field(default_factory=dict)

def load_contract(path: Path) -> OpsContract:
    try:
        text = Path(path).read_text()
    except FileNotFoundError as e:
        raise FileNotFoundError(f"contract not found: {path}") from e
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"failed to parse contract {path}: {e}") from e
    return OpsContract.model_validate(raw)


log = logging.getLogger(__name__)


def contract_hash(contract: OpsContract) -> str:
    """Stable hash of the contract's *content* (not just its path).

    Lives here rather than in ``agent.main`` so the /recheck idempotency key and
    the rollback preview's freshness marker are provably the same function. Two
    hashes that are "the same algorithm, written twice" are two hashes.
    """
    return hashlib.sha256(contract.model_dump_json().encode()).hexdigest()[:16]


# Mirrors ``workers/rollback.ProposeRequest.contract_env``'s ``max_length``.
# Pinned equal by test_propose_reason_boundary so the two cannot drift apart —
# the producer/consumer skew IS the ds-j0i bug class.
_WORKER_CONTRACT_ENV_MAX_KEYS = 64


def contract_preview_payload(contract_path: str) -> dict[str, Any]:
    """The contract fields the Rollback Worker needs to describe what a rollback
    would change (ds-uwc), or ``{}`` if the contract cannot be loaded.

    The worker owns no contract, so it cannot answer "does the TARGET revision
    satisfy the contract" by itself — and it must not be handed observed env
    values to answer it with, because it would then be storing them. So it gets
    the contract's own literals, which are public (they are in
    ``demo/ops-contract.yaml`` in a public repo), and returns booleans.

    Never raises. On the /recheck path a bad contract is already a 500 further
    up; on the chat path it must not take down a rollback PROPOSAL over a failed
    PREVIEW. ``{}`` means the worker records no snapshot and the approval page
    says it could not read one.
    """
    try:
        contract = load_contract(Path(contract_path))
    except Exception as e:  # noqa: BLE001
        log.warning("contract preview unavailable: %s", type(e).__name__)
        return {}
    env = {n: r.value for n, r in contract.expected_env.items()}
    if len(env) > _WORKER_CONTRACT_ENV_MAX_KEYS:
        # ds-j0i: the worker declares ``contract_env: dict | None`` with
        # ``max_length=64`` and ``extra="forbid"``, while OpsContract.expected_env
        # has no cardinality limit — so a 65-variable contract would 422 EVERY
        # rollback proposal, autonomous and chat alike. Same shape as the
        # ``reason`` outage: unbounded at the producer, bounded at the consumer.
        #
        # Degrade to {} rather than sending a truncated subset. This payload is
        # documented as optional and explicitly promises not to break a proposal
        # over a preview, and the approval page renders an honest "could not read
        # a snapshot" when it is absent. A 64-key SLICE would instead render as a
        # complete contract that happens to omit variables — a preview that lies
        # is worse than no preview, especially on the page an operator approves a
        # production rollback from.
        log.warning(
            "contract preview omitted: too many variables for the worker bound",
            extra={"variables": len(env), "max": _WORKER_CONTRACT_ENV_MAX_KEYS},
        )
        return {}
    return {
        "contract_env": env,
        "contract_hash": contract_hash(contract),
    }
