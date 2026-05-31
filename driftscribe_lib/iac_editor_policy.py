"""Deterministic, fail-closed file-write policy for the ``tofu-editor`` worker.

PURE + offline: no FastAPI, no ``agent.*`` import, no network. This module
validates the file-writes / branch / base that the future ``tofu-editor``
worker (Phase D) will commit and open as ONE ``iac/``-only pull request. It
reuses the static gate's path/foundation/suffix constants
(:mod:`tools.iac_static_gate`) as the single source of truth so the worker's
file-level policy and CI's content gate cannot drift apart.

The trust split (design §3.4, §4): the editor only writes HCL *text* and opens
PRs; HCL *content* policy (providers/modules/provisioners/secrets) is the
existing CI static gate (AGENT mode) on the resulting PR. This module is the
file-level allowlist + foundation guard + branch/base/size bounds.

``EditorPolicyError.status_code`` distinguishes policy/authorization failures
(403) from schema-shaped/empty/oversize failures (422); the worker maps these
straight onto ``HTTPException``.
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass

from tools.iac_static_gate import (
    ALLOWED_AGENT_DOC_SUFFIX,
    ALLOWED_AGENT_SUFFIX,
    IAC_PREFIX,
    PROTECTED_FOUNDATION,
)

ALLOWED_BRANCH_PREFIX = "infra/"
ALLOWED_BASE = "main"
EDITOR_LABEL = "driftscribe-infra"

# Constants are plain strings (".tf"/".md") — wrap, do NOT iterate them.
_ALLOWED_SUFFIXES = {ALLOWED_AGENT_SUFFIX, ALLOWED_AGENT_DOC_SUFFIX}
_FOUNDATION = frozenset(PROTECTED_FOUNDATION)   # PROTECTED_FOUNDATION is a tuple

# DoS bounds (Codex IMPORTANT): multi-file authoring is a bigger surface than
# the single-file upgrade-docs worker.
MAX_FILES = 32
MAX_FILE_BYTES = 200_000
MAX_TITLE = 200
MAX_BODY = 20_000

# Mirror workers/upgrade_docs/main.py::_BRANCH_TAIL.
_BRANCH_TAIL = re.compile(r"[A-Za-z0-9._/-]{1,200}\Z")


@dataclass
class EditorPolicyError(Exception):
    status_code: int   # 403 = policy, 422 = schema-shaped
    reason: str

    def __str__(self) -> str:
        return f"{self.status_code}: {self.reason}"


def _validate_one_path(path: str) -> None:
    if not path or path != path.strip():
        raise EditorPolicyError(422, f"empty/whitespace path: {path!r}")
    if path.startswith("/"):
        raise EditorPolicyError(403, f"absolute path forbidden: {path!r}")
    if path != posixpath.normpath(path) or ".." in path.split("/"):
        raise EditorPolicyError(403, f"non-normalized path forbidden: {path!r}")
    if not path.startswith(IAC_PREFIX):
        raise EditorPolicyError(403, f"path must be under {IAC_PREFIX!r}: {path!r}")
    if posixpath.splitext(path)[1] not in _ALLOWED_SUFFIXES:
        raise EditorPolicyError(403, f"suffix not allowed: {path!r}")
    if path in _FOUNDATION:
        raise EditorPolicyError(403, f"foundation file is operator-only: {path!r}")


def validate_file_writes(writes: list[dict]) -> list[dict]:
    if not writes:
        raise EditorPolicyError(422, "no file writes supplied")
    if len(writes) > MAX_FILES:
        raise EditorPolicyError(422, f"too many files (> {MAX_FILES})")
    seen: set[str] = set()
    for w in writes:
        path, content = w.get("path", ""), w.get("content", "")
        _validate_one_path(path)
        if path in seen:
            raise EditorPolicyError(403, f"duplicate path: {path!r}")
        seen.add(path)
        if not content or not content.strip():
            raise EditorPolicyError(422, f"empty content for {path!r}")
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise EditorPolicyError(422, f"file too large: {path!r}")
    return writes


def validate_branch(branch: str) -> None:
    if not branch.startswith(ALLOWED_BRANCH_PREFIX):
        raise EditorPolicyError(403, f"branch must start with {ALLOWED_BRANCH_PREFIX!r}")
    tail = branch[len(ALLOWED_BRANCH_PREFIX):]
    if not tail or ".." in branch or not _BRANCH_TAIL.fullmatch(tail):
        raise EditorPolicyError(403, f"invalid branch ref: {branch!r}")


def validate_base(base: str) -> None:
    if base != ALLOWED_BASE:
        raise EditorPolicyError(403, f"base must be {ALLOWED_BASE!r}")
