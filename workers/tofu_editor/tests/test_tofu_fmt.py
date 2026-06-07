"""The tofu-editor worker runs ``tofu fmt`` on every authored ``.tf`` file
before committing, so agent-authored HCL is canonical and the required `tofu`
CI check (`tofu -chdir=iac fmt -check`) passes without a manual fixup commit.

Both authoring paths (single-agent ``open_infra_pr_tool`` and the D5 fan-out)
converge on this worker's ``/open-pr``, so formatting HERE benefits both.

These tests use the REAL ``tofu`` binary (the worker's pinned 1.12.0; available
locally in CI/dev), proving the committed content is byte-identical to
``tofu fmt`` output. ``.md`` files are never formatted, and a fmt failure is
fail-soft (the original content is committed; CI stays the backstop).
"""
import os
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("IAC_EDITOR_TARGET_REPO", "adi-prasetyo/driftscribe")
os.environ.setdefault("GITHUB_TOKEN", "test-token")
os.environ.setdefault("OWN_URL", "https://tofu-editor.example.com")
os.environ.setdefault(
    "ALLOWED_CALLERS",
    "driftscribe-agent@test-proj.iam.gserviceaccount.com",
)

from workers.tofu_editor import main as tofu_editor_main  # noqa: E402
from workers.tofu_editor.main import _verify_caller_dep, app  # noqa: E402

_HAS_TOFU = shutil.which("tofu") is not None
_needs_tofu = pytest.mark.skipif(not _HAS_TOFU, reason="tofu binary not on PATH")


def _real_tofu_fmt(content: str) -> str:
    return subprocess.run(
        ["tofu", "fmt", "-"], input=content, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def client(monkeypatch):
    captured: list = []

    def fake_open_iac_pr(repo, **kwargs):
        captured.append({"repo": repo, **kwargs})
        return {
            "url": "https://github.com/adi-prasetyo/driftscribe/pull/123",
            "number": 123,
            "branch": kwargs.get("branch"),
            "labeled": True,
            "label_error": None,
            "reused": False,
        }

    monkeypatch.setattr(tofu_editor_main.ds_github, "open_iac_pr", fake_open_iac_pr)
    monkeypatch.setattr(tofu_editor_main, "_get_repo", lambda: object())
    app.dependency_overrides[_verify_caller_dep] = (
        lambda: "driftscribe-agent@test-proj.iam.gserviceaccount.com"
    )
    yield TestClient(app), captured
    app.dependency_overrides.clear()


# A deliberately un-formatted resource: unaligned `=`, ragged indentation — the
# exact shape that broke the `tofu` CI check on PR #66 (Phase 3).
_MESSY_TF = 'resource "google_storage_bucket" "b" {\nname="x"\n    project = "p"\n}\n'


def _body_with(tf_content: str, *, md: bool = False) -> dict:
    files = [{"path": "iac/bucket.tf", "content": tf_content}]
    if md:
        files.append({"path": "iac/README.md", "content": "# iac\nno  fmt  here\n"})
    return {
        "target_repo": "adi-prasetyo/driftscribe",
        "branch": "infra/fmt-test",
        "base": "main",
        "title": "feat(iac): add bucket",
        "body": "Adds a bucket.",
        "files": files,
    }


@_needs_tofu
def test_open_pr_formats_tf_content_before_commit(client) -> None:
    tc, captured = client
    r = tc.post("/open-pr", json=_body_with(_MESSY_TF))
    assert r.status_code == 200, r.text

    committed = {f["path"]: f["content"] for f in captured[0]["files"]}
    # The committed .tf is byte-identical to `tofu fmt` output (so CI's
    # `tofu fmt -check` passes) and is NOT the raw, ragged input.
    assert committed["iac/bucket.tf"] == _real_tofu_fmt(_MESSY_TF)
    assert committed["iac/bucket.tf"] != _MESSY_TF


@_needs_tofu
def test_open_pr_does_not_format_markdown(client) -> None:
    tc, captured = client
    r = tc.post("/open-pr", json=_body_with(_MESSY_TF, md=True))
    assert r.status_code == 200, r.text

    committed = {f["path"]: f["content"] for f in captured[0]["files"]}
    # The .md passes through verbatim (double spaces and all) — fmt is .tf-only.
    assert committed["iac/README.md"] == "# iac\nno  fmt  here\n"


@_needs_tofu
def test_open_pr_committed_tf_is_fmt_check_clean(client) -> None:
    """End-to-end guarantee: re-running `tofu fmt -check` on the committed .tf
    succeeds (exit 0) — i.e. the worker's output is idempotently formatted."""
    tc, captured = client
    tc.post("/open-pr", json=_body_with(_MESSY_TF))
    committed = captured[0]["files"][0]["content"]

    proc = subprocess.run(
        ["tofu", "fmt", "-check", "-"], input=committed, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_run_tofu_fmt_is_fail_soft_on_error(monkeypatch) -> None:
    """If the tofu binary is missing / errors / rejects the input, the original
    content is returned unchanged (CI's `tofu fmt -check` stays the backstop)."""
    # Simulate a missing/erroring binary.
    def _boom(*a, **k):
        raise OSError("tofu not found")

    monkeypatch.setattr(tofu_editor_main.subprocess, "run", _boom)
    original = 'resource "x" "y" {\nbad="fmt"\n}\n'
    assert tofu_editor_main._run_tofu_fmt(original) == original


def test_run_tofu_fmt_returns_original_on_nonzero_exit(monkeypatch) -> None:
    class _Proc:
        returncode = 2
        stdout = ""
        stderr = "parse error"

    monkeypatch.setattr(tofu_editor_main.subprocess, "run", lambda *a, **k: _Proc())
    original = "definitely { not valid hcl\n"
    assert tofu_editor_main._run_tofu_fmt(original) == original


def test_open_pr_revalidates_size_after_format(client, monkeypatch) -> None:
    """`tofu fmt` can grow a file past MAX_FILE_BYTES even when the RAW payload
    was under it — the post-format revalidation must 422 with NO GitHub side
    effect (Codex review blocker)."""
    # Raw input is tiny (passes the first validate_file_writes); the (faked) fmt
    # output is over MAX_FILE_BYTES (200_000), so the second validation rejects it.
    monkeypatch.setattr(
        tofu_editor_main, "_run_tofu_fmt", lambda c, *a, **k: "x" * 200_001
    )
    tc, captured = client
    r = tc.post("/open-pr", json=_body_with('resource "a" "b" {}\n'))
    assert r.status_code == 422, r.text
    assert captured == []  # fail-closed: open_iac_pr never called


def test_format_tf_files_budget_exhausted_skips_fmt(monkeypatch) -> None:
    """Once the aggregate fmt budget is spent, remaining files are committed
    UNFORMATTED (fail-soft) and `tofu fmt` is not invoked — bounding worst-case
    request time under the coordinator's worker HTTP timeout (Codex review)."""
    calls: list = []
    monkeypatch.setattr(
        tofu_editor_main, "_run_tofu_fmt", lambda c, *a, **k: calls.append(c) or "FMT"
    )
    monkeypatch.setattr(tofu_editor_main, "_TOFU_FMT_TOTAL_BUDGET_S", 0.0)

    out = tofu_editor_main._format_tf_files([{"path": "iac/a.tf", "content": "raw"}])
    assert out == [{"path": "iac/a.tf", "content": "raw"}]  # unchanged
    assert calls == []  # fmt skipped


def test_format_tf_files_within_budget_formats(monkeypatch) -> None:
    """Sanity counter-test: with budget available, .tf files ARE formatted."""
    monkeypatch.setattr(tofu_editor_main, "_run_tofu_fmt", lambda c, *a, **k: "FMT")
    monkeypatch.setattr(tofu_editor_main, "_TOFU_FMT_TOTAL_BUDGET_S", 60.0)

    out = tofu_editor_main._format_tf_files(
        [{"path": "iac/a.tf", "content": "raw"}, {"path": "iac/R.md", "content": "raw"}]
    )
    assert out[0]["content"] == "FMT"  # .tf formatted
    assert out[1]["content"] == "raw"  # .md untouched
