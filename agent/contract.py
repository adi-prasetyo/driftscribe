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
        return str(v)

    @model_validator(mode="after")
    def operator_note_required_when_manual(self) -> "EnvVarRule":
        if self.allow_manual_change and not self.operator_note:
            raise ValueError(
                "operator_note is required when allow_manual_change=true "
                "(operators need to know what flipping this does)"
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
    raw = yaml.safe_load(Path(path).read_text())
    return OpsContract.model_validate(raw)
