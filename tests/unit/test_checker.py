from pathlib import Path
from checker.cli import check_docs_cover_contract

CONTRACT = """
service: payment-demo
environment: production
cloud_run_service: payment-demo
region: asia-northeast1
github_repo: theghostsquad00/driftscribe
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
    docs = tmp_path / "docs"
    docs.mkdir()
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


def test_fails_friendly_on_empty_contract(tmp_path):
    p = tmp_path / "empty.yaml"
    p.write_text("")
    r = check_docs_cover_contract(p, tmp_path)
    assert not r.ok
    assert any("empty" in f or "mapping" in f for f in r.failures)


def test_fails_friendly_on_entry_missing_docs(tmp_path):
    contract = """
service: x
environment: production
cloud_run_service: x
region: asia-northeast1
github_repo: x/x
expected_env:
  ORPHAN:
    value: "1"
    allow_manual_change: false
"""
    p = tmp_path / "ops-contract.yaml"
    p.write_text(contract)
    r = check_docs_cover_contract(p, tmp_path)
    assert not r.ok
    assert any("ORPHAN" in f and "docs" in f for f in r.failures)


def test_fails_friendly_on_entry_missing_section(tmp_path):
    contract = """
service: x
environment: production
cloud_run_service: x
region: asia-northeast1
github_repo: x/x
expected_env:
  PARTIAL:
    value: "1"
    docs: { file: docs/r.md }
    allow_manual_change: false
"""
    p = tmp_path / "ops-contract.yaml"
    p.write_text(contract)
    r = check_docs_cover_contract(p, tmp_path)
    assert not r.ok
    assert any("PARTIAL" in f and "section" in f for f in r.failures)
