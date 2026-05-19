import pytest
from agent.contract import load_contract

QUOTED = """
service: payment-demo
environment: production
cloud_run_service: payment-demo
region: asia-northeast1
github_repo: theghostsquad00/driftscribe
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

def test_operator_note_with_newline_rejected(tmp_path):
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
    operator_note: "line one\\nline two"
""")
    with pytest.raises(Exception, match="single line"):
        load_contract(p)


def test_yaml_null_value_rejected(tmp_path):
    # ~ in YAML is null; must not silently become the string "None"
    p = tmp_path / "c.yaml"
    p.write_text("""
service: x
environment: production
cloud_run_service: x
region: asia-northeast1
github_repo: x/x
expected_env:
  FLAG:
    value: ~
    docs: { file: docs/r.md, section: S }
    allow_manual_change: false
""")
    with pytest.raises(Exception, match="string"):
        load_contract(p)

def test_load_contract_wraps_yaml_parse_error_with_path(tmp_path):
    p = tmp_path / "broken.yaml"
    p.write_text("service: [unbalanced\n")
    with pytest.raises(ValueError, match="broken.yaml"):
        load_contract(p)

def test_load_contract_wraps_missing_file_with_path(tmp_path):
    missing = tmp_path / "does-not-exist.yaml"
    with pytest.raises(FileNotFoundError, match="does-not-exist.yaml"):
        load_contract(missing)

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
