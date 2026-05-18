# DriftScribe MVP Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Apply @superpowers:test-driven-development for every code task and @superpowers:verification-before-completion before claiming any task done.

**Goal:** Ship a working AI DevOps agent ("DriftScribe") for the Google Cloud Japan / Findy DevOps × AI Agent Hackathon 2026. The agent watches live Cloud Run config, compares it against a declared operational contract (`ops-contract.yaml`), and either updates the runbook via a GitHub PR, opens a drift issue, or opens an escalation issue — depending on whether the change is sanctioned.

**Architecture:** Three-layer system. (1) ADK agent shell with four tools (Cloud Run reader, debug-config caller, GitHub history search, contract loader) emits a structured `DecisionProposal`. (2) Deterministic validator gates that proposal against contract rules + path allowlist + secret-leak guard. (3) Action layer creates the GitHub PR/issue. State + idempotency in Firestore. Triggered manually via `POST /recheck` (primary demo path) and by Audit Log → Eventarc → `/eventarc` (production path, final phase).

**Tech Stack:** Python 3.12, FastAPI, Google ADK (`google-adk`), Gemini 2.5 Flash, `google-cloud-run` admin client, `google-cloud-firestore`, PyGithub, pydantic v2, pydantic-settings, uv for dependency management, Terraform for infra, GitHub Actions for CI. Public repo: `github.com/theghostsquad00/driftscribe` (private during build).

**Hackathon constraints:** ~6 dev days, 1 person. Total Phase 0–9. Demo flow is 5 beats (bootstrap → sanctioned → unsanctioned → uncertain → CI gate). Eventarc is the LAST phase — the demo must still work via `/recheck` if Eventarc slips.

---

## Operating Principles (read before starting)

- **Verify before claiming done.** Every task ends with a verification command. Don't say "passes" — show the output. See @superpowers:verification-before-completion.
- **DRY_RUN is the default.** All side-effecting actions (PR creation, issue creation, Firestore writes) check `DRY_RUN=true` first. Tests run in DRY_RUN. Demo flips to false.
- **Tests use fakes, not the network.** No test should call real GCP, real GitHub, or real Gemini. Use `pytest` fixtures with `respx` for HTTP, in-memory fake stores for Firestore.
- **Commit every passing task.** No half-done commits. If a task takes too long, split it.
- **Don't touch Eventarc until Phase 9.** Resist the urge.

---

## Phase 0 — Repo skeleton (½ day)

### Task 0.1: Create directory structure

**Files:** All new.

**Step 1: Run from repo root**

```bash
cd ~/driftscribe
mkdir -p agent checker demo/docs infra/terraform scripts tests/unit tests/integration .github/workflows docs/plans
touch agent/__init__.py checker/__init__.py tests/__init__.py tests/unit/__init__.py tests/integration/__init__.py
```

**Step 2: Verify**

```bash
tree -L 2 -I '.git'
```

Expected: matches the scaffold tree from the architecture doc.

**Step 3: Commit**

```bash
git add -A
git commit -m "chore: scaffold repo directories"
```

---

### Task 0.2: Set up `pyproject.toml` (uv)

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
    "google-adk>=0.2",
    "google-cloud-run>=0.10",
    "google-cloud-firestore>=2.19",
    "google-cloud-logging>=3.11",
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
    "mypy>=1.13",
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

**Step 2: Install with uv**

```bash
uv venv
uv pip install -e ".[dev]"
```

Expected: success, `.venv/` created.

**Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "chore: pyproject.toml with uv + agent/checker deps"
```

---

### Task 0.3: Create `.gitignore`, `README.md`, `Makefile`

**Files:** Create three files.

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
.terraform/
*.tfstate
*.tfstate.backup
.DS_Store
```

**Step 2: `README.md`** (minimal — full one in Phase 8)

```markdown
# DriftScribe

AI DevOps agent for live Cloud Run drift detection. Submission for DevOps × AI Agent Hackathon 2026 (Google Cloud Japan / Findy).

**Status:** Under construction.

See `docs/architecture.md` and `docs/plans/2026-05-18-driftscribe-mvp.md`.
```

**Step 3: `Makefile`**

```makefile
.PHONY: install test lint typecheck run-agent run-demo dry-recheck

install:
	uv pip install -e ".[dev]"

test:
	pytest -v

lint:
	ruff check .

typecheck:
	mypy agent checker

run-agent:
	DRY_RUN=true uvicorn agent.main:app --reload --port 8080

run-demo:
	uvicorn demo.main:app --reload --port 8081

dry-recheck:
	curl -s -X POST http://localhost:8080/recheck | jq .
```

**Step 4: Commit**

```bash
git add .gitignore README.md Makefile
git commit -m "chore: gitignore, README placeholder, Makefile"
```

---

## Phase 1 — Pure logic (½ day)

All TDD. No cloud, no network, no LLM.

### Task 1.1: Contract schema (TDD)

**Files:**
- Create: `agent/contract.py`
- Test: `tests/unit/test_contract.py`

**Step 1: Write failing test**

```python
# tests/unit/test_contract.py
import pytest
from agent.contract import OpsContract, EnvVarRule, load_contract

VALID_YAML = """
service: payment-demo
environment: production
cloud_run_service: payment-demo
region: asia-northeast1
github_repo: theghostsquad00/driftscribe
expected_env:
  PAYMENT_MODE:
    value: mock
    docs:
      file: docs/runbook.md
      section: Runtime Configuration
    allow_manual_change: false
  FEATURE_NEW_CHECKOUT:
    value: "false"
    docs:
      file: docs/runbook.md
      section: Feature Flags
    allow_manual_change: true
"""

def test_contract_parses_valid_yaml(tmp_path):
    p = tmp_path / "ops-contract.yaml"
    p.write_text(VALID_YAML)
    contract = load_contract(p)
    assert contract.service == "payment-demo"
    assert contract.cloud_run_service == "payment-demo"
    assert contract.region == "asia-northeast1"
    assert contract.expected_env["PAYMENT_MODE"].value == "mock"
    assert contract.expected_env["PAYMENT_MODE"].allow_manual_change is False
    assert contract.expected_env["FEATURE_NEW_CHECKOUT"].allow_manual_change is True

def test_contract_rejects_missing_required_fields(tmp_path):
    p = tmp_path / "ops-contract.yaml"
    p.write_text("service: x\n")  # missing cloud_run_service, region, etc.
    with pytest.raises(Exception):
        load_contract(p)
```

**Step 2: Run failing test**

```bash
pytest tests/unit/test_contract.py -v
```

Expected: `ImportError: cannot import name 'OpsContract' from 'agent.contract'`.

**Step 3: Implement**

```python
# agent/contract.py
from pathlib import Path
from typing import Dict
import yaml
from pydantic import BaseModel, Field

class DocsRef(BaseModel):
    file: str
    section: str

class EnvVarRule(BaseModel):
    value: str
    docs: DocsRef
    allow_manual_change: bool = False

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

**Step 4: Run tests**

```bash
pytest tests/unit/test_contract.py -v
```

Expected: 2 passed.

**Step 5: Commit**

```bash
git add agent/contract.py tests/unit/test_contract.py
git commit -m "feat(contract): ops-contract.yaml schema + loader"
```

---

### Task 1.2: Decision data model (TDD)

**Files:**
- Create: `agent/models.py`
- Test: `tests/unit/test_models.py`

**Step 1: Failing test**

```python
# tests/unit/test_models.py
import pytest
from agent.models import DecisionProposal, DecisionAction, EnvDiff, Evidence

def test_decision_proposal_serialises_to_dict():
    p = DecisionProposal(
        action=DecisionAction.DOCS_PR,
        env_diffs=[EnvDiff(name="FEATURE_NEW_CHECKOUT", old="false", new="true")],
        evidence=Evidence(
            live_value="true",
            contract_status="present_allow_manual",
            debug_config_value="true",
            recent_pr_match=None,
        ),
        rationale="FEATURE_NEW_CHECKOUT is in contract with allow_manual_change=true; documenting new value.",
        confidence=0.92,
    )
    d = p.model_dump()
    assert d["action"] == "docs_pr"
    assert d["env_diffs"][0]["name"] == "FEATURE_NEW_CHECKOUT"

def test_decision_action_enum_values():
    assert DecisionAction.DOCS_PR.value == "docs_pr"
    assert DecisionAction.DRIFT_ISSUE.value == "drift_issue"
    assert DecisionAction.ESCALATION.value == "escalation"
    assert DecisionAction.NO_OP.value == "no_op"
```

**Step 2: Run failing test**

```bash
pytest tests/unit/test_models.py -v
```

Expected: ImportError.

**Step 3: Implement**

```python
# agent/models.py
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel

class DecisionAction(str, Enum):
    DOCS_PR = "docs_pr"
    DRIFT_ISSUE = "drift_issue"
    ESCALATION = "escalation"
    NO_OP = "no_op"

class EnvDiff(BaseModel):
    name: str
    old: Optional[str]
    new: Optional[str]

class Evidence(BaseModel):
    live_value: Optional[str]
    contract_status: str  # "absent" | "present_allow_manual" | "present_disallow_manual"
    debug_config_value: Optional[str] = None
    recent_pr_match: Optional[str] = None  # PR URL if found

class DecisionProposal(BaseModel):
    action: DecisionAction
    env_diffs: List[EnvDiff]
    evidence: Evidence
    rationale: str
    confidence: float
```

**Step 4: Run tests**

```bash
pytest tests/unit/test_models.py -v
```

Expected: 2 passed.

**Step 5: Commit**

```bash
git add agent/models.py tests/unit/test_models.py
git commit -m "feat(models): DecisionProposal + Evidence + DecisionAction"
```

---

### Task 1.3: Classifier (sanctioned / unsanctioned / uncertain) (TDD)

**Files:**
- Create: `agent/classifier.py`
- Test: `tests/unit/test_classifier.py`

**Step 1: Failing test**

```python
# tests/unit/test_classifier.py
from agent.classifier import classify, ClassificationInput
from agent.contract import OpsContract, EnvVarRule, DocsRef
from agent.models import DecisionAction

def _contract(env_rules):
    return OpsContract(
        service="payment-demo",
        environment="production",
        cloud_run_service="payment-demo",
        region="asia-northeast1",
        github_repo="theghostsquad00/driftscribe",
        expected_env=env_rules,
    )

def _rule(value="x", allow=False):
    return EnvVarRule(
        value=value,
        docs=DocsRef(file="docs/runbook.md", section="S"),
        allow_manual_change=allow,
    )

def test_sanctioned_change_allow_manual():
    contract = _contract({"FEATURE_X": _rule("false", allow=True)})
    live = {"FEATURE_X": "true"}
    out = classify(ClassificationInput(contract=contract, live_env=live, recent_prs=[]))
    assert out.action == DecisionAction.DOCS_PR

def test_unsanctioned_drift_disallow_manual():
    contract = _contract({"PAYMENT_MODE": _rule("mock", allow=False)})
    live = {"PAYMENT_MODE": "live"}
    out = classify(ClassificationInput(contract=contract, live_env=live, recent_prs=[]))
    assert out.action == DecisionAction.DRIFT_ISSUE

def test_uncertain_when_var_not_in_contract():
    contract = _contract({})
    live = {"NEW_THING": "x"}
    out = classify(ClassificationInput(contract=contract, live_env=live, recent_prs=[]))
    assert out.action == DecisionAction.ESCALATION

def test_no_op_when_live_matches_contract():
    contract = _contract({"PAYMENT_MODE": _rule("mock", allow=False)})
    live = {"PAYMENT_MODE": "mock"}
    out = classify(ClassificationInput(contract=contract, live_env=live, recent_prs=[]))
    assert out.action == DecisionAction.NO_OP

def test_recent_pr_promotes_uncertain_to_sanctioned():
    # A new env var with a recent PR mentioning it = sanctioned, not escalation
    contract = _contract({})
    live = {"NEW_THING": "x"}
    out = classify(ClassificationInput(
        contract=contract,
        live_env=live,
        recent_prs=[{"url": "https://...", "title": "Add NEW_THING flag", "body": "Adds NEW_THING=x"}],
    ))
    assert out.action == DecisionAction.DOCS_PR
```

**Step 2: Run failing test**

```bash
pytest tests/unit/test_classifier.py -v
```

Expected: ImportError.

**Step 3: Implement**

```python
# agent/classifier.py
from typing import Dict, List, Any
from pydantic import BaseModel
from agent.contract import OpsContract
from agent.models import DecisionProposal, DecisionAction, EnvDiff, Evidence

class ClassificationInput(BaseModel):
    contract: OpsContract
    live_env: Dict[str, str]
    recent_prs: List[Dict[str, Any]] = []

def _pr_mentions(prs: List[Dict[str, Any]], var_name: str) -> str | None:
    for pr in prs:
        haystack = f"{pr.get('title','')} {pr.get('body','')}".lower()
        if var_name.lower() in haystack:
            return pr.get("url")
    return None

def classify(inp: ClassificationInput) -> DecisionProposal:
    diffs: list[EnvDiff] = []
    decisions: list[DecisionAction] = []
    primary_evidence: Evidence | None = None

    contract_vars = set(inp.contract.expected_env.keys())
    live_vars = set(inp.live_env.keys())

    # Compute symmetric diff
    for name in contract_vars | live_vars:
        expected = inp.contract.expected_env.get(name)
        live_val = inp.live_env.get(name)
        expected_val = expected.value if expected else None

        if expected and live_val == expected_val:
            continue  # no change

        diff = EnvDiff(name=name, old=expected_val, new=live_val)
        diffs.append(diff)

        if expected is None:
            # Live has a var not in contract → uncertain unless recent PR mentions it
            pr_url = _pr_mentions(inp.recent_prs, name)
            if pr_url:
                decisions.append(DecisionAction.DOCS_PR)
                primary_evidence = Evidence(
                    live_value=live_val,
                    contract_status="absent",
                    recent_pr_match=pr_url,
                )
            else:
                decisions.append(DecisionAction.ESCALATION)
                primary_evidence = Evidence(
                    live_value=live_val,
                    contract_status="absent",
                )
        else:
            # In contract; check allow_manual_change
            if expected.allow_manual_change:
                decisions.append(DecisionAction.DOCS_PR)
                primary_evidence = Evidence(
                    live_value=live_val,
                    contract_status="present_allow_manual",
                )
            else:
                decisions.append(DecisionAction.DRIFT_ISSUE)
                primary_evidence = Evidence(
                    live_value=live_val,
                    contract_status="present_disallow_manual",
                )

    if not decisions:
        return DecisionProposal(
            action=DecisionAction.NO_OP,
            env_diffs=[],
            evidence=Evidence(live_value=None, contract_status="match"),
            rationale="Live state matches contract.",
            confidence=1.0,
        )

    # If any drift_issue, that wins (most serious); else escalation > docs_pr
    priority = [DecisionAction.DRIFT_ISSUE, DecisionAction.ESCALATION, DecisionAction.DOCS_PR]
    chosen = next(p for p in priority if p in decisions)

    rationale_map = {
        DecisionAction.DOCS_PR: "Change is sanctioned (allow_manual_change=true or recent PR mention); updating docs.",
        DecisionAction.DRIFT_ISSUE: "Change violates contract (allow_manual_change=false); refusing to document.",
        DecisionAction.ESCALATION: "Var not in contract and no recent PR mention; escalating for review.",
    }

    return DecisionProposal(
        action=chosen,
        env_diffs=diffs,
        evidence=primary_evidence,  # type: ignore[arg-type]
        rationale=rationale_map[chosen],
        confidence=0.85,  # deterministic classifier; LLM may overwrite later
    )
```

**Step 4: Run tests**

```bash
pytest tests/unit/test_classifier.py -v
```

Expected: 5 passed.

**Step 5: Commit**

```bash
git add agent/classifier.py tests/unit/test_classifier.py
git commit -m "feat(classifier): deterministic sanctioned/unsanctioned/uncertain logic"
```

---

### Task 1.4: Deterministic validator (TDD)

This is the post-LLM safety gate. It accepts a `DecisionProposal` (whether from classifier or LLM) and rejects unsafe ones.

**Files:**
- Create: `agent/validator.py`
- Test: `tests/unit/test_validator.py`

**Step 1: Failing test**

```python
# tests/unit/test_validator.py
import pytest
from agent.validator import validate, ValidationError
from agent.models import DecisionProposal, DecisionAction, EnvDiff, Evidence
from agent.contract import OpsContract, EnvVarRule, DocsRef

def _contract():
    return OpsContract(
        service="payment-demo",
        environment="production",
        cloud_run_service="payment-demo",
        region="asia-northeast1",
        github_repo="theghostsquad00/driftscribe",
        expected_env={
            "PAYMENT_MODE": EnvVarRule(
                value="mock",
                docs=DocsRef(file="docs/runbook.md", section="Runtime Configuration"),
                allow_manual_change=False,
            ),
        },
    )

def _good_proposal():
    return DecisionProposal(
        action=DecisionAction.DRIFT_ISSUE,
        env_diffs=[EnvDiff(name="PAYMENT_MODE", old="mock", new="live")],
        evidence=Evidence(live_value="live", contract_status="present_disallow_manual"),
        rationale="Contract disallows manual change.",
        confidence=0.9,
    )

def test_validator_passes_correct_proposal():
    validate(_good_proposal(), _contract())  # no raise

def test_validator_rejects_docs_pr_when_contract_disallows_manual():
    p = _good_proposal()
    p.action = DecisionAction.DOCS_PR
    with pytest.raises(ValidationError, match="allow_manual_change=False"):
        validate(p, _contract())

def test_validator_rejects_secret_like_value_in_diff():
    p = _good_proposal()
    p.env_diffs = [EnvDiff(name="API_KEY", old="oldsecret", new="newsecret")]
    p.action = DecisionAction.DOCS_PR
    with pytest.raises(ValidationError, match="secret"):
        validate(p, _contract())

def test_validator_rejects_unknown_action():
    p = _good_proposal()
    # Bypass enum validation by direct assignment
    p.__dict__["action"] = "delete_repo"
    with pytest.raises(ValidationError):
        validate(p, _contract())
```

**Step 2: Run failing test**

```bash
pytest tests/unit/test_validator.py -v
```

**Step 3: Implement**

```python
# agent/validator.py
import re
from agent.models import DecisionProposal, DecisionAction
from agent.contract import OpsContract

class ValidationError(Exception):
    pass

# Heuristic — refined over time. For MVP, reject anything resembling a credential.
_SECRET_NAME_PATTERN = re.compile(
    r"(SECRET|TOKEN|KEY|PASSWORD|PASSWD|CRED|PRIVATE)",
    re.IGNORECASE,
)

def validate(proposal: DecisionProposal, contract: OpsContract) -> None:
    """Raise ValidationError if proposal violates safety rules."""

    # 1. Action must be a known enum value
    if not isinstance(proposal.action, DecisionAction):
        try:
            DecisionAction(proposal.action)
        except ValueError:
            raise ValidationError(f"Unknown action: {proposal.action!r}")

    # 2. If docs_pr, every diff'd var must EITHER be absent from contract
    #    (uncertain → docs_pr requires recent_pr_match) OR be allow_manual_change=True.
    if proposal.action == DecisionAction.DOCS_PR:
        for diff in proposal.env_diffs:
            rule = contract.expected_env.get(diff.name)
            if rule is None:
                if not proposal.evidence.recent_pr_match:
                    raise ValidationError(
                        f"docs_pr for unknown var {diff.name!r} requires recent_pr_match evidence"
                    )
            elif not rule.allow_manual_change:
                raise ValidationError(
                    f"docs_pr for {diff.name!r} rejected: contract says allow_manual_change=False"
                )

    # 3. Secret-leak guard: never emit docs_pr containing a secret-like var name
    if proposal.action == DecisionAction.DOCS_PR:
        for diff in proposal.env_diffs:
            if _SECRET_NAME_PATTERN.search(diff.name):
                raise ValidationError(
                    f"refusing docs_pr that would document secret-like var {diff.name!r}"
                )
```

**Step 4: Run tests**

```bash
pytest tests/unit/test_validator.py -v
```

Expected: 4 passed.

**Step 5: Commit**

```bash
git add agent/validator.py tests/unit/test_validator.py
git commit -m "feat(validator): deterministic safety gate for DecisionProposal"
```

---

### Task 1.5: Checker CLI (TDD)

The consistency-gate CLI that runs in GitHub Actions on every PR. Verifies docs cover the contract.

**Files:**
- Create: `checker/cli.py`
- Test: `tests/unit/test_checker.py`

**Step 1: Failing test**

```python
# tests/unit/test_checker.py
from pathlib import Path
from checker.cli import check_docs_cover_contract, CheckResult

CONTRACT = """
service: payment-demo
environment: production
cloud_run_service: payment-demo
region: asia-northeast1
github_repo: theghostsquad00/driftscribe
expected_env:
  PAYMENT_MODE:
    value: mock
    docs:
      file: docs/runbook.md
      section: Runtime Configuration
    allow_manual_change: false
"""

GOOD_RUNBOOK = """
# Runbook

## Runtime Configuration

This service uses `PAYMENT_MODE=mock` in production.
"""

BAD_RUNBOOK = """
# Runbook

## Some Other Section

Nothing about env vars here.
"""

def test_check_passes_when_all_vars_documented(tmp_path):
    contract = tmp_path / "ops-contract.yaml"
    contract.write_text(CONTRACT)
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "runbook.md").write_text(GOOD_RUNBOOK)
    result = check_docs_cover_contract(contract_path=contract, repo_root=tmp_path)
    assert result.ok, result.failures

def test_check_fails_when_var_missing_from_docs(tmp_path):
    contract = tmp_path / "ops-contract.yaml"
    contract.write_text(CONTRACT)
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "runbook.md").write_text(BAD_RUNBOOK)
    result = check_docs_cover_contract(contract_path=contract, repo_root=tmp_path)
    assert not result.ok
    assert any("PAYMENT_MODE" in f for f in result.failures)
```

**Step 2: Run failing test**

```bash
pytest tests/unit/test_checker.py -v
```

**Step 3: Implement**

```python
# checker/cli.py
from dataclasses import dataclass, field
from pathlib import Path
from typing import List
import sys
import typer
import yaml

app = typer.Typer(help="DriftScribe consistency gate")

@dataclass
class CheckResult:
    ok: bool
    failures: List[str] = field(default_factory=list)

def check_docs_cover_contract(contract_path: Path, repo_root: Path) -> CheckResult:
    contract = yaml.safe_load(contract_path.read_text())
    failures: list[str] = []
    for var_name, rule in contract.get("expected_env", {}).items():
        docs_file = repo_root / rule["docs"]["file"]
        if not docs_file.exists():
            failures.append(f"{var_name}: docs file missing at {docs_file}")
            continue
        content = docs_file.read_text()
        if var_name not in content:
            failures.append(f"{var_name}: not mentioned in {docs_file}")
            continue
        section = rule["docs"]["section"]
        if section not in content:
            failures.append(f"{var_name}: docs section '{section}' not found in {docs_file}")
    return CheckResult(ok=len(failures) == 0, failures=failures)

@app.command()
def check(
    contract: Path = typer.Option(Path("ops-contract.yaml"), help="Path to ops-contract.yaml"),
    repo_root: Path = typer.Option(Path("."), help="Repo root for resolving doc paths"),
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

**Step 4: Run tests**

```bash
pytest tests/unit/test_checker.py -v
```

Expected: 2 passed.

**Step 5: Manual smoke**

```bash
cd /tmp && rm -rf checktest && mkdir -p checktest/docs && cd checktest
cat > ops-contract.yaml <<'EOF'
service: x
environment: production
cloud_run_service: x
region: asia-northeast1
github_repo: x/x
expected_env:
  FOO:
    value: "1"
    docs: { file: docs/runbook.md, section: Config }
    allow_manual_change: true
EOF
echo "## Config" > docs/runbook.md
echo "FOO=1" >> docs/runbook.md
cd ~/driftscribe && uv run driftscribe-check --contract /tmp/checktest/ops-contract.yaml --repo-root /tmp/checktest
```

Expected: `✓ All contract vars are documented.` exit 0.

**Step 6: Commit**

```bash
git add checker/cli.py tests/unit/test_checker.py
git commit -m "feat(checker): driftscribe-check CLI + first rule"
```

---

## Phase 2 — Cloud Run reader + DRY_RUN rendering (1 day)

### Task 2.1: Cloud Run env reader (TDD with fake)

**Files:**
- Create: `agent/cloud_run_client.py`
- Test: `tests/unit/test_cloud_run_client.py`

**Step 1: Failing test**

```python
# tests/unit/test_cloud_run_client.py
from unittest.mock import MagicMock
from agent.cloud_run_client import read_live_env

def test_read_live_env_returns_dict_from_revision():
    # Fake google.cloud.run_v2 service response
    fake_client = MagicMock()
    fake_service = MagicMock()
    fake_service.template.containers = [
        MagicMock(env=[
            MagicMock(name="PAYMENT_MODE", value="live"),
            MagicMock(name="FEATURE_NEW_CHECKOUT", value="true"),
        ])
    ]
    # MagicMock auto-names confuse `name=`; set explicitly:
    fake_service.template.containers[0].env[0].name = "PAYMENT_MODE"
    fake_service.template.containers[0].env[0].value = "live"
    fake_service.template.containers[0].env[1].name = "FEATURE_NEW_CHECKOUT"
    fake_service.template.containers[0].env[1].value = "true"
    fake_client.get_service.return_value = fake_service
    env = read_live_env("payment-demo", "asia-northeast1", "my-proj", client=fake_client)
    assert env == {"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "true"}
```

**Step 2: Run failing**

```bash
pytest tests/unit/test_cloud_run_client.py -v
```

**Step 3: Implement**

```python
# agent/cloud_run_client.py
from typing import Dict, Optional
from google.cloud import run_v2

def read_live_env(
    service: str,
    region: str,
    project: str,
    client: Optional[run_v2.ServicesClient] = None,
) -> Dict[str, str]:
    """Read the env block from the latest revision of a Cloud Run service."""
    client = client or run_v2.ServicesClient()
    name = f"projects/{project}/locations/{region}/services/{service}"
    svc = client.get_service(name=name)
    env: Dict[str, str] = {}
    # In production there's typically one container; iterate just in case.
    for container in svc.template.containers:
        for ev in container.env:
            if ev.value:  # skip value_source (secrets)
                env[ev.name] = ev.value
    return env
```

**Step 4: Run**

```bash
pytest tests/unit/test_cloud_run_client.py -v
```

Expected: 1 passed.

**Step 5: Commit**

```bash
git add agent/cloud_run_client.py tests/unit/test_cloud_run_client.py
git commit -m "feat(cloud-run): live env reader using google-cloud-run admin client"
```

---

### Task 2.2: PR/issue body renderer (TDD)

**Files:**
- Create: `agent/renderer.py`
- Test: `tests/unit/test_renderer.py`

**Step 1: Failing test**

```python
# tests/unit/test_renderer.py
from agent.renderer import render_docs_pr_body, render_drift_issue_body, render_escalation_issue_body
from agent.models import DecisionProposal, DecisionAction, EnvDiff, Evidence

def _proposal(action, name="PAYMENT_MODE", old="mock", new="live", status="present_disallow_manual"):
    return DecisionProposal(
        action=action,
        env_diffs=[EnvDiff(name=name, old=old, new=new)],
        evidence=Evidence(live_value=new, contract_status=status),
        rationale="test",
        confidence=0.9,
    )

def test_drift_issue_body_includes_evidence_table():
    body = render_drift_issue_body(_proposal(DecisionAction.DRIFT_ISSUE))
    assert "PAYMENT_MODE" in body
    assert "mock" in body and "live" in body
    assert "contract" in body.lower()
    assert "Evidence" in body

def test_escalation_body_lists_uncertainty_reasons():
    p = _proposal(DecisionAction.ESCALATION, name="NEW_THING", old=None, new="x", status="absent")
    body = render_escalation_issue_body(p)
    assert "NEW_THING" in body
    assert "no contract entry" in body.lower() or "absent" in body.lower()
    assert "reviewer" in body.lower() or "intentional" in body.lower()

def test_docs_pr_body_describes_targeted_change():
    p = _proposal(DecisionAction.DOCS_PR, name="FEATURE_X", old="false", new="true",
                   status="present_allow_manual")
    body = render_docs_pr_body(p)
    assert "FEATURE_X" in body
    assert "true" in body
```

**Step 2: Run failing**

```bash
pytest tests/unit/test_renderer.py -v
```

**Step 3: Implement**

```python
# agent/renderer.py
from agent.models import DecisionProposal

_EVIDENCE_TEMPLATE = """\
| Observed | Value |
|---|---|
| Live env value | `{live}` |
| Contract status | `{status}` |
| Debug endpoint | `{debug}` |
| Recent PR match | {pr} |
"""

def _evidence_table(p: DecisionProposal) -> str:
    e = p.evidence
    return _EVIDENCE_TEMPLATE.format(
        live=e.live_value or "—",
        status=e.contract_status,
        debug=e.debug_config_value or "—",
        pr=e.recent_pr_match or "—",
    )

def render_docs_pr_body(p: DecisionProposal) -> str:
    diffs = "\n".join(f"- `{d.name}`: `{d.old}` → `{d.new}`" for d in p.env_diffs)
    return f"""\
## DriftScribe — sanctioned change detected

{p.rationale}

### Changes

{diffs}

### Evidence

{_evidence_table(p)}

> Generated by DriftScribe. The change appears sanctioned per `ops-contract.yaml`.
> Please review and merge to keep documentation in sync with production.
"""

def render_drift_issue_body(p: DecisionProposal) -> str:
    diffs = "\n".join(f"- `{d.name}`: contract says `{d.old}`, live says `{d.new}`" for d in p.env_diffs)
    return f"""\
## DriftScribe — unsanctioned production drift

{p.rationale}

### Drift

{diffs}

### Evidence

{_evidence_table(p)}

### Recommended action

- Investigate why production differs from the operational contract.
- If the change is intentional, update `ops-contract.yaml` (set `allow_manual_change: true` or revise `value`) and re-run DriftScribe.
- If the change is **not** intentional, roll back via `gcloud run services update --update-env-vars`.

> DriftScribe will not update documentation while the contract is violated.
"""

def render_escalation_issue_body(p: DecisionProposal) -> str:
    diffs = "\n".join(f"- `{d.name}` (live: `{d.new}`)" for d in p.env_diffs)
    return f"""\
## DriftScribe — uncertain change requires review

{p.rationale}

### Observed

{diffs}

### Evidence

{_evidence_table(p)}

### What I don't know

I observed a variable in production that is **not in the operational contract**, and I could not find a recent merged PR that mentions it. I need a human to confirm intent.

### Reviewer action

- Reply **`/driftscribe sanction`** if this change was intentional. DriftScribe will then add an entry to `ops-contract.yaml` and update the runbook.
- Reply **`/driftscribe reject`** if this change was unauthorized. DriftScribe will then open a rollback-tracking drift issue.

> Generated by DriftScribe. Sanction/reject hooks are a Phase 2 enhancement.
"""
```

**Step 4: Run tests**

```bash
pytest tests/unit/test_renderer.py -v
```

Expected: 3 passed.

**Step 5: Commit**

```bash
git add agent/renderer.py tests/unit/test_renderer.py
git commit -m "feat(renderer): PR / drift-issue / escalation Markdown bodies"
```

---

### Task 2.3: Wire `/recheck` FastAPI endpoint in DRY_RUN mode

**Files:**
- Create: `agent/config.py`
- Create: `agent/main.py`
- Test: `tests/integration/test_recheck_dry_run.py`

**Step 1: Failing integration test**

```python
# tests/integration/test_recheck_dry_run.py
import os
from fastapi.testclient import TestClient
from unittest.mock import patch

os.environ["DRY_RUN"] = "true"
os.environ["GCP_PROJECT"] = "test-proj"
os.environ["TARGET_REGION"] = "asia-northeast1"
os.environ["TARGET_SERVICE"] = "payment-demo"
os.environ["CONTRACT_PATH"] = "demo/ops-contract.yaml"
os.environ["GITHUB_REPO"] = "theghostsquad00/driftscribe"
os.environ["GITHUB_TOKEN"] = "fake"

from agent.main import app

def test_recheck_renders_drift_issue_in_dry_run(tmp_path, monkeypatch):
    # Prepare fake contract file
    contract = tmp_path / "ops-contract.yaml"
    contract.write_text("""
service: payment-demo
environment: production
cloud_run_service: payment-demo
region: asia-northeast1
github_repo: theghostsquad00/driftscribe
expected_env:
  PAYMENT_MODE:
    value: mock
    docs: { file: docs/runbook.md, section: Runtime Configuration }
    allow_manual_change: false
""")
    monkeypatch.setenv("CONTRACT_PATH", str(contract))

    with patch("agent.main.read_live_env") as m:
        m.return_value = {"PAYMENT_MODE": "live"}
        client = TestClient(app)
        r = client.post("/recheck")
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "drift_issue"
    assert "PAYMENT_MODE" in body["rendered_body"]
    assert body["dry_run"] is True
```

**Step 2: Run failing**

```bash
pytest tests/integration/test_recheck_dry_run.py -v
```

**Step 3: Implement config**

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
    debug_config_url: str = ""  # optional
    gemini_api_key: str = ""    # populated in Phase 5

def get_settings() -> Settings:
    return Settings()
```

**Step 4: Implement main**

```python
# agent/main.py
from fastapi import FastAPI, HTTPException
from pathlib import Path

from agent.classifier import classify, ClassificationInput
from agent.cloud_run_client import read_live_env
from agent.config import get_settings
from agent.contract import load_contract
from agent.models import DecisionAction
from agent.renderer import (
    render_docs_pr_body,
    render_drift_issue_body,
    render_escalation_issue_body,
)
from agent.validator import validate

app = FastAPI(title="DriftScribe Agent")

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.post("/recheck")
def recheck():
    s = get_settings()
    contract = load_contract(Path(s.contract_path))
    try:
        live_env = read_live_env(s.target_service, s.target_region, s.gcp_project)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"cloud run read failed: {e}")

    proposal = classify(ClassificationInput(
        contract=contract,
        live_env=live_env,
        recent_prs=[],  # Phase 5 will populate
    ))
    validate(proposal, contract)

    if proposal.action == DecisionAction.NO_OP:
        rendered = "(no action)"
    elif proposal.action == DecisionAction.DOCS_PR:
        rendered = render_docs_pr_body(proposal)
    elif proposal.action == DecisionAction.DRIFT_ISSUE:
        rendered = render_drift_issue_body(proposal)
    else:
        rendered = render_escalation_issue_body(proposal)

    return {
        "action": proposal.action.value,
        "rendered_body": rendered,
        "rationale": proposal.rationale,
        "evidence": proposal.evidence.model_dump(),
        "diffs": [d.model_dump() for d in proposal.env_diffs],
        "dry_run": s.dry_run,
    }
```

**Step 5: Run tests**

```bash
pytest tests/integration/test_recheck_dry_run.py -v
```

Expected: 1 passed.

**Step 6: Manual smoke**

```bash
DRY_RUN=true GCP_PROJECT=local TARGET_SERVICE=payment-demo \
  CONTRACT_PATH=demo/ops-contract.yaml \
  uvicorn agent.main:app --port 8080 &
# (will fail at Cloud Run read until we have a real service; that's fine for now)
```

**Step 7: Commit**

```bash
git add agent/config.py agent/main.py tests/integration/test_recheck_dry_run.py
git commit -m "feat(api): /recheck endpoint wires classifier + renderer in DRY_RUN"
```

---

## Phase 3 — GitHub side effects (½ day)

### Task 3.1: GitHub actions client (TDD with PyGithub mock)

**Files:**
- Create: `agent/github_actions.py`
- Test: `tests/unit/test_github_actions.py`

**Step 1: Failing test**

```python
# tests/unit/test_github_actions.py
from unittest.mock import MagicMock
from agent.github_actions import open_drift_issue, open_escalation_issue, open_docs_pr

def test_open_drift_issue_calls_create_issue():
    fake_repo = MagicMock()
    open_drift_issue(repo=fake_repo, title="Drift: PAYMENT_MODE", body="body", dry_run=False)
    fake_repo.create_issue.assert_called_once()
    args = fake_repo.create_issue.call_args
    assert "PAYMENT_MODE" in args.kwargs["title"]
    assert "driftscribe" in [l.lower() for l in args.kwargs["labels"]]

def test_dry_run_does_not_call_github():
    fake_repo = MagicMock()
    result = open_drift_issue(repo=fake_repo, title="t", body="b", dry_run=True)
    fake_repo.create_issue.assert_not_called()
    assert result["dry_run"] is True
    assert result["url"] is None
```

**Step 2: Run failing**

```bash
pytest tests/unit/test_github_actions.py -v
```

**Step 3: Implement**

```python
# agent/github_actions.py
from typing import Optional, Dict, Any
from github import Github
from github.Repository import Repository

def get_repo(token: str, repo_full_name: str) -> Repository:
    return Github(token).get_repo(repo_full_name)

def open_drift_issue(repo: Repository, title: str, body: str, dry_run: bool) -> Dict[str, Any]:
    if dry_run:
        return {"dry_run": True, "url": None, "title": title}
    issue = repo.create_issue(
        title=title,
        body=body,
        labels=["driftscribe", "drift"],
    )
    return {"dry_run": False, "url": issue.html_url, "number": issue.number}

def open_escalation_issue(repo: Repository, title: str, body: str, dry_run: bool) -> Dict[str, Any]:
    if dry_run:
        return {"dry_run": True, "url": None, "title": title}
    issue = repo.create_issue(
        title=title,
        body=body,
        labels=["driftscribe", "escalation"],
    )
    return {"dry_run": False, "url": issue.html_url, "number": issue.number}

def open_docs_pr(
    repo: Repository,
    branch: str,
    base: str,
    title: str,
    body: str,
    file_path: str,
    new_content: str,
    dry_run: bool,
) -> Dict[str, Any]:
    if dry_run:
        return {"dry_run": True, "url": None, "branch": branch, "preview": new_content[:500]}

    # Create branch from base
    base_ref = repo.get_branch(base)
    repo.create_git_ref(ref=f"refs/heads/{branch}", sha=base_ref.commit.sha)

    # Update or create file
    try:
        existing = repo.get_contents(file_path, ref=branch)
        repo.update_file(
            path=file_path,
            message=f"docs: DriftScribe update for {file_path}",
            content=new_content,
            sha=existing.sha,
            branch=branch,
        )
    except Exception:
        repo.create_file(
            path=file_path,
            message=f"docs: DriftScribe initial {file_path}",
            content=new_content,
            branch=branch,
        )

    pr = repo.create_pull(title=title, body=body, head=branch, base=base)
    pr.add_to_labels("driftscribe", "docs")
    return {"dry_run": False, "url": pr.html_url, "number": pr.number}
```

**Step 4: Run tests**

```bash
pytest tests/unit/test_github_actions.py -v
```

Expected: 2 passed.

**Step 5: Commit**

```bash
git add agent/github_actions.py tests/unit/test_github_actions.py
git commit -m "feat(github): drift/escalation issue + docs PR creators with DRY_RUN guard"
```

---

### Task 3.2: Wire GitHub actions into `/recheck`

**Files:**
- Modify: `agent/main.py`
- Test: extend `tests/integration/test_recheck_dry_run.py`

**Step 1: Extend test**

```python
# tests/integration/test_recheck_dry_run.py (add)
def test_recheck_dry_run_simulates_github_calls(tmp_path, monkeypatch):
    # ... same contract setup ...
    # In DRY_RUN, response should include `github_action_preview` keys
    with patch("agent.main.read_live_env") as m:
        m.return_value = {"PAYMENT_MODE": "live"}
        client = TestClient(app)
        r = client.post("/recheck")
    body = r.json()
    assert body["github"]["dry_run"] is True
    assert body["github"]["url"] is None
```

**Step 2: Update `agent/main.py`**

```python
# inside recheck() — replace return value
# (full file would be too long; key addition shown)

from agent.github_actions import open_drift_issue, open_escalation_issue, open_docs_pr, get_repo

# ... after computing `proposal` and `rendered`:

github_result: dict
if s.dry_run:
    repo = None  # type: ignore
else:
    repo = get_repo(s.github_token, s.github_repo)

if proposal.action == DecisionAction.NO_OP:
    github_result = {"dry_run": s.dry_run, "url": None, "action": "no_op"}
elif proposal.action == DecisionAction.DRIFT_ISSUE:
    github_result = open_drift_issue(
        repo=repo,  # type: ignore
        title=f"[DriftScribe] Drift: {', '.join(d.name for d in proposal.env_diffs)}",
        body=rendered,
        dry_run=s.dry_run,
    )
elif proposal.action == DecisionAction.ESCALATION:
    github_result = open_escalation_issue(
        repo=repo,  # type: ignore
        title=f"[DriftScribe] Review: {', '.join(d.name for d in proposal.env_diffs)}",
        body=rendered,
        dry_run=s.dry_run,
    )
else:  # DOCS_PR
    # File content generation is Phase 5+; for now, append a stub line to runbook
    docs_file_path = next(iter(contract.expected_env.values())).docs.file
    branch = f"driftscribe/{proposal.env_diffs[0].name.lower()}-{int(time.time())}"
    github_result = open_docs_pr(
        repo=repo,  # type: ignore
        branch=branch,
        base="main",
        title=f"docs(driftscribe): document {proposal.env_diffs[0].name}",
        body=rendered,
        file_path=docs_file_path,
        new_content="(placeholder — runbook update generation lands in Phase 5)",
        dry_run=s.dry_run,
    )

return {
    "action": proposal.action.value,
    "rendered_body": rendered,
    "rationale": proposal.rationale,
    "evidence": proposal.evidence.model_dump(),
    "diffs": [d.model_dump() for d in proposal.env_diffs],
    "dry_run": s.dry_run,
    "github": github_result,
}
```

(Add `import time` at the top.)

**Step 3: Run tests**

```bash
pytest tests/integration/test_recheck_dry_run.py -v
```

**Step 4: Commit**

```bash
git add agent/main.py tests/integration/test_recheck_dry_run.py
git commit -m "feat(api): wire /recheck to GitHub actions (DRY_RUN aware)"
```

---

## Phase 4 — Firestore state + observability (½ day)

### Task 4.1: Firestore state module (TDD with fake)

**Files:**
- Create: `agent/firestore_state.py`
- Test: `tests/unit/test_firestore_state.py`

We use an in-memory dict-backed fake for tests; real client only in prod.

**Step 1: Failing test**

```python
# tests/unit/test_firestore_state.py
from agent.firestore_state import InMemoryStateStore

def test_idempotency_returns_existing_decision_for_repeat_event():
    store = InMemoryStateStore()
    store.record_event("event-1", {"trigger": "manual"})
    store.record_decision("decision-1", "event-1", {"action": "drift_issue", "url": "https://..."})
    again = store.find_decision_for_event("event-1")
    assert again is not None
    assert again["action"] == "drift_issue"

def test_no_decision_returns_none():
    store = InMemoryStateStore()
    assert store.find_decision_for_event("missing") is None
```

**Step 2: Run failing**

```bash
pytest tests/unit/test_firestore_state.py -v
```

**Step 3: Implement**

```python
# agent/firestore_state.py
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, Protocol
from datetime import datetime, timezone

class StateStore(Protocol):
    def record_event(self, event_key: str, metadata: dict) -> None: ...
    def record_decision(self, decision_id: str, event_key: str, decision: dict) -> None: ...
    def find_decision_for_event(self, event_key: str) -> Optional[dict]: ...
    def get_decision(self, decision_id: str) -> Optional[dict]: ...

class InMemoryStateStore:
    def __init__(self) -> None:
        self._events: Dict[str, dict] = {}
        self._decisions: Dict[str, dict] = {}
        self._event_to_decision: Dict[str, str] = {}

    def record_event(self, event_key: str, metadata: dict) -> None:
        self._events[event_key] = {**metadata, "ts": datetime.now(timezone.utc).isoformat()}

    def record_decision(self, decision_id: str, event_key: str, decision: dict) -> None:
        self._decisions[decision_id] = {**decision, "event_key": event_key,
                                        "ts": datetime.now(timezone.utc).isoformat()}
        self._event_to_decision[event_key] = decision_id

    def find_decision_for_event(self, event_key: str) -> Optional[dict]:
        did = self._event_to_decision.get(event_key)
        return self._decisions.get(did) if did else None

    def get_decision(self, decision_id: str) -> Optional[dict]:
        return self._decisions.get(decision_id)

# Real Firestore-backed store — used at runtime only
class FirestoreStateStore:
    def __init__(self, project: str) -> None:
        from google.cloud import firestore
        self._db = firestore.Client(project=project)

    def record_event(self, event_key: str, metadata: dict) -> None:
        self._db.collection("events").document(event_key).set(
            {**metadata, "ts": datetime.now(timezone.utc).isoformat()},
            merge=True,
        )

    def record_decision(self, decision_id: str, event_key: str, decision: dict) -> None:
        self._db.collection("decisions").document(decision_id).set(
            {**decision, "event_key": event_key,
             "ts": datetime.now(timezone.utc).isoformat()},
        )
        self._db.collection("events").document(event_key).update({"decision_id": decision_id})

    def find_decision_for_event(self, event_key: str) -> Optional[dict]:
        ev = self._db.collection("events").document(event_key).get()
        if not ev.exists or "decision_id" not in ev.to_dict():
            return None
        did = ev.to_dict()["decision_id"]
        d = self._db.collection("decisions").document(did).get()
        return d.to_dict() if d.exists else None

    def get_decision(self, decision_id: str) -> Optional[dict]:
        d = self._db.collection("decisions").document(decision_id).get()
        return d.to_dict() if d.exists else None
```

**Step 4: Run tests**

```bash
pytest tests/unit/test_firestore_state.py -v
```

Expected: 2 passed.

**Step 5: Commit**

```bash
git add agent/firestore_state.py tests/unit/test_firestore_state.py
git commit -m "feat(state): in-memory + firestore state store with idempotency"
```

---

### Task 4.2: Wire state store into `/recheck` and add `/runs/{id}`

**Files:**
- Modify: `agent/main.py`

**Step 1: Update main.py to record events + decisions, expose `/runs/{id}`**

```python
# agent/main.py — additions

import uuid
import hashlib
import json
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

def _event_key(trigger: str, payload: dict) -> str:
    # Stable hash so re-fired events are deduped
    h = hashlib.sha256(
        (trigger + json.dumps(payload, sort_keys=True)).encode()
    ).hexdigest()[:16]
    return f"{trigger}-{h}"

# In recheck(), at the start:
event_key = _event_key("manual_recheck", {"service": s.target_service})
state = get_state()
existing = state.find_decision_for_event(event_key)
if existing:
    return existing

# ... existing logic to produce response ...

decision_id = str(uuid.uuid4())
state.record_event(event_key, {"trigger": "manual_recheck"})
state.record_decision(decision_id, event_key, response)
response["decision_id"] = decision_id
return response

# New endpoint:
@app.get("/runs/{decision_id}")
def get_run(decision_id: str):
    state = get_state()
    d = state.get_decision(decision_id)
    if not d:
        raise HTTPException(status_code=404)
    return d
```

**Step 2: Add integration test for `/runs/{id}`**

```python
# tests/integration/test_runs_endpoint.py
import os
os.environ["DRY_RUN"] = "true"
os.environ["GCP_PROJECT"] = ""
os.environ["CONTRACT_PATH"] = "demo/ops-contract.yaml"
os.environ["GITHUB_REPO"] = "theghostsquad00/driftscribe"
os.environ["GITHUB_TOKEN"] = "fake"

from fastapi.testclient import TestClient
from unittest.mock import patch
from agent.main import app

def test_runs_endpoint_returns_decision_recorded_by_recheck(tmp_path, monkeypatch):
    contract = tmp_path / "ops-contract.yaml"
    contract.write_text("""
service: payment-demo
environment: production
cloud_run_service: payment-demo
region: asia-northeast1
github_repo: theghostsquad00/driftscribe
expected_env:
  PAYMENT_MODE:
    value: mock
    docs: { file: docs/runbook.md, section: Runtime Configuration }
    allow_manual_change: false
""")
    monkeypatch.setenv("CONTRACT_PATH", str(contract))

    with patch("agent.main.read_live_env") as m:
        m.return_value = {"PAYMENT_MODE": "live"}
        client = TestClient(app)
        r = client.post("/recheck")
        decision_id = r.json()["decision_id"]
        r2 = client.get(f"/runs/{decision_id}")
    assert r2.status_code == 200
    assert r2.json()["action"] == "drift_issue"
```

**Step 3: Run tests**

```bash
pytest tests/integration -v
```

**Step 4: Commit**

```bash
git add agent/main.py tests/integration/test_runs_endpoint.py
git commit -m "feat(state): idempotency + /runs/{id} observability endpoint"
```

---

## Phase 5 — `driftscribe init` CLI (½ day)

Bootstraps `ops-contract.yaml` from current live Cloud Run state and opens a "DriftScribe bootstrap" PR.

### Task 5.1: `agent/cli.py` with `init` subcommand (TDD)

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

def test_init_generates_contract_yaml_from_live_env(tmp_path):
    with patch("agent.cli.read_live_env") as m:
        m.return_value = {"PAYMENT_MODE": "mock", "FEATURE_X": "false"}
        result = runner.invoke(app, [
            "init",
            "--service", "payment-demo",
            "--region", "asia-northeast1",
            "--project", "my-proj",
            "--github-repo", "theghostsquad00/driftscribe",
            "--output", str(tmp_path / "ops-contract.yaml"),
            "--dry-run",
        ])
    assert result.exit_code == 0, result.output
    out = yaml.safe_load((tmp_path / "ops-contract.yaml").read_text())
    assert out["cloud_run_service"] == "payment-demo"
    assert "PAYMENT_MODE" in out["expected_env"]
    # Conservative defaults
    assert out["expected_env"]["PAYMENT_MODE"]["allow_manual_change"] is False
```

**Step 2: Run failing**

```bash
pytest tests/unit/test_cli_init.py -v
```

**Step 3: Implement**

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
    dry_run: bool = typer.Option(False, help="Don't open a PR, just write the file"),
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
                "allow_manual_change": False,  # conservative default
            }
            for name, value in live.items()
        },
    }
    output.write_text(yaml.safe_dump(contract, sort_keys=False))
    typer.echo(f"✓ Wrote {output}")
    if not dry_run:
        typer.echo("(PR opening lands in Phase 5.2)")

if __name__ == "__main__":
    app()
```

**Step 4: Run tests**

```bash
pytest tests/unit/test_cli_init.py -v
```

**Step 5: Commit**

```bash
git add agent/cli.py tests/unit/test_cli_init.py
git commit -m "feat(cli): driftscribe init generates ops-contract.yaml from live env"
```

---

### Task 5.2: `init --open-pr` flow

**Files:**
- Modify: `agent/cli.py`
- Test: extend `tests/unit/test_cli_init.py`

**Step 1: Extend test**

```python
def test_init_open_pr_calls_github_actions(tmp_path):
    with patch("agent.cli.read_live_env") as m, \
         patch("agent.cli.open_docs_pr") as pr:
        m.return_value = {"X": "1"}
        pr.return_value = {"dry_run": False, "url": "https://github.com/x/x/pull/1", "number": 1}
        result = runner.invoke(app, [
            "init",
            "--service", "demo", "--region", "asia-northeast1",
            "--project", "p", "--github-repo", "theghostsquad00/driftscribe",
            "--output", str(tmp_path / "ops-contract.yaml"),
            "--open-pr",
            "--token", "fake",
        ])
    assert result.exit_code == 0
    pr.assert_called_once()
```

**Step 2: Update cli.py to support `--open-pr`** (handle creating both the contract file and a stub runbook section, then PR).

```python
# additions to agent/cli.py
from agent.github_actions import open_docs_pr, get_repo

@app.command()
def init(
    service: str = typer.Option(...),
    region: str = typer.Option("asia-northeast1"),
    project: str = typer.Option(...),
    github_repo: str = typer.Option(...),
    output: Path = typer.Option(Path("ops-contract.yaml")),
    docs_file: str = typer.Option("docs/runbook.md"),
    docs_section: str = typer.Option("Runtime Configuration"),
    open_pr: bool = typer.Option(False, "--open-pr"),
    token: str = typer.Option("", "--token"),
    base: str = typer.Option("main"),
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
    contract_yaml = yaml.safe_dump(contract, sort_keys=False)
    output.write_text(contract_yaml)
    typer.echo(f"✓ Wrote {output}")

    if not open_pr:
        return

    runbook = "# Runbook\n\n## " + docs_section + "\n\n"
    runbook += "These environment variables were bootstrapped by `driftscribe init`:\n\n"
    for k, v in live.items():
        runbook += f"- `{k}` = `{v}`\n"

    repo = get_repo(token, github_repo)
    branch = f"driftscribe/bootstrap-{service}"
    body = f"""\
## DriftScribe bootstrap

This PR initialises `ops-contract.yaml` and the runbook section for `{service}` based on its current live Cloud Run configuration in `{region}`.

All env vars are conservatively set to `allow_manual_change: false`. After merging, flip the flag for any var that operators *should* be able to change manually (e.g. feature flags).
"""
    res = open_docs_pr(
        repo=repo,
        branch=branch,
        base=base,
        title=f"driftscribe(init): bootstrap contract for {service}",
        body=body,
        file_path=str(output),
        new_content=contract_yaml,
        dry_run=False,
    )
    typer.echo(f"✓ Opened PR: {res['url']}")
```

**Step 3: Run + commit**

```bash
pytest tests/unit/test_cli_init.py -v
git add agent/cli.py tests/unit/test_cli_init.py
git commit -m "feat(cli): driftscribe init --open-pr opens bootstrap PR"
```

---

## Phase 6 — ADK agent shell (1 day)

This is where the "AI agent" credential lives. The ADK agent wraps four tools and emits a `DecisionProposal`. The deterministic validator (Phase 1) still gates side effects.

### Task 6.1: Define ADK tools

**Files:**
- Create: `agent/adk_tools.py`
- Test: `tests/unit/test_adk_tools.py`

ADK tool signatures depend on the `google-adk` API. For each tool we want:

1. `read_live_env_tool(service, region, project) -> dict`
2. `call_debug_config_tool(url) -> dict`
3. `search_recent_prs_tool(repo, keywords, days) -> list[dict]`
4. `load_contract_tool(path) -> dict`

**Step 1: Failing test (focus on shape, not ADK internals)**

```python
# tests/unit/test_adk_tools.py
from unittest.mock import patch
from agent.adk_tools import read_live_env_tool, search_recent_prs_tool, load_contract_tool

def test_read_live_env_tool_returns_dict():
    with patch("agent.adk_tools.read_live_env") as m:
        m.return_value = {"X": "1"}
        assert read_live_env_tool("svc", "asia-northeast1", "proj") == {"X": "1"}

def test_load_contract_tool_returns_dict(tmp_path):
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

def test_search_recent_prs_filters_by_keyword():
    fake_prs = [
        {"title": "Add FEATURE_X", "body": "", "url": "u1"},
        {"title": "Unrelated", "body": "", "url": "u2"},
    ]
    with patch("agent.adk_tools._list_recent_merged_prs") as m:
        m.return_value = fake_prs
        result = search_recent_prs_tool("x/x", ["FEATURE_X"], 7)
        assert len(result) == 1
        assert result[0]["url"] == "u1"
```

**Step 2: Run failing**

```bash
pytest tests/unit/test_adk_tools.py -v
```

**Step 3: Implement**

```python
# agent/adk_tools.py
from pathlib import Path
from typing import List, Dict, Any
from datetime import datetime, timedelta, timezone
import httpx
import yaml

from agent.cloud_run_client import read_live_env
from github import Github

def read_live_env_tool(service: str, region: str, project: str) -> Dict[str, str]:
    """Read the env block from the latest revision of a Cloud Run service."""
    return read_live_env(service, region, project)

def call_debug_config_tool(url: str) -> Dict[str, Any]:
    """Call the target service's /debug/config endpoint. Returns {} on failure."""
    try:
        r = httpx.get(url, timeout=5.0)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"_error": str(e)}

def _list_recent_merged_prs(repo_full: str, days: int, token: str = "") -> List[Dict[str, Any]]:
    g = Github(token) if token else Github()
    repo = g.get_repo(repo_full)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    out: list[dict] = []
    for pr in repo.get_pulls(state="closed", sort="updated", direction="desc"):
        if pr.merged_at is None or pr.merged_at < since:
            continue
        out.append({"title": pr.title or "", "body": pr.body or "", "url": pr.html_url})
    return out

def search_recent_prs_tool(repo_full: str, keywords: List[str], days: int = 7, token: str = "") -> List[Dict[str, Any]]:
    """Find merged PRs in last N days whose title/body matches any keyword."""
    prs = _list_recent_merged_prs(repo_full, days, token)
    lowered = [k.lower() for k in keywords]
    return [
        pr for pr in prs
        if any(kw in (pr["title"] + " " + pr["body"]).lower() for kw in lowered)
    ]

def load_contract_tool(path: str) -> Dict[str, Any]:
    return yaml.safe_load(Path(path).read_text())
```

**Step 4: Run + commit**

```bash
pytest tests/unit/test_adk_tools.py -v
git add agent/adk_tools.py tests/unit/test_adk_tools.py
git commit -m "feat(agent): ADK tool wrappers — cloud run, debug config, PR search, contract"
```

---

### Task 6.2: ADK agent definition

**Files:**
- Create: `agent/adk_agent.py`
- Test: `tests/unit/test_adk_agent.py`

This wraps tools in an ADK Agent + LlmRequest. Specifics depend on the ADK API at the time of build; pseudocode shown.

**Step 1: Skeleton**

```python
# agent/adk_agent.py
from typing import Any, Dict
from google.adk import Agent
from google.adk.tools import Tool

from agent.adk_tools import (
    read_live_env_tool,
    call_debug_config_tool,
    search_recent_prs_tool,
    load_contract_tool,
)

SYSTEM_PROMPT = """\
You are DriftScribe, an AI DevOps agent that detects and triages drift between
a deployed Cloud Run service's live configuration and the team's declared
operational contract (ops-contract.yaml).

Your job:
1. Use the provided tools to gather evidence: live Cloud Run env, /debug/config
   if available, recent merged PRs, and the operational contract.
2. Identify env vars that have drifted from the contract.
3. For each drift, classify as one of:
   - docs_pr        — change is sanctioned (contract allows manual change OR
                      a recent PR mentions the var)
   - drift_issue    — change violates the contract
                      (allow_manual_change=false; not a sanctioned change)
   - escalation     — change is a NEW var not in the contract, and no recent
                      PR mentions it. Reviewer needed.
   - no_op          — live state matches contract
4. Emit a single JSON DecisionProposal. Do not perform side effects.

Rules:
- If you cannot reach a tool, say so in `rationale`; do not invent values.
- Never propose `docs_pr` for a var whose contract entry says
  `allow_manual_change: false`.
- Never include secret-like values in the proposal body. Names containing
  SECRET, TOKEN, KEY, PASSWORD, CRED are off-limits for documentation actions.

Output schema:
{
  "action": "docs_pr" | "drift_issue" | "escalation" | "no_op",
  "env_diffs": [{"name": str, "old": str|null, "new": str|null}],
  "evidence": {
    "live_value": str|null,
    "contract_status": "absent" | "present_allow_manual" | "present_disallow_manual" | "match",
    "debug_config_value": str|null,
    "recent_pr_match": str|null
  },
  "rationale": str,
  "confidence": float (0.0–1.0)
}
"""

def build_agent() -> Agent:
    return Agent(
        name="driftscribe",
        model="gemini-2.5-flash",
        system_instruction=SYSTEM_PROMPT,
        tools=[
            Tool.from_function(read_live_env_tool),
            Tool.from_function(call_debug_config_tool),
            Tool.from_function(search_recent_prs_tool),
            Tool.from_function(load_contract_tool),
        ],
    )

def run_agent(user_msg: str) -> Dict[str, Any]:
    agent = build_agent()
    result = agent.run(user_msg)
    return result.output_json  # ADK provides JSON-mode parsing; adapt to current SDK
```

**Step 2: Smoke test (mocked LLM)** — full ADK testing requires `pytest-asyncio` + `respx` and is non-trivial; for hackathon, smoke-test via the integration path in Task 6.3 instead.

**Step 3: Commit**

```bash
git add agent/adk_agent.py
git commit -m "feat(agent): ADK Agent with 4 tools and structured DecisionProposal output"
```

---

### Task 6.3: Replace `/recheck` classifier with ADK agent (gated by env flag)

**Files:**
- Modify: `agent/main.py`

**Step 1: Add `USE_ADK` flag to settings**

```python
# agent/config.py — add
use_adk: bool = False
```

**Step 2: In `recheck()`, branch on `use_adk`**

```python
# inside recheck()
if s.use_adk:
    from agent.adk_agent import run_agent
    user_msg = (
        f"Detect drift for Cloud Run service `{s.target_service}` in `{s.target_region}` "
        f"(project `{s.gcp_project}`). Contract is at `{s.contract_path}`. "
        f"GitHub repo for PR history: `{s.github_repo}`. "
        f"If a /debug/config URL is provided ({s.debug_config_url or 'none'}), use it."
    )
    raw = run_agent(user_msg)
    proposal = DecisionProposal.model_validate(raw)
else:
    proposal = classify(ClassificationInput(
        contract=contract, live_env=live_env, recent_prs=[],
    ))
validate(proposal, contract)
```

**Step 3: Manual smoke (requires real Gemini key)**

```bash
USE_ADK=true GEMINI_API_KEY=... DRY_RUN=true \
  GCP_PROJECT=... TARGET_SERVICE=payment-demo \
  uvicorn agent.main:app --port 8080 &
curl -s -X POST http://localhost:8080/recheck | jq .
```

Expect: response with `action` + `rationale` from the LLM, plus the deterministic validator still passing/failing as appropriate.

**Step 4: Commit**

```bash
git add agent/main.py agent/config.py
git commit -m "feat(api): USE_ADK toggle — route /recheck through ADK agent"
```

---

## Phase 7 — Demo target service (½ day)

A tiny payment-demo FastAPI app deployed alongside the agent. It does two things: (1) be a Cloud Run service whose env we can poke; (2) expose `/debug/config`.

### Task 7.1: Demo app

**Files:**
- Create: `demo/main.py`, `demo/Dockerfile`, `demo/pyproject.toml`, `demo/docs/runbook.md`, `demo/ops-contract.yaml`

**Step 1: `demo/main.py`**

```python
# demo/main.py
import os
import logging
from fastapi import FastAPI

# Allowlist of env keys safe to expose at /debug/config (never secrets)
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

**Step 4: `demo/ops-contract.yaml`**

```yaml
service: payment-demo
environment: production
cloud_run_service: payment-demo
region: asia-northeast1
github_repo: theghostsquad00/driftscribe
expected_env:
  PAYMENT_MODE:
    value: mock
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
```

**Step 5: `demo/docs/runbook.md`** (intentionally minimal at demo start)

```markdown
# payment-demo Runbook

## Runtime Configuration

- `PAYMENT_MODE=mock` — controls whether real payments hit the gateway. Must be `mock` in test environments.

## Feature Flags

- `FEATURE_NEW_CHECKOUT=false` — operator-toggleable new checkout flow.
```

**Step 6: Smoke**

```bash
cd demo && uvicorn main:app --port 8081 &
PAYMENT_MODE=mock FEATURE_NEW_CHECKOUT=false curl -s http://localhost:8081/debug/config | jq .
```

**Step 7: Commit**

```bash
git add demo/
git commit -m "feat(demo): payment-demo Cloud Run app + /debug/config + initial docs"
```

---

## Phase 8 — CI consistency-gate + deploy (½ day)

### Task 8.1: GitHub Actions workflows

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `.github/workflows/docs-check.yml`

**Step 1: `ci.yml`**

```yaml
name: ci
on:
  push:
    branches: [main]
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

**Step 2: `docs-check.yml`**

```yaml
name: docs-check
on:
  pull_request:
    paths:
      - "demo/**"
      - "ops-contract.yaml"
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

**Step 3: Commit**

```bash
git add .github/workflows/
git commit -m "ci: tests + driftscribe-check on every PR"
```

---

### Task 8.2: Cloud Build + deploy

**Files:**
- Create: `infra/cloudbuild.yaml`
- Create: `infra/terraform/main.tf`, `services.tf`, `iam.tf`, `firestore.tf`, `variables.tf`

**Step 1: `infra/cloudbuild.yaml`** (builds both images, pushes, deploys)

```yaml
steps:
  - name: gcr.io/cloud-builders/docker
    args: ['build', '-t', 'gcr.io/$PROJECT_ID/driftscribe-agent:$SHORT_SHA', '-f', 'Dockerfile.agent', '.']
  - name: gcr.io/cloud-builders/docker
    args: ['build', '-t', 'gcr.io/$PROJECT_ID/payment-demo:$SHORT_SHA', '-f', 'demo/Dockerfile', 'demo']
  - name: gcr.io/cloud-builders/docker
    args: ['push', 'gcr.io/$PROJECT_ID/driftscribe-agent:$SHORT_SHA']
  - name: gcr.io/cloud-builders/docker
    args: ['push', 'gcr.io/$PROJECT_ID/payment-demo:$SHORT_SHA']
  - name: gcr.io/google.com/cloudsdktool/cloud-sdk
    entrypoint: gcloud
    args:
      - run
      - deploy
      - driftscribe-agent
      - --image=gcr.io/$PROJECT_ID/driftscribe-agent:$SHORT_SHA
      - --region=asia-northeast1
      - --allow-unauthenticated
      - --set-env-vars=DRY_RUN=false,GCP_PROJECT=$PROJECT_ID,TARGET_SERVICE=payment-demo,TARGET_REGION=asia-northeast1,GITHUB_REPO=theghostsquad00/driftscribe,CONTRACT_PATH=/contract/ops-contract.yaml
  - name: gcr.io/google.com/cloudsdktool/cloud-sdk
    entrypoint: gcloud
    args:
      - run
      - deploy
      - payment-demo
      - --image=gcr.io/$PROJECT_ID/payment-demo:$SHORT_SHA
      - --region=asia-northeast1
      - --allow-unauthenticated
      - --set-env-vars=PAYMENT_MODE=mock,FEATURE_NEW_CHECKOUT=false
```

**Step 2: `Dockerfile.agent`** (root level)

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir uv && uv pip install --system -e .
COPY agent/ ./agent/
COPY checker/ ./checker/
COPY demo/ops-contract.yaml /contract/ops-contract.yaml
COPY demo/docs/ /contract/docs/
ENV PORT=8080
CMD ["uvicorn", "agent.main:app", "--host", "0.0.0.0", "--port", "8080"]
```

**Step 3: Terraform** — minimum needed for hackathon:

```hcl
# infra/terraform/main.tf
terraform {
  required_providers {
    google = { source = "hashicorp/google", version = "~> 5.0" }
  }
}

provider "google" {
  project = var.project
  region  = var.region
}

# infra/terraform/variables.tf
variable "project" { type = string }
variable "region"  { type = string, default = "asia-northeast1" }
variable "github_repo" { type = string, default = "theghostsquad00/driftscribe" }
variable "github_token_secret_id" { type = string }

# infra/terraform/firestore.tf
resource "google_firestore_database" "default" {
  project     = var.project
  name        = "(default)"
  location_id = "asia-northeast1"
  type        = "FIRESTORE_NATIVE"
}

# infra/terraform/iam.tf
# Service accounts + roles for agent + eventarc + cloud build deferred to Terraform README.
```

**Step 4: Commit**

```bash
git add infra/ Dockerfile.agent
git commit -m "infra: cloud build pipeline + terraform skeleton"
```

**Step 5: Manual deploy**

```bash
gcloud builds submit --config=infra/cloudbuild.yaml
```

**Step 6: Verify on real GCP**

```bash
gcloud run services list --region asia-northeast1
curl -s "$(gcloud run services describe payment-demo --region asia-northeast1 --format='value(status.url)')/debug/config" | jq .
```

**Step 7: Commit deploy notes**

```bash
git commit --allow-empty -m "chore: first cloud run deploy verified"
```

---

## Phase 9 — Eventarc (1 day, final)

### Task 9.1: Determine real Audit Log filter values

**File:** Create `scripts/inspect_audit_log.sh`

```bash
#!/usr/bin/env bash
# Trigger a real env-var change, then dump the resulting audit log entry so
# we can copy the exact serviceName / methodName / resourceName into Terraform.
set -euo pipefail

PROJECT="${1:?usage: $0 PROJECT}"
SERVICE="${2:-payment-demo}"
REGION="${3:-asia-northeast1}"

echo "Triggering a no-op env-var update..."
gcloud run services update "$SERVICE" \
  --region "$REGION" \
  --project "$PROJECT" \
  --update-env-vars "_DRIFTSCRIBE_PROBE=$(date +%s)"

sleep 10

echo "Latest matching audit log entries:"
gcloud logging read \
  'logName=~"cloudaudit.googleapis.com" AND resource.type="cloud_run_revision"' \
  --project "$PROJECT" \
  --limit 1 \
  --format json | jq '.[0] | {serviceName: .protoPayload.serviceName, methodName: .protoPayload.methodName, resourceName: .protoPayload.resourceName}'
```

**Steps:** chmod +x; run; copy values into Terraform below.

---

### Task 9.2: Terraform Eventarc trigger

**File:** `infra/terraform/eventarc.tf`

```hcl
resource "google_service_account" "eventarc_invoker" {
  project      = var.project
  account_id   = "eventarc-invoker"
  display_name = "Eventarc invoker for DriftScribe"
}

resource "google_cloud_run_v2_service_iam_member" "agent_invoker" {
  project  = var.project
  location = var.region
  name     = "driftscribe-agent"
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.eventarc_invoker.email}"
}

resource "google_eventarc_trigger" "cloud_run_config_change" {
  name     = "driftscribe-cloud-run-config-change"
  location = var.region
  project  = var.project

  matching_criteria {
    attribute = "type"
    value     = "google.cloud.audit.log.v1.written"
  }
  matching_criteria {
    attribute = "serviceName"
    value     = "run.googleapis.com"   # adjust if inspect_audit_log shows different
  }
  matching_criteria {
    attribute = "methodName"
    value     = "google.cloud.run.v2.Services.UpdateService"  # adjust per inspect
  }

  destination {
    cloud_run_service {
      service = "driftscribe-agent"
      region  = var.region
      path    = "/eventarc"
    }
  }

  service_account = google_service_account.eventarc_invoker.email
}
```

---

### Task 9.3: `/eventarc` endpoint

**File:** modify `agent/main.py`

```python
from fastapi import Request

@app.post("/eventarc")
async def eventarc_handler(req: Request):
    # CloudEvents arrive as JSON in the body; extract a stable event id for idempotency
    body = await req.json()
    event_id = req.headers.get("ce-id") or body.get("id", "unknown")

    state = get_state()
    existing = state.find_decision_for_event(event_id)
    if existing:
        return existing

    # Re-use the same path as /recheck — never trust the event payload directly,
    # always re-read live Cloud Run config.
    s = get_settings()
    state.record_event(event_id, {"trigger": "eventarc", "raw": body})
    return _do_recheck(event_key=event_id, trigger="eventarc")
```

(Refactor the body of `recheck()` into `_do_recheck(event_key, trigger)` for reuse.)

**Step:** Commit, deploy, then manually trigger a real Cloud Run env update and confirm `/runs/{id}` shows an entry.

```bash
gcloud run services update payment-demo --region asia-northeast1 \
  --update-env-vars FEATURE_NEW_CHECKOUT=true
# Wait ~30s, then:
curl -s "$(gcloud run services describe driftscribe-agent --region asia-northeast1 --format='value(status.url)')/runs" | jq .
```

---

## Final demo script (5 beats)

| Beat | Action | Expected output |
|---|---|---|
| 0 (bootstrap) | `driftscribe init --service payment-demo --region asia-northeast1 --project $PROJECT --github-repo theghostsquad00/driftscribe --open-pr --token $GH_TOKEN` | Bootstrap PR opened with ops-contract.yaml + runbook |
| A (sanctioned) | `gcloud run services update payment-demo --update-env-vars FEATURE_NEW_CHECKOUT=true` then `curl POST /recheck` | Docs PR opened updating Feature Flags section |
| B (unsanctioned) | `gcloud run services update payment-demo --update-env-vars PAYMENT_MODE=live` then `/recheck` | Drift issue opened, **no** docs PR |
| C (uncertain) | `gcloud run services update payment-demo --update-env-vars NEW_THING=x` then `/recheck` | Escalation issue with evidence table |
| D (consistency gate) | Open a PR that breaks docs/runbook.md (delete the FEATURE_NEW_CHECKOUT mention); CI fails. Then a follow-up PR from DriftScribe (Beat A) makes it pass. | Failing then passing check on the PR |

---

## Stretch (only if time permits)

- Phase 10: `/driftscribe sanction` comment hook on escalation issues that flips `allow_manual_change` and reopens as docs PR.
- Phase 11: stale-PR follow-up — agent closes its own PRs after 7d unreviewed.
- Phase 12: Cloud Audit Log → drift via IAM bindings, Cloud SQL config, secret rotations.
