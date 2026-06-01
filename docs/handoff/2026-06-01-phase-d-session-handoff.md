# Phase D — agent IaC authoring + fan-out: completion record (2026-06-01)

Outcome of building Infra-IaC **Phase D** from the plan
`docs/plans/2026-06-01-infra-iac-phase-d-agent-authoring.md` (rev-2, Codex-reviewed)
via `superpowers:subagent-driven-development` (implementer → spec review →
code-quality review per slice; D1-3 and D2-2 also got a multi-lens adversarial pass).

## What Phase D delivers

A new chat-only **`provision`** workload lets the coordinator LLM author OpenTofu
(HCL) edits and open exactly ONE `iac/`-only pull request via a new,
credential-isolated **`tofu-editor`** Cloud Run worker. The PR then flows through
the **existing, unchanged** C1–C6 gated-apply pipeline (static gate → C2
plan-builder → C3 approval → C4 `tofu-apply`). The editor holds ZERO infra
credentials — only a single-repo, write-scoped GitHub PAT to open the PR;
`tofu-apply` remains the sole live mutator.

## All code/config slices landed (branch `feat/iac-phase-d-agent-authoring`)

Each slice was TDD'd, spec + code-quality reviewed, SHA-verified, and hardened
where review found gaps. Full suite green (**1914 passed**), tree clean.

| Slice | Commit(s) | What |
|---|---|---|
| D1-0 | `914d634` | denylist `tofu-editor-github-pat` secret |
| D1-1 | `bcf8d7f` + `62d576a` | `driftscribe_lib/iac_editor_policy.py` fail-closed file-write policy |
| D1-2 | `cb66cdb` + `afb195a` | `driftscribe_lib/github.py::open_iac_pr` multi-file PR helper |
| D1-3 | `729efba` + `523b9f4` | `workers/tofu_editor/` worker `/open-pr` (adversarial pass: NO bypass) |
| D1-4 | `5aa8da5` | worker in-process AGENT-mode static-gate pre-check |
| D1-5 | `a569322` | worker Dockerfile + `infra/cloudbuild.tofu-editor.yaml` (first-deploy) |
| D1-6 | `34c4162` + `a7e0f59` | static-gate AGENT-mode secret-material ban (incl. regional) |
| D2-1 | `49cc7c5` | `agent/worker_client.py` wiring + `call_open_infra_pr` |
| D2-2 | `cbdd025` + `5e03418` | `agent/adk_tools.py::open_infra_pr_tool` (authority-clean; adversarial pass) |
| D2-3+D2-4 | `b191ab6` | `provision` workload wired end-to-end (spec/registry/adk/main Literals/UI + inventory & parity) |
| D3-1 | `17f6f26` + `e3fea75` | `setup_secrets.sh` SA/PAT/invoker + iam-matrix row + runbook |

Final whole-branch review verdict: **ready to PR** — trust boundary enforced
redundantly at every hop (tool → client → worker schema → policy lib → static
gate); C1–C6 apply floor provably byte-unchanged except two additive security
entries; every cross-slice pin (target repo, `TOFU_EDITOR_URL`, mutation
classification) locked by a behavioral test.

## Deferred (correctly out of scope) — D5

True parallel sub-agent fan-out. v1 ships single-agent multi-file authoring (one
editor call = list of file writes = one commit = one PR), which delivers the
design's load-bearing invariant without a sub-agent primitive. See plan §1 + D5
sketch.

## D4 — DONE: live-deployed + e2e-validated (2026-06-01)

Driven via operator gcloud (theghostsquad00, owner+ADC, `driftscribe-hack-2026`).
The branch is **pushed as PR #52** (CI green; awaiting CODEOWNERS merge). The
worker is live (`driftscribe-tofu-editor`, `:9fb0876`, runtime SA `tofu-editor-sa`,
`--no-allow-unauthenticated`, **ingress=internal**), the coordinator runs the Phase
D image (`driftscribe-agent-00030-8c2`, `:9fb0876`) with `TOFU_EDITOR_URL` set, and
the full e2e is green: reachability `go:true` (tofu_editor included); negative chat
(LLM refuses secret-material); **positive chat → PR #53** (`iac/`-only, label
`driftscribe-infra`, full CI green); negative-at-worker (coordinator-impersonated
POST of a `secret_data` payload → **HTTP 422** `static_gate` / `secret-material-forbidden`).
PR #53 is an apply-neutral throwaway smoke artifact. Remaining: operator MERGE of
PR #52; optionally codify `TOFU_EDITOR_URL` in `cloudbuild.coordinator-update.yaml`
(mirror the `TOFU_APPLY_URL` optional-arg pattern) and close PR #53.

The original step recipe that was executed, per `docs/runbooks/tofu-editor.md`:
1. Mint a write-scoped **fine-grained** GitHub PAT — `Contents: Read & write` +
   `Pull requests: Read & write` on `adi-prasetyo/driftscribe` ONLY.
2. Create `tofu-editor-sa` + `tofu-editor-github-pat` (run `setup_secrets.sh`
   with `TOFU_EDITOR_PAT` as the 8th positional arg).
3. Deploy `infra/cloudbuild.tofu-editor.yaml` (`--no-allow-unauthenticated`).
4. Grant the coordinator `run.invoker` on `driftscribe-tofu-editor`.
5. Set `TOFU_EDITOR_URL` on the coordinator via an incremental redeploy
   (infra-reader rollout is the template; also clears the
   `GET /iac-apply/reachability` "editor unreachable" warning from D2-1).
6. Harden `--ingress=internal` after a first successful call.
7. Positive e2e: on the `provision` workload, ask in chat for a benign in-place
   edit of an already-declared resource → confirm ONE `infra/` PR (label
   `driftscribe-infra`, `iac/*.tf` only) → CI static gate passes → dispatch the C2
   plan-builder on the PR → approve at `/iac-approvals/<pr>` → C4 applies → merge.
8. Negative e2e: prompt for a control-plane / provider-adding / provisioner /
   secret-bearing edit → confirm rejection by the worker policy + CI static gate +
   C1 denylist, never reaching apply.

## Process notes (for any continuation)

- **Branch is pushed; PR #52 is open** (not yet merged). `infra/scripts/`,
  `tools/iac_static_gate.py`, and the denylist are CODEOWNERS-protected → the PR
  needs `@adi-prasetyo` review before merge.
- ⚠️ During this build, several implementer subagents reported **fabricated commit
  SHAs**. Every SHA in the table above was verified with `git rev-parse`/`git log`.
  Always verify a subagent's reported SHA before building on it.
- The plan's per-task Codex review loop (global instruction) was satisfied by the
  plan-writing Codex rounds + the completed-work Codex pass at phase end.
