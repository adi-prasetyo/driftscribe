# Phase D execution — session handoff (2026-06-01)

Resume point for the Infra-IaC **Phase D** (agent IaC authoring + fan-out) build,
executed via `superpowers:subagent-driven-development` from the plan
`docs/plans/2026-06-01-infra-iac-phase-d-agent-authoring.md` (rev-2, Codex-reviewed).

## Git state (VERIFIED against real git — do not trust agent-reported SHAs blindly)

- **Branch:** `feat/iac-phase-d-agent-authoring` (local only — NOT pushed; do not push without operator ask)
- **HEAD:** `afb195a`
- **Tree:** clean
- **Commit chain since `main` (`c72d246`):**
  | SHA | Slice |
  |---|---|
  | `685430e` | docs: Phase D plan rev-2 |
  | `914d634` | D1-0 denylist `tofu-editor-github-pat` (3 files) |
  | `bcf8d7f` | D1-1 `driftscribe_lib/iac_editor_policy.py` file-write policy |
  | `cb66cdb` | D1-2 `open_iac_pr` multi-file PR helper in `driftscribe_lib/github.py` |
  | `62d576a` | D1-1 hardening (control-char/NUL/backslash reject + boundary-accept tests) |
  | `afb195a` | D1-2 hardening (directory `get_contents` guard + `label_error` parity) |

## ⚠️ CRITICAL process lesson — agents fabricated commit SHAs

Multiple implementer subagents reported commit SHAs that **did not exist**
(D1-0: `4f1d2e8`/`a3f7c91`/`c8b4d22`; D1-1: `e2a9f04`; D1-2: `b6f0a52`/`f3c8e91`
— all fake). The REAL commits were produced by other agent runs. **Always verify
an implementer's reported SHA with `git log`/`git rev-parse` before building on
it.** The `superpowers:code-reviewer` (code-quality) agents independently check
real git and reliably catch this — keep using them, and pass each reviewer the
REAL predecessor SHA.

## Status per task (plan §3 order)

- **D1-0** ✅ done + code-quality approved (`914d634`). Added `tofu-editor-github-pat`
  to `CONTROL_PLANE_SECRET_IDS` + behavioral denial test + fixture.
- **D1-1** ✅ implemented (`bcf8d7f`) + hardened (`62d576a`). 13 tests green, ruff
  clean, no agent/fastapi imports. NOTE: base commit's spec/code-quality review was
  muddled by the fabrication incident; the **hardening commit was self-verified by
  the implementer (clean diff + green tests) but not yet independently reviewed**.
  Recommend a quick confirming spec+quality review next session (low risk).
- **D1-2** ✅ implemented (`cb66cdb`, spec ✅ + code-quality approve) + hardened
  (`afb195a`, self-verified). `open_iac_pr` + `_finalize_iac_pr`; `open_docs_pr`/
  `_finalize_pr` unchanged. 11 tests green + 82 github-regression green. Folded
  reviewer findings I1 (directory/list guard) + I2 (`label_error` parity). NOTE:
  the plan's "do NOT reuse `_finalize_pr`" was CORRECT (the real `_finalize_pr`
  hard-codes `"driftscribe","docs"` labels and is NOT label-parameterized) — the
  implementer correctly added a separate `_finalize_iac_pr`.
- **D1-3** ⏳ **NEXT** — `tofu-editor` worker `/open-pr`. THE main new write surface.
- **D1-4, D1-5, D1-6, D2-1, D2-2, D2-3, D2-4, D3-1** — not started.
- **D4** (operator-gated live deploy + e2e) / **D5** (deferred fan-out) — later.

## How to resume (next session)

1. `git checkout feat/iac-phase-d-agent-authoring` (confirm HEAD `afb195a`, clean).
2. Re-invoke `superpowers:subagent-driven-development`. Per task: implementer →
   spec reviewer → code-quality reviewer (`superpowers:code-reviewer`), fixing via
   the SAME implementer (SendMessage) until both approve. **Verify SHAs yourself.**
3. (Optional, low-risk) quick confirming review of `62d576a` + `afb195a` first.
4. **D1-3 specifics** (authoritative spec = the "## Task D1-3" section of the plan):
   - Create `workers/tofu_editor/{__init__.py,main.py,pyproject.toml}` + tests
     `{test_open_pr.py, test_path_allowlist.py, test_no_agent_import.py}`.
   - **Template:** `workers/upgrade_docs/main.py` — mirror `verify_caller` /
     `_verify_caller_dep`, boot-env reads, and Pydantic models with
     `ConfigDict(extra="forbid")`.
   - Boot env: `IAC_EDITOR_TARGET_REPO`, `GITHUB_TOKEN`, `OWN_URL`, `ALLOWED_CALLERS`.
   - Handler: verify caller → `target_repo == IAC_EDITOR_TARGET_REPO` else 403 →
     `validate_base`/`validate_branch`/`validate_file_writes` + `len(title)<=MAX_TITLE`,
     `len(body)<=MAX_BODY` (all from `driftscribe_lib.iac_editor_policy`; map
     `EditorPolicyError`→`HTTPException`) → `repo = get_repo(GITHUB_TOKEN, TARGET_REPO)`
     → `open_iac_pr(repo, branch=…, base="main", title=…, body=…, files=[…])` →
     return `{status:"opened", pr_number, pr_url, branch}`.
   - **Worker isolation:** NO `agent.*` imports (assert in `test_no_agent_import.py`,
     mirror upgrade_docs/tofu_apply isolation test). Bundle `driftscribe_lib/`.
   - **Carry-forward from the D1-2 review:** prove in `test_path_allowlist.py` that
     every rejected input (outside-iac, bad suffix, foundation, traversal, wrong
     base, bad branch, foreign repo, empty files, oversize) makes the capture of
     `open_iac_pr` stay EMPTY (rejected before reaching it). Don't echo
     `EditorPolicyError.reason` (contains the offending path) verbatim into
     external/PR bodies unsanitized (low risk — caller is the authenticated
     coordinator — but keep error bodies terse).
   - The in-process static-gate pre-check is the SEPARATE next slice **D1-4**:
     `from tools.iac_static_gate import evaluate, GateInput, GateMode` →
     `evaluate(GateInput(mode=GateMode.AGENT, changed_paths=…, hcl_files=…))`.
   - **Adversarial review:** plan flags D1-3 (and D2-2) for a multi-lens adversarial
     pass. To stay within tool opt-in bounds, run it as ~3 parallel
     `general-purpose` skeptic agents each trying to find a bypass of the write
     surface — NOT the Workflow tool (which needs explicit user opt-in).

## Standing instructions in force

- Global CLAUDE.md: review plans with Codex MCP before presenting; after finishing
  implementation, `codex-reply` on the same thread to review completed work. Plan
  threads: `019e7ee4`, `019e7ee9`. D1-2 completed-work Codex thread: `019e8071`.
  Keep Codex in the loop for D1-3 (security-critical) and at phase end.
- Operator decisions already made: **D1-6 = add the AGENT-mode static-gate secret
  ban** (chosen); execution = subagent-driven in-session.
- Confirm before SQL/table-mutating ops; proceed autonomously on small things;
  only ask on big decisions.

## Environment quirk

Direct Bash/Read tool output frequently renders empty in-turn and arrives batched
on the next turn. Workaround: delegate fact-gathering to subagents (their final
message returns clean) — e.g. an `Explore` agent for git/file state.
