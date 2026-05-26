import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parents[2])


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _rev_parse(repo, ref="HEAD"):
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=repo, capture_output=True, text=True
    ).stdout.strip()


def _run_gate(repo, base, head, mode):
    return subprocess.run(
        [sys.executable, "-m", "tools.iac_static_gate",
         "--base", base, "--head", head, "--mode", mode],
        cwd=repo, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": _REPO_ROOT},
    )


def test_cli_rejects_provisioner_pr(tmp_path: Path):
    repo = tmp_path
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "iac").mkdir()
    (repo / "iac" / "main.tf").write_text('resource "google_x" "y" {}\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _rev_parse(repo)
    (repo / "iac" / "main.tf").write_text(
        'resource "google_x" "y" { provisioner "local-exec" { command = "id" } }\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "bad")
    head = _rev_parse(repo)

    proc = _run_gate(repo, base, head, "agent")
    assert proc.returncode != 0
    assert "arbitrary-execution" in (proc.stdout + proc.stderr)


def test_cli_clean_iac_change_exits_zero(tmp_path: Path):
    repo = tmp_path
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "iac").mkdir()
    (repo / "iac" / "main.tf").write_text('resource "google_x" "y" {}\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _rev_parse(repo)
    (repo / "iac" / "main.tf").write_text(
        'resource "google_cloud_run_v2_service" "y" { name = "payment-demo" }\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "clean")
    head = _rev_parse(repo)

    proc = _run_gate(repo, base, head, "agent")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_cli_scans_content_of_unicode_named_file_in_operator_mode(tmp_path: Path):
    # core.quotePath would otherwise return `"iac/caf\303\251.tf"`, so _git_show
    # reads the wrong path and the CONTENT is never structurally scanned. In
    # operator mode there is no path rule, so an unscanned provisioner would
    # slip through. The gate must scan the content regardless of the name.
    repo = tmp_path
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "iac").mkdir()
    (repo / "iac" / "base.tf").write_text('resource "google_x" "y" {}\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _rev_parse(repo)
    (repo / "iac" / "café.tf").write_text(
        'resource "google_x" "y" { provisioner "local-exec" { command = "id" } }\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "unicode")
    head = _rev_parse(repo)

    proc = _run_gate(repo, base, head, "operator")
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "arbitrary-execution" in (proc.stdout + proc.stderr)


def test_cli_operator_tf_json_not_falsely_parse_errored(tmp_path: Path):
    # JSON-syntax HCL is not structurally analyzed in v1 (hcl2.loads can't
    # parse it). It is hard-rejected in agent mode via disallowed-file-type;
    # in operator mode it is governed by human review + CODEOWNERS. The CLI
    # must NOT read its content (which would always yield a spurious
    # hcl-parse-error), so a legit operator .tf.json change exits clean.
    repo = tmp_path
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "iac").mkdir()
    (repo / "iac" / "base.tf").write_text('resource "google_x" "y" {}\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _rev_parse(repo)
    (repo / "iac" / "config.tf.json").write_text(
        '{"resource": {"google_x": {"y": {}}}}\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "json")
    head = _rev_parse(repo)

    proc = _run_gate(repo, base, head, "operator")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "hcl-parse-error" not in (proc.stdout + proc.stderr)


def test_cli_tolerates_deleted_iac_file(tmp_path: Path):
    # A change that DELETES an iac/.tf file: git show <head>:<path> fails, the
    # file has no content to gate, so it must be skipped without crashing.
    repo = tmp_path
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "iac").mkdir()
    (repo / "iac" / "keep.tf").write_text('resource "google_x" "y" {}\n')
    (repo / "iac" / "gone.tf").write_text('resource "google_x" "z" {}\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _rev_parse(repo)
    (repo / "iac" / "gone.tf").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "delete")
    head = _rev_parse(repo)

    proc = _run_gate(repo, base, head, "agent")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Traceback" not in (proc.stdout + proc.stderr)


def test_cli_rejects_change_outside_iac_in_agent_mode(tmp_path: Path):
    repo = tmp_path
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "iac").mkdir()
    (repo / "iac" / "main.tf").write_text('resource "google_x" "y" {}\n')
    (repo / ".github").mkdir()
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _rev_parse(repo)
    (repo / ".github" / "ci.yml").write_text("on: push\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "outside")
    head = _rev_parse(repo)

    proc = _run_gate(repo, base, head, "agent")
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "path-outside-iac" in (proc.stdout + proc.stderr)


def test_cli_operator_foundation_edit_exits_zero(tmp_path: Path):
    # Operator mode may touch foundation files (backend/encryption/providers).
    repo = tmp_path
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "iac").mkdir()
    (repo / "iac" / "versions.tf").write_text(
        'terraform { required_providers { google = { source = "hashicorp/google" } } }\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _rev_parse(repo)
    (repo / "iac" / "versions.tf").write_text(
        'terraform {\n'
        '  required_providers { google = { source = "hashicorp/google" } }\n'
        '  backend "gcs" { bucket = "b" }\n'
        '}\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "foundation")
    head = _rev_parse(repo)

    proc = _run_gate(repo, base, head, "operator")
    assert proc.returncode == 0, proc.stdout + proc.stderr
