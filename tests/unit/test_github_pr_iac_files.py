"""Unit tests for driftscribe_lib.github.list_pr_iac_tf_files.

Returns the OpenTofu source files a PR adds/modifies under ``iac/``, fetched at
the PR head SHA, for the approval page's "view source" affordance. Mirrors the
PyGithub-mock style of the other github unit tests — no network. Covers the
path/status/extension filter, deterministic ordering, fetch-at-head_sha, and the
size caps (per-file omission + count/total truncation) that keep a Firestore
cache doc under the 1 MiB limit.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from github import UnknownObjectException

from driftscribe_lib import github as gh


class _FakeFile:
    def __init__(self, filename: str, status: str = "added"):
        self.filename = filename
        self.status = status


class _FakeContent:
    def __init__(self, raw: bytes):
        self.decoded_content = raw


class _FakeRepo:
    def __init__(self, files, contents):
        self._files = files
        self._contents = contents
        self.get_contents_calls: list[tuple[str, str | None]] = []

    def get_pull(self, n):
        pull = MagicMock()
        pull.get_files.return_value = self._files
        return pull

    def get_contents(self, path, ref=None):
        self.get_contents_calls.append((path, ref))
        raw = self._contents.get(path)
        if raw is None:
            raise UnknownObjectException(404, {"message": "Not Found"}, None)
        return _FakeContent(raw)


_HEAD = "a" * 40


def test_filters_to_iac_tf_added_or_modified_sorted_and_fetches_at_head():
    files = [
        _FakeFile("iac/b.tf", "modified"),
        _FakeFile("iac/a.tf", "added"),
        _FakeFile("README.md", "added"),          # not under iac/
        _FakeFile("iac/notes.txt", "added"),       # not .tf
        _FakeFile("iac/gone.tf", "removed"),        # removed status
        _FakeFile("app/main.tf", "added"),          # .tf but not under iac/
    ]
    contents = {
        "iac/a.tf": b'resource "a" {}\n',
        "iac/b.tf": b'resource "b" {}\n',
    }
    repo = _FakeRepo(files, contents)

    out = gh.list_pr_iac_tf_files(repo, 42, _HEAD)

    assert out["truncated"] is False
    assert [f["path"] for f in out["files"]] == ["iac/a.tf", "iac/b.tf"]  # sorted
    assert out["files"][0]["content"] == 'resource "a" {}\n'
    # Every content fetch is pinned to the PR head SHA, not a branch name.
    assert all(ref == _HEAD for (_p, ref) in repo.get_contents_calls)
    assert set(p for (p, _r) in repo.get_contents_calls) == {"iac/a.tf", "iac/b.tf"}


def test_empty_when_no_iac_tf_changes():
    repo = _FakeRepo([_FakeFile("README.md", "added")], {})
    out = gh.list_pr_iac_tf_files(repo, 42, _HEAD)
    assert out == {"files": [], "truncated": False}


def test_per_file_too_large_is_listed_with_none_content():
    big = b"x" * 50
    contents = {"iac/big.tf": big, "iac/small.tf": b"y\n"}
    repo = _FakeRepo(
        [_FakeFile("iac/big.tf"), _FakeFile("iac/small.tf")], contents
    )
    out = gh.list_pr_iac_tf_files(repo, 42, _HEAD, max_bytes_per_file=10)
    by_path = {f["path"]: f for f in out["files"]}
    assert by_path["iac/big.tf"]["content"] is None  # omitted, still listed
    assert by_path["iac/big.tf"]["bytes"] == 50
    assert by_path["iac/small.tf"]["content"] == "y\n"


def test_total_bytes_cap_truncates_remaining_files():
    contents = {"iac/a.tf": b"a" * 8, "iac/b.tf": b"b" * 8, "iac/c.tf": b"c" * 8}
    repo = _FakeRepo(
        [_FakeFile("iac/a.tf"), _FakeFile("iac/b.tf"), _FakeFile("iac/c.tf")],
        contents,
    )
    out = gh.list_pr_iac_tf_files(repo, 42, _HEAD, max_total_bytes=10)
    assert out["truncated"] is True
    # First file fits (8 <= 10); second would exceed total → dropped.
    assert [f["path"] for f in out["files"]] == ["iac/a.tf"]


def test_file_count_cap_truncates():
    files = [_FakeFile(f"iac/f{i}.tf") for i in range(5)]
    contents = {f"iac/f{i}.tf": b"z\n" for i in range(5)}
    repo = _FakeRepo(files, contents)
    out = gh.list_pr_iac_tf_files(repo, 42, _HEAD, max_files=2)
    assert out["truncated"] is True
    assert len(out["files"]) == 2
    assert [f["path"] for f in out["files"]] == ["iac/f0.tf", "iac/f1.tf"]


def test_path_traversal_segments_are_skipped():
    repo = _FakeRepo(
        [_FakeFile("iac/../secrets/evil.tf"), _FakeFile("iac/ok.tf")],
        {"iac/ok.tf": b"ok\n", "iac/../secrets/evil.tf": b"nope\n"},
    )
    out = gh.list_pr_iac_tf_files(repo, 42, _HEAD)
    assert [f["path"] for f in out["files"]] == ["iac/ok.tf"]
