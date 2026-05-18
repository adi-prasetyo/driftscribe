# DriftScribe MVP Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Apply @superpowers:test-driven-development for every code task and @superpowers:verification-before-completion before claiming any task done.

**Goal:** Ship a working AI DevOps agent ("DriftScribe") for the Google Cloud Japan / Findy DevOps × AI Agent Hackathon 2026. The agent watches live Cloud Run config, compares it against a declared operational contract (`ops-contract.yaml`), and either updates the runbook via a GitHub PR, opens a drift issue, or opens an escalation issue — depending on whether the change is sanctioned.

**Architecture:** Three-layer system. (1) ADK agent shell with four tools (Cloud Run reader, debug-config caller, GitHub history search, contract loader) emits a structured `DecisionProposal`. (2) Deterministic validator gates that proposal against contract rules + path allowlist + secret-leak guard. (3) Action layer creates the GitHub PR/issue. State + idempotency in Firestore. Triggered manually via `POST /recheck` (primary demo path) and by Audit Log → Eventarc → `/eventarc` (production path, final phase).

**Tech Stack:** Python 3.12, FastAPI, Google ADK (`google-adk`), Gemini 2.5 Flash, `google-cloud-run` admin client, `google-cloud-firestore`, PyGithub, pydantic v2, pydantic-settings, uv for dependency management, `gcloud` scripts for infra, GitHub Actions for CI. Private repo: `github.com/adi-prasetyo/driftscribe`.

**Hackathon constraints:** ~6 dev days, 1 person. Total Phase 0–10. Demo flow is 5 beats (bootstrap → sanctioned → unsanctioned → uncertain → CI gate). Eventarc is Phase 9, NOT critical-path. Submission deliverables are Phase 10.

---

## Operating Principles (read before starting)

- **Verify before claiming done.** Every task ends with a verification command. Don't say "passes" — show the output. @superpowers:verification-before-completion.
- **DRY_RUN is the default.** All side-effecting actions (PR/issue creation, Firestore writes) check `DRY_RUN=true` first. Tests run in DRY_RUN.
- **Tests use fakes, not the network.** No test should call real GCP, real GitHub, or real Gemini. Use `tmp_path`, `MagicMock`, `respx`. Demo files in `demo/` are committed fixtures so tests don't depend on later phases.
- **Commit every passing task.** No half-done commits.
- **Don't touch Eventarc until Phase 9.** And don't touch 9.2 until 9.1 yields real audit log values.

---

## Phase 0 — Repo skeleton + demo fixtures (½ day)

### Task 0.1: Directory structure

**Files:** All new.

**Step 1: Create dirs**

```bash
cd ~/driftscribe
mkdir -p agent checker demo/docs infra/scripts scripts tests/unit tests/integration .github/workflows docs
touch agent/__init__.py checker/__init__.py tests/__init__.py tests/unit/__init__.py tests/integration/__init__.py
```

**Step 2: Verify**

```bash
find . -type d -not -path './.git*' | sort
```

Expected: matches scaffold tree.

**Step 3: Commit**

```bash
git add -A
git commit -m "chore: scaffold repo directories"
```

---

### Task 0.2: `pyproject.toml` (uv)

**Files:** Create `pyproject.toml`.

**Step 1: Write file**

```toml
[project]
name = "driftscribe"
version = "0.1.0"
description = "AI DevOps agent for live Cloud Run drift detection"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "pydantic>=2.9",
    "pydantic-settings>=2.6",
    "google-adk>=0.4",
    "google-cloud-run>=0.10",
    "google-cloud-firestore>=2.19",
    "google-genai>=0.5",
    "pygithub>=2.5",
    "httpx>=0.27",
    "pyyaml>=6.0",
    "typer>=0.15",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "respx>=0.21",
    "ruff>=0.7",
]

[project.scripts]
driftscribe = "agent.cli:app"
driftscribe-check = "checker.cli:app"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py312"
```

**Step 2: Install**

```bash
uv venv && uv pip install -e ".[dev]"
```

**Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "chore: pyproject.toml with uv"
```

---

### Task 0.3: `.gitignore`, `README.md`, `Makefile`, `.env.example`

**Step 1: `.gitignore`**

```
.venv/
__pycache__/
*.pyc
.pytest_cache/
.mypy_cache/
.ruff_cache/
.env
.env.local
*.egg-info/
dist/
build/
.DS_Store
```

**Step 2: `README.md`** (placeholder; full one in Phase 10)

```markdown
# DriftScribe

AI DevOps agent for live Cloud Run drift detection. Submission for DevOps × AI Agent Hackathon 2026 (Google Cloud Japan / Findy).

**Status:** Under construction. See `docs/plans/2026-05-18-driftscribe-mvp.md`.
```

**Step 3: `Makefile`**

```makefile
.PHONY: install test lint run-agent run-demo dry-recheck

install:
	uv pip install -e ".[dev]"

test:
	uv run pytest -v

lint:
	uv run ruff check .

run-agent:
	DRY_RUN=true uv run uvicorn agent.main:app --reload --port 8080

run-demo:
	uv run uvicorn demo.main:app --reload --port 8081

dry-recheck:
	curl -s -X POST http://localhost:8080/recheck | jq .
```

**Step 4: `.env.example`**

```bash
# Copy to .env and fill in. NEVER commit .env.
DRY_RUN=true
GCP_PROJECT=your-project
TARGET_SERVICE=payment-demo
TARGET_REGION=asia-northeast1
CONTRACT_PATH=demo/ops-contract.yaml
GITHUB_REPO=adi-prasetyo/driftscribe
GITHUB_TOKEN=
GOOGLE_API_KEY=
DEBUG_CONFIG_URL=
USE_ADK=false
```

**Step 5: Commit**

```bash
git add .gitignore README.md Makefile .env.example
git commit -m "chore: gitignore, README placeholder, Makefile, .env.example"
```

---

### Task 0.4: Demo fixtures (contract + initial runbook)

Demo files exist from day 1 so tests, the checker, and integration tests have something to point at. The runbook is intentionally stale — Beat A in the final demo updates it.

**Files:**
- Create: `demo/ops-contract.yaml`
- Create: `demo/docs/runbook.md`

**Step 1: `demo/ops-contract.yaml`**

```yaml
service: payment-demo
environment: production
cloud_run_service: payment-demo
region: asia-northeast1
github_repo: adi-prasetyo/driftscribe
expected_env:
  PAYMENT_MODE:
    value: "mock"
    docs:
      file: demo/docs/runbook.md
      section: Runtime Configuration
    allow_manual_change: false
  FEATURE_NEW_CHECKOUT:
    value: "false"
    docs:
      file: demo/docs/runbook.md
      section: Feature Flags
    allow_manual_change: true
    operator_note: "Operator-toggleable: enables the new checkout flow. Safe to flip without a redeploy."
```

(All values quoted to dodge YAML-boolean traps; the loader normalizes anyway.)

**Step 2: `demo/docs/runbook.md`**

```markdown
# payment-demo Runbook

## Runtime Configuration

- `PAYMENT_MODE=mock` — controls whether payments hit the real gateway. Must be `mock` in non-production environments. Changes require a PR.

## Feature Flags

- `FEATURE_NEW_CHECKOUT=false` — operator-toggleable new checkout flow. Safe to flip without a redeploy.
```

**Step 3: Commit**

```bash
git add demo/ops-contract.yaml demo/docs/runbook.md
git commit -m "chore(demo): initial contract + runbook fixtures"
```

---

## Phase 1 — Pure logic (1 day)

All TDD. No cloud, no network, no LLM.

### Task 1.1: Data models (TDD)

**Files:**
- Create: `agent/models.py`
- Test: `tests/unit/test_models.py`

**Step 1: Failing test**

```python
# tests/unit/test_models.py
from agent.models import (
    DecisionProposal, DecisionAction, EnvDiff, ContractStatus,
)

def test_env_diff_holds_per_var_evidence():
    d = EnvDiff(
        name="PAYMENT_MODE",
        expected="mock",
        live="live",
        contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,
        debug_config_value="live",
        recent_pr_match=None,
    )
    assert d.name == "PAYMENT_MODE"
    assert d.contract_status == ContractStatus.PRESENT_DISALLOW_MANUAL

def test_decision_proposal_serialises():
    p = DecisionProposal(
        action=DecisionAction.DRIFT_ISSUE,
        env_diffs=[EnvDiff(
            name="PAYMENT_MODE", expected="mock", live="live",
            contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,
        )],
        target_docs_file="demo/docs/runbook.md",
        target_docs_section="Runtime Configuration",
        rationale="Contract disallows manual change.",
        confidence=0.9,
        requires_human_review=False,
    )
    d = p.model_dump()
    assert d["action"] == "drift_issue"
    assert d["env_diffs"][0]["contract_status"] == "present_disallow_manual"

def test_action_enum_values():
    assert DecisionAction.DOCS_PR.value == "docs_pr"
    assert DecisionAction.DRIFT_ISSUE.value == "drift_issue"
    assert DecisionAction.ESCALATION.value == "escalation"
    assert DecisionAction.NO_OP.value == "no_op"
```

**Step 2: Run failing**

```bash
uv run pytest tests/unit/test_models.py -v
```

**Step 3: Implement**

```python
# agent/models.py
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
    rationale: str
    confidence: float
    requires_human_review: bool = False
    blocked_reason: str | None = None
```

**Step 4: Run + commit**

```bash
uv run pytest tests/unit/test_models.py -v
git add agent/models.py tests/unit/test_models.py
git commit -m "feat(models): EnvDiff (per-diff evidence) + DecisionProposal"
```

---

### Task 1.2: Contract schema + loader (TDD with YAML edge cases)

**Files:**
- Create: `agent/contract.py`
- Test: `tests/unit/test_contract.py`

**Step 1: Failing test**

```python
# tests/unit/test_contract.py
import pytest
from agent.contract import OpsContract, load_contract

QUOTED = """
service: payment-demo
environment: production
cloud_run_service: payment-demo
region: asia-northeast1
github_repo: adi-prasetyo/driftscribe
expected_env:
  PAYMENT_MODE:
    value: "mock"
    docs: { file: demo/docs/runbook.md, section: Runtime Configuration }
    allow_manual_change: false
  FEATURE_X:
    value: "false"
    docs: { file: demo/docs/runbook.md, section: Feature Flags }
    allow_manual_change: true
    operator_note: "Operator-safe flag."
"""

UNQUOTED_BOOL = """
service: x
environment: production
cloud_run_service: x
region: asia-northeast1
github_repo: x/x
expected_env:
  FLAG:
    value: false
    docs: { file: docs/r.md, section: S }
    allow_manual_change: true
    operator_note: "n"
"""

def test_quoted_string_values_load(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(QUOTED)
    c = load_contract(p)
    assert c.expected_env["PAYMENT_MODE"].value == "mock"
    assert c.expected_env["FEATURE_X"].operator_note.startswith("Operator-safe")

def test_yaml_boolean_value_is_normalised_to_string(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(UNQUOTED_BOOL)
    c = load_contract(p)
    # YAML booleans must round-trip as strings — Cloud Run env vars are always strings
    assert c.expected_env["FLAG"].value == "false"
    assert isinstance(c.expected_env["FLAG"].value, str)

def test_yaml_integer_value_is_normalised_to_string(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("""
service: x
environment: production
cloud_run_service: x
region: asia-northeast1
github_repo: x/x
expected_env:
  COUNT:
    value: 42
    docs: { file: docs/r.md, section: S }
    allow_manual_change: false
""")
    c = load_contract(p)
    assert c.expected_env["COUNT"].value == "42"

def test_missing_required_fields_raises(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("service: x\n")
    with pytest.raises(Exception):
        load_contract(p)

def test_allow_manual_true_without_operator_note_raises(tmp_path):
    # Forces docs to be informative when operators can flip a var manually
    p = tmp_path / "c.yaml"
    p.write_text("""
service: x
environment: production
cloud_run_service: x
region: asia-northeast1
github_repo: x/x
expected_env:
  FLAG:
    value: "false"
    docs: { file: docs/r.md, section: S }
    allow_manual_change: true
""")
    with pytest.raises(Exception, match="operator_note"):
        load_contract(p)

def test_docs_path_traversal_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("""
service: x
environment: production
cloud_run_service: x
region: asia-northeast1
github_repo: x/x
expected_env:
  FLAG:
    value: "false"
    docs: { file: ../../etc/passwd, section: S }
    allow_manual_change: false
""")
    with pytest.raises(Exception, match="path"):
        load_contract(p)
```

**Step 2: Run failing**

```bash
uv run pytest tests/unit/test_contract.py -v
```

**Step 3: Implement**

```python
# agent/contract.py
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
```

**Step 4: Run + commit**

```bash
uv run pytest tests/unit/test_contract.py -v
git add agent/contract.py tests/unit/test_contract.py
git commit -m "feat(contract): schema + loader with scalar normalisation, path guard, operator_note rule"
```

---

### Task 1.3: Classifier (TDD)

**Files:**
- Create: `agent/classifier.py`
- Test: `tests/unit/test_classifier.py`

**Step 1: Failing test**

```python
# tests/unit/test_classifier.py
from agent.classifier import classify, ClassificationInput
from agent.contract import OpsContract, EnvVarRule, DocsRef
from agent.models import DecisionAction, ContractStatus

def _contract(env_rules):
    return OpsContract(
        service="payment-demo",
        environment="production",
        cloud_run_service="payment-demo",
        region="asia-northeast1",
        github_repo="adi-prasetyo/driftscribe",
        expected_env=env_rules,
    )

def _rule(value="x", allow=False, note=None):
    return EnvVarRule(
        value=value,
        docs=DocsRef(file="demo/docs/runbook.md", section="S"),
        allow_manual_change=allow,
        operator_note=note,
    )

def test_no_op_when_live_matches_contract():
    c = _contract({"PAYMENT_MODE": _rule("mock", allow=False)})
    out = classify(ClassificationInput(contract=c, live_env={"PAYMENT_MODE": "mock"}))
    assert out.action == DecisionAction.NO_OP
    assert out.env_diffs == []

def test_sanctioned_change_when_allow_manual():
    c = _contract({"FEATURE_X": _rule("false", allow=True, note="op note")})
    out = classify(ClassificationInput(contract=c, live_env={"FEATURE_X": "true"}))
    assert out.action == DecisionAction.DOCS_PR
    d = out.env_diffs[0]
    assert d.expected == "false" and d.live == "true"
    assert d.contract_status == ContractStatus.PRESENT_ALLOW_MANUAL
    assert out.target_docs_file == "demo/docs/runbook.md"
    assert out.target_docs_section == "S"

def test_unsanctioned_drift_when_disallow_manual():
    c = _contract({"PAYMENT_MODE": _rule("mock", allow=False)})
    out = classify(ClassificationInput(contract=c, live_env={"PAYMENT_MODE": "live"}))
    assert out.action == DecisionAction.DRIFT_ISSUE
    assert out.env_diffs[0].contract_status == ContractStatus.PRESENT_DISALLOW_MANUAL

def test_escalation_when_var_absent_and_no_pr_match():
    c = _contract({})
    out = classify(ClassificationInput(contract=c, live_env={"NEW_THING": "x"}))
    assert out.action == DecisionAction.ESCALATION
    assert out.env_diffs[0].contract_status == ContractStatus.ABSENT
    assert out.requires_human_review is True

def test_recent_pr_promotes_absent_var_to_docs_pr():
    c = _contract({})
    prs = [{
        "title": "Add NEW_THING flag",
        "body": "Introduces NEW_THING for the checkout flow.",
        "url": "https://github.com/x/x/pull/1",
        "merged": True,
    }]
    out = classify(ClassificationInput(
        contract=c, live_env={"NEW_THING": "x"}, recent_prs=prs,
    ))
    assert out.action == DecisionAction.DOCS_PR
    assert out.env_diffs[0].recent_pr_match == "https://github.com/x/x/pull/1"

def test_recent_pr_must_match_exact_var_name_not_substring():
    # A PR mentioning "FEATURE_NEW" must NOT promote a drift of "FEATURE_NEW_CHECKOUT"
    c = _contract({})
    prs = [{"title": "Add FEATURE_NEW", "body": "FEATURE_NEW=1", "url": "u", "merged": True}]
    out = classify(ClassificationInput(
        contract=c, live_env={"FEATURE_NEW_CHECKOUT": "true"}, recent_prs=prs,
    ))
    # FEATURE_NEW_CHECKOUT is NOT in the PR (only FEATURE_NEW is). Should escalate.
    assert out.action == DecisionAction.ESCALATION

def test_unmerged_pr_does_not_promote():
    c = _contract({})
    prs = [{"title": "Add NEW_THING", "body": "", "url": "u", "merged": False}]
    out = classify(ClassificationInput(
        contract=c, live_env={"NEW_THING": "x"}, recent_prs=prs,
    ))
    assert out.action == DecisionAction.ESCALATION

def test_multi_var_drift_takes_most_serious_action():
    # If any drift is DRIFT_ISSUE, the whole decision is DRIFT_ISSUE
    c = _contract({
        "PAYMENT_MODE": _rule("mock", allow=False),
        "FEATURE_X": _rule("false", allow=True, note="op"),
    })
    live = {"PAYMENT_MODE": "live", "FEATURE_X": "true"}
    out = classify(ClassificationInput(contract=c, live_env=live))
    assert out.action == DecisionAction.DRIFT_ISSUE
    assert len(out.env_diffs) == 2
```

**Step 2: Run failing**

```bash
uv run pytest tests/unit/test_classifier.py -v
```

**Step 3: Implement**

```python
# agent/classifier.py
import re
from typing import Any
from pydantic import BaseModel
from agent.contract import OpsContract
from agent.models import DecisionProposal, DecisionAction, EnvDiff, ContractStatus

class ClassificationInput(BaseModel):
    contract: OpsContract
    live_env: dict[str, str]
    recent_prs: list[dict[str, Any]] = []

def _strict_pr_match(prs: list[dict], var_name: str) -> str | None:
    """Find a merged PR that mentions the EXACT var name as a token (not substring)."""
    token = re.compile(rf"\b{re.escape(var_name)}\b")
    for pr in prs:
        if not pr.get("merged"):
            continue
        haystack = f"{pr.get('title','')} {pr.get('body','')}"
        if token.search(haystack):
            return pr.get("url")
    return None

_ACTION_PRIORITY = [
    DecisionAction.DRIFT_ISSUE,
    DecisionAction.ESCALATION,
    DecisionAction.DOCS_PR,
]

_RATIONALE = {
    DecisionAction.DOCS_PR: "Change is sanctioned (contract allows manual or a recent merged PR mentions the var); updating docs.",
    DecisionAction.DRIFT_ISSUE: "Change violates the contract (allow_manual_change=false). Refusing to document.",
    DecisionAction.ESCALATION: "Variable observed in production has no contract entry and no recent merged PR mentions it. Reviewer needed.",
}

def classify(inp: ClassificationInput) -> DecisionProposal:
    diffs: list[EnvDiff] = []
    actions: list[DecisionAction] = []

    contract_vars = set(inp.contract.expected_env.keys())
    live_vars = set(inp.live_env.keys())

    for name in sorted(contract_vars | live_vars):
        expected = inp.contract.expected_env.get(name)
        live_val = inp.live_env.get(name)
        expected_val = expected.value if expected else None

        if expected and live_val == expected_val:
            continue  # no drift for this var

        if expected is None:
            # Live has a var not in contract → uncertain unless strict PR match
            pr_url = _strict_pr_match(inp.recent_prs, name)
            diffs.append(EnvDiff(
                name=name,
                expected=None,
                live=live_val,
                contract_status=ContractStatus.ABSENT,
                recent_pr_match=pr_url,
            ))
            actions.append(DecisionAction.DOCS_PR if pr_url else DecisionAction.ESCALATION)
        else:
            status = (
                ContractStatus.PRESENT_ALLOW_MANUAL if expected.allow_manual_change
                else ContractStatus.PRESENT_DISALLOW_MANUAL
            )
            diffs.append(EnvDiff(
                name=name,
                expected=expected_val,
                live=live_val,
                contract_status=status,
            ))
            actions.append(
                DecisionAction.DOCS_PR if expected.allow_manual_change else DecisionAction.DRIFT_ISSUE
            )

    if not diffs:
        return DecisionProposal(
            action=DecisionAction.NO_OP,
            env_diffs=[],
            rationale="Live state matches contract.",
            confidence=1.0,
        )

    chosen = next(p for p in _ACTION_PRIORITY if p in actions)

    primary = diffs[0]
    primary_rule = inp.contract.expected_env.get(primary.name)
    if primary_rule:
        target_file = primary_rule.docs.file
        target_section = primary_rule.docs.section
    else:
        target_file = "demo/docs/runbook.md"
        target_section = "Runtime Configuration"

    return DecisionProposal(
        action=chosen,
        env_diffs=diffs,
        target_docs_file=target_file,
        target_docs_section=target_section,
        rationale=_RATIONALE[chosen],
        confidence=0.9,
        requires_human_review=(chosen == DecisionAction.ESCALATION),
    )
```

**Step 4: Run + commit**

```bash
uv run pytest tests/unit/test_classifier.py -v
git add agent/classifier.py tests/unit/test_classifier.py
git commit -m "feat(classifier): per-diff statuses, strict PR token match, priority-based action choice"
```

---

### Task 1.4: Deterministic validator (TDD)

Post-LLM safety gate. Accepts a `DecisionProposal` (from classifier or LLM) and rejects unsafe ones.

**Files:**
- Create: `agent/validator.py`
- Test: `tests/unit/test_validator.py`

**Step 1: Failing test**

```python
# tests/unit/test_validator.py
import pytest
from agent.validator import validate, ValidationError
from agent.models import DecisionProposal, DecisionAction, EnvDiff, ContractStatus
from agent.contract import OpsContract, EnvVarRule, DocsRef

def _contract():
    return OpsContract(
        service="payment-demo",
        environment="production",
        cloud_run_service="payment-demo",
        region="asia-northeast1",
        github_repo="adi-prasetyo/driftscribe",
        expected_env={
            "PAYMENT_MODE": EnvVarRule(
                value="mock",
                docs=DocsRef(file="demo/docs/runbook.md", section="Runtime Configuration"),
                allow_manual_change=False,
            ),
            "FEATURE_X": EnvVarRule(
                value="false",
                docs=DocsRef(file="demo/docs/runbook.md", section="Feature Flags"),
                allow_manual_change=True,
                operator_note="Operator-safe",
            ),
        },
    )

def _proposal(action, name, expected_val, live, status):
    return DecisionProposal(
        action=action,
        env_diffs=[EnvDiff(
            name=name, expected=expected_val, live=live, contract_status=status,
        )],
        target_docs_file="demo/docs/runbook.md",
        target_docs_section="Runtime Configuration",
        rationale="t", confidence=0.9,
    )

def test_validator_passes_correct_drift_issue():
    p = _proposal(DecisionAction.DRIFT_ISSUE, "PAYMENT_MODE", "mock", "live",
                   ContractStatus.PRESENT_DISALLOW_MANUAL)
    validate(p, _contract())

def test_validator_passes_correct_docs_pr():
    p = _proposal(DecisionAction.DOCS_PR, "FEATURE_X", "false", "true",
                   ContractStatus.PRESENT_ALLOW_MANUAL)
    p.target_docs_section = "Feature Flags"
    validate(p, _contract())

def test_validator_rejects_docs_pr_when_contract_disallows_manual():
    p = _proposal(DecisionAction.DOCS_PR, "PAYMENT_MODE", "mock", "live",
                   ContractStatus.PRESENT_DISALLOW_MANUAL)
    with pytest.raises(ValidationError, match="allow_manual_change"):
        validate(p, _contract())

def test_validator_rejects_docs_pr_for_unknown_var_without_pr_match():
    p = DecisionProposal(
        action=DecisionAction.DOCS_PR,
        env_diffs=[EnvDiff(
            name="UNKNOWN", expected=None, live="x",
            contract_status=ContractStatus.ABSENT, recent_pr_match=None,
        )],
        target_docs_file="demo/docs/runbook.md",
        target_docs_section="Runtime Configuration",
        rationale="t", confidence=0.9,
    )
    with pytest.raises(ValidationError, match="recent_pr_match"):
        validate(p, _contract())

def test_validator_rejects_secret_like_var_in_docs_pr():
    p = _proposal(DecisionAction.DOCS_PR, "API_TOKEN", "x", "y",
                   ContractStatus.PRESENT_ALLOW_MANUAL)
    with pytest.raises(ValidationError, match="secret"):
        validate(p, _contract())

def test_validator_rejects_target_docs_file_outside_repo():
    p = _proposal(DecisionAction.DOCS_PR, "FEATURE_X", "false", "true",
                   ContractStatus.PRESENT_ALLOW_MANUAL)
    p.target_docs_file = "../etc/passwd"
    with pytest.raises(ValidationError, match="path"):
        validate(p, _contract())

def test_validator_rejects_target_section_not_in_contract_for_known_var():
    p = _proposal(DecisionAction.DOCS_PR, "FEATURE_X", "false", "true",
                   ContractStatus.PRESENT_ALLOW_MANUAL)
    p.target_docs_section = "Hallucinated Section"
    with pytest.raises(ValidationError, match="section"):
        validate(p, _contract())
```

**Step 2: Run failing**

```bash
uv run pytest tests/unit/test_validator.py -v
```

**Step 3: Implement**

```python
# agent/validator.py
import re
from pathlib import Path
from agent.models import DecisionProposal, DecisionAction
from agent.contract import OpsContract

class ValidationError(Exception):
    pass

_SECRET_NAME_PATTERN = re.compile(
    r"(SECRET|TOKEN|KEY|PASSWORD|PASSWD|CRED|PRIVATE)",
    re.IGNORECASE,
)

def _validate_path(p: str | None) -> None:
    if p is None:
        return
    if p.startswith("/") or ".." in Path(p).parts:
        raise ValidationError(f"target docs path rejected (absolute or traversal): {p!r}")

def validate(proposal: DecisionProposal, contract: OpsContract) -> None:
    """Raise ValidationError if proposal violates safety rules."""

    # 1. Action must be a known enum
    if not isinstance(proposal.action, DecisionAction):
        try:
            DecisionAction(proposal.action)
        except ValueError as e:
            raise ValidationError(f"unknown action: {proposal.action!r}") from e

    # 2. Path guards
    _validate_path(proposal.target_docs_file)

    # 3. Docs PR semantics
    if proposal.action == DecisionAction.DOCS_PR:
        for diff in proposal.env_diffs:
            rule = contract.expected_env.get(diff.name)
            if rule is None:
                if not diff.recent_pr_match:
                    raise ValidationError(
                        f"docs_pr for unknown var {diff.name!r} requires recent_pr_match evidence"
                    )
            elif not rule.allow_manual_change:
                raise ValidationError(
                    f"docs_pr for {diff.name!r} rejected: contract says allow_manual_change=False"
                )

            # Secret-leak guard
            if _SECRET_NAME_PATTERN.search(diff.name):
                raise ValidationError(
                    f"refusing docs_pr that would document secret-like var {diff.name!r}"
                )

        # Target section must match contract for known vars
        for diff in proposal.env_diffs:
            rule = contract.expected_env.get(diff.name)
            if rule and proposal.target_docs_section and rule.docs.section != proposal.target_docs_section:
                raise ValidationError(
                    f"target_docs_section {proposal.target_docs_section!r} does not match "
                    f"contract section {rule.docs.section!r} for {diff.name!r}"
                )
```

**Step 4: Run + commit**

```bash
uv run pytest tests/unit/test_validator.py -v
git add agent/validator.py tests/unit/test_validator.py
git commit -m "feat(validator): path guards, contract semantics, secret-leak, section pinning"
```

---

### Task 1.5: Consistency-gate checker CLI (TDD)

**Files:**
- Create: `checker/cli.py`
- Test: `tests/unit/test_checker.py`

**Step 1: Failing test**

```python
# tests/unit/test_checker.py
from pathlib import Path
from checker.cli import check_docs_cover_contract

CONTRACT = """
service: payment-demo
environment: production
cloud_run_service: payment-demo
region: asia-northeast1
github_repo: adi-prasetyo/driftscribe
expected_env:
  PAYMENT_MODE:
    value: "mock"
    docs: { file: docs/runbook.md, section: Runtime Configuration }
    allow_manual_change: false
  FEATURE_X:
    value: "false"
    docs: { file: docs/runbook.md, section: Feature Flags }
    allow_manual_change: true
    operator_note: "op"
"""

GOOD_RUNBOOK = """\
# Runbook

## Runtime Configuration

- `PAYMENT_MODE=mock` controls payment behaviour.

## Feature Flags

- `FEATURE_X=false` is operator-toggleable. **Operator note:** Operators may flip this without a redeploy.
"""

def _scaffold(tmp_path, runbook_text):
    (tmp_path / "ops-contract.yaml").write_text(CONTRACT)
    docs = tmp_path / "docs"; docs.mkdir()
    (docs / "runbook.md").write_text(runbook_text)
    return tmp_path

def test_passes_when_each_var_in_its_section(tmp_path):
    repo = _scaffold(tmp_path, GOOD_RUNBOOK)
    r = check_docs_cover_contract(repo / "ops-contract.yaml", repo)
    assert r.ok, r.failures

def test_fails_when_var_present_in_wrong_section(tmp_path):
    # PAYMENT_MODE appears, but inside Feature Flags section, not Runtime Configuration
    bad = """\
# Runbook

## Feature Flags

- `PAYMENT_MODE=mock`
- `FEATURE_X=false` **Operator note:** op.
"""
    repo = _scaffold(tmp_path, bad)
    r = check_docs_cover_contract(repo / "ops-contract.yaml", repo)
    assert not r.ok
    assert any("PAYMENT_MODE" in f and "Runtime Configuration" in f for f in r.failures)

def test_fails_when_allow_manual_var_missing_operator_note(tmp_path):
    bad = """\
# Runbook

## Runtime Configuration

- `PAYMENT_MODE=mock`

## Feature Flags

- `FEATURE_X=false`
"""
    repo = _scaffold(tmp_path, bad)
    r = check_docs_cover_contract(repo / "ops-contract.yaml", repo)
    assert not r.ok
    assert any("FEATURE_X" in f and "operator" in f.lower() for f in r.failures)

def test_fails_when_docs_file_missing(tmp_path):
    (tmp_path / "ops-contract.yaml").write_text(CONTRACT)
    # no docs/ dir created
    r = check_docs_cover_contract(tmp_path / "ops-contract.yaml", tmp_path)
    assert not r.ok

def test_fails_when_path_traversal_in_contract(tmp_path):
    bad_contract = """
service: x
environment: production
cloud_run_service: x
region: asia-northeast1
github_repo: x/x
expected_env:
  FLAG:
    value: "1"
    docs: { file: ../etc/passwd, section: S }
    allow_manual_change: false
"""
    p = tmp_path / "ops-contract.yaml"
    p.write_text(bad_contract)
    r = check_docs_cover_contract(p, tmp_path)
    assert not r.ok
```

**Step 2: Run failing**

```bash
uv run pytest tests/unit/test_checker.py -v
```

**Step 3: Implement**

```python
# checker/cli.py
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List
import typer
import yaml

app = typer.Typer(help="DriftScribe consistency gate")

@dataclass
class CheckResult:
    ok: bool
    failures: List[str] = field(default_factory=list)

def _split_sections(md: str) -> dict[str, str]:
    """Return {section_title: section_body} for `## section` headers."""
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in md.splitlines():
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(buf)
            current = line[3:].strip()
            buf = []
        else:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf)
    return sections

def _path_safe(p: str) -> bool:
    return not p.startswith("/") and ".." not in Path(p).parts

def check_docs_cover_contract(contract_path: Path, repo_root: Path) -> CheckResult:
    raw = yaml.safe_load(contract_path.read_text())
    failures: list[str] = []
    for var_name, rule in raw.get("expected_env", {}).items():
        docs_file = rule.get("docs", {}).get("file", "")
        if not _path_safe(docs_file):
            failures.append(f"{var_name}: docs.file path rejected: {docs_file!r}")
            continue

        full_path = repo_root / docs_file
        if not full_path.exists():
            failures.append(f"{var_name}: docs file missing at {full_path}")
            continue

        sections = _split_sections(full_path.read_text())
        section_name = rule["docs"]["section"]
        if section_name not in sections:
            failures.append(f"{var_name}: docs section '{section_name}' not found in {docs_file}")
            continue

        section_body = sections[section_name]
        # Match the env var as a token (word boundary), case-sensitive
        if not re.search(rf"\b{re.escape(var_name)}\b", section_body):
            failures.append(f"{var_name}: not mentioned in section '{section_name}' of {docs_file}")
            continue

        if rule.get("allow_manual_change") and "operator note" not in section_body.lower():
            failures.append(
                f"{var_name}: allow_manual_change=true but no 'operator note' in section '{section_name}'"
            )

    return CheckResult(ok=len(failures) == 0, failures=failures)

@app.command()
def check(
    contract: Path = typer.Option(Path("demo/ops-contract.yaml")),
    repo_root: Path = typer.Option(Path(".")),
):
    """Run the consistency gate."""
    result = check_docs_cover_contract(contract, repo_root)
    if result.ok:
        typer.echo("✓ All contract vars are documented.")
        raise typer.Exit(0)
    typer.echo("✗ Consistency gate failed:")
    for f in result.failures:
        typer.echo(f"  - {f}")
    raise typer.Exit(1)

if __name__ == "__main__":
    app()
```

**Step 4: Smoke test against demo fixtures**

```bash
uv run pytest tests/unit/test_checker.py -v
uv run driftscribe-check
```

Expected: `✓ All contract vars are documented.`

**Step 5: Commit**

```bash
git add checker/cli.py tests/unit/test_checker.py
git commit -m "feat(checker): section-scoped + operator-note + path-guard rules"
```

---

## Phase 2 — Cloud Run reader + renderer + DRY_RUN /recheck (1 day)

### Task 2.1: Cloud Run env reader (TDD with mock)

**Files:**
- Create: `agent/cloud_run_client.py`
- Test: `tests/unit/test_cloud_run_client.py`

**Step 1: Failing test**

```python
# tests/unit/test_cloud_run_client.py
from unittest.mock import MagicMock
from agent.cloud_run_client import read_live_env

def _env_var(name, value):
    m = MagicMock()
    m.name = name
    m.value = value
    return m

def test_read_live_env_extracts_env_block():
    client = MagicMock()
    container = MagicMock()
    container.env = [_env_var("PAYMENT_MODE", "live"), _env_var("FEATURE_X", "true")]
    svc = MagicMock()
    svc.template.containers = [container]
    client.get_service.return_value = svc

    env = read_live_env("payment-demo", "asia-northeast1", "p", client=client)
    assert env == {"PAYMENT_MODE": "live", "FEATURE_X": "true"}

def test_read_live_env_skips_value_source_secrets():
    client = MagicMock()
    secret = MagicMock(); secret.name = "DB_PASSWORD"; secret.value = ""
    plain = _env_var("PAYMENT_MODE", "live")
    container = MagicMock()
    container.env = [secret, plain]
    svc = MagicMock()
    svc.template.containers = [container]
    client.get_service.return_value = svc

    env = read_live_env("s", "r", "p", client=client)
    assert "DB_PASSWORD" not in env
    assert env["PAYMENT_MODE"] == "live"
```

**Step 2: Run failing → implement**

```python
# agent/cloud_run_client.py
from google.cloud import run_v2

def read_live_env(service: str, region: str, project: str, client=None) -> dict[str, str]:
    """Read the env block from the latest revision of a Cloud Run service."""
    client = client or run_v2.ServicesClient()
    name = f"projects/{project}/locations/{region}/services/{service}"
    svc = client.get_service(name=name)
    env: dict[str, str] = {}
    for container in svc.template.containers:
        for ev in container.env:
            if ev.value:  # skip value_source-only entries (secrets)
                env[ev.name] = ev.value
    return env
```

**Step 3: Commit**

```bash
uv run pytest tests/unit/test_cloud_run_client.py -v
git add agent/cloud_run_client.py tests/unit/test_cloud_run_client.py
git commit -m "feat(cloud-run): live env reader (skips value_source secrets)"
```

---

### Task 2.2: PR/issue/escalation body renderer (TDD)

**Files:**
- Create: `agent/renderer.py`
- Test: `tests/unit/test_renderer.py`

**Step 1: Failing test**

```python
# tests/unit/test_renderer.py
from agent.renderer import render_docs_pr_body, render_drift_issue_body, render_escalation_issue_body
from agent.models import DecisionProposal, DecisionAction, EnvDiff, ContractStatus

def _proposal(action, diffs, **overrides):
    return DecisionProposal(
        action=action, env_diffs=diffs,
        target_docs_file="demo/docs/runbook.md",
        target_docs_section="Runtime Configuration",
        rationale="r", confidence=0.9, **overrides,
    )

def test_drift_issue_body_has_evidence_table_per_diff():
    p = _proposal(DecisionAction.DRIFT_ISSUE, [
        EnvDiff(name="PAYMENT_MODE", expected="mock", live="live",
                contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL),
    ])
    body = render_drift_issue_body(p)
    assert "PAYMENT_MODE" in body
    assert "mock" in body and "live" in body
    assert "present_disallow_manual" in body or "disallow" in body.lower()

def test_escalation_body_calls_out_missing_evidence():
    p = _proposal(DecisionAction.ESCALATION, [
        EnvDiff(name="NEW_THING", expected=None, live="x",
                contract_status=ContractStatus.ABSENT, recent_pr_match=None),
    ], requires_human_review=True)
    body = render_escalation_issue_body(p)
    assert "NEW_THING" in body
    assert "absent" in body.lower() or "no contract entry" in body.lower()
    assert "reviewer" in body.lower() or "intentional" in body.lower()

def test_docs_pr_body_describes_change():
    p = _proposal(DecisionAction.DOCS_PR, [
        EnvDiff(name="FEATURE_X", expected="false", live="true",
                contract_status=ContractStatus.PRESENT_ALLOW_MANUAL),
    ])
    body = render_docs_pr_body(p)
    assert "FEATURE_X" in body and "true" in body
```

**Step 2: Run failing → implement**

```python
# agent/renderer.py
from agent.models import DecisionProposal, EnvDiff

def _diff_row(d: EnvDiff) -> str:
    return (
        f"| `{d.name}` | `{d.expected or '—'}` | `{d.live or '—'}` | "
        f"`{d.contract_status.value}` | {d.recent_pr_match or '—'} | "
        f"`{d.debug_config_value or '—'}` |"
    )

def _evidence_table(proposal: DecisionProposal) -> str:
    header = "| Var | Expected | Live | Status | Recent PR | /debug/config |\n|---|---|---|---|---|---|"
    rows = "\n".join(_diff_row(d) for d in proposal.env_diffs)
    return f"{header}\n{rows}"

def render_docs_pr_body(p: DecisionProposal) -> str:
    return f"""\
## DriftScribe — sanctioned change detected

{p.rationale}

### Changes

{_evidence_table(p)}

### Confidence

{p.confidence:.2f}

> Generated by DriftScribe. The change appears sanctioned per `ops-contract.yaml`.
> Please review and merge to keep documentation in sync with production.
"""

def render_drift_issue_body(p: DecisionProposal) -> str:
    return f"""\
## DriftScribe — unsanctioned production drift

{p.rationale}

### Drift

{_evidence_table(p)}

### Recommended action

- Investigate why production differs from the operational contract.
- If the change is intentional, update `ops-contract.yaml` (set `allow_manual_change: true` and provide an `operator_note`, or revise `value`) and re-run DriftScribe.
- If the change is **not** intentional, roll back via `gcloud run services update --update-env-vars`.

> DriftScribe will not update documentation while the contract is violated.
"""

def render_escalation_issue_body(p: DecisionProposal) -> str:
    return f"""\
## DriftScribe — uncertain change requires review

{p.rationale}

### Observed (no contract entry, no recent PR mention)

{_evidence_table(p)}

### What I don't know

I observed variables in production that are **not in the operational contract**, and I could not find a recent merged PR that mentions them by exact name. I need a human to confirm intent before I touch documentation.

### Reviewer action

- If this change was intentional: add the var(s) to `ops-contract.yaml` with the appropriate `allow_manual_change` and `operator_note`, then re-run DriftScribe.
- If this change was unauthorized: roll back the affected Cloud Run service, then re-run DriftScribe.

> Generated by DriftScribe.
"""
```

**Step 3: Commit**

```bash
uv run pytest tests/unit/test_renderer.py -v
git add agent/renderer.py tests/unit/test_renderer.py
git commit -m "feat(renderer): per-diff evidence table for PR/drift/escalation"
```

---

### Task 2.3: FastAPI `/recheck` in DRY_RUN mode

Wires loader + cloud_run_client + classifier + validator + renderer. No GitHub, no Firestore yet.

**Files:**
- Create: `agent/config.py`
- Create: `agent/main.py`
- Test: `tests/integration/test_recheck_dry_run.py`

**Step 1: `agent/config.py`**

```python
# agent/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    dry_run: bool = True
    gcp_project: str = ""
    target_region: str = "asia-northeast1"
    target_service: str = "payment-demo"
    contract_path: str = "demo/ops-contract.yaml"
    github_repo: str = ""
    github_token: str = ""
    debug_config_url: str = ""
    google_api_key: str = ""
    use_adk: bool = False

def get_settings() -> Settings:
    return Settings()
```

**Step 2: `agent/main.py`**

```python
# agent/main.py
from pathlib import Path
from fastapi import FastAPI, HTTPException

from agent.classifier import classify, ClassificationInput
from agent.cloud_run_client import read_live_env
from agent.config import get_settings
from agent.contract import load_contract
from agent.models import DecisionAction, DecisionProposal
from agent.renderer import (
    render_docs_pr_body, render_drift_issue_body, render_escalation_issue_body,
)
from agent.validator import validate

app = FastAPI(title="DriftScribe Agent")

@app.get("/healthz")
def healthz():
    return {"ok": True}

def _render_for(action: DecisionAction, proposal: DecisionProposal) -> str:
    if action == DecisionAction.NO_OP:
        return "(no action)"
    if action == DecisionAction.DOCS_PR:
        return render_docs_pr_body(proposal)
    if action == DecisionAction.DRIFT_ISSUE:
        return render_drift_issue_body(proposal)
    return render_escalation_issue_body(proposal)

@app.post("/recheck")
def recheck():
    s = get_settings()
    contract = load_contract(Path(s.contract_path))
    try:
        live_env = read_live_env(s.target_service, s.target_region, s.gcp_project)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"cloud run read failed: {e}")

    proposal = classify(ClassificationInput(
        contract=contract, live_env=live_env, recent_prs=[],
    ))
    validate(proposal, contract)

    rendered = _render_for(proposal.action, proposal)

    return {
        "action": proposal.action.value,
        "rendered_body": rendered,
        "rationale": proposal.rationale,
        "diffs": [d.model_dump() for d in proposal.env_diffs],
        "target_docs_file": proposal.target_docs_file,
        "target_docs_section": proposal.target_docs_section,
        "requires_human_review": proposal.requires_human_review,
        "dry_run": s.dry_run,
    }
```

**Step 3: Integration test**

```python
# tests/integration/test_recheck_dry_run.py
import os
os.environ["DRY_RUN"] = "true"
os.environ["GCP_PROJECT"] = "test-proj"

from fastapi.testclient import TestClient
from unittest.mock import patch
from agent.main import app

def test_recheck_renders_drift_issue_when_live_violates_contract():
    with patch("agent.main.read_live_env") as m:
        m.return_value = {"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "false"}
        client = TestClient(app)
        r = client.post("/recheck")
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "drift_issue"
    assert "PAYMENT_MODE" in body["rendered_body"]
    assert body["dry_run"] is True

def test_recheck_no_op_when_live_matches_contract():
    with patch("agent.main.read_live_env") as m:
        m.return_value = {"PAYMENT_MODE": "mock", "FEATURE_NEW_CHECKOUT": "false"}
        client = TestClient(app)
        r = client.post("/recheck")
    assert r.json()["action"] == "no_op"

def test_recheck_escalation_for_unknown_var():
    with patch("agent.main.read_live_env") as m:
        m.return_value = {"PAYMENT_MODE": "mock", "FEATURE_NEW_CHECKOUT": "false", "NEW_THING": "x"}
        client = TestClient(app)
        r = client.post("/recheck")
    assert r.json()["action"] == "escalation"
```

**Step 4: Commit**

```bash
uv run pytest tests/integration -v
git add agent/config.py agent/main.py tests/integration/test_recheck_dry_run.py
git commit -m "feat(api): /recheck wires loader+reader+classifier+validator+renderer (DRY_RUN)"
```

---

## Phase 3 — Runbook patcher + GitHub side effects (1 day)

**Critical:** the patcher must produce real content before we ship a docs PR. Otherwise Beat A of the demo opens an empty PR and the CI gate (Beat D) has nothing to validate against.

### Task 3.1: Runbook patcher (TDD)

Takes the current runbook content + a list of `EnvDiff` + the contract → returns updated runbook content. Section-targeted. Idempotent.

**Files:**
- Create: `agent/runbook_patcher.py`
- Test: `tests/unit/test_runbook_patcher.py`

**Step 1: Failing test**

```python
# tests/unit/test_runbook_patcher.py
from agent.runbook_patcher import patch_runbook
from agent.contract import OpsContract, EnvVarRule, DocsRef
from agent.models import EnvDiff, ContractStatus

def _contract():
    return OpsContract(
        service="payment-demo", environment="production",
        cloud_run_service="payment-demo", region="asia-northeast1",
        github_repo="adi-prasetyo/driftscribe",
        expected_env={
            "FEATURE_NEW_CHECKOUT": EnvVarRule(
                value="false",
                docs=DocsRef(file="demo/docs/runbook.md", section="Feature Flags"),
                allow_manual_change=True,
                operator_note="Operator-toggleable: enables the new checkout flow.",
            ),
        },
    )

STARTING_RUNBOOK = """\
# payment-demo Runbook

## Runtime Configuration

- `PAYMENT_MODE=mock` controls real vs mock payments.

## Feature Flags

- `FEATURE_NEW_CHECKOUT=false` — operator-toggleable new checkout flow.
"""

def test_patch_updates_value_in_section():
    diff = EnvDiff(name="FEATURE_NEW_CHECKOUT", expected="false", live="true",
                   contract_status=ContractStatus.PRESENT_ALLOW_MANUAL)
    new = patch_runbook(STARTING_RUNBOOK, [diff], _contract())
    # Old value gone, new value in
    assert "FEATURE_NEW_CHECKOUT=true" in new
    assert "FEATURE_NEW_CHECKOUT=false" not in new
    # Section name still present
    assert "## Feature Flags" in new
    # Operator note preserved
    assert "operator" in new.lower()

def test_patch_is_idempotent_when_already_up_to_date():
    diff = EnvDiff(name="FEATURE_NEW_CHECKOUT", expected="false", live="false",
                   contract_status=ContractStatus.MATCH)
    new = patch_runbook(STARTING_RUNBOOK, [diff], _contract())
    assert new == STARTING_RUNBOOK

def test_patch_appends_new_var_to_section_if_missing():
    diff = EnvDiff(name="FEATURE_NEW_CHECKOUT", expected=None, live="true",
                   contract_status=ContractStatus.ABSENT,
                   recent_pr_match="https://github.com/x/x/pull/1")
    contract = _contract()
    # Remove the var from contract for this test (simulating "absent" case)
    minimal_runbook = "# Runbook\n\n## Feature Flags\n\n(none yet)\n"
    new = patch_runbook(minimal_runbook, [diff], contract)
    assert "FEATURE_NEW_CHECKOUT=true" in new
```

**Step 2: Run failing**

```bash
uv run pytest tests/unit/test_runbook_patcher.py -v
```

**Step 3: Implement**

```python
# agent/runbook_patcher.py
import re
from agent.contract import OpsContract
from agent.models import EnvDiff, ContractStatus

def _section_pattern(section: str) -> re.Pattern[str]:
    return re.compile(
        rf"(##\s+{re.escape(section)}\s*\n)(.*?)(?=\n##\s+|\Z)",
        re.DOTALL,
    )

def _update_var_line(body: str, name: str, new_value: str, operator_note: str | None) -> str:
    """Replace any existing bullet for `name` with one carrying the new value + note."""
    note = f" **Operator note:** {operator_note}" if operator_note else ""
    new_line = f"- `{name}={new_value}` —{note}"
    pattern = re.compile(rf"^- `{re.escape(name)}=.*?`.*$", re.MULTILINE)
    if pattern.search(body):
        return pattern.sub(new_line, body)
    # Append (preserve trailing blank line if any)
    if body.endswith("\n"):
        return body + new_line + "\n"
    return body + "\n" + new_line + "\n"

def patch_runbook(content: str, diffs: list[EnvDiff], contract: OpsContract) -> str:
    """Apply per-diff updates to a runbook. Idempotent."""
    for diff in diffs:
        # No change required for MATCH
        if diff.contract_status == ContractStatus.MATCH:
            continue
        rule = contract.expected_env.get(diff.name)
        section = rule.docs.section if rule else "Runtime Configuration"
        operator_note = rule.operator_note if rule else None
        new_value = diff.live if diff.live is not None else ""

        pat = _section_pattern(section)
        m = pat.search(content)
        if not m:
            # Section missing — append a stub at end
            content += f"\n## {section}\n\n"
            m = pat.search(content)
        header, body = m.group(1), m.group(2)
        new_body = _update_var_line(body, diff.name, new_value, operator_note)
        content = content[: m.start()] + header + new_body + content[m.end() :]
    return content
```

**Step 4: Run + commit**

```bash
uv run pytest tests/unit/test_runbook_patcher.py -v
git add agent/runbook_patcher.py tests/unit/test_runbook_patcher.py
git commit -m "feat(patcher): section-targeted, idempotent runbook updates"
```

---

### Task 3.2: GitHub actions client (TDD with PyGithub mocks)

**Files:**
- Create: `agent/github_actions.py`
- Test: `tests/unit/test_github_actions.py`

**Step 1: Failing test**

```python
# tests/unit/test_github_actions.py
from unittest.mock import MagicMock
from agent.github_actions import open_drift_issue, open_escalation_issue, open_docs_pr

def test_open_drift_issue_creates_labeled_issue():
    repo = MagicMock()
    open_drift_issue(repo, title="t", body="b", dry_run=False)
    repo.create_issue.assert_called_once()
    kw = repo.create_issue.call_args.kwargs
    assert "driftscribe" in kw["labels"]

def test_dry_run_skips_github_call():
    repo = MagicMock()
    res = open_drift_issue(repo, title="t", body="b", dry_run=True)
    repo.create_issue.assert_not_called()
    assert res["dry_run"] is True
    assert res["url"] is None

def test_open_docs_pr_creates_branch_updates_file_and_opens_pr():
    repo = MagicMock()
    base = MagicMock(); base.commit.sha = "sha-1"
    repo.get_branch.return_value = base
    existing = MagicMock(); existing.sha = "file-sha"
    repo.get_contents.return_value = existing
    pr = MagicMock(); pr.html_url = "https://...pull/42"; pr.number = 42
    repo.create_pull.return_value = pr

    res = open_docs_pr(
        repo=repo, branch="b", base="main", title="t", body="b",
        file_path="demo/docs/runbook.md", new_content="content",
        dry_run=False,
    )
    repo.create_git_ref.assert_called_once()
    repo.update_file.assert_called_once()
    repo.create_pull.assert_called_once()
    assert res["url"].endswith("pull/42")
```

**Step 2: Run failing → implement**

```python
# agent/github_actions.py
from typing import Any
from github import Github
from github.Repository import Repository

def get_repo(token: str, repo_full_name: str) -> Repository:
    return Github(token).get_repo(repo_full_name)

def _issue_result(dry_run: bool, issue=None, title: str = "") -> dict[str, Any]:
    if dry_run:
        return {"dry_run": True, "url": None, "title": title}
    return {"dry_run": False, "url": issue.html_url, "number": issue.number}

def open_drift_issue(repo: Repository, title: str, body: str, dry_run: bool) -> dict[str, Any]:
    if dry_run:
        return _issue_result(True, title=title)
    issue = repo.create_issue(title=title, body=body, labels=["driftscribe", "drift"])
    return _issue_result(False, issue=issue)

def open_escalation_issue(repo: Repository, title: str, body: str, dry_run: bool) -> dict[str, Any]:
    if dry_run:
        return _issue_result(True, title=title)
    issue = repo.create_issue(title=title, body=body, labels=["driftscribe", "escalation"])
    return _issue_result(False, issue=issue)

def open_docs_pr(
    repo: Repository, branch: str, base: str, title: str, body: str,
    file_path: str, new_content: str, dry_run: bool,
) -> dict[str, Any]:
    if dry_run:
        return {"dry_run": True, "url": None, "branch": branch, "preview": new_content[:500]}

    base_ref = repo.get_branch(base)
    repo.create_git_ref(ref=f"refs/heads/{branch}", sha=base_ref.commit.sha)

    try:
        existing = repo.get_contents(file_path, ref=branch)
        repo.update_file(
            path=file_path,
            message=f"docs(driftscribe): update {file_path}",
            content=new_content,
            sha=existing.sha,
            branch=branch,
        )
    except Exception:
        repo.create_file(
            path=file_path,
            message=f"docs(driftscribe): initial {file_path}",
            content=new_content,
            branch=branch,
        )

    pr = repo.create_pull(title=title, body=body, head=branch, base=base)
    pr.add_to_labels("driftscribe", "docs")
    return {"dry_run": False, "url": pr.html_url, "number": pr.number}
```

**Step 3: Run + commit**

```bash
uv run pytest tests/unit/test_github_actions.py -v
git add agent/github_actions.py tests/unit/test_github_actions.py
git commit -m "feat(github): drift/escalation issues + docs PR with branch+file ops"
```

---

### Task 3.3: Wire patcher + GitHub into `/recheck`

**Files:**
- Modify: `agent/main.py`

**Step 1: Update `agent/main.py`** to add a `_perform_action` helper that:
- For NO_OP → returns no-op result
- For DOCS_PR → reads target docs file from repo working copy (or PR's base), calls `patch_runbook`, then `open_docs_pr` with the new content
- For DRIFT_ISSUE → `open_drift_issue`
- For ESCALATION → `open_escalation_issue`

```python
# additions to agent/main.py
import time
from pathlib import Path
from agent.github_actions import (
    get_repo, open_docs_pr, open_drift_issue, open_escalation_issue,
)
from agent.runbook_patcher import patch_runbook

def _perform_action(s, contract, proposal, rendered: str) -> dict:
    if proposal.action == DecisionAction.NO_OP:
        return {"dry_run": s.dry_run, "url": None, "action": "no_op"}

    repo = None if s.dry_run else get_repo(s.github_token, s.github_repo)
    diffs_str = ", ".join(d.name for d in proposal.env_diffs)

    if proposal.action == DecisionAction.DRIFT_ISSUE:
        return open_drift_issue(
            repo=repo,  # type: ignore
            title=f"[DriftScribe] Drift: {diffs_str}",
            body=rendered, dry_run=s.dry_run,
        )

    if proposal.action == DecisionAction.ESCALATION:
        return open_escalation_issue(
            repo=repo,  # type: ignore
            title=f"[DriftScribe] Review: {diffs_str}",
            body=rendered, dry_run=s.dry_run,
        )

    # DOCS_PR: build new file content via patcher
    target = proposal.target_docs_file or "demo/docs/runbook.md"
    target_path = Path(target)
    current = target_path.read_text() if target_path.exists() else f"# Runbook\n\n## {proposal.target_docs_section}\n\n"
    new_content = patch_runbook(current, proposal.env_diffs, contract)

    branch = f"driftscribe/{proposal.env_diffs[0].name.lower()}-{int(time.time())}"
    return open_docs_pr(
        repo=repo,  # type: ignore
        branch=branch, base="main",
        title=f"docs(driftscribe): update {proposal.env_diffs[0].name}",
        body=rendered,
        file_path=target,
        new_content=new_content,
        dry_run=s.dry_run,
    )

# Then in recheck() after `rendered = _render_for(...)`:
github_result = _perform_action(s, contract, proposal, rendered)
# Add to response dict:
#   "github": github_result,
```

**Step 2: Extend integration test**

```python
# tests/integration/test_recheck_dry_run.py — add

def test_recheck_dry_run_returns_github_preview_for_docs_pr():
    with patch("agent.main.read_live_env") as m:
        m.return_value = {"PAYMENT_MODE": "mock", "FEATURE_NEW_CHECKOUT": "true"}
        client = TestClient(app)
        r = client.post("/recheck")
    body = r.json()
    assert body["action"] == "docs_pr"
    assert body["github"]["dry_run"] is True
    # Preview shows the patched runbook content
    assert "FEATURE_NEW_CHECKOUT=true" in body["github"]["preview"]

def test_recheck_dry_run_returns_github_preview_for_drift_issue():
    with patch("agent.main.read_live_env") as m:
        m.return_value = {"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "false"}
        client = TestClient(app)
        r = client.post("/recheck")
    body = r.json()
    assert body["action"] == "drift_issue"
    assert body["github"]["dry_run"] is True
```

**Step 3: Run + commit**

```bash
uv run pytest tests/integration -v
git add agent/main.py tests/integration/test_recheck_dry_run.py
git commit -m "feat(api): /recheck wires patcher + GitHub actions (DRY_RUN aware)"
```

---

## Phase 4 — Firestore state + observability (½ day)

### Task 4.1: State store (TDD with in-memory + Firestore impls)

**Files:**
- Create: `agent/firestore_state.py`
- Test: `tests/unit/test_firestore_state.py`

(Same as v1, but renamed for clarity.)

**Step 1: Test**

```python
# tests/unit/test_firestore_state.py
from agent.firestore_state import InMemoryStateStore

def test_idempotency_repeat_event_returns_existing_decision():
    s = InMemoryStateStore()
    s.record_event("ev-1", {"trigger": "manual"})
    s.record_decision("dec-1", "ev-1", {"action": "drift_issue"})
    assert s.find_decision_for_event("ev-1")["action"] == "drift_issue"

def test_missing_event_returns_none():
    s = InMemoryStateStore()
    assert s.find_decision_for_event("missing") is None
```

**Step 2: Implement** — same `InMemoryStateStore` + `FirestoreStateStore` as v1 (see v1 plan for full code; unchanged).

**Step 3: Commit**

```bash
git add agent/firestore_state.py tests/unit/test_firestore_state.py
git commit -m "feat(state): in-memory + firestore stores"
```

---

### Task 4.2: Live-env-hashed idempotency key + `/runs/{id}`

**THE bug from v1:** event_key was just a hash of the service name → every `/recheck` returned the cached first decision. Fix: include the live env hash so a new drift = a new event.

**Files:**
- Modify: `agent/main.py`
- Test: extend `tests/integration/test_recheck_dry_run.py`

**Step 1: Update event_key**

```python
# agent/main.py — additions
import hashlib, json, uuid
from agent.firestore_state import InMemoryStateStore, FirestoreStateStore

_state_singleton = None

def get_state():
    global _state_singleton
    if _state_singleton is None:
        s = get_settings()
        if s.dry_run or not s.gcp_project:
            _state_singleton = InMemoryStateStore()
        else:
            _state_singleton = FirestoreStateStore(project=s.gcp_project)
    return _state_singleton

def _event_key(trigger: str, service: str, live_env: dict) -> str:
    payload = {"trigger": trigger, "service": service, "live_env": live_env}
    h = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    return f"{trigger}-{service}-{h}"

# Refactor recheck() into _do_recheck(trigger, force):
def _do_recheck(trigger: str, force: bool = False) -> dict:
    s = get_settings()
    contract = load_contract(Path(s.contract_path))
    try:
        live_env = read_live_env(s.target_service, s.target_region, s.gcp_project)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"cloud run read failed: {e}")

    event_key = _event_key(trigger, s.target_service, live_env)
    state = get_state()
    if not force:
        existing = state.find_decision_for_event(event_key)
        if existing:
            return existing

    proposal = classify(ClassificationInput(
        contract=contract, live_env=live_env, recent_prs=[],
    ))
    validate(proposal, contract)
    rendered = _render_for(proposal.action, proposal)
    github_result = _perform_action(s, contract, proposal, rendered)

    decision_id = str(uuid.uuid4())
    response = {
        "decision_id": decision_id,
        "event_key": event_key,
        "action": proposal.action.value,
        "rendered_body": rendered,
        "rationale": proposal.rationale,
        "diffs": [d.model_dump() for d in proposal.env_diffs],
        "target_docs_file": proposal.target_docs_file,
        "target_docs_section": proposal.target_docs_section,
        "requires_human_review": proposal.requires_human_review,
        "dry_run": s.dry_run,
        "github": github_result,
        "trigger": trigger,
    }
    state.record_event(event_key, {"trigger": trigger})
    state.record_decision(decision_id, event_key, response)
    return response

@app.post("/recheck")
def recheck(force: bool = False):
    return _do_recheck("manual_recheck", force=force)

@app.get("/runs/{decision_id}")
def get_run(decision_id: str):
    d = get_state().get_decision(decision_id)
    if not d:
        raise HTTPException(status_code=404)
    return d
```

**Step 2: Integration test for live-env-hash key**

```python
# tests/integration/test_recheck_dry_run.py — add

def test_recheck_with_changed_live_env_returns_fresh_decision():
    """Demo Beat B (PAYMENT_MODE=live) and Beat C (NEW_THING=x) must not collide."""
    client = TestClient(app)
    with patch("agent.main.read_live_env") as m:
        m.return_value = {"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "false"}
        r1 = client.post("/recheck").json()
    with patch("agent.main.read_live_env") as m:
        m.return_value = {"PAYMENT_MODE": "mock", "FEATURE_NEW_CHECKOUT": "false", "NEW_THING": "x"}
        r2 = client.post("/recheck").json()
    assert r1["action"] == "drift_issue"
    assert r2["action"] == "escalation"
    assert r1["decision_id"] != r2["decision_id"]
    assert r1["event_key"] != r2["event_key"]

def test_runs_endpoint_returns_recorded_decision():
    client = TestClient(app)
    with patch("agent.main.read_live_env") as m:
        m.return_value = {"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "false"}
        r1 = client.post("/recheck").json()
    r2 = client.get(f"/runs/{r1['decision_id']}")
    assert r2.status_code == 200
    assert r2.json()["action"] == "drift_issue"

def test_force_param_bypasses_idempotency_cache():
    client = TestClient(app)
    with patch("agent.main.read_live_env") as m:
        m.return_value = {"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "false"}
        r1 = client.post("/recheck").json()
        r2 = client.post("/recheck?force=true").json()
    # Same live state → cache hit on first; force=true → fresh decision_id
    assert r1["decision_id"] != r2["decision_id"]
```

**Step 3: Commit**

```bash
uv run pytest tests/integration -v
git add agent/main.py tests/integration/test_recheck_dry_run.py
git commit -m "feat(state): live-env-hashed event_key + force= bypass + /runs/{id}"
```

---

## Phase 5 — `driftscribe init` CLI (local-only) (¼ day)

Scope reduced from v1: no `--open-pr`. User runs `init`, gets a contract file locally, opens the PR themselves via `gh pr create`. Saves ~3 hours.

### Task 5.1: `agent/cli.py`

**Files:**
- Create: `agent/cli.py`
- Test: `tests/unit/test_cli_init.py`

**Step 1: Failing test**

```python
# tests/unit/test_cli_init.py
from unittest.mock import patch
import yaml
from typer.testing import CliRunner
from agent.cli import app

runner = CliRunner()

def test_init_writes_contract_with_conservative_defaults(tmp_path):
    with patch("agent.cli.read_live_env") as m:
        m.return_value = {"PAYMENT_MODE": "mock", "FEATURE_X": "false"}
        result = runner.invoke(app, [
            "init",
            "--service", "payment-demo",
            "--region", "asia-northeast1",
            "--project", "my-proj",
            "--github-repo", "adi-prasetyo/driftscribe",
            "--output", str(tmp_path / "ops-contract.yaml"),
        ])
    assert result.exit_code == 0, result.output
    out = yaml.safe_load((tmp_path / "ops-contract.yaml").read_text())
    assert out["cloud_run_service"] == "payment-demo"
    assert "PAYMENT_MODE" in out["expected_env"]
    # Conservative: no manual change without explicit approval
    assert out["expected_env"]["PAYMENT_MODE"]["allow_manual_change"] is False
```

**Step 2: Implement**

```python
# agent/cli.py
from pathlib import Path
import typer
import yaml
from agent.cloud_run_client import read_live_env

app = typer.Typer(help="DriftScribe CLI")

@app.command()
def init(
    service: str = typer.Option(...),
    region: str = typer.Option("asia-northeast1"),
    project: str = typer.Option(...),
    github_repo: str = typer.Option(...),
    output: Path = typer.Option(Path("ops-contract.yaml")),
    docs_file: str = typer.Option("docs/runbook.md"),
    docs_section: str = typer.Option("Runtime Configuration"),
):
    """Bootstrap ops-contract.yaml from current live Cloud Run state."""
    live = read_live_env(service, region, project)
    contract = {
        "service": service,
        "environment": "production",
        "cloud_run_service": service,
        "region": region,
        "github_repo": github_repo,
        "expected_env": {
            name: {
                "value": value,
                "docs": {"file": docs_file, "section": docs_section},
                "allow_manual_change": False,
            }
            for name, value in live.items()
        },
    }
    output.write_text(yaml.safe_dump(contract, sort_keys=False))
    typer.echo(f"✓ Wrote {output}")
    typer.echo("\nNext steps:")
    typer.echo("  1. Review the generated contract and add operator_note for any allow_manual_change=true var")
    typer.echo("  2. Run `git add` and `gh pr create` to open the bootstrap PR")

if __name__ == "__main__":
    app()
```

**Step 3: Commit**

```bash
uv run pytest tests/unit/test_cli_init.py -v
git add agent/cli.py tests/unit/test_cli_init.py
git commit -m "feat(cli): driftscribe init (local file only; PR creation is user's job)"
```

---

## Phase 6 — ADK agent shell (correct API) (1 day)

ADK as a thin shell over the four tools. The deterministic validator from Phase 1 still gates side effects.

**ADK reality check (per Codex):**
- API is `Agent(name=..., model=..., instruction=..., tools=[python_function])`
- Execution: `Runner(agent, app_name, session_service)` + `await session_service.create_session(...)` + `async for event in runner.run_async(...)`
- `output_schema` cannot coexist with tool use — so we prompt for JSON text, then parse + Pydantic-validate.

### Task 6.1: Tool wrappers (TDD)

**Files:**
- Create: `agent/adk_tools.py`
- Test: `tests/unit/test_adk_tools.py`

**Step 1: Failing test**

```python
# tests/unit/test_adk_tools.py
from unittest.mock import patch
from agent.adk_tools import (
    read_live_env_tool, call_debug_config_tool, search_recent_prs_tool, load_contract_tool,
)

def test_read_live_env_tool_returns_dict():
    with patch("agent.adk_tools.read_live_env") as m:
        m.return_value = {"X": "1"}
        assert read_live_env_tool("s", "r", "p") == {"X": "1"}

def test_load_contract_tool_parses_yaml(tmp_path):
    (tmp_path / "c.yaml").write_text("""
service: s
environment: production
cloud_run_service: s
region: asia-northeast1
github_repo: x/x
expected_env: {}
""")
    d = load_contract_tool(str(tmp_path / "c.yaml"))
    assert d["service"] == "s"

def test_search_recent_prs_tool_filters_by_exact_token():
    fake = [
        {"title": "Add NEW_THING", "body": "", "url": "u1", "merged": True},
        {"title": "Unrelated change", "body": "", "url": "u2", "merged": True},
        {"title": "Has NEW_THINGEXT", "body": "", "url": "u3", "merged": True},  # substring, NOT a token
    ]
    with patch("agent.adk_tools._list_recent_merged_prs") as m:
        m.return_value = fake
        result = search_recent_prs_tool("x/x", ["NEW_THING"], 7)
    urls = [r["url"] for r in result]
    assert urls == ["u1"]  # u3 must NOT match (substring is not a token)
```

**Step 2: Implement**

```python
# agent/adk_tools.py
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import httpx
import yaml
from github import Github
from agent.cloud_run_client import read_live_env

def read_live_env_tool(service: str, region: str, project: str) -> dict[str, str]:
    """Read the current env block from the latest revision of a Cloud Run service."""
    return read_live_env(service, region, project)

def call_debug_config_tool(url: str) -> dict[str, Any]:
    """Call the target service's /debug/config endpoint. Returns {} on failure."""
    try:
        r = httpx.get(url, timeout=5.0)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"_error": str(e)}

def _list_recent_merged_prs(repo_full: str, days: int, token: str = "") -> list[dict]:
    g = Github(token) if token else Github()
    repo = g.get_repo(repo_full)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    out: list[dict] = []
    for pr in repo.get_pulls(state="closed", sort="updated", direction="desc"):
        if pr.merged_at is None or pr.merged_at < since:
            continue
        out.append({
            "title": pr.title or "", "body": pr.body or "",
            "url": pr.html_url, "merged": True,
        })
    return out

def search_recent_prs_tool(
    repo_full: str, keywords: list[str], days: int = 7, token: str = "",
) -> list[dict]:
    """Find merged PRs in last N days whose title/body contains any keyword as a token."""
    prs = _list_recent_merged_prs(repo_full, days, token)
    patterns = [re.compile(rf"\b{re.escape(k)}\b") for k in keywords]
    return [
        pr for pr in prs
        if any(p.search(pr["title"] + " " + pr["body"]) for p in patterns)
    ]

def load_contract_tool(path: str) -> dict[str, Any]:
    """Load and return the parsed ops-contract.yaml as a dict."""
    return yaml.safe_load(Path(path).read_text())
```

**Step 3: Commit**

```bash
uv run pytest tests/unit/test_adk_tools.py -v
git add agent/adk_tools.py tests/unit/test_adk_tools.py
git commit -m "feat(agent): ADK tool wrappers — Cloud Run, debug config, PR search, contract"
```

---

### Task 6.2: ADK agent + Runner

**Files:**
- Create: `agent/adk_agent.py`

**Step 1: Implement (no TDD — real integration tested in 6.3)**

```python
# agent/adk_agent.py
import json
import re
import uuid
from typing import Any
from google.adk import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from agent.adk_tools import (
    read_live_env_tool, call_debug_config_tool, search_recent_prs_tool, load_contract_tool,
)
from agent.models import DecisionProposal

SYSTEM_PROMPT = """\
You are DriftScribe, an AI DevOps agent that detects and triages drift between
a deployed Cloud Run service's live configuration and the team's declared
operational contract (ops-contract.yaml).

For each invocation, you must:
1. Call `load_contract_tool` with the contract path.
2. Call `read_live_env_tool` with the service/region/project.
3. Optionally call `call_debug_config_tool` if a /debug/config URL is given.
4. For variables that differ from the contract, call `search_recent_prs_tool`
   with the var names as keywords.
5. Emit a single JSON DecisionProposal — and ONLY that JSON, no prose around it.

Output schema (JSON, no other text):

{
  "action": "docs_pr" | "drift_issue" | "escalation" | "no_op",
  "env_diffs": [
    {
      "name": "STRING",
      "expected": "STRING_OR_NULL",
      "live": "STRING_OR_NULL",
      "contract_status": "absent" | "present_allow_manual" | "present_disallow_manual" | "match",
      "debug_config_value": "STRING_OR_NULL",
      "recent_pr_match": "STRING_OR_NULL"
    }
  ],
  "target_docs_file": "STRING_OR_NULL",
  "target_docs_section": "STRING_OR_NULL",
  "rationale": "STRING",
  "confidence": 0.0_to_1.0,
  "requires_human_review": true_or_false
}

Rules:
- If you cannot reach a tool, say so in `rationale`; do NOT invent values.
- Never propose `docs_pr` for a var whose contract entry says `allow_manual_change: false`.
- Never propose `docs_pr` for a var name containing SECRET, TOKEN, KEY, PASSWORD, CRED, PRIVATE.
- For an absent (not-in-contract) var, only propose `docs_pr` if a recent merged PR
  mentions the EXACT var name (word boundary, case-sensitive). Otherwise `escalation`.
"""

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)

def build_agent() -> Agent:
    return Agent(
        name="driftscribe",
        model="gemini-2.5-flash",
        instruction=SYSTEM_PROMPT,
        tools=[
            read_live_env_tool,
            call_debug_config_tool,
            search_recent_prs_tool,
            load_contract_tool,
        ],
    )

async def run_agent(user_msg: str) -> DecisionProposal:
    agent = build_agent()
    session_service = InMemorySessionService()
    session_id = str(uuid.uuid4())
    await session_service.create_session(
        app_name="driftscribe", user_id="driftscribe-runtime", session_id=session_id,
    )
    runner = Runner(agent=agent, app_name="driftscribe", session_service=session_service)
    msg = types.Content(role="user", parts=[types.Part(text=user_msg)])

    final_text: str | None = None
    async for event in runner.run_async(
        user_id="driftscribe-runtime", session_id=session_id, new_message=msg,
    ):
        if event.is_final_response() and event.content and event.content.parts:
            final_text = event.content.parts[0].text

    if not final_text:
        raise RuntimeError("ADK agent produced no final response")

    # Be tolerant of code fences / leading prose
    m = _JSON_BLOCK.search(final_text)
    raw_json = m.group(0) if m else final_text
    payload = json.loads(raw_json)
    return DecisionProposal.model_validate(payload)
```

**Step 2: Commit**

```bash
git add agent/adk_agent.py
git commit -m "feat(agent): ADK Agent + Runner + InMemorySessionService (correct API)"
```

---

### Task 6.3: Wire ADK into `/recheck` (skip pre-read when USE_ADK)

**Files:**
- Modify: `agent/main.py`

**Step 1: Update `_do_recheck`** to branch on `USE_ADK`. When true, the agent's own tool calls do the Cloud Run read — we don't duplicate it.

```python
# agent/main.py — refactor _do_recheck

async def _do_recheck(trigger: str, force: bool = False) -> dict:
    s = get_settings()
    contract = load_contract(Path(s.contract_path))

    if s.use_adk:
        # ADK agent does its own Cloud Run read via tools
        from agent.adk_agent import run_agent
        user_msg = (
            f"Detect drift for Cloud Run service `{s.target_service}` in region `{s.target_region}` "
            f"(GCP project `{s.gcp_project}`). The contract path is `{s.contract_path}`. "
            f"GitHub repo for PR history is `{s.github_repo}`. "
            f"/debug/config URL: `{s.debug_config_url or 'not provided'}`."
        )
        proposal = await run_agent(user_msg)
        # For idempotency we still need the live env hash — fetch once for the event key
        try:
            live_env = read_live_env(s.target_service, s.target_region, s.gcp_project)
        except Exception:
            live_env = {d.name: d.live or "" for d in proposal.env_diffs}
    else:
        try:
            live_env = read_live_env(s.target_service, s.target_region, s.gcp_project)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"cloud run read failed: {e}")
        proposal = classify(ClassificationInput(
            contract=contract, live_env=live_env, recent_prs=[],
        ))

    validate(proposal, contract)
    # ... rest same as before (idempotency key, render, perform action, record) ...

# Update endpoint to be async
@app.post("/recheck")
async def recheck(force: bool = False):
    return await _do_recheck("manual_recheck", force=force)

@app.post("/eventarc")
async def eventarc():  # stub for Phase 9
    raise HTTPException(status_code=501, detail="Phase 9")
```

**Step 2: Smoke (requires GOOGLE_API_KEY)**

```bash
USE_ADK=true GOOGLE_API_KEY=$GOOGLE_API_KEY DRY_RUN=true \
  GCP_PROJECT=$GCP_PROJECT TARGET_SERVICE=payment-demo \
  uv run uvicorn agent.main:app --port 8080 &
curl -s -X POST http://localhost:8080/recheck | jq .
```

Expected: same response shape as classifier path, plus a less mechanical `rationale`.

**Step 3: Commit**

```bash
git add agent/main.py
git commit -m "feat(api): USE_ADK routes /recheck through ADK agent; no duplicate Cloud Run read"
```

---

## Phase 7 — Demo app (½ day)

The contract + initial runbook already exist (Phase 0.4). This phase adds the runnable FastAPI app.

### Task 7.1: Demo FastAPI app

**Files:**
- Create: `demo/main.py`, `demo/Dockerfile`, `demo/pyproject.toml`

**Step 1: `demo/main.py`**

```python
# demo/main.py
import logging, os
from fastapi import FastAPI

SAFE_KEYS = {"PAYMENT_MODE", "FEATURE_NEW_CHECKOUT", "FEATURE_BETA_UI"}

app = FastAPI(title="payment-demo")

@app.on_event("startup")
async def log_config():
    cfg = {k: os.environ.get(k, "<unset>") for k in SAFE_KEYS}
    logging.info("Runtime config loaded: %s", " ".join(f"{k}={v}" for k, v in cfg.items()))

@app.get("/")
def root():
    return {"service": "payment-demo", "ok": True}

@app.get("/debug/config")
def debug_config():
    return {
        "service": "payment-demo",
        "config": {k: os.environ.get(k, "<unset>") for k in SAFE_KEYS},
        "revision": os.environ.get("K_REVISION", "local"),
    }
```

**Step 2: `demo/pyproject.toml`**

```toml
[project]
name = "payment-demo"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["fastapi>=0.115", "uvicorn[standard]>=0.32"]
```

**Step 3: `demo/Dockerfile`**

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir .
COPY . .
ENV PORT=8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
```

**Step 4: Smoke**

```bash
cd demo && PAYMENT_MODE=mock FEATURE_NEW_CHECKOUT=false \
  uv run uvicorn main:app --port 8081 &
curl -s http://localhost:8081/debug/config | jq .
```

Expected: JSON with `config.PAYMENT_MODE=mock`.

**Step 5: Commit**

```bash
git add demo/main.py demo/pyproject.toml demo/Dockerfile
git commit -m "feat(demo): payment-demo FastAPI app + /debug/config + Dockerfile"
```

---

## Phase 8 — Deploy (gcloud scripts + Secret Manager) (½ day)

No heavy Terraform. Use Cloud Build YAML + Secret Manager for tokens. Terraform stays as a Phase 11 stretch.

**Pre-deploy security hardening (added per Codex Phase-7 review — MUST address before any non-DRY_RUN deploy):**

The deploy below uses `--allow-unauthenticated` on both services. For `payment-demo` that's fine (read-only `/debug/config`). For `driftscribe-agent` with `DRY_RUN=false`, anyone with the URL can call `/recheck` and trigger real GitHub issues/PRs. Pick one mitigation before deploying with `DRY_RUN=false`:

- **Option A (cheapest):** add a shared-secret header check in `agent/main.py::recheck`. Require `X-DriftScribe-Token` matching a Secret-Manager-backed value. Reject otherwise with 401.
- **Option B:** drop `--allow-unauthenticated` and require an IAM-authenticated invoker. Eventarc's service account would need `roles/run.invoker` separately.
- **Option C:** keep DRY_RUN=true for the demo and use `?force=true` curls to refresh decisions on stage. Side effects stay simulated.

Recommendation: **Option C for the live judges-watching demo** (no risk of demo-day randos hitting the URL), then switch to Option A or B post-judging if the project continues.

**Cost-cap reality (per Codex):** GCP budget alerts are NOT a hard cap. To genuinely bound spend:
- Use a fresh GCP project for this hackathon (easy to nuke).
- Set Cloud Run `--min-instances=0 --max-instances=1 --concurrency=1` for both services.
- Set a budget *alert* at $5 and a Pub/Sub-driven kill-switch (Cloud Function that disables billing) only if you're paranoid — extra work, skip unless you've been burned.
- Run `infra/scripts/teardown.sh` (create this; not in the plan yet) to nuke services + delete the project when done.

### Task 8.1: `Dockerfile.agent` + `cloudbuild.yaml`

**Files:**
- Create: `Dockerfile.agent`, `infra/cloudbuild.yaml`, `infra/scripts/setup_secrets.sh`

**Step 1: `Dockerfile.agent`**

```dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml ./
RUN uv pip install --system -e .
COPY agent/ ./agent/
COPY checker/ ./checker/
# Preserve the contract-relative path so DOCS_ROOT=/contract resolves
# `demo/docs/runbook.md` to `/contract/demo/docs/runbook.md`.
COPY demo/ /contract/demo/
ENV PORT=8080
ENV DOCS_ROOT=/contract
ENV CONTRACT_PATH=/contract/demo/ops-contract.yaml
# Shell form so $PORT expands at runtime. Cloud Run injects PORT and expects
# the container to listen on its chosen value; hardcoding --port 8080 would
# break health checks the moment Cloud Run picked a different port.
CMD ["sh", "-c", "uvicorn agent.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
```

**Step 2: `infra/scripts/setup_secrets.sh`** (MUST run before first deploy)

```bash
#!/usr/bin/env bash
set -euo pipefail

PROJECT="${1:?usage: $0 PROJECT GITHUB_TOKEN GOOGLE_API_KEY}"
GITHUB_TOKEN="${2:?}"
GOOGLE_API_KEY="${3:?}"

# Enable APIs (artifactregistry added per Codex Phase-7 review — Cloud Build
# pushes images to Artifact Registry, not the legacy gcr.io bucket)
gcloud services enable --project "$PROJECT" \
  run.googleapis.com \
  eventarc.googleapis.com \
  firestore.googleapis.com \
  secretmanager.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com

# Artifact Registry repo for the agent + demo images (idempotent)
gcloud artifacts repositories describe driftscribe \
  --project "$PROJECT" --location=asia-northeast1 >/dev/null 2>&1 || \
gcloud artifacts repositories create driftscribe \
  --project "$PROJECT" --location=asia-northeast1 --repository-format=docker \
  --description="DriftScribe agent + payment-demo images"

# IAM grants (idempotent — gcloud is happy to re-bind)
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
CLOUDBUILD_SA="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"

# Cloud Build SA needs to push to AR, deploy to Cloud Run, and act-as the
# default compute SA (which runs the deployed services).
for role in \
  roles/artifactregistry.writer \
  roles/run.admin \
  roles/iam.serviceAccountUser \
; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${CLOUDBUILD_SA}" --role="$role" >/dev/null
done

# Default compute SA (which the deployed agent will run as) needs Secret
# Manager + Firestore + Cloud Run read so /recheck can read live state.
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
for role in \
  roles/secretmanager.secretAccessor \
  roles/datastore.user \
  roles/run.viewer \
; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${COMPUTE_SA}" --role="$role" >/dev/null
done

# Create secrets (idempotent)
for secret in github-pat gemini-api-key; do
  gcloud secrets describe "$secret" --project "$PROJECT" >/dev/null 2>&1 || \
    gcloud secrets create "$secret" --project "$PROJECT" --replication-policy=automatic
done

echo -n "$GITHUB_TOKEN"   | gcloud secrets versions add github-pat   --project "$PROJECT" --data-file=-
echo -n "$GOOGLE_API_KEY" | gcloud secrets versions add gemini-api-key --project "$PROJECT" --data-file=-

# Firestore (idempotent)
gcloud firestore databases describe --project "$PROJECT" >/dev/null 2>&1 || \
  gcloud firestore databases create --project "$PROJECT" --location=asia-northeast1 --type=firestore-native

echo "✓ secrets + firestore ready"
```

**Step 3: `infra/cloudbuild.yaml`**

```yaml
# Artifact Registry path (per Codex Phase-7 review — gcr.io is the legacy
# Container Registry; AR is what the IAM grants in setup_secrets.sh authorize).
# Image refs use ${_AR_PATH} so this stays single-source across the file.
substitutions:
  _AR_PATH: 'asia-northeast1-docker.pkg.dev/${PROJECT_ID}/driftscribe'

steps:
  - name: gcr.io/cloud-builders/docker
    args: ['build', '-t', '${_AR_PATH}/driftscribe-agent:${SHORT_SHA}', '-f', 'Dockerfile.agent', '.']
  - name: gcr.io/cloud-builders/docker
    args: ['build', '-t', '${_AR_PATH}/payment-demo:${SHORT_SHA}', '-f', 'demo/Dockerfile', 'demo']
  - name: gcr.io/cloud-builders/docker
    args: ['push', '${_AR_PATH}/driftscribe-agent:${SHORT_SHA}']
  - name: gcr.io/cloud-builders/docker
    args: ['push', '${_AR_PATH}/payment-demo:${SHORT_SHA}']

  - name: gcr.io/google.com/cloudsdktool/cloud-sdk
    entrypoint: gcloud
    args:
      - run
      - deploy
      - payment-demo
      - --image=${_AR_PATH}/payment-demo:${SHORT_SHA}
      - --region=asia-northeast1
      - --allow-unauthenticated
      - --min-instances=0
      - --max-instances=1
      - --concurrency=1
      - --set-env-vars=PAYMENT_MODE=mock,FEATURE_NEW_CHECKOUT=false

  - name: gcr.io/google.com/cloudsdktool/cloud-sdk
    entrypoint: gcloud
    args:
      - run
      - deploy
      - driftscribe-agent
      - --image=${_AR_PATH}/driftscribe-agent:${SHORT_SHA}
      - --region=asia-northeast1
      # SECURITY: see Phase 8 pre-deploy note. For the live demo we leave
      # --allow-unauthenticated + DRY_RUN=true (Option C). Flip to DRY_RUN=false
      # ONLY after adding the X-DriftScribe-Token guard (Option A) or removing
      # --allow-unauthenticated (Option B).
      - --allow-unauthenticated
      - --min-instances=0
      - --max-instances=1
      - --concurrency=1
      - --set-env-vars=DRY_RUN=true,GCP_PROJECT=$PROJECT_ID,TARGET_SERVICE=payment-demo,TARGET_REGION=asia-northeast1,GITHUB_REPO=adi-prasetyo/driftscribe,USE_ADK=true
      - --set-secrets=GITHUB_TOKEN=github-pat:latest,GOOGLE_API_KEY=gemini-api-key:latest
```

**Step 4: Manual deploy**

```bash
chmod +x infra/scripts/setup_secrets.sh
infra/scripts/setup_secrets.sh "$GCP_PROJECT" "$GITHUB_TOKEN" "$GOOGLE_API_KEY"
gcloud builds submit --project "$GCP_PROJECT" --config=infra/cloudbuild.yaml
```

**Step 5: Verify**

```bash
gcloud run services list --region asia-northeast1 --project "$GCP_PROJECT"
curl -s "$(gcloud run services describe payment-demo --region asia-northeast1 --project "$GCP_PROJECT" --format='value(status.url)')/debug/config" | jq .
```

Expected: both services listed, `payment-demo` `/debug/config` returns JSON.

**Step 6: Commit**

```bash
git add Dockerfile.agent infra/cloudbuild.yaml infra/scripts/setup_secrets.sh
git commit -m "infra: gcloud-based deploy + Secret Manager bindings"
```

---

### Task 8.2: GitHub Actions CI

**Files:**
- Create: `.github/workflows/ci.yml`, `.github/workflows/docs-check.yml`

**Step 1: `ci.yml`**

```yaml
name: ci
on:
  push: { branches: [main] }
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv venv && uv pip install -e ".[dev]"
      - run: uv run ruff check .
      - run: uv run pytest -v
```

**Step 2: `docs-check.yml`** — the demoable consistency gate

```yaml
name: docs-check
on:
  pull_request:
    paths:
      - "demo/**"
      - "demo/ops-contract.yaml"
jobs:
  driftscribe-check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv venv && uv pip install -e ".[dev]"
      - run: uv run driftscribe-check --contract demo/ops-contract.yaml --repo-root .
```

**Step 3: Push branch, open PR to trigger workflows, verify both green.**

**Step 4: Commit**

```bash
git add .github/workflows/
git commit -m "ci: tests + driftscribe-check on PRs"
```

---

## Phase 9 — Eventarc (1 day) — 9.1 is a MANDATORY GATE

### Task 9.1 (MANDATORY GATE): Inspect real Audit Log values

**DO NOT proceed to 9.2 without completing this task.** Guessed filter values were Codex's #1 Eventarc warning.

**Files:**
- Create: `infra/scripts/inspect_audit_log.sh`

**Step 1: Write script**

```bash
#!/usr/bin/env bash
# Triggers a real env-var change, then dumps the resulting audit log entry so
# we can copy the exact serviceName / methodName / resourceName into Terraform.
set -euo pipefail

PROJECT="${1:?usage: $0 PROJECT [SERVICE] [REGION]}"
SERVICE="${2:-payment-demo}"
REGION="${3:-asia-northeast1}"

echo "Triggering a no-op env-var update..."
gcloud run services update "$SERVICE" \
  --region "$REGION" \
  --project "$PROJECT" \
  --update-env-vars "_DRIFTSCRIBE_PROBE=$(date +%s)"

echo "Waiting for audit log propagation (up to 60s)..."
for i in $(seq 1 12); do
  sleep 5
  result=$(gcloud logging read \
    'protoPayload.serviceName="run.googleapis.com" AND protoPayload.methodName=~"UpdateService"' \
    --project "$PROJECT" --limit 1 --format json \
    --freshness=2m 2>/dev/null || true)
  if [ "$(echo "$result" | jq 'length')" -gt 0 ]; then
    break
  fi
  echo "  (attempt $i: no entries yet)"
done

echo "Latest matching entry:"
echo "$result" | jq '.[0] | {
  serviceName: .protoPayload.serviceName,
  methodName: .protoPayload.methodName,
  resourceName: .protoPayload.resourceName,
  resource_type: .resource.type
}'
echo ""
echo "Copy these values into the Eventarc trigger filter (Task 9.2)."
```

**Step 2: Run**

```bash
chmod +x infra/scripts/inspect_audit_log.sh
infra/scripts/inspect_audit_log.sh "$GCP_PROJECT"
```

**Step 3:** record the actual `methodName` (likely `google.cloud.run.v2.Services.UpdateService` but VERIFY) and `resourceName` (e.g. `projects/.../services/payment-demo`) in `infra/scripts/audit_log_values.txt` for reference.

**Step 4: Commit**

```bash
git add infra/scripts/inspect_audit_log.sh infra/scripts/audit_log_values.txt
git commit -m "infra: audit log inspection script + recorded real filter values"
```

---

### Task 9.2: Eventarc trigger via gcloud (no Terraform)

**Files:**
- Create: `infra/scripts/create_eventarc_trigger.sh`

**Step 1: Write script using the values from 9.1**

```bash
#!/usr/bin/env bash
set -euo pipefail

PROJECT="${1:?usage: $0 PROJECT [METHOD_NAME]}"
METHOD_NAME="${2:-google.cloud.run.v2.Services.UpdateService}"  # from 9.1
REGION="asia-northeast1"
TARGET_SERVICE="payment-demo"

SA_EMAIL="eventarc-invoker@${PROJECT}.iam.gserviceaccount.com"

# Create SA if missing
gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT" >/dev/null 2>&1 || \
  gcloud iam service-accounts create eventarc-invoker --project "$PROJECT"

# Grant invoker on agent service
gcloud run services add-iam-policy-binding driftscribe-agent \
  --region "$REGION" --project "$PROJECT" \
  --member "serviceAccount:$SA_EMAIL" \
  --role "roles/run.invoker"

# Grant eventarc receiver
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member "serviceAccount:$SA_EMAIL" --role "roles/eventarc.eventReceiver"

# Create trigger — scope to our target service only via resourceName
gcloud eventarc triggers create driftscribe-cloud-run-config-change \
  --project "$PROJECT" --location "$REGION" \
  --destination-run-service driftscribe-agent \
  --destination-run-region "$REGION" \
  --destination-run-path "/eventarc" \
  --event-filters "type=google.cloud.audit.log.v1.written" \
  --event-filters "serviceName=run.googleapis.com" \
  --event-filters "methodName=$METHOD_NAME" \
  --event-filters-path-pattern "resourceName=projects/${PROJECT}/locations/${REGION}/services/${TARGET_SERVICE}" \
  --service-account "$SA_EMAIL"
```

**Step 2: Commit**

```bash
git add infra/scripts/create_eventarc_trigger.sh
git commit -m "infra: eventarc trigger creation (scoped to payment-demo by resourceName)"
```

---

### Task 9.3: `/eventarc` endpoint

**Files:**
- Modify: `agent/main.py`

**Step 1: Wire endpoint**

```python
# agent/main.py — replace stub

@app.post("/eventarc")
async def eventarc_handler(req: Request):
    body = await req.json()
    event_id = req.headers.get("ce-id") or body.get("id", "unknown")

    state = get_state()
    existing = state.find_decision_for_event(event_id)
    if existing:
        return existing

    state.record_event(event_id, {"trigger": "eventarc", "raw": body})
    # Always re-read live Cloud Run, never trust the event payload values
    return await _do_recheck("eventarc", force=True)
```

(Update `_do_recheck` to accept an optional pre-known event_key for eventarc reuse — small refactor; pseudocode shown.)

**Step 2: Redeploy and verify end-to-end**

```bash
gcloud builds submit --project "$GCP_PROJECT" --config=infra/cloudbuild.yaml
infra/scripts/create_eventarc_trigger.sh "$GCP_PROJECT"

# Trigger a real change
gcloud run services update payment-demo --region asia-northeast1 \
  --project "$GCP_PROJECT" \
  --update-env-vars FEATURE_NEW_CHECKOUT=true

# Wait ~30s, then check the agent's logs for an /eventarc invocation
gcloud run services logs read driftscribe-agent --region asia-northeast1 --project "$GCP_PROJECT" --limit 50
```

Expected: log line showing `/eventarc` received an event and recorded a decision.

**Step 3: Commit**

```bash
git add agent/main.py
git commit -m "feat(api): /eventarc handler (idempotent; always re-reads live Cloud Run)"
```

---

## Phase 10 — Submission deliverables (½ day)

These are what the hackathon actually judges. Don't skip.

### Task 10.1: `README.md` (full)

Replace the placeholder. Must include: tagline, problem, demo flow, architecture, repo layout, how to reproduce, links to ProtoPedia and demo URL.

### Task 10.2: `docs/demo-script.md`

The 5-beat demo with exact gcloud commands and what to point at on screen:

| Beat | Command | What to show |
|---|---|---|
| 0. Bootstrap | `driftscribe init --service payment-demo --region asia-northeast1 --project $GCP_PROJECT --github-repo adi-prasetyo/driftscribe --output demo/ops-contract.yaml --docs-file demo/docs/runbook.md` | The generated YAML + a manually-opened PR. |
| A. Sanctioned | `gcloud run services update payment-demo --update-env-vars FEATURE_NEW_CHECKOUT=true` then `curl -s -X POST $AGENT_URL/recheck` | The docs PR DriftScribe opens. |
| B. Unsanctioned | `gcloud run services update payment-demo --update-env-vars PAYMENT_MODE=live` then `curl /recheck` | The drift issue. |
| C. Uncertain | `gcloud run services update payment-demo --update-env-vars NEW_THING=x` then `curl /recheck` | The escalation issue with evidence table. |
| D. CI gate | Open a PR that deletes the `FEATURE_NEW_CHECKOUT` line from `demo/docs/runbook.md`. CI fails. Then DriftScribe's Beat A PR merges and CI is green. | The red X turning green. |

### Task 10.3: `docs/protopedia.md`

Title, tagline, problem statement, architecture diagram (PNG), repo URL, demo URL, demo video URL.

### Task 10.4: 90-second demo video outline

```
0:00–0:10  Tagline: "GitOps reconciles infrastructure. DriftScribe reconciles knowledge about infrastructure."
0:10–0:20  Problem: 3am incident, runbook says PAYMENT_MODE=mock, prod is live.
0:20–0:35  Beat 0 — bootstrap: agent reads live env, writes contract, opens PR.
0:35–0:50  Beat A — sanctioned: flip FEATURE_NEW_CHECKOUT, docs PR appears.
0:50–1:05  Beat B — unsanctioned: flip PAYMENT_MODE, drift issue appears (no docs change).
1:05–1:20  Beat C — uncertain: add NEW_THING, escalation issue with evidence table.
1:20–1:30  Beat D — CI gate fails on stale docs, passes after the agent's PR.
```

### Task 10.5: Screenshot collection

Capture and commit under `docs/screenshots/`: docs PR diff, drift issue, escalation issue with evidence table, CI red X, CI green check, `/runs/{id}` JSON.

### Task 10.6: Public-repo cleanup checklist

Before flipping `adi-prasetyo/driftscribe` to public:
- Revoke any GitHub tokens used during build (Settings → Developer settings → Personal access tokens).
- Verify `.env` is gitignored and never committed (`git log --all --full-history -- .env`).
- Verify no `GOOGLE_API_KEY` ended up in commit history.
- Rotate any tokens that touched the repo as a precaution.
- Add a clear `LICENSE` (MIT is fine for a hackathon).

### Task 10.7: Commit Phase 10

```bash
git add README.md docs/demo-script.md docs/protopedia.md docs/screenshots/ LICENSE
git commit -m "docs(submission): README, demo script, ProtoPedia draft, screenshots, cleanup checklist"
```

---

## Stretch (if budget allows)

- Phase 11: Terraform port of the gcloud scripts (for the "production-grade" narrative).
- Phase 12: `/driftscribe sanction` comment hook on escalation issues.
- Phase 13: Stale-PR follow-up — close agent's own PRs after 7d unreviewed.
- Phase 14: Cloud Audit Log → drift for IAM bindings / Cloud SQL config / secret rotations.
