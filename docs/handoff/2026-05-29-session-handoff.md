# Session handoff — DriftScribe / infra-IaC agent — 2026-05-29

> **For the next Claude agent:** read this top-to-bottom before doing anything. Memory (`~/.claude/projects/-home-adi-driftscribe/memory/MEMORY.md`) loads automatically and gives you the long-running project state — this doc is the *pickup point* for the specific work that was in flight.

## TL;DR — where we are

Phase **C2** of the **infra-IaC agent initiative** is **fully shipped and closed**.

- PR #11 merged as squash commit `a689d8e` on `main` (2026-05-29).
- Codex completed-work review (thread `019e6e11-a3d0-78d3-9040-d0f53b60947b`) returned: *"No blockers found. Clean from a security/invariant perspective. Mark it closed."*
- Local tree clean on `main`; worktree at `.worktrees/phase-c2-plan-builder` already removed.
- All memory files updated (`infra_iac_agent.md` body + `MEMORY.md` index line).

**Nothing left to code for C2.** The remaining work is **operator-side** (one-time GCP/GitHub setup) and then **the next phase (C3)**.

---

## Open OPERATOR follow-ups (must run BEFORE the first `workflow_dispatch`)

These are recorded verbatim in PR #11's description and in memory. They are NOT code work — they require the operator's live `gcloud` ADC + GitHub Settings access.

1. **Re-run the bootstrap script** (idempotent — applies the new `storage.objectCreator` IAM binding AND the tightened WIF condition that pins `workflow_dispatch` to `refs/heads/main`):
   ```bash
   cd /home/adi/driftscribe
   PROJECT=driftscribe-hack-2026 infra/scripts/setup_iac_backend.sh
   ```
2. **Set three GitHub secrets** at `Settings → Secrets and variables → Actions` for the repo:
   - `GCP_WIF_PROVIDER` — full provider resource path printed by bootstrap
   - `GCP_TOFU_PLAN_BUILDER_SA` — `tofu-plan-builder@driftscribe-hack-2026.iam.gserviceaccount.com`
   - `GCP_TOFU_STATE_KMS_KEY` — full KMS key path printed by bootstrap
3. **Smoke-test:** open a trivial `iac/` PR, dispatch the `iac` workflow (Actions → iac → Run workflow → enter the PR number), verify:
   - The artifact triplet (`plan.tfplan`, `plan.json`, `metadata.json`) lands in `gs://driftscribe-hack-2026-tofu-artifacts/pr-<N>/<head_sha>/run-<id>-<attempt>/`.
   - A `tofu show` diff comment appears on the PR.

Until #1+#2 land, every dispatch fails fast at the `gcp-auth` step — no partial state.

---

## What landed in C2 (so you can answer questions about it without re-reading the PR)

| Layer | Adds |
|---|---|
| **Helpers** | `tools/iac_plan_metadata.py` (c2.v1 schema — the C3 input contract) · `tools/iac_plan_diff_summary.py` (PR-comment formatter, self-sized truncation, dynamic backtick fence) · `tools/iac_plan_artifact_upload.py` (two-step SDK uploader, generation read in-band) |
| **Bootstrap** | New 5d IAM block (`objectCreator` write-only) + WIF condition tightened so `workflow_dispatch` requires `ref == refs/heads/main` (`infra/scripts/setup_iac_backend.sh`) |
| **Workflow** | New `plan-builder` job in `.github/workflows/iac.yml` — 22 steps, two-layer ref pin, pure-git diff-guard with `--no-renames -z`, hardcoded `--mode agent` static-gate, denylist-before-upload, two-step upload, PR comment |
| **Tests** | 70+ new unit tests across 5 files; 13 structural invariant tests pinning the workflow YAML; full repo suite 1283 green; ruff clean |
| **Docs** | `iac/README.md` Phase C2 subsection + `.github/CODEOWNERS` entries |

**Plan doc (Codex rev-4 approved):** `docs/plans/2026-05-28-infra-iac-phase-c2-plan-builder.md` — 2545 lines. The §0 "hard invariants" section there is the canonical security contract for the workflow; do not regress any of those 14 items.

---

## What's next — Phase C3 → C6 (the rest of the initiative)

The big-picture goal of the initiative: a chat-driven IaC agent where chat opens a PR → the trusted GHA builds an immutable plan artifact (C2 ✅) → the operator approves via an HMAC-signed approval record bound to that exact artifact generation (C3) → a separately-permissioned apply worker re-runs the denylist and applies (C4) → coordinator wires it into chat (C5) → an e2e proves the loop (C6).

| Phase | Scope | Where to start |
|---|---|---|
| **C3** | Plan-bound approval HMAC schema in `driftscribe_lib/approvals.py`. The approval record binds to the exact `(plan_sha256, plan_object_generation, metadata_object_generation)` tuple from c2.v1, so an approval can't be re-used for a different plan. | New design doc: brainstorm first (`superpowers:brainstorming`) → review with Codex MCP (`mcp__codex__codex`) → plan (`superpowers:writing-plans`) → Codex re-review of plan → user. |
| **C4** | `tofu-apply` worker. Broad IAM, private ingress, owns the HMAC verification key. Re-runs the C1 denylist on the saved plan before apply. Freshness check (rejects stale approvals). Applies by pinned object generation, not by path. | Same brainstorm → plan → Codex flow. C4 is the highest-risk slice because it's the first thing with write IAM. |
| **C5** | Coordinator integration. Chat surfaces the approval URL; operator clicks; worker fires. | Design after C3+C4 lock. |
| **C6** | Hand-written e2e through the full gate. Requires operator + live ADC + a real trivial `iac/` PR. | Last. |

**Standing CLAUDE.md instructions to obey** (verbatim from `~/.claude/CLAUDE.md`):
- *"always confirm with me before executing sql to alter table or any operations that will modify the table directly, unless i have already specifically instructed you to do so"*
- *"When writing implementation plans, always review the plan with the Codex MCP agent (`mcp__codex__codex`) to get a second opinion before presenting it to the user. After finishing the implementation, use `mcp__codex__codex-reply` to follow up on the same thread so Codex can review the completed work against the original plan. Do not pass a `model` parameter — omitting it lets Codex use its current recommended (effectively latest) model."*

**Execution preference** (recorded in memory `user_execution_preferences`): subagent-driven (`superpowers:subagent-driven-development`) — fresh implementer per task, spec-compliance review then code-quality review, re-loop until both approve.

---

## Quick orientation for the new agent

```bash
cd /home/adi/driftscribe
git log --oneline -5                         # last commit should be a689d8e
git status                                    # should be clean on main
gh pr view 11                                 # the C2 PR + operator follow-ups in the body
cat docs/plans/2026-05-28-infra-iac-phase-c2-plan-builder.md | head -200   # §0 invariants
cat ~/.claude/projects/-home-adi-driftscribe/memory/infra_iac_agent.md     # full initiative state
```

Key files to know about (don't read unless relevant):
- `.github/workflows/iac.yml` — the workflow with the new `plan-builder` job
- `tools/iac_plan_metadata.py` — c2.v1 schema (the C3 input contract — read this BEFORE designing C3)
- `tools/iac_plan_artifact_upload.py` — two-step uploader semantics (matters for C4 freshness check)
- `infra/scripts/setup_iac_backend.sh` — bootstrap; section 5d is the artifact-bucket IAM, footer is the Phase-C summary
- `iac/README.md` — Phase C2 subsection documents the user-facing contract

---

## Decisions/conventions worth not relitigating

These were settled across 4 Codex review rounds for C2; if you find yourself questioning them, re-read the plan §0 first.

- **`workflow_dispatch` only** for v1 (no auto-trigger on PR). Two-layer ref pin: WIF condition + workflow `if:` both require `refs/heads/main`.
- **Pure-git diff-guard** (`git diff --name-only --no-renames -z BASE HEAD`) — NOT `gh api`. Runs AFTER pinned checkout, BEFORE `uv sync`.
- **Static-gate `--mode agent` is hardcoded** in the workflow — not derived from PR content.
- **Two-step upload** with `metadata.json` written LAST, after both `plan.tfplan` and `plan.json` generations are known. C4 keys off `metadata.json`'s presence as the "this plan slot is complete" signal.
- **Path scheme:** `pr-<N>/<head_sha>/run-<run_id>-<run_attempt>/` — re-runs get fresh folders, no overwrites.
- **Denylist (C1) runs BEFORE upload.** A denied plan never produces an artifact.
- **`tofu_version: '1.12.0'`** is pinned in `setup-opentofu`.
- **Workflow-level permissions floor is `contents: read` only.** Only `plan-builder` has `id-token: write`. A 13th invariant test pins this.

---

## If the user comes in cold and asks "what was happening"

Show them the TL;DR + the three open operator follow-ups. Ask whether they want to:
- **(a)** run the operator steps and smoke-test C2 first, OR
- **(b)** start brainstorming **C3** (the HMAC schema) now, with operator steps deferred.

Either is valid. (a) unblocks observable progress; (b) keeps code velocity going.
