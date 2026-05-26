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
