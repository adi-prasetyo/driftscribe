"""Tests for the `WorkloadSpec` pydantic schema (Phase 17.A.1).

The schema is the parsed shape of `workloads/<name>/workload.yaml`. It
carries *symbolic* names only — tool names, worker names, action names —
which the registry resolves to real callables and `WorkerEndpoint`s at
load time. Real URLs, secrets, repos, audiences NEVER appear in YAML;
that's a Codex-flagged security blocker (see Phase 17 plan header).

These tests pin three properties:

1. A valid drift YAML parses into a `WorkloadSpec`.
2. Missing required fields raise `ValidationError`.
3. An unknown `name` (not in the
   `Literal["drift", "upgrade", "explore", "provision"]` set) is
   rejected at the schema layer, not later by the registry.
"""
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from agent.workloads.spec import WorkloadSpec


VALID_DRIFT_YAML = """\
name: drift
display_name: Cloud Run env drift
descriptor: Cloud Run config
description: Detect drift between live Cloud Run env vars and the declared contract.
system_prompt_file: system_prompt.txt
contract_file: ../../demo/ops-contract.yaml
enabled_tool_names:
  - drift_read_live_env
  - drift_patch_docs
  - drift_propose_rollback
  - notify
  - load_contract
  - search_recent_prs
worker_names:
  - drift_reader
  - drift_docs
  - drift_rollback
  - notifier
observation_kind: cloud_run_env
action_names:
  - docs_pr
  - drift_issue
  - escalation
  - no_op
  - rollback
"""


def _parse(yaml_text: str) -> WorkloadSpec:
    return WorkloadSpec.model_validate(yaml.safe_load(yaml_text))


def test_valid_drift_yaml_parses():
    spec = _parse(VALID_DRIFT_YAML)
    assert spec.name == "drift"
    assert spec.display_name == "Cloud Run env drift"
    assert spec.descriptor == "Cloud Run config"
    assert spec.system_prompt_file == "system_prompt.txt"
    assert spec.contract_file == "../../demo/ops-contract.yaml"
    assert "drift_read_live_env" in spec.enabled_tool_names
    assert "drift_reader" in spec.worker_names
    assert spec.observation_kind == "cloud_run_env"
    assert "rollback" in spec.action_names


def test_contract_file_may_be_null():
    """Upgrade workload won't have a YAML contract (Phase 17.C uses
    repo lockfile + advisory feed instead). Schema must allow that."""
    spec = _parse(VALID_DRIFT_YAML.replace(
        "contract_file: ../../demo/ops-contract.yaml",
        "contract_file: null",
    ))
    assert spec.contract_file is None


def test_missing_required_field_raises_validation_error():
    # Drop display_name.
    bad = VALID_DRIFT_YAML.replace(
        "display_name: Cloud Run env drift\n", "",
    )
    with pytest.raises(ValidationError, match="display_name"):
        _parse(bad)


def test_missing_descriptor_raises_validation_error():
    # descriptor is required (Phase 17.G crew rename) — a workload with no
    # domain subtitle is a manifest bug, not a silent empty string.
    bad = VALID_DRIFT_YAML.replace("descriptor: Cloud Run config\n", "")
    with pytest.raises(ValidationError, match="descriptor"):
        _parse(bad)


def test_unknown_workload_name_rejected_at_literal_layer():
    bad = VALID_DRIFT_YAML.replace("name: drift", "name: kubernetes")
    with pytest.raises(ValidationError, match="name"):
        _parse(bad)


def test_unknown_observation_kind_rejected_at_literal_layer():
    bad = VALID_DRIFT_YAML.replace(
        "observation_kind: cloud_run_env",
        "observation_kind: bigquery_table",
    )
    with pytest.raises(ValidationError, match="observation_kind"):
        _parse(bad)


def test_extra_fields_in_yaml_rejected():
    """Defense in depth: a typo or sneaky extra field in YAML must not
    silently extend the schema. extra='forbid' makes that loud."""
    bad = VALID_DRIFT_YAML + "secret_url: https://attacker.example.com\n"
    with pytest.raises(ValidationError, match="secret_url"):
        _parse(bad)


def test_enabled_tool_names_must_be_strings():
    bad = VALID_DRIFT_YAML.replace(
        "  - drift_read_live_env", "  - 42",
    )
    with pytest.raises(ValidationError):
        _parse(bad)


def test_repo_workload_yaml_files_exist_and_parse(tmp_path):
    """The two shipped workload manifests must parse cleanly. We don't
    instantiate the registry here (that's `test_workload_registry`) —
    just that the YAML is valid against the schema."""
    repo_root = Path(__file__).resolve().parents[2]
    for name in ("drift", "upgrade", "explore", "provision"):
        path = repo_root / "workloads" / name / "workload.yaml"
        assert path.exists(), f"missing workload manifest: {path}"
        WorkloadSpec.model_validate(yaml.safe_load(path.read_text()))
