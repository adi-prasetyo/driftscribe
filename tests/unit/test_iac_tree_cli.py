"""Tests for the C6 tools CLIs: tools.iac_tree_hash + tools.iac_tree_sidecar.

These are thin `python -m` wrappers over driftscribe_lib.iac_tree (which has its own
unit tests). Here we exercise the CLI contract the plan-builder workflow relies on:
exit codes, stdout shape, env handling.
"""

import json

from driftscribe_lib.iac_tree import iac_tree_hash
from tools import iac_tree_hash as hash_cli
from tools import iac_tree_sidecar as sidecar_cli

_HEAD = "a" * 40
_BASE = "b" * 40
_SHA = "c" * 64
_JSON_SHA = "d" * 64
_TREE = "e" * 64


def _sidecar_env(**overrides):
    base = dict(
        META_REPO="adi-prasetyo/driftscribe",
        META_PR_NUMBER="42",
        META_HEAD_SHA=_HEAD,
        META_BASE_SHA=_BASE,
        META_WORKFLOW_RUN_ID="1234567890",
        META_WORKFLOW_RUN_ATTEMPT="1",
        META_PLAN_SHA256=_SHA,
        META_PLAN_JSON_SHA256=_JSON_SHA,
        IAC_TREE_HASH=_TREE,
    )
    base.update(overrides)
    return base


# --- iac_tree_hash CLI ----------------------------------------------------


def test_hash_cli_prints_lib_hash(tmp_path, capsys):
    (tmp_path / "main.tf").write_text("resource x {}")
    rc = hash_cli._main([str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert out == iac_tree_hash(tmp_path)  # no trailing newline
    assert "\n" not in out


def test_hash_cli_fails_closed_on_missing_dir(tmp_path, capsys):
    rc = hash_cli._main([str(tmp_path / "nope")])
    assert rc == 1
    assert capsys.readouterr().out == ""  # nothing on stdout — fail-closed


# --- iac_tree_sidecar CLI -------------------------------------------------


def test_sidecar_cli_emits_canonical_json(capsys):
    rc = sidecar_cli._main(_sidecar_env())
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["schema_version"] == "c6.v1"
    assert parsed["iac_tree_hash"] == _TREE
    assert parsed["plan_sha256"] == _SHA
    assert parsed["repo"] == "adi-prasetyo/driftscribe"
    assert out.endswith("\n")


def test_sidecar_cli_missing_env_exits_2(capsys):
    env = _sidecar_env()
    del env["IAC_TREE_HASH"]
    rc = sidecar_cli._main(env)
    assert rc == 2


def test_sidecar_cli_malformed_exits_1(capsys):
    rc = sidecar_cli._main(_sidecar_env(IAC_TREE_HASH="not-hex"))
    assert rc == 1
