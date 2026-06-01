# Phase D5 — parallel sub-agent fan-out: session handoff (2026-06-01)

Outcome of building Infra-IaC **Phase D5** (parallel sub-agent fan-out for the
`provision` IaC-authoring coordinator) from the plan
`docs/plans/2026-06-01-infra-iac-phase-d5-fanout.md` (rev-3, Codex-reviewed,
verdict **READY**) via `superpowers:subagent-driven-development` (fresh
implementer per slice → controller spec + code-quality review; D5-6 also got an
independent adversarial review pass).

## What D5 delivers

The `provision` coordinator can now decompose ONE infra-authoring request into N
**independent, disjoint** `iac/` file slices, author them with N sub-agents **in
parallel** (ADK `ParallelAgent`, native merged event stream → live in the SSE
timeline), then a **deterministic code barrier** merges the per-slice file-writes
and makes **exactly ONE** `call_open_infra_pr`. The load-bearing Phase-D
invariant is preserved end-to-end: one editor call → one commit → one PR → the
**unchanged** C1–C6 gated-apply pipeline. A single-slice (coupled/simple) plan
transparently falls back to today's single-agent `run_chat_stream` path.

**Trust boundary UNCHANGED.** Sub-agents author HCL text only and hold **no**
editor/PR/apply/mutation tool (filtered by symbolic *and* callable name); the one
convergent `call_open_infra_pr` is byte-identical to Phase D's; the resulting PR
still flows through the AGENT-mode static gate, the C1 denylist, human approval,
and the `tofu-apply` re-verify. **No new worker, SA, secret, or IAM** — fan-out is
entirely coordinator-internal. Deploy = a coordinator image rebuild.

## Code/config slices landed (branch `feat/iac-phase-d5-fanout`, off `main`)

Each slice was TDD'd, controller-reviewed, and **SHA-verified with `git`** (no
fabrication this build). Full `tests/unit` green at every step. SHAs:

| Slice | Commit | What |
|---|---|---|
| plan | `ea96198` | the rev-3 Codex-READY D5 plan |
| D5-1 | `7007283` | `SliceSpec`, `FanoutError`/`FanoutFailureKind`, `validate_slice_specs`; public `validate_iac_path` |
| D5-2 | `91a5202` | content-only authority-clean `submit_slice_file` tool factory (path pinned in closure) |
| D5-3 | `a8ac0a4` | slice-author agent factory + `resolve_provision_read_tools` (double-filter); canonical `MUTATION_TOOL_NAMES` |
| D5-4 | `656d30d` | `decompose()` structured plan agent (own session; JSON-string `submit_plan`); typed fail-open/closed |
| D5-5 | `bd6f3d8` | `author_slices_parallel()` — ADK `ParallelAgent` + deterministic fail-closed barrier `_merge_slice_sinks` |
| D5-6 | `fcebe92` | `run_provision_fanout_stream()` orchestrator + shared `derive_iac_pr_authority`; `_emit_final_response` DRY |
| D5-7 | `d965a84` | route `/chat?workload=provision` (JSON + SSE) through the orchestrator; provision prompt note; parity tests |
| D5-8 | (this) | docs: runbook fan-out note + Phase-D plan D5 marked implemented + this handoff |

Final unit-suite state after D5-7: **1315 passed**, tree clean.

## Key design properties (for any continuation)

- **Deterministic barrier, never an LLM merge.** Each slice records its file into
  a per-slice sink via `submit_slice_file`; after `ParallelAgent` completes, code
  re-runs `validate_file_writes` (disjoint paths + byte bounds — the SAME function
  the worker enforces) and assembles the merged `files`. Any slice that errors or
  never submits fails the WHOLE fan-out **closed** (no partial PR).
- **One monotonic SSE `seq`** for the whole committed run: buffered decompose
  events (flushed only on commit N≥2, discarded on fallback/fail-closed),
  live parallel-author events, and the single trailing `final_response`.
- **`FanoutFailureKind`** (not HTTP status) drives fail-open (`DECOMPOSE_NON_POLICY`
  → delegate to single-agent) vs fail-closed (`POLICY`/`AUTHORING` → surface, no PR).
- **Shared `derive_iac_pr_authority(title, *, clock=None, rng=None)`** (in
  `agent/adk_tools.py`) is the single source of `target_repo`/`infra/`-branch
  derivation — both the single-agent `open_infra_pr_tool` and the fan-out
  orchestrator route through it, so they can't drift (byte-identical to the prior
  inline derivation; injectable clock/rng for deterministic equality tests).
- The single editor call runs via `asyncio.to_thread(call_open_infra_pr, …)` with
  **positional args and NO `base=`** (the wrapper pins `base="main"` internally;
  passing it would crash).

## D5-9 — DONE: live-deployed + multi-slice e2e validated (2026-06-01)

Driven via operator gcloud (`theghostsquad00`, owner+ADC, `driftscribe-hack-2026`).
Coordinator rebuilt from branch HEAD via `infra/cloudbuild.coordinator-update.yaml`
(`--update-env-vars` preserved `TOFU_EDITOR_URL`/`TOFU_APPLY_URL`); **no new
SA/secret/IAM/worker**. Live revisions: `:055da7a` (`driftscribe-agent-00031-tww`)
then `:46036da` (`driftscribe-agent-00032-tmx`) after the deploy-fix below.

- **Deploy-time bug found + fixed live (`46036da`):** the first provision fan-out
  `/chat` 500'd with `No module named 'tools'`. `agent/fanout.py` imports
  `driftscribe_lib.iac_editor_policy`, which does `from tools.iac_static_gate
  import …`; `tools` is not a setuptools package and `Dockerfile.agent` didn't
  copy it (pre-D5 the coordinator never imported `iac_editor_policy`). Fixed by
  copying `tools/{__init__,iac_static_gate}.py` + `PYTHONPATH=/app` (mirrors
  `workers/tofu_editor/Dockerfile`); pinned by
  `tests/integration/test_dockerfile_agent_packaging.py`.
- **Reachability:** `GET /iac-apply/reachability` → `go:true`, all 9 workers
  reachable incl. `tofu_editor`.
- **Positive e2e (SSE):** `/chat?workload=provision` request for two independent
  `iac/` buckets → timeline showed **two parallel slice authors** (branches
  `driftscribe_fanout.driftscribe_slice_0_iac_d5_probe_a_tf` /
  `…_slice_1_iac_d5_probe_b_tf`, tagged slice_id 0/1 → `iac/d5-probe-a.tf` /
  `iac/d5-probe-b.tf`), 4 buffered `phase=decompose` events flushed on commit,
  one `submit_plan`, one `submit_slice_file` per slice, **exactly one
  `final_response`**, `tool_calls=["open_infra_pr"]` → **ONE PR #56** (label
  `driftscribe-infra`, two `iac/*.tf`, clean independent HCL) → CI **static-gate
  pass / tofu pass / lint-test pass**. PR #56 was an apply-neutral throwaway →
  **closed + branch deleted** (the D4/PR-#53 pattern).
- **Negative e2e (SSE):** a secret-material multi-slice request → **fail-closed**:
  reply "Could not author the infrastructure change: slice authoring failed…",
  `tool_calls=[]`, **NO PR** (max PR stayed #56), graceful `done` frame (no 500).
  The fan-out caught the offending slice at AUTHORING → `FanoutError(AUTHORING)` →
  no partial/duplicate PR.
- Known cosmetic residual (Codex non-blocking): the AUTHORING fail-closed reply
  surfaces the `TaskGroup` wrapper string rather than the inner cause.

**Phase D5 is fully shipped — code on branch (PR #55, awaiting merge) + live on prod.**

---

## (historical) D5-9 recipe as planned — USER-GATED

**Coordinator rebuild only; no new infra/SA/secret/IAM/worker.**

1. Rebuild the coordinator: `infra/cloudbuild.coordinator-update.yaml` with
   `_TAG=$(git rev-parse --short HEAD)` (the `TOFU_EDITOR_URL` substitution is
   already codified — Phase D `be394a7`).
2. **Positive e2e:** on `/chat?workload=provision`, ask for a benign change
   spanning **two** independent already-declared `iac/` resources → confirm the
   SSE timeline shows two parallel slice authors → ONE PR (label
   `driftscribe-infra`, two `iac/*.tf` files) → CI static gate passes →
   (optionally) C2 → approve → C4 → merge.
3. **Negative e2e:** a request forcing two slices onto the SAME file, or a
   secret/provider slice → confirm fail-closed (decompose validate or static
   gate), never a partial/duplicate PR. Drive the worker-level negative as D4 did.
4. Record live IDs (coordinator rev, PR number, run IDs) in this handoff + memory.

## Residuals / notes

- **⚠️ Verify subagent-reported SHAs.** (No fabrication occurred this build — all
  eight verified against `git log` — but the discipline stands.)
- **Pre-existing test-isolation hazard** (NOT introduced by D5, flag for a future
  cleanup): `tests/unit/test_coordinator_tool_inventory.py`'s reimport probe
  restores `sys.modules` but **not** the `agent` package's submodule attributes,
  so after it runs, `agent.<sub>` (package attribute) and `sys.modules["agent.<sub>"]`
  can diverge. This makes pytest's string-target `monkeypatch.setattr("agent.x.y", …)`
  patch a stale module. D5-6's `test_fanout_orchestrator.py` works around it with a
  `_live()` (`importlib.import_module`) helper that patches the exact object the
  orchestrator's lazy `import` reads. Production is unaffected (nothing pops
  `sys.modules` at runtime). Hardening the inventory test's teardown to restore the
  package attributes would remove the need for the workaround.
- **End-of-phase Codex completed-work review** (per the global instruction) should
  run on the plan thread `019e82c0-fbdb-7161-9eb4-e0a7e055ad06` before the branch
  is finished. Two structural notes to adjudicate there: (1) `MUTATION_TOOL_NAMES`
  living in `agent/fanout.py` (enforcer-owns, auditor-imports); (2) the module-level
  `from google.adk.runners import Runner` in `fanout.py` (the test mock seam, a
  deliberate exception to the "lazy ADK imports" rule).
