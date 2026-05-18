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
