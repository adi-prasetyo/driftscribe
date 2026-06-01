# Phase D — Agent authoring + fan-out (IaC editor) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or
> superpowers:subagent-driven-development) to implement this plan task-by-task.
> The two new write surfaces — the `tofu-editor` worker (D1-3) and the
> coordinator editor tool (D2-2) — get a multi-lens adversarial review before
> merge, same discipline as the C4/C6 sole-mutator edits.

**Status:** rev-2 (author: agent, 2026-06-01). **Two Codex review rounds folded**
(threads `019e7ee4` + `019e7ee9`, both read-only). All BLOCKERS from those
rounds are corrected inline below; see §"Review history".

**Goal:** Let an operator ask the coordinator, in chat, to make an
infrastructure change, and have the LLM **author the OpenTofu (HCL) edit itself**
and open exactly ONE `iac/`-only pull request — which then flows through the
**existing, unchanged** Phase C/C6 gated-apply pipeline (static gate → C2
plan-builder → C3 approval → C4 `tofu-apply`).

**Architecture:** Add a new write-capable **`provision`** workload (chat-only,
no autonomous `/recheck`) whose single mutation tool routes to a new
**`tofu-editor`** Cloud Run worker. The editor worker holds ZERO infra
credentials — its only capability is a write-scoped GitHub PAT to open ONE PR,
branch `infra/<slug>`, base `main`, label `driftscribe-infra`, writing **only**
under `iac/` (file-level allowlist, foundation-protected, fail-closed). The
coordinator agent authors all HCL slices for a change and submits them in one
editor call → one branch → one commit → one PR. The dangerous apply floor is
reused **byte-for-byte**: nothing in C2/C3/C4/C6 changes. This mirrors the
existing trust split — `workers/upgrade_docs` (opens PRs) + `workers/tofu_apply`
(sole mutator) — exactly.

**Tech Stack:** Python 3.12, FastAPI, PyGithub, OpenTofu 1.12.0 (CI/worker
only — the editor never runs `tofu`), Cloud Run, GitHub Actions WIF, pytest
(`uv run pytest`), ruff.

---

## 0. Context & invariants (read before touching code)

**Predecessors (all merged + proven live):** Phase A (`f08932f`), B (`e71f778`),
C1 (`ed26d7a`), C2 (`a689d8e`), C3 (`180281c`), C4 (`fd9bc32`), C5a–g
(PRs #21–#33), C6 (PRs #41–#49).

**Design parent:** `docs/plans/2026-05-27-infra-iac-agent-design.md` §3.4, §4,
§8 "Phase D", §9, §11 decision 7.

**Non-negotiable trust boundary (do not relitigate):** editor agents and the
`tofu-editor` worker ONLY write HCL text and open PRs (zero infra creds, zero
live-mutation). `tofu-apply` remains the sole mutator. Phase D adds only the
authoring front end.

**Codex security verdict (rev-2):** with the worker exposing only `/open-pr`, the
coordinator calling only `call_open_infra_pr`, and C2/C3/C4 unchanged, there is
**no apply-without-human path** — the new surface is PR *text* only, contained by
the static gate + C1 denylist + human approval + apply-worker re-verify. The
fixes below close the residual gaps Codex flagged.

**Forward-compat hooks ALREADY in the tree (verified):**
- `driftscribe_lib/iac_plan_denylist.py` hard-denies the editor **service**
  (`driftscribe-tofu-editor`, `tofu-editor` in `CONTROL_PLANE_SERVICE_NAMES`)
  and **SA** (`tofu-editor-sa` in `CONTROL_PLANE_SA_ACCOUNT_IDS`).
  **GAP (Codex BLOCKER):** `CONTROL_PLANE_SECRET_IDS` does **NOT** yet include
  `tofu-editor-github-pat` — added in **D1-0** below.
- `tools/iac_static_gate.py` `GateMode.AGENT` enforces, in CI, the HCL content
  policy. **AGENT mode is selected by the `.github/workflows/iac.yml` shell as
  `HAS_INFRA_LABEL == true` OR `HEAD_REF` starts with `infra/`** (an **OR**, not
  an AND — Codex IMPORTANT). The editor's output PRs satisfy both anyway (label
  `driftscribe-infra` + branch `infra/...`).
- Verified static-gate exports: `IAC_PREFIX = "iac/"`; `ALLOWED_AGENT_SUFFIX =
  ".tf"` and `ALLOWED_AGENT_DOC_SUFFIX = ".md"` (**plain strings**);
  `PROTECTED_FOUNDATION` (**a tuple**) = `("iac/.terraform.lock.hcl",
  "iac/versions.tf", "iac/providers.tf", "iac/variables.tf", "iac/imports.tf")`
  — **`iac/backend.tf` is NOT in it** (backend lives in `versions.tf`). The
  in-process entry point is **`evaluate(GateInput(mode, changed_paths,
  hcl_files))`** → `list[Violation]`; the CLI is `main(argv)`.

**Branch-prefix convention (critical):** Phase D is `workers/`/`agent/`/`infra/`/
`tools/`/`tests/` code; it does NOT author `iac/*.tf`. Every Phase D PR uses a
**`feat/iac-phase-d-...`** branch — NOT `infra/` (which would trip static-gate
AGENT mode on the Python diff). `infra/` + `driftscribe-infra` is reserved for
the *output* PRs the worker opens at runtime.

**Test/lint:** `uv run pytest` (testpaths `tests/unit`, `tests/integration`,
`workers`; `tests/e2e` excluded). `ruff check`. ~1808 tests green as of C6; keep
green every task.

**CODEOWNERS** requires `@adi-prasetyo` review on `/infra/scripts/`, `/.github/`,
and the IaC gate/denylist/approval files (`/tools/iac_static_gate.py`,
`/tools/iac_plan_*.py`, `/driftscribe_lib/iac_plan_denylist.py`, …). D1-0, D1-4,
and D3-1 touch CODEOWNERS-protected files → owner review.

---

## 1. Scope & non-goals (YAGNI — Codex confirmed this is the right call)

**In scope (Phase D v1):** the `tofu-editor` worker (one `iac/`-only PR from a
set of file writes), the `provision` chat workload, one coordinator tool
(`provision_open_infra_pr`), deploy/IAM/docs, an operator-gated live e2e.

**Deferred / out of scope (state in the PR; do not silently drop):**
- **Parallel sub-agent fan-out.** No sub-agent primitive exists today (the
  coordinator runs a single ADK `Agent` per request). v1 achieves the design's
  load-bearing invariant — **ONE PR carrying all slices** — by having the single
  coordinator agent author every slice and submit them in one editor call (a
  list of file writes → one commit → one PR). Codex verdict: *"Phase D's
  essential capability is one editor call → multiple file writes → one commit →
  one PR → existing approval/apply pipeline. True sub-agent fan-out adds
  coordination/conflict complexity without changing the trust boundary. The
  one-call model is acceptable if duplicate paths are rejected, file count is
  bounded, and retry/conflict behavior is explicit."* See D5 sketch.
- **Provider / module / lockfile authoring** (static-gate forbids; operator-only).
- **Create-class autonomy** — applying a *new* resource still needs the C6
  operator re-bake. v1 demos on an in-place update (the C5 path, no re-bake).
- **`/recheck` autonomy for `provision`** — chat-only, route-refused like
  `explore`.

---

## 2. Key design decisions (rev-2, Codex-reviewed)

1. **Editor writes LLM-authored HCL verbatim; CI is the content gate.** The
   worker enforces *file-level* policy (path under `iac/`, suffix `.tf`/`.md`,
   not foundation, branch/base/label/repo pinned, size bounds). HCL *content*
   policy (providers/modules/provisioners) is the existing CI static gate
   (AGENT mode) on the resulting PR.
2. **One editor call = one branch = one commit = one PR**, carrying a bounded
   list of `{path, content}` writes.
3. **Authority-clean tool surface** (mirror `upgrade_propose_pr_tool`): the LLM
   supplies only `files` + `title` + `body`. Repo (code-side pin), branch
   (`infra/<slug>-<ts>-<hex>`), base (`main`), label (`driftscribe-infra`) are
   derived server-side.
4. **Target repo is a code-side authority pin, not YAML** (mirror
   `UPGRADE_TARGET_REGISTRY`/`resolve_upgrade_target`). The **coordinator**
   resolves it from a registry pin + `IAC_EDITOR_TARGET_REPO_OVERRIDE` env (for
   e2e parity). The **worker** independently pins `IAC_EDITOR_TARGET_REPO` at
   boot and re-validates. A CI parity test ties the two so an e2e redirect can't
   drift them (Codex IMPORTANT). The coordinator tool must NOT read the worker's
   boot env directly.
5. **Names (match the existing forward-compat half-wiring):** worker `tofu_editor`,
   service `driftscribe-tofu-editor`, SA `tofu-editor-sa`, secret
   `tofu-editor-github-pat`, env `TOFU_EDITOR_URL`, canonical endpoint
   `/open-pr`, label `driftscribe-infra`.
6. **`provision` is chat-only**, route-refused on `/recheck`; `action_names: []`.
   It is a NEW workload that is *write-capable* (unlike `explore`), so the
   inventory disjointness tests treat it accordingly (D2-3).
7. **Only the `name` Literal changes in `spec.py`** (`observation_kind` already
   permits `"none"`). **BUT** `agent/main.py` `RecheckRequest.workload` and
   `ChatRequest.workload` are *separate* `Literal["drift","upgrade","explore"]`
   and MUST also gain `"provision"`, else `/chat?workload=provision` 422s before
   routing (Codex BLOCKER — D2-4).
8. **Secret-bearing HCL (NEW surface — Codex BLOCKER, decision required).** An
   authoring agent could try to commit a secret in HCL (e.g.
   `google_secret_manager_secret_version.secret_data`, or a literal credential
   in an env value). The repo's **GitGuardian Security Checks** required status
   check catches recognizable secret patterns on every PR, but not an arbitrary
   `secret_data = "<literal>"`. **Decision (operator-approved 2026-06-01): add
   the AGENT-mode static-gate rule** banning authoring of
   `google_secret_manager_secret_*` resources and inline `secret_data`/
   credential attributes (D1-6), keeping secret material operator-only — same
   spirit as the foundation/provider bans. (The accept-and-document alternative —
   GitGuardian + denylist + human review only — was considered and declined.)
   Note: the C1 denylist already blocks any *plan* that mutates a control-plane
   secret; D1-6 covers *authoring* arbitrary new secret material in HCL.

---

## 3. Slice plan overview

| Slice | Title | Surface | Branch |
|---|---|---|---|
| **D1-0** | Denylist: add `tofu-editor-github-pat` to `CONTROL_PLANE_SECRET_IDS` | `driftscribe_lib/` | `feat/iac-phase-d-denylist-secret` |
| **D1-1** | Editor file-write policy primitives (lib, pure) | `driftscribe_lib/` | `feat/iac-phase-d-editor-policy` |
| **D1-2** | Multi-file PR helper `open_iac_pr` | `driftscribe_lib/github.py` | `feat/iac-phase-d-github-multifile` |
| **D1-3** | `tofu-editor` worker `/open-pr` (NEW WRITE SURFACE — adversarial review) | `workers/tofu_editor/` | `feat/iac-phase-d-editor-worker` |
| **D1-4** | Worker in-process AGENT-mode static-gate pre-check | `workers/tofu_editor/` | `feat/iac-phase-d-editor-gate` |
| **D1-5** | Dockerfile + `infra/cloudbuild.tofu-editor.yaml` + tests | `workers/`, `infra/` | `feat/iac-phase-d-editor-build` |
| **D1-6** | static-gate secret-authoring ban (operator-approved) | `tools/`, `tests/` | `feat/iac-phase-d-secret-gate` |
| **D2-1** | `worker_client` wiring + `call_open_infra_pr` | `agent/worker_client.py` | `feat/iac-phase-d-worker-client` |
| **D2-2** | `open_infra_pr_tool` (authority-clean ADK tool — adversarial review) | `agent/adk_tools.py` | `feat/iac-phase-d-editor-tool` |
| **D2-3** | Registry + spec Literal + adk_agent tool sets + inventory/parity tests | `agent/`, `tests/` | `feat/iac-phase-d-registry` |
| **D2-4** | `provision` workload + request-model Literals + chat-only + UI | `workloads/`, `agent/` | `feat/iac-phase-d-provision-workload` |
| **D3-1** | Deploy/IAM: setup_secrets.sh + iam-matrix + runbook | `infra/`, `docs/` | `feat/iac-phase-d-deploy-iam` |
| **D4** | Operator-gated live deploy + e2e (no code) | — | — |
| **D5** | (Deferred sketch) parallel sub-agent fan-out | — | — |

Order: D1-0 → D1-1 → D1-2 → D1-3 (+adversarial) → D1-4 → D1-5 → D1-6 →
D2-1 → D2-2 (+adversarial) → D2-3 → D2-4 → D3-1 → D4 → D5(future).
Each code slice is its own PR + Codex review round.

---

## Task D1-0: Denylist — protect the editor PAT secret

Close the Codex BLOCKER: the editor's GitHub PAT secret must be on the
self-protection denylist before the worker that uses it exists.

**Files:** Modify `driftscribe_lib/iac_plan_denylist.py`; Test
`tests/unit/test_iac_plan_denylist_lib.py`.

**Steps (TDD):**
1. Add a test asserting a plan that mutates
   `google_secret_manager_secret`/`...secret_version` named `tofu-editor-github-pat`
   is denied (mirror the existing `tofu-apply`/control-plane-secret cases).
2. Run → fail.
3. Add `"tofu-editor-github-pat"` to `CONTROL_PLANE_SECRET_IDS` (next to the
   existing `plan-hmac-key` / coordinator-token entries; keep the
   forward-compat comment style).
4. Run → pass; run the full denylist suite.
5. Commit: `feat(iac): D1-0 — denylist tofu-editor-github-pat secret (forward-compat)`

---

## Task D1-1: Editor file-write policy primitives (lib, pure, offline)

Deterministic, fail-closed file-write policy, reusing the static gate's
constants as the single source of truth. No FastAPI, no `agent.*` import.

**Files:** Create `driftscribe_lib/iac_editor_policy.py`; Test
`tests/unit/test_iac_editor_policy.py`.

**Step 1 — Failing test** (note the corrected foundation cases — NO `backend.tf`;
and the branch-tail rules mirroring `workers/upgrade_docs`):

```python
import pytest
from driftscribe_lib.iac_editor_policy import (
    EditorPolicyError, validate_file_writes, validate_branch, validate_base,
    ALLOWED_BRANCH_PREFIX, ALLOWED_BASE, EDITOR_LABEL,
)

def _w(path, content="resource x {}\n"):
    return {"path": path, "content": content}

def test_accepts_iac_tf_and_md():
    assert validate_file_writes([_w("iac/cloudrun.tf"), _w("iac/README.md")])

def test_rejects_path_outside_iac():
    with pytest.raises(EditorPolicyError) as e:
        validate_file_writes([_w("agent/main.py")])
    assert e.value.status_code == 403

def test_rejects_absolute_and_traversal():
    for bad in ("/iac/x.tf", "iac/../agent/x.tf", "iac/./../x.tf", "iac//x.tf"):
        with pytest.raises(EditorPolicyError):
            validate_file_writes([_w(bad)])

def test_rejects_non_tf_md_suffix():
    for bad in ("iac/evil.sh", "iac/x.tofu", "iac/x.tf.json", "iac/x.tfvars"):
        with pytest.raises(EditorPolicyError):
            validate_file_writes([_w(bad)])

def test_rejects_foundation_files():
    # backend.tf is NOT protected (backend lives in versions.tf) — do not list it.
    for f in ("iac/versions.tf", "iac/providers.tf", "iac/variables.tf",
              "iac/imports.tf", "iac/.terraform.lock.hcl"):
        with pytest.raises(EditorPolicyError) as e:
            validate_file_writes([_w(f)])
        assert e.value.status_code == 403

def test_rejects_empty_list_dupes_empty_content():
    with pytest.raises(EditorPolicyError): validate_file_writes([])
    with pytest.raises(EditorPolicyError): validate_file_writes([_w("iac/a.tf"), _w("iac/a.tf")])
    with pytest.raises(EditorPolicyError): validate_file_writes([_w("iac/a.tf", content="")])

def test_size_bounds():
    big = "x" * (200_001)
    with pytest.raises(EditorPolicyError):
        validate_file_writes([_w("iac/a.tf", content=big)])     # per-file cap
    with pytest.raises(EditorPolicyError):
        validate_file_writes([_w(f"iac/f{i}.tf") for i in range(33)])  # file-count cap

def test_branch_rules():
    validate_branch("infra/add-bucket-x-20260601-ab12cd")
    for bad in ("upgrade/x", "infra/", "infra/..", "infra/a b", "infra/" + "z"*300):
        with pytest.raises(EditorPolicyError):
            validate_branch(bad)

def test_base_and_constants():
    validate_base("main")
    with pytest.raises(EditorPolicyError): validate_base("dev")
    assert ALLOWED_BRANCH_PREFIX == "infra/" and ALLOWED_BASE == "main"
    assert EDITOR_LABEL == "driftscribe-infra"
```

**Step 3 — Implement** (note `_ALLOWED_SUFFIXES = {ALLOWED_AGENT_SUFFIX,
ALLOWED_AGENT_DOC_SUFFIX}` — the constants are STRINGS; `frozenset(PROTECTED_
FOUNDATION)` for membership since it's a tuple; branch-tail regex mirrors
`workers/upgrade_docs/main.py::_BRANCH_TAIL`):

```python
# driftscribe_lib/iac_editor_policy.py
from __future__ import annotations
import posixpath
import re
from dataclasses import dataclass
from tools.iac_static_gate import (
    IAC_PREFIX, PROTECTED_FOUNDATION,
    ALLOWED_AGENT_SUFFIX, ALLOWED_AGENT_DOC_SUFFIX,
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
    def __str__(self) -> str: return f"{self.status_code}: {self.reason}"


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
```

> **Pre-step:** open `tools/iac_static_gate.py` and confirm the imported names
> (all verified present at lines 52/58/71/72). Confirm
> `workers/upgrade_docs/main.py::_BRANCH_TAIL` regex and match it.

**Steps 2/4** — run fail then pass: `uv run pytest tests/unit/test_iac_editor_policy.py -v`.

**Step 5 — Commit:**
`feat(iac): D1-1 — tofu-editor file-write policy (lib, fail-closed, size-bounded)`

---

## Task D1-2: Multi-file PR helper `open_iac_pr`

`open_docs_pr` (verified signature) takes a **`Repository`** object and a single
`file_path`/`new_content`, and `_finalize_pr` hard-labels `"driftscribe","docs"`.
Add a sibling that commits a **list** of writes on one branch and labels the PR
`driftscribe-infra` — do NOT reuse `_finalize_pr` (wrong labels).

**Files:** Modify `driftscribe_lib/github.py`; Test `tests/unit/test_github_open_iac_pr.py`.

**Contract (matches the existing idiom):**
```python
def open_iac_pr(
    repo: Repository, *, branch: str, base: str, title: str, body: str,
    files: list[dict], label: str = "driftscribe-infra", dry_run: bool = False,
) -> dict[str, Any]:
    # 1. create_git_ref refs/heads/<branch> off repo.get_branch(base).commit.sha
    #    (reuse _is_already_exists / _find_open_pr_for_head idempotency idiom)
    # 2. for each {path, content}: get_contents(path, ref=branch) → update_file,
    #    else (UnknownObjectException) create_file. One commit message per file
    #    ("feat(iac): author <path>"), branch=<branch>.
    # 3. create_pull(title, body, head=branch, base=base) (reuse the already-exists
    #    backstop), then pr.add_to_labels(label) (best-effort, like _finalize_pr).
    # 4. return {"url": pr.html_url, "number": pr.number, "branch": branch,
    #            "labeled": ..., "reused": ...}
```

**Worker call site** (D1-3) builds the repo via the verified
`get_repo(token, repo_full_name)` (**token first**), then passes the `Repository`.

Test: mock the `Repository`; assert one ref off `base`, create-vs-update routed by
existence, `create_pull(base="main")`, label `driftscribe-infra` applied, and the
return shape. (PR-result shape is `{url, number, ...}`, NOT `pr_url/pr_number` —
the worker adapts.)

Commit: `feat(iac): D1-2 — open_iac_pr multi-file PR helper (lib)`

---

## Task D1-3: `tofu-editor` worker `/open-pr` (NEW WRITE SURFACE)

> **REQUIRED:** multi-lens adversarial review (Workflow) on this slice.

Mirror `workers/upgrade_docs/main.py` shape. **Worker isolation:** MUST NOT
import `agent.*` (assert in a test). Bundles `driftscribe_lib/` +
`tools/iac_static_gate.py` (+ `tools/__init__.py`; the gate imports
`driftscribe_lib.iac_hcl`, already bundled).

**Files:** `workers/tofu_editor/{__init__.py,main.py,pyproject.toml}`,
`workers/tofu_editor/tests/{__init__.py,test_open_pr.py,test_path_allowlist.py,test_no_agent_import.py}`.

**Boot env (mirror upgrade_docs ~97-102):**
```python
TARGET_REPO = os.environ["IAC_EDITOR_TARGET_REPO"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]            # from tofu-editor-github-pat
OWN_URL = os.environ["OWN_URL"].rstrip("/")
ALLOWED_CALLERS = frozenset(e.strip() for e in
    os.environ["ALLOWED_CALLERS"].split(",") if e.strip())   # = driftscribe-agent@ only
```

**Request models (`extra="forbid"`, size-bounded):**
```python
class FileWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    content: str

class OpenIacPrRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_repo: str
    branch: str
    base: str
    title: str
    body: str
    files: list[FileWrite]
```

**`/open-pr` handler:**
1. `caller = Depends(_verify_caller_dep)` (`verify_caller(request, own_url=OWN_URL,
   allowed_callers=ALLOWED_CALLERS)` — mirror upgrade_docs `_verify_caller_dep`).
2. `if req.target_repo != TARGET_REPO: 403`.
3. `validate_base(req.base)`, `validate_branch(req.branch)`,
   `validate_file_writes([f.model_dump() for f in req.files])`,
   plus `len(req.title) <= MAX_TITLE`, `len(req.body) <= MAX_BODY` — all from
   `iac_editor_policy`; map `EditorPolicyError` → `HTTPException`.
4. `repo = get_repo(GITHUB_TOKEN, TARGET_REPO)`;
   `open_iac_pr(repo, branch=req.branch, base="main", title=req.title,
   body=req.body, files=[f.model_dump() for f in req.files])`.
5. Return `{"status": "opened", "pr_number": r["number"], "pr_url": r["url"],
   "branch": r["branch"]}`.

**`test_path_allowlist.py`** — canonical fail-closed idiom (seed env before
import; override `_verify_caller_dep` via `app.dependency_overrides`; monkeypatch
`open_iac_pr` with a capture): for each rejected input (`agent/x.py`, `iac/x.sh`,
`iac/versions.tf`, traversal, wrong base, bad branch, foreign repo, empty files,
oversize) the capture stays **empty** and status is 403/422.
**`test_open_pr.py`** — happy multi-file path → `open_iac_pr` called once with
expected kwargs → 200 with `pr_number`; caller-verify fail → 403; unknown field → 422.
**`test_no_agent_import.py`** — assert the worker module imports cleanly without
`agent` on the path (mirror the upgrade_docs/tofu_apply isolation test).

Commit: `feat(iac): D1-3 — tofu-editor worker /open-pr (iac-only, fail-closed)`

---

## Task D1-4: Worker in-process AGENT-mode static-gate pre-check

Fail fast before opening a junk PR. Use the verified entry point.

**Files:** Modify `workers/tofu_editor/main.py`; Test
`workers/tofu_editor/tests/test_static_gate_precheck.py`.

```python
from tools.iac_static_gate import evaluate, GateInput, GateMode
# after file-write validation, before open_iac_pr:
paths = tuple(f.path for f in req.files)
hcl = {f.path: f.content for f in req.files if f.path.endswith(".tf")}
violations = evaluate(GateInput(mode=GateMode.AGENT, changed_paths=paths, hcl_files=hcl))
if violations:
    raise HTTPException(422, {"error": "static_gate", "violations":
        [{"rule": v.rule, "detail": v.detail} for v in violations]})
```

Test: an HCL body with `provisioner "local-exec"` (or a `module` block, or a
disallowed provider) → 422, capture empty; a clean `resource` body → reaches
`open_iac_pr`. (This makes the worker's content policy identical to CI's, since
both call `evaluate`.)

Commit: `feat(iac): D1-4 — tofu-editor in-process AGENT-mode static-gate pre-check`

---

## Task D1-5: Dockerfile + cloudbuild + tests

**Files:**
- `workers/tofu_editor/Dockerfile` — copy `workers/upgrade_docs/Dockerfile`;
  COPY `driftscribe_lib/`, **`tools/__init__.py` + `tools/iac_static_gate.py`**
  (Codex IMPORTANT: package structure, not a loose file), and
  `workers/tofu_editor/`; same fastapi/uvicorn/PyGithub deps; `CMD uvicorn
  workers.tofu_editor.main:app`.
- `infra/cloudbuild.tofu-editor.yaml` — **template = `infra/cloudbuild.infra-reader.yaml`**
  (a FIRST-DEPLOY `gcloud run deploy`), NOT `cloudbuild.upgrade-docs-update.yaml`
  (update-only). Build `workers/tofu_editor/Dockerfile` (context = repo root) →
  push `driftscribe-tofu-editor:${_TAG}` → `gcloud run deploy
  driftscribe-tofu-editor --no-allow-unauthenticated
  --service-account=tofu-editor-sa@... --set-env-vars=IAC_EDITOR_TARGET_REPO=...,
  ALLOWED_CALLERS=driftscribe-agent@... --set-secrets=GITHUB_TOKEN=
  tofu-editor-github-pat:latest` → post-deploy resolve URL → write back `OWN_URL`.
  Run as `cloudbuild-deploy-sa@...`, `CLOUD_LOGGING_ONLY`. **No CI trigger.**
- Test `workers/tofu_editor/tests/test_dockerfile.py` (mirror
  `workers/tofu_apply/tests/test_dockerfile.py`).
- Test `tests/unit/test_cloudbuild_tofu_editor.py` (mirror
  `tests/unit/test_cloudbuild_infra_reader.py`: only `driftscribe-tofu-editor`,
  never `payment-demo`; `--no-allow-unauthenticated`).

Commit: `feat(iac): D1-5 — tofu-editor Dockerfile + first-deploy cloudbuild`

---

## Task D1-6: static-gate secret-authoring ban (operator-approved 2026-06-01)

The operator chose the "add a gate rule" option in §2.8. Touches a
CODEOWNERS-protected core security file — own review required.

**Files:** Modify `tools/iac_static_gate.py` (+ `driftscribe_lib/iac_hcl.py` only
if a new primitive is needed); Tests `tests/unit/test_iac_static_gate*.py`.

Add an AGENT-mode rule: reject authoring `google_secret_manager_secret` /
`google_secret_manager_secret_version` resources and inline `secret_data` /
literal-credential attributes. Table-driven tests (benign passes; each
secret-bearing construct rejected; OPERATOR mode unaffected). Because both CI and
the worker call `evaluate`, this rule covers both paths at once.

Commit: `feat(iac): D1-6 — static gate bans agent-authored secret material`

---

## Task D2-1: `worker_client` wiring + `call_open_infra_pr`

**Files:** Modify `agent/worker_client.py`; Test `tests/unit/test_worker_client_tofu_editor.py`.

Add to `_WORKER_URL_ENV`: `"tofu_editor": "TOFU_EDITOR_URL",`. Add to
`WORKER_ENDPOINTS`: `"tofu_editor": "/open-pr",`. Add wrapper (ADK-reachable like
`call_close_pr`, but routing fields fixed here):
```python
def call_open_infra_pr(target_repo, branch, title, body, files) -> dict:
    return call("tofu_editor", {"target_repo": target_repo, "branch": branch,
        "base": "main", "title": title, "body": body, "files": files})
```
Test: monkeypatch `TOFU_EDITOR_URL` + `mint_id_token` + httpx; assert POST to
`/open-pr`, audience == root URL, payload passthrough.

> **Deploy-sequencing note (Codex IMPORTANT):** adding `tofu_editor` to
> `_WORKER_URL_ENV` puts it in the `GET /iac-apply/reachability` fan-out loop.
> Until `TOFU_EDITOR_URL` is set on the coordinator, that diagnostic will report
> the editor unreachable. Acceptable (diagnostic, non-fatal), but D3-1/D4 must
> set `TOFU_EDITOR_URL` in the same incremental redeploy that ships this wiring.

Commit: `feat(iac): D2-1 — worker_client tofu-editor wiring + call_open_infra_pr`

---

## Task D2-2: `open_infra_pr_tool` (authority-clean ADK tool)

> Adversarial review pairs with D1-3.

**Files:** Modify `agent/adk_tools.py`; Test `tests/unit/test_open_infra_pr_tool.py`.

Mirror `upgrade_propose_pr_tool` (authority-clean) + `patch_docs_tool` (computes
its own branch). LLM-facing signature exposes ONLY `files`, `title`, `body`. The
tool: resolves `target_repo` via `_get_iac_editor_target()` (mirror
`_get_upgrade_target`; registry pin + `IAC_EDITOR_TARGET_REPO_OVERRIDE` — NOT the
worker boot env); computes `branch = f"infra/{slug(title)}-{ts}-{hex}"` (mirror
`patch_docs_tool`); calls `worker_client.call_open_infra_pr(...)`; returns a
compact result (pr_number, pr_url, branch, + next-steps reminder: operator must
dispatch the C2 plan-builder on the PR number and approve at `/iac-approvals/<pr>`,
and create-class needs a re-bake).

Test: a happy call routes through `call_open_infra_pr` with a derived `infra/`
branch + the pinned repo; the signature accepts no repo/branch/base/label arg.

Commit: `feat(iac): D2-2 — open_infra_pr_tool (authority-clean editor tool)`

---

## Task D2-3: Registry + spec Literal + adk_agent tool sets + inventory/parity tests

**Files:** `agent/workloads/spec.py`, `agent/workloads/registry.py`,
`agent/adk_agent.py`, `tests/unit/test_coordinator_tool_inventory.py`,
`tests/unit/test_workload_spec.py`, + a new parity test.

1. **`spec.py:73`** — `name: Literal["drift", "upgrade", "explore", "provision"]`
   (update the docstring "closed set" note). Update `tests/unit/test_workload_spec.py`
   (stale "only drift/upgrade/explore" assertions).
2. **`registry.py`** — import `open_infra_pr_tool`;
   `_TOOL_REGISTRY["provision_open_infra_pr"] = open_infra_pr_tool`;
   `_WORKER_REGISTRY["tofu_editor"] = WorkerSpec(url_env="TOFU_EDITOR_URL")`; add
   an `IAC_EDITOR_TARGET` pin + `resolve_iac_editor_target()` (mirror
   `UPGRADE_TARGET_REGISTRY`/`resolve_upgrade_target`, with
   `IAC_EDITOR_TARGET_REPO_OVERRIDE`).
3. **`adk_agent.py`** — add `provision_open_infra_pr` to `COORDINATOR_TOOLS`; add
   ordered `PROVISION_WORKLOAD_TOOL_NAMES` (reader tools + `provision_open_infra_pr`);
   ensure it is NOT in `CHAT_ONLY_TOOL_NAMES`.
4. **`test_coordinator_tool_inventory.py`** (Codex: mind callable-vs-symbolic —
   verify by reading the test's `actual = ...` derivation):
   - `EXPECTED_TOOL_NAMES` (line 70) pins **callable** names → add
     `open_infra_pr_tool`.
   - `_MUTATION_TOOL_NAMES` (line 151) → add the **symbolic** `provision_open_infra_pr`.
   - `_MUTATION_WORKER_NAMES` (line 167) → add `tofu_editor`.
   - import + include `PROVISION_WORKLOAD_TOOL_NAMES`; update `_ALL_WORKLOAD_TOOL_NAMES`.
   - add a regression test: `explore` STILL excludes `provision_open_infra_pr`;
     `provision` intentionally includes a mutation tool (so it is NOT asserted
     read-only).
5. **New parity test** `tests/unit/test_worker_registry_parity.py` (Codex
   IMPORTANT): assert `worker_client._WORKER_URL_ENV["tofu_editor"] ==
   registry._WORKER_REGISTRY["tofu_editor"].url_env == "TOFU_EDITOR_URL"` (and,
   ideally, the same parity for every shared worker).

Run `uv run pytest tests/unit -q`.

Commit: `feat(iac): D2-3 — provision in spec/registry/adk + inventory & parity pins`

---

## Task D2-4: `provision` workload + request-model Literals + chat-only + UI

**Files:** `workloads/provision/{workload.yaml,system_prompt.md,chat_system_prompt.md}`,
`agent/main.py`, `agent/templates/transparency.html`,
`tests/integration/test_ui_transparency.py`, `tests/unit/test_provision_workload.py`.

**`workloads/provision/workload.yaml`:**
```yaml
name: provision
display_name: "Provision (infra edits)"
description: >
  Author OpenTofu (IaC) changes from a chat request and open ONE iac/-only
  pull request for the gated apply pipeline to plan, approve, and apply. It
  never touches live infra directly — it only writes HCL and opens a PR.
system_prompt_file: system_prompt.md
chat_system_prompt_file: chat_system_prompt.md
enabled_tool_names:
  - drift_read_live_env
  - read_project_inventory
  - load_contract
  - search_developer_docs
  - retrieve_developer_doc
  - provision_open_infra_pr
worker_names:
  - drift_reader
  - infra_reader
  - tofu_editor
observation_kind: none
action_names: []
```

**`agent/main.py` (Codex BLOCKER):**
- add `"provision"` to BOTH `RecheckRequest.workload` and `ChatRequest.workload`
  `Literal[...]` fields (else `/chat?workload=provision` → 422 before routing);
- add `"provision"` to `CHAT_ONLY_WORKLOAD_NAMES` (line 938, currently
  `frozenset({"explore"})`) so `/recheck` route-refuses it.

**Prompts** (`system_prompt.md` + `chat_system_prompt.md`): read current state
first; author minimal `iac/*.tf` edits matching existing style; NEVER add
providers/modules/provisioners/secrets or touch foundation files (the gate
rejects them); prefer in-place edits of already-declared resources; cite
developer-knowledge docs in the PR body; after opening the PR, tell the operator
the exact next steps (dispatch the C2 plan-builder workflow on the PR number,
then review + approve at `/iac-approvals/<pr>`); state that creating brand-new
resources needs an operator re-bake (C6) before apply.

**UI** (`transparency.html` + `test_ui_transparency.py`, Codex IMPORTANT): add
`provision` to the workload dropdown (currently drift/upgrade/explore) and the
tool-label map (`provision_open_infra_pr` → "Open infra PR"); update the
integration test's expected option/label set.

**Tests** (`test_provision_workload.py`): `load_workload("provision")` resolves
end-to-end (with `TOFU_EDITOR_URL` + reader env set); `/recheck?workload=provision`
→ refused; `/chat?workload=provision` exposes `provision_open_infra_pr`.

Commit: `feat(iac): D2-4 — provision workload + request Literals + chat-only + UI`

---

## Task D3-1: Deploy / IAM / docs

> Touches `/infra/scripts/` → CODEOWNERS `@adi-prasetyo` review.

**Files:** `infra/scripts/setup_secrets.sh`, `docs/architecture/iam-matrix.md`,
`docs/runbooks/tofu-editor.md`.

1. **`setup_secrets.sh`** — add `tofu-editor-sa` (external-create-then-gate like
   `tofu-apply-sa`, ~lines 216-223, OR the SA loop ~136); add a
   `tofu-editor-github-pat` secret block (mirror `upgrade-docs-github-pat`
   ~425-455, describe-then-create, skip-with-instructions if PAT arg omitted);
   add `bind_secret tofu-editor-github-pat "$TOFU_EDITOR_SA"` (~494,
   per-secret accessor only); add `driftscribe-tofu-editor` to the coordinator
   `run.invoker` loop (~652).
2. **`iam-matrix.md`** — add a `tofu-editor-sa@…` row. Positive:
   `secretmanager.secretAccessor` on `tofu-editor-github-pat` ONLY + coordinator
   `run.invoker` on `driftscribe-tofu-editor`. Negative: no project-level GCP
   role; no apply/state/KMS; PAT is write-scoped to the driftscribe repo's `iac/`
   PRs only and is DISTINCT from `tofu-apply` and the docs PATs. (Model on the
   `upgrade-docs-sa` row — PR writer, not a GCP mutator.)
3. **`docs/runbooks/tofu-editor.md`** — operator steps: mint the write-scoped
   fine-grained PAT (Contents:Write + Pull-requests:Write on
   `adi-prasetyo/driftscribe` only); create SA + secret; run
   `infra/cloudbuild.tofu-editor.yaml`; grant coordinator `run.invoker`; set
   `TOFU_EDITOR_URL` on the coordinator (incremental redeploy — infra-reader
   rollout is the template — which also clears the reachability-diagnostic
   warning from D2-1); harden ingress `internal` once verified. Cross-link the
   C2 dispatch + `/iac-approvals` downstream.

Commit: `feat(iac): D3-1 — tofu-editor SA/PAT/invoker + iam-matrix + runbook`

---

## Task D4: Operator-gated live deploy + e2e (no code)

1. Mint the write-scoped fine-grained PAT; create `tofu-editor-sa` +
   `tofu-editor-github-pat`; run `infra/cloudbuild.tofu-editor.yaml`.
2. Grant coordinator `run.invoker` on `driftscribe-tofu-editor`; set
   `TOFU_EDITOR_URL` on the coordinator (incremental redeploy, preserve all other
   env/secrets — infra-reader rollout is the template; this also clears the
   reachability warning); harden the worker to `--ingress=internal` after the
   first successful call.
3. **Positive e2e (in-place update — C5 path, no re-bake):** on the `provision`
   workload, ask in chat for a benign in-place edit of an already-declared
   resource (e.g. a label/annotation on `payment-demo`). Confirm: ONE PR opens
   (`infra/...`, label `driftscribe-infra`, `iac/*.tf` only); CI static gate
   (AGENT mode) passes; dispatch the C2 plan-builder on the PR; review the diff;
   approve at `/iac-approvals/<pr>`; C4 applies; the approved head merges.
4. **Negative e2e (injection containment):** prompt for a control-plane /
   provider-adding / provisioner / secret-bearing edit; confirm rejection by the
   worker policy (D1) and/or CI static gate + C1 denylist, never reaching apply.
5. Revoke temp grants; record results + Codex close-out in the runbook; update
   memory `infra_iac_agent.md`.

**Exit criteria (design §8 Phase D):** chat request → correct one-PR plan that
applies after approval, then merges; injection attempts caught by the gate +
denylist; Codex sign-off; full suite green.

---

## D5 (IMPLEMENTED 2026-06-01): parallel sub-agent fan-out

> **Status: IMPLEMENTED** — built as its own plan,
> `docs/plans/2026-06-01-infra-iac-phase-d5-fanout.md` (Codex-reviewed rev-3
> READY), on branch `feat/iac-phase-d5-fanout`. The sketch below was the
> original deferral note; the realized design follows it closely (ADK
> `ParallelAgent` for concurrent authoring + a deterministic code barrier that
> re-validates with `validate_file_writes` and makes exactly ONE
> `call_open_infra_pr`; single-slice/coupled changes fall back to the
> single-agent path; the worker + PR + C1–C6 apply contract is byte-unchanged;
> no new SA/secret/IAM/worker). The original sketch text is kept for history:

Net-new orchestration (no sub-agent primitive today). When/if built:
coordinator-side decomposition → parallel sub-agent slice authoring → barrier
collecting all slices → ONE `call_open_infra_pr` with the merged `files` set
(worker + PR contract unchanged) → conflict handling for slices touching the same
file. Until then, document that authoring is single-agent/sequential. Codex
confirmed deferring this does not weaken Phase D's trust boundary.

---

## Risks & residuals

- **Authoring quality (the real Phase D risk).** Safety does not depend on the
  LLM being correct; usefulness does. Mitigate via prompts + D1-4 pre-check;
  iterate during D4.
- **Junk PRs** — visible/recoverable (same as upgrade PRs); D1-4 reduces them.
- **Create-class friction** — new-resource applies still need the C6 re-bake.
- **Worker writes arbitrary HCL** — contained by D1-1 file allowlist + D1-4/CI
  AGENT-mode gate + (D1-6) secret ban + C1 denylist + human approval +
  apply-worker re-verify. No new apply-time trust.
- **Two worker registries** — kept in sync + pinned by the D2-3 parity test.
- **Reachability diagnostic** — temporarily reports the editor unreachable until
  `TOFU_EDITOR_URL` is deployed; sequenced in D3-1/D4.

---

## Review history

- **rev-1** (2026-06-01): initial draft (agent).
- **rev-2** (2026-06-01): folded **two Codex review rounds** (read-only threads
  `019e7ee4-6d0c-7fd3-9521-bdfb676d70b9` and `019e7ee9-b2cf-7c41-810a-6a783ea5e96d`).
  BLOCKERS fixed: suffix constants are strings (D1-1 `_ALLOWED_SUFFIXES`);
  `iac/backend.tf` removed from foundation test (`PROTECTED_FOUNDATION` is a
  tuple w/o it); `open_iac_pr` takes a `Repository` (D1-2) and labels
  `driftscribe-infra` (no `_finalize_pr` reuse); `agent/main.py` `RecheckRequest`/
  `ChatRequest` Literals must add `provision` (D2-4); `tofu-editor-github-pat`
  added to `CONTROL_PLANE_SECRET_IDS` (D1-0); cloudbuild first-deploy template =
  `cloudbuild.infra-reader.yaml` (D1-5); branch-tail validation (D1-1);
  secret-bearing-HCL handling (D1-6 decision). IMPORTANTs folded: gate entry is
  `evaluate(GateInput(...))` (D1-4); AGENT-mode selection is label OR branch;
  `EXPECTED_TOOL_NAMES` = callable names vs symbolic in the mutation sets (D2-3);
  worker-name parity test (D2-3); `IAC_EDITOR_TARGET_REPO_OVERRIDE` parity (D2-2/3);
  reachability sequencing (D2-1); request-size bounds (D1-1/D1-3); Dockerfile
  copies `tools/__init__.py` (D1-5); transparency UI + test (D2-4). Codex
  security verdict: no apply-without-human path; deferring D5 is correct.
- **rev-2 decisions (2026-06-01):** operator approved D1-6 (add the AGENT-mode
  static-gate secret ban) and chose subagent-driven execution in this session.
  An optional third Codex pass on rev-2 was deferred; per-task Codex review runs
  during execution instead.
