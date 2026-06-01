# Phase D5 — Parallel sub-agent fan-out (IaC authoring) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to
> implement this plan task-by-task.

**Goal:** Let the `provision` coordinator decompose one infra-authoring request into
N disjoint slices, author them with N sub-agents **in parallel**, then barrier-merge
all slices into **exactly ONE** `call_open_infra_pr` — preserving the load-bearing
Phase-D invariant (one editor call → one commit → one PR → unchanged C1–C6 apply path).

**Architecture:** A code-level orchestrator (`agent/fanout.py`) runs three stages:
**(1) decompose** — one structured LLM call returns a validated `list[SliceSpec]`
(disjoint `iac/` target paths, bounded N); **(2) author in parallel** — N constrained
slice-author `LlmAgent`s wrapped in ADK **`ParallelAgent`**, each authoring ONLY its
one pinned file via a content-only `submit_slice_file` tool (no editor/PR tool), their
natively-merged event stream flowing into the existing SSE timeline; **(3) barrier +
merge** — deterministic code (NOT an LLM) collects the per-slice file-writes,
re-validates disjointness/bounds with the existing `validate_file_writes`, and makes
ONE `call_open_infra_pr`. Single-slice decomposition transparently falls back to
today's single-agent `run_chat_stream` path. **No new worker, SA, secret, or IAM** —
fan-out is entirely coordinator-internal; deploy = a coordinator image rebuild.

**Tech Stack:** Python 3.14, `google-adk==1.33.0` (`Agent`/`LlmAgent`, `ParallelAgent`
from `google.adk.agents`, `Runner`, `InMemorySessionService`), Pydantic v2, pytest.
Reuses `driftscribe_lib/iac_editor_policy.py` (`validate_file_writes`,
`_validate_one_path`), `agent/worker_client.py::call_open_infra_pr`, and the
`agent/adk_agent.py` event-emit helpers (`_emit_event_logs`, `_emit_llm_usage`).

---

## 1. Scope & non-goals

**In scope (D5 v1):**
- `agent/fanout.py`: decomposition, slice-author agent factory, parallel authoring via
  ADK `ParallelAgent`, deterministic barrier/merge, the single editor call, and an
  SSE-shaped streaming orchestrator.
- A content-only `submit_slice_file` tool (authority-clean: the slice's target path is
  server-pinned per slice; the LLM supplies only `content`).
- Wiring `provision` `/chat` (JSON + SSE) to the fan-out path with single-slice
  fallback; provision system-prompt note; transparency in the reply/timeline.
- Tests pinning every new contract + parity/inventory tests that keep the editor and
  worker contracts byte-unchanged.
- Docs (runbook note + handoff).

**Deferred / out of scope (state in the PR; do not silently drop):**
- **Cross-slice dependency ordering.** v1 slices are **independent and disjoint by
  construction** (one file each, no inter-slice references resolved at author time).
  Ordered/staged authoring (slice B depends on slice A's output) is future work.
- **Same-file co-authoring / HCL merge.** Two slices may NOT target the same file
  (duplicate-path is a hard 403 in the editor, and concatenating LLM-authored HCL is
  unsafe). Decomposition assigns disjoint paths; a collision fails closed.
- **Multi-PR fan-out.** The invariant is ONE PR. N independent PRs is explicitly not
  this phase (and is already achievable by N sequential single-agent calls today).
- **Provider/module/lockfile/secret authoring, foundation edits, create-class
  autonomy** — unchanged from Phase D (static gate + C1 denylist + C6 re-bake still
  govern; D5 changes orchestration only, never the apply trust boundary).
- **`/recheck` autonomy for provision** — still chat-only/route-refused.

**Trust-boundary statement (must appear in the PR description):** D5 adds NO new
apply-time authority. Sub-agents author HCL text only and hold no editor/PR/infra
tool; the SINGLE convergent `call_open_infra_pr` is byte-identical to Phase D's; the
resulting PR still flows through the static gate (AGENT mode), C1 denylist, human
approval, and the `tofu-apply` re-verify. Codex confirmed (Phase D rev-2) that
deferring/!changing this boundary is correct; D5 keeps it intact.

---

## 2. Key design decisions

1. **ADK `ParallelAgent` for the authoring step, code-level orchestration around it.**
   `ParallelAgent` (`google.adk.agents`) runs dynamically-constructed sub-agents
   concurrently in isolated branches with a *natively-merged* event stream — so the N
   parallel authors show up in the existing SSE timeline for free. Decompose (before
   the graph is built, since N is dynamic) and the barrier/merge/editor-call (after)
   live in plain async code so the **barrier is deterministic, not an LLM step**.
2. **Deterministic barrier — never an LLM merge.** Each slice agent records its file
   into a request-scoped collector via the `submit_slice_file` tool; after the
   `ParallelAgent` completes, code reads the collector, re-runs `validate_file_writes`
   (disjoint paths, `MAX_FILES=32`, per-/total-byte bounds — the SAME function the
   worker enforces), and assembles the merged `files`. A slice that never submitted →
   fail closed (`FanoutError`), no partial PR.
3. **Authority-clean slice tool.** `submit_slice_file(content: str, citations:
   list[str] = [])` — the target **path is pinned server-side per slice** (from the
   validated `SliceSpec`), NOT an LLM arg. `content` + `citations` are non-authority
   payload (citations are doc references folded into the PR-body manifest; they carry
   no authority and resolve the Codex "no citation channel" inconsistency). Mirrors the
   Phase-D `open_infra_pr_tool` authority-clean philosophy (LLM supplies content only,
   never repo/branch/base/label/path). Slice agents are built with read tools + this one
   tool and **NO** `provision_open_infra_pr` / editor / worker-mutation tool. Mechanism:
   one **per-slice** `sink` dict captured in the tool closure (one sink per slice → no
   cross-slice shared mutable state → no race under ParallelAgent's asyncio queue;
   Codex-confirmed). The ADK-native alternative — `submit_slice_file(content,
   tool_context)` writing `tool_context.state[f"slice::{path}"]` (carried via
   `EventActions.state_delta`) — is noted but NOT used in v1 (closure sink is simpler +
   directly testable).
4. **Disjoint paths enforced twice.** `validate_slice_specs` rejects duplicate/colliding
   target paths at decompose time (reusing `iac_editor_policy._validate_one_path` for
   the `iac/`-prefix + `.tf`/`.md` + non-foundation + no-`..` rules); the post-barrier
   `validate_file_writes` rejects them again on the assembled set (defense in depth).
5. **Bounded fan-out; "one slice" means the legacy single-agent path.** `MAX_SLICES =
   8` (well under the editor's `MAX_FILES=32`). **A slice is one independent `iac/`
   file** that can be authored with no reference to another slice's output. Coupled /
   interdependent multi-file changes are NOT split — they decompose to **one slice**,
   which means *fall back to today's `run_chat_stream` single-agent path* (which already
   authors multiple interdependent files in one editor call). So fan-out triggers
   **only** for N≥2 genuinely independent files; everything else takes the proven
   single-agent path. Decomposition returning 0 slices → error.
6. **One convergent editor call, unchanged — via the existing wrapper.** The barrier
   calls **`worker_client.call_open_infra_pr(target_repo, branch, title, body,
   files=merged)`** exactly once. NOTE (Codex blocker): the wrapper takes only
   `(target_repo, branch, title, body, files)` and **pins `base="main"` internally**
   (`worker_client.py:505`) — do NOT pass `base=`, it will crash. Before the call,
   validate the composed PR title/body against the policy bounds
   (`validate_title_body` / `MAX_TITLE`/`MAX_BODY` in `iac_editor_policy.py`) since the
   manifest can grow the body. The call is synchronous (`httpx.Client`,
   `worker_client.py:373`); run it via `await asyncio.to_thread(...)` so the 30s window
   doesn't block the SSE event loop. Repo/branch/base/label are derived server-side via
   the **shared** `derive_iac_pr_authority(title)` helper extracted from
   `open_infra_pr_tool` (`_get_iac_editor_target()`, `infra/<slug>-<ts>-<hex>`, base
   `main`, label `driftscribe-infra`). The tofu-editor worker and `OpenIacPrRequest`
   schema are **untouched**.
7. **PR title/body are composed deterministically, not by a free LLM.** The decompose
   step returns an overall `pr_title` + `pr_body_intro`; the barrier appends a
   per-slice manifest (goal + path) and any doc citations the slice agents recorded.
   No separate "compose" LLM step that could fabricate a PR number/URL.
8. **Streaming preserved.** The orchestrator yields the SAME SSE item shapes as
   `run_chat_stream` (`{"type":"event",...}` / final `{"type":"result",...}`), reusing
   `_emit_event_logs`/`_emit_llm_usage` and the `_stream()` seq/insert_id augmentation,
   so the Phase-22 timeline + `/chat` JSON contract stay byte-compatible. Sub-agent
   events are tagged (branch name) so the UI can group them by slice (D5-6).
9. **Provision stays chat-only.** Fan-out is reachable only on `/chat?workload=provision`
   (JSON + SSE). `/recheck` remains route-refused. No request-model Literal changes
   (provision already exists from Phase D); only an internal routing branch is added.
10. **Deploy is a no-op beyond a coordinator rebuild.** No `setup_secrets.sh` change,
    no new SA/secret/worker/IAM, no `cloudbuild.*` change. D5 ships by rebuilding the
    coordinator image (`cloudbuild.coordinator-update.yaml`) — already proven in D4.
11. **ParallelAgent failure is fail-closed (Codex blocker).** `ParallelAgent` uses
    `asyncio.TaskGroup` (`parallel_agent.py:71`): if any slice sub-agent raises, the
    group cancels siblings and the run **raises through**. `author_slices_parallel`
    MUST wrap the run in try/except, **discard all collected sink writes**, and raise a
    deterministic `FanoutError(502, …, kind=AUTHORING)` — never open a partial PR.
    **But let `asyncio.CancelledError` propagate** (outer `/chat` request cancellation
    must NOT be swallowed/converted — Codex rev-3): catch `Exception`, re-raise
    `CancelledError`. Tested (one sub-agent raises → sibling cancellation → no editor
    call; outer cancel propagates).
12. **Exactly ONE operator `final_response` (Codex blocker).** `run_chat_stream` treats
    every `event.is_final_response()` as reply text + a `final_response` log
    (`adk_agent.py:847`); in a `ParallelAgent` EACH sub-agent emits its own final.
    During slice authoring D5 streams **only** tool-call / thought / usage events (via a
    dedicated emit path), suppressing per-slice `final_response`; the SINGLE operator
    `final_response` is emitted by the orchestrator **after** the barrier + editor
    outcome. Tested: N slice finals do NOT complete the timeline; exactly one final is
    logged; it follows the editor result.
13. **Branch/slice event tagging (Codex blocker).** `_emit_event_logs` does not carry
    `event.branch`. D5 adds a thin wrapper that injects `branch` / `slice_id` /
    `target_path` into each streamed slice event so the UI can group interleaved
    parallel events. The `seq`/`insert_id`/`timestamp` augmentation (`_stream`) is
    preserved and tested for monotonicity across interleaving.
14. **Session lifecycle + decompose-event buffering (Codex rev-3 fix).** Decompose and
    parallel-authoring use **separate** `InMemorySessionService` sessions, so decompose
    chatter cannot leak into slice prompts via shared root-branch history (ParallelAgent
    branch contexts include root events). **Decompose events are BUFFERED, not streamed
    live:** they are emitted (tagged `phase=decompose`) only once fan-out is committed
    (`N≥2`). On the single-slice / non-policy fallback the buffered decompose events are
    **discarded** and the orchestrator delegates cleanly to `run_chat_stream` — whose
    `_stream()` starts `seq` at 1 — so the legacy path stays **byte-identical to today**
    and there are no duplicate `seq`/`insert_id` keys. (The outer generator owns the
    single `seq` counter for the committed-fan-out path.)
15. **Decomposition failure fails OPEN to the single-agent path (Codex important) —
    EXCEPT policy violations.** A non-policy decompose failure (timeout, malformed
    structured output, model produced no plan) → fall back to `run_chat_stream`
    single-agent (the proven path; the operator still gets a PR attempt). A **policy**
    failure surfaced by `validate_slice_specs` (foundation path, secret/provider intent,
    duplicate/colliding path, >MAX_SLICES) → **fail closed** with the violation, never
    silently downgraded. Tested both ways.

---

## 3. Slice plan overview

| Slice | Title | Surface | Branch |
|---|---|---|---|
| **D5-1** | `SliceSpec` + `validate_slice_specs` (pure lib) | `agent/fanout.py` | `feat/iac-d5-slicespec` |
| **D5-2** | `submit_slice_file` tool factory (content-only, path-pinned) | `agent/fanout.py` | `feat/iac-d5-slice-tool` |
| **D5-3** | Slice-author agent factory (read tools + submit, NO editor) | `agent/fanout.py` | `feat/iac-d5-slice-agent` |
| **D5-4** | `decompose()` — structured LLM call → validated `list[SliceSpec]` | `agent/fanout.py` | `feat/iac-d5-decompose` |
| **D5-5** | `author_slices_parallel()` — ADK `ParallelAgent` + deterministic barrier | `agent/fanout.py` | `feat/iac-d5-parallel-author` |
| **D5-6** | `run_provision_fanout_stream()` orchestrator + one editor call + SSE | `agent/fanout.py`, `agent/adk_agent.py` | `feat/iac-d5-orchestrator` |
| **D5-7** | Wire `/chat` provision routing + single-slice fallback + prompt + parity/inventory tests | `agent/`, `workloads/provision/`, `tests/` | `feat/iac-d5-wire` |
| **D5-8** | Docs: runbook note + handoff + deferred-list update | `docs/` | `feat/iac-d5-docs` |
| **D5-9** | Operator: coordinator rebuild + live multi-slice e2e (no new infra) | — | — |

Dependency order: D5-1 → D5-2 → D5-3 → D5-4 → D5-5 → D5-6 → D5-7 → D5-8 → D5-9.

---

## 4. Slice detail

> Each slice: TDD (write failing test → run/confirm fail → minimal impl → run/confirm
> pass → commit). Reuse existing helpers; match `agent/` style (module docstring,
> `from __future__ import annotations`, typed signatures, fail-closed). Keep the full
> suite green (`pytest -q`) at each commit.

### D5-1: `SliceSpec` + `validate_slice_specs`

**Files:** Create `agent/fanout.py`; Test `tests/unit/test_fanout_slicespec.py`.

- `SliceSpec` (Pydantic v2, `extra="forbid"`): `goal: str` (non-empty, bounded len),
  `target_path: str`. Optional `doc_citations: list[str] = []`.
- `class FanoutFailureKind(Enum)`: `POLICY`, `DECOMPOSE_NON_POLICY`, `AUTHORING`,
  `EDITOR`. The orchestrator's fail-open/fail-closed branch keys off **`kind`**, NOT the
  HTTP status (Codex rev-3): fail OPEN to single-agent only for `DECOMPOSE_NON_POLICY`;
  everything else fails closed.
- `class FanoutError(Exception)` with `status: int`, `detail: str`, and
  `kind: FanoutFailureKind` (status is for the API/user surface; `kind` drives safety
  branching). Mirror `EditorPolicyError` otherwise.
- `MAX_SLICES = 8`.
- First, in `driftscribe_lib/iac_editor_policy.py` expose a thin **public**
  `validate_iac_path(path: str) -> None` that delegates to the existing
  `_validate_one_path` (so the fan-out path does not import a private symbol; keep
  `validate_file_writes` calling the same internal). One-line change + a test that the
  public wrapper behaves identically.
- `validate_slice_specs(specs: list[SliceSpec]) -> None`: raise `FanoutError(422, …)`
  if `len(specs) == 0` or `> MAX_SLICES`; for each, call `validate_iac_path(
  spec.target_path)` (enforces `iac/` prefix, `.tf`/`.md` suffix, non-foundation,
  no `..`); enforce **disjoint** `target_path`s (duplicate →
  `FanoutError(422, "duplicate slice path: …")`).

**Tests:** empty → 422; 9 slices → 422; duplicate paths → 422; foundation path
(`iac/versions.tf`) → 422; traversal (`iac/../x.tf`) → 422; non-`iac/` → 422; valid
2-slice set → no raise; `validate_iac_path` parity with `_validate_one_path`.

### D5-2: `submit_slice_file` tool factory

**Files:** `agent/fanout.py`; Test `tests/unit/test_fanout_submit_tool.py`.

- `make_submit_slice_file(target_path: str, sink: dict) -> Callable`: returns a function
  `submit_slice_file(content: str, citations: list[str] = []) -> dict` whose docstring
  (ADK uses it as the tool description) tells the model to submit the FULL file content
  for its assigned path plus any doc citations. Signature uses
  `citations: list[str] | None = None` (no mutable default — Codex nit), normalized to
  `[]`. It records `sink["file"] = {"path": target_path, "content": content}` and
  `sink["citations"] = citations or []`, returning an ack
  `{"status":"recorded","path":target_path,"bytes":len(content)}`.
- The returned callable's signature must expose **only** `content` + `citations`
  (authority-clean — no `path`/`target_path`/repo/branch arg). The path is captured
  from the closure; citations are non-authority metadata for the PR manifest.

**Tests:** signature params == `{"content","citations"}` (no `path`/`target_path`/repo);
calling it populates `sink["file"]` with the *pinned* path regardless of content;
citations recorded; second call overwrites (last-write-wins within a slice — assert
documented behavior); empty/whitespace content recorded as-is (the barrier rejects it,
not the tool); ack shape.

### D5-3: Slice-author agent factory

**Files:** `agent/fanout.py`; Test `tests/unit/test_fanout_slice_agent.py`.

- `build_slice_author_agent(spec: SliceSpec, read_tools: list, sink: dict) -> Agent`:
  returns an ADK `Agent` (`name=f"driftscribe_slice_{slug(spec.target_path)}"`,
  `model="gemini-2.5-flash"`, `BuiltInPlanner(ThinkingConfig(include_thoughts=True))`)
  whose `instruction` is a constrained slice prompt (author EXACTLY `spec.target_path`
  to achieve `spec.goal`; minimal/in-place; cite docs; **call `submit_slice_file` with
  the final content; do not attempt to open a PR**). `tools = read_tools +
  [make_submit_slice_file(spec.target_path, sink)]`.
- `read_tools` are the provision workload's read tools, resolved from the registry by
  filtering out mutation tools by **BOTH** the symbolic workload name
  (`provision_open_infra_pr`) **AND** the callable name (`open_infra_pr_tool`) — they
  differ (Codex important), so filtering on only one would leak the editor tool into a
  slice agent. Centralize this filter so it can't drift.

**Tests:** the built agent's tool names include the read tools + `submit_slice_file`
and **exclude** `provision_open_infra_pr` / `open_infra_pr_tool` / any name in the
mutation set (reuse `_MUTATION_TOOL_NAMES` from `test_coordinator_tool_inventory`);
explicit test that a slice agent cannot be constructed carrying the editor callable;
name is identifier-safe; instruction references the pinned path.

### D5-4: `decompose()`

**Files:** `agent/fanout.py`; Test `tests/unit/test_fanout_decompose.py`.

- `async def decompose(prompt: str, *, read_tools) -> DecomposeResult` where
  `DecomposeResult` = `{slices: list[SliceSpec], pr_title: str, pr_body_intro: str}`.
  Runs a single ADK decomposition `Agent` over the operator prompt in its **own**
  `InMemorySessionService` session (decision 14 — isolated from the authoring session so
  decompose chatter cannot leak into slice prompts), using a structured
  `submit_plan(slices, pr_title, pr_body_intro)` tool (record into a closure sink — same
  pattern as `submit_slice_file`), then `validate_slice_specs(result.slices)`.
- The decompose prompt instructs: split into INDEPENDENT one-file slices ONLY when the
  files have no cross-references; for any coupled/interdependent change, return a
  **single** slice (→ caller falls back to single-agent multi-file authoring).
- Outcome typing so the caller can apply decision 15: return a `DecomposeResult` on
  success; raise `FanoutError(<422 policy | 5xx non-policy>)` distinguishing
  **policy** failures (from `validate_slice_specs`) from **non-policy** failures
  (timeout, malformed/empty structured output) so the orchestrator fails CLOSED on the
  former and OPEN (→ single-agent) on the latter.

**Tests (mock the Runner/agent output — do NOT hit Gemini; mirror
`tests/unit/test_provision_workload.py` mocking):** a 2-slice plan → validated
`DecomposeResult`; a colliding/foundation/secret-path plan → `FanoutError` flagged
**policy** (fail-closed); a 1-slice plan → `len==1` (caller falls back); malformed/empty
structured output → `FanoutError` flagged **non-policy** (caller fails open); assert the
decompose session is distinct from any authoring session.

### D5-5: `author_slices_parallel()` + deterministic barrier

**Files:** `agent/fanout.py`; Test `tests/unit/test_fanout_parallel_author.py`.

- `async def author_slices_parallel(specs, *, read_tools, event_sink=None) ->
  AuthorResult` (explicit shape: `{files: list[{"path","content"}], citations:
  dict[path,list[str]]}`): build a per-slice `sink` dict + a slice agent for each spec;
  wrap the N agents in `ParallelAgent(name="driftscribe_fanout", sub_agents=[…])`; run
  via a `Runner` on a dedicated authoring session with a **custom slice-event loop**
  that calls ONLY `_emit_event_logs()` + `_emit_llm_usage()` and **never** executes
  `run_chat_stream`'s `is_final_response()` reply-collection branch (Codex rev-3 — that
  branch is what would emit per-slice finals). Forward **tagged** events to `event_sink`
  (decision 13 — inject `branch`/`slice_id`/`target_path`). **Fail-closed exception handling (decision
  11):** wrap the run in try/except; if the `ParallelAgent` `TaskGroup` raises (a slice
  sub-agent errored → siblings cancelled), discard ALL sink writes and raise
  `FanoutError(502, "slice authoring failed: …")` — no partial result escapes.
- **Barrier checks (only on a clean run):** every slice produced a non-empty file (else
  `FanoutError(502, "slice <path> produced no file")`); `validate_file_writes([...])`
  on the assembled list (disjoint + bounds). Return the merged `files`
  (`list[{"path","content"}]`) in slice order, plus collected citations.

**Tests (mock slice agents to deterministically populate their sinks — no real LLM):**
N=3 → 3 merged files in order; a slice whose agent never submits → `FanoutError(502)`;
**a slice whose agent raises → sibling cancellation, NO partial result, `FanoutError`,
editor never called**; empty/whitespace content → rejected; post-merge duplicate path
(crafted sink) → `validate_file_writes` raises; assert `ParallelAgent` constructed with
N sub-agents (concurrency actually used); events forwarded to `event_sink` carry the
slice tag and contain no per-slice `final_response`.

### D5-6: `run_provision_fanout_stream()` orchestrator + single editor call

**Files:** `agent/fanout.py`; minor helper exports from `agent/adk_agent.py`
(`_emit_event_logs`, `_emit_llm_usage`, `_stream`-equivalent); Test
`tests/unit/test_fanout_orchestrator.py`.

- `async def run_provision_fanout_stream(prompt, session_id=None) -> AsyncIterator[dict]`:
  1. Resolve provision read tools (registry, minus mutation tools by symbolic AND
     callable name — D5-3).
  2. `decompose(...)` into a **buffer** (events not yet yielded — decision 14), inside
     try/except branching on `FanoutError.kind` (decision 15): `kind ==
     DECOMPOSE_NON_POLICY` → **discard the buffer** and **delegate** to
     `run_chat_stream(prompt, session_id, workload="provision")` then return (fail-open,
     legacy path byte-identical, `seq` restarts at 1 cleanly). `kind == POLICY` → yield
     a final result surfacing the violation (fail-closed), no PR. If `len(slices) == 1`
     → discard buffer + delegate to `run_chat_stream` (single-agent path).
  3. Else (N≥2, committed): **flush the buffered decompose events** through the outer
     `_stream()` (tagged `phase=decompose`), then `author_slices_parallel(...)`,
     forwarding tagged sub-agent events through the SAME outer `_stream()`
     seq counter (one monotonic `seq` for the whole committed run; per-slice
     `final_response` suppressed — decisions 12/13).
  4. Derive authority via the **shared** `derive_iac_pr_authority(title)` helper
     extracted from `agent/adk_tools.py::open_infra_pr_tool` (`_get_iac_editor_target()`,
     `infra/<slug>-<ts>-<hex>`, base pinned downstream). To keep its equality testable
     despite `time.time()`/`secrets.token_hex` (Codex important), the helper takes
     optional injected `clock`/`rng` (defaulting to real ones); the equality test calls
     the ONE shared helper, not two independent derivations.
  5. Compose `title = result.pr_title`, `body = result.pr_body_intro + per-slice
     manifest (goal + path + citations)`; run `validate_title_body(title, body)` (MAX
     bounds) BEFORE the call.
  6. `await asyncio.to_thread(worker_client.call_open_infra_pr, target_repo, branch,
     title, body, merged_files)` **exactly once** — NOTE: no `base=` arg (wrapper pins
     it; passing it crashes). `to_thread` keeps the sync httpx call off the event loop.
     On `WorkerClientError` surface status/detail in the final result item (do NOT
     fabricate a PR).
  7. Emit the SINGLE operator `final_response` + yield final
     `{"type":"result","reply":<summary incl. pr_number, pr_url, exact C2→approve→(C6 if
     create-class) next steps>, "tool_calls":[…], "session_id":sid}`. `tool_calls`
     reflects operator-facing mutation only — i.e. one synthetic `open_infra_pr`
     entry — NOT the internal `submit_slice_file`/`submit_plan` calls (document this).

**Tests (mock `decompose`, `author_slices_parallel`, `call_open_infra_pr`):**
multi-slice → `call_open_infra_pr` invoked **once**, called WITHOUT a `base` kwarg, with
the merged files + derived authority (assert captured args); single-slice →
`run_chat_stream` delegated (fan-out NOT used); **non-policy** decompose failure →
`run_chat_stream` delegated (fail-open); **policy** decompose failure → final result
surfaces the violation, editor NOT called (fail-closed); editor `WorkerClientError(403)`
→ surfaced, no fabricated PR; exactly ONE `final_response`, emitted AFTER the editor
outcome; `seq` stays monotonic across interleaved slice events; derived authority comes
from the single shared helper.

### D5-7: Wire `/chat` provision routing + fallback + prompt + parity

**Files:** `agent/main.py` (the `/chat` provision branch, JSON + SSE), `agent/adk_agent.py`
(routing helper if needed), `workloads/provision/system_prompt.md`,
`tests/unit/test_provision_fanout_route.py`, update
`tests/unit/test_coordinator_tool_inventory.py` /
`tests/unit/test_provision_workload.py`.

- In `/chat`, when `workload == "provision"`, route through
  `run_provision_fanout_stream` (which internally falls back to single-agent for 1
  slice). Keep drift/upgrade/explore paths byte-unchanged. SSE + JSON both supported
  (JSON = drain, mirroring `run_chat`/`run_chat_stream`).
- Provision system prompt: add a short paragraph that for a request spanning multiple
  independent `iac/` files, the coordinator may author them as parallel slices merged
  into ONE PR (no behavior the operator must do differently; transparency only).
- **Parity/inventory tests:** slice-author agents never carry a mutation tool;
  provision still exposes exactly one editor convergence (`call_open_infra_pr` once);
  `/recheck?workload=provision` still route-refused (unchanged); drift/upgrade/explore
  `/chat` unaffected (regression guard).

**Tests:** `/chat?workload=provision` multi-slice (mocked) → one PR + SSE events for N
slices; single-slice → identical to today's single-agent output; other workloads
unchanged; recheck still 503.

### D5-8: Docs

**Files:** `docs/runbooks/tofu-editor.md` (add a "parallel fan-out (D5)" note: no new
infra; coordinator-internal; one PR), `docs/plans/2026-06-01-infra-iac-phase-d-agent-authoring.md`
(mark the D5 sketch "implemented — see this plan"), a short
`docs/handoff/2026-06-01-phase-d5-session-handoff.md`.

### D5-9: Operator live e2e (no new infra)

- Rebuild the coordinator (`cloudbuild.coordinator-update.yaml`, `_TAG=$(git rev-parse
  --short HEAD)`) — no SA/secret/IAM/worker changes.
- Positive e2e: on `/chat?workload=provision`, ask for a benign change spanning **two**
  independent already-declared `iac/` resources → confirm the SSE timeline shows two
  parallel slice authors → ONE PR (label `driftscribe-infra`, two `iac/*.tf` files) →
  CI static gate passes → (optionally) C2 → approve → C4 → merge.
- Negative e2e: a request that forces two slices onto the SAME file, or a
  secret/provider slice → confirm fail-closed (decompose validate or static gate),
  never a partial/duplicate PR. Drive the worker-level negative exactly as D4 did.
- Then revert coordinator to prior rev if desired (or keep). Record live IDs in the
  handoff + memory.

---

## 5. Risks & residuals

- **Decomposition quality** (the real D5 risk, like Phase D authoring): safety does not
  depend on the LLM decomposing well; usefulness does. Bad split → at worst a rejected
  or low-value PR (visible/recoverable), never an unsafe apply. Single-slice fallback
  bounds the downside.
- **Partial authoring** — a slice agent that errors/never submits fails the WHOLE
  fan-out closed (no partial PR). Explicit `FanoutError`.
- **Event-stream interleaving** — N parallel sub-agents interleave events; tag by
  ParallelAgent branch so the UI can group by slice; assert ordering-stability of the
  `seq` augmentation in tests.
- **Latency** — N agents run concurrently; the editor call still has the 30s
  `_HTTPX_TIMEOUT`. Authoring is the slow part and is parallel; the single editor call
  is fast (commit + PR open). No timeout change needed.
- **Drift between fan-out and single-agent authority derivation** — mitigated by the
  shared `derive_iac_pr_authority` helper + an equality test (D5-6).
- **Trust boundary** — unchanged: one editor call, same schema, same gates. Sub-agents
  hold no mutation tool (parity test). No new infra/SA/secret.
- **Decompose sees only `(goal, target_path)`, not content (Codex rev-3 nit).**
  `validate_slice_specs` cannot catch provider/secret/provisioner *content* intent from
  a SliceSpec alone — that is fine: such content is still caught downstream by the
  slice agents' in-authoring constraints, the worker's AGENT-mode static gate, and the
  D1-6 secret ban on the resulting PR. Decompose-time validation is path/structure only;
  the content gate remains the existing static gate (no regression, defense in depth).

---

## 6. Review history

- **rev-1** (2026-06-01): initial draft (agent), grounded in a fresh architecture map
  (ADK 1.33.0 confirmed to ship `ParallelAgent`/`SequentialAgent`/`AgentTool`;
  `run_chat_stream` integration point; editor convergence + `validate_file_writes`
  duplicate-path 403 as the hard constraint).
- **rev-2** (2026-06-01): folded one Codex review round (thread
  `019e82c0-fbdb-7161-9eb4-e0a7e055ad06`). **BLOCKERS fixed:** (1) `call_open_infra_pr`
  takes no `base=` — the wrapper pins `base="main"` internally (decision 6, D5-6); (2)
  `ParallelAgent`'s `asyncio.TaskGroup` raises-through on a sub-agent exception →
  fail-closed catch/discard/`FanoutError` (decision 11, D5-5); (3) per-sub-agent
  `final_response` would corrupt the single-reply timeline → suppress per-slice finals,
  emit exactly one operator final after the barrier (decision 12, D5-6); (4) event
  branch/slice tagging is not in `_emit_event_logs` → explicit tagging wrapper (decision
  13, D5-5); (5) citation channel had no parameter → `submit_slice_file(content,
  citations)` (decision 3, D5-2). **IMPORTANTs folded:** separate decompose/authoring
  sessions (decision 14); decomposition fails OPEN to single-agent on non-policy
  failure, CLOSED on policy violation (decision 15, D5-4); "one slice" = legacy
  single-agent multi-file authoring, fan-out only for N≥2 independent files (decision 5);
  shared `derive_iac_pr_authority` with injectable clock/rng for testable equality
  (D5-6); read-tool filter by symbolic AND callable name (D5-3); `validate_title_body`
  before the editor call + `asyncio.to_thread` for the sync httpx call (decision 6);
  public `validate_iac_path` wrapper (D5-1). **NITs folded:** `tool_calls` shows only
  operator-facing mutation; extra tests (empty content, malformed decompose,
  `WorkerClientError` after authoring).
- **rev-3** (2026-06-01): second Codex round on the same thread — verdict
  **READY-WITH-NITS**, all rev-2 blocker fixes confirmed. Folded the two remaining
  must-fixes + nits: (1) **decompose-event buffering** — buffer decompose events and
  flush only on commit (N≥2), discard on fallback, so the delegated `run_chat_stream`
  legacy path stays byte-identical and there are no duplicate `seq`/`insert_id` keys
  (decision 14, D5-6); (2) **typed `FanoutFailureKind` enum** drives the
  fail-open/fail-closed branch instead of HTTP status (D5-1, decision 15, D5-6); (3)
  let `asyncio.CancelledError` propagate, don't convert outer cancellation to
  `FanoutError` (decision 11); (4) the slice-event loop uses ONLY
  `_emit_event_logs`/`_emit_llm_usage`, never `run_chat_stream`'s `is_final_response`
  reply branch (D5-5); (5) `citations: list[str] | None = None` (D5-2); (6)
  `author_slices_parallel` returns an explicit `AuthorResult{files, citations}` (D5-5);
  (7) residual note: decompose validates path/structure only, content gate stays the
  static gate (Risks). **Codex final: READY** to implement.
