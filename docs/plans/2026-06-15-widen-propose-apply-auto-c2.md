# Widen `propose_apply`: auto-dispatch the C2 plan-builder Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** When the agent opens an infrastructure PR while the autonomy dial is in `propose_apply`, automatically dispatch the C2 plan-builder (`workflow_dispatch` on `iac.yml`, `pr_number`, ref `main`) so the operator's `/iac-approvals/<N>` page is approve-ready with no manual `gh workflow run`. **The human Approve gate is untouched** — this only removes the manual plan-build step that precedes approval.

**Architecture:** The tofu-editor worker (which already opens the PR and holds the only write-capable, single-repo PAT) performs the dispatch as a fail-soft step right after `create_pull`, gated by a `dispatch_plan_builder` boolean the coordinator passes in the `/open-pr` body. The coordinator sets that boolean iff the autonomy mode is `propose_apply`. The mode reaches the single-agent tool via a request-scoped contextvar (the fan-out path already has `autonomy_mode` explicitly). The coordinator reply copy branches on the worker's returned `plan_builder_dispatched` flag. Apply authorization (the HMAC/CSRF-signed, plan-bound approval minted only on the operator's `/iac-approvals` GET→POST) is **not** changed in any way.

**Tech Stack:** Python 3.12, FastAPI, PyGithub (`Workflow.create_dispatch`), pytest, Jinja2 (approval page), Cloud Run (coordinator + tofu-editor worker), GitHub Actions (`iac.yml`).

---

## Operator prerequisite (BLOCKING for the live effect; feature is ship-safe without it)

Grant **Actions: Read and write** to the **`tofu-editor-github-pat`** fine-grained PAT, scoped to the single repo `adi-prasetyo/driftscribe`. `workflow_dispatch` via the API requires `actions: write`; the PAT currently has only `Contents: write` + `Pull requests: write`.

- I cannot do this — it is a GitHub account action the operator performs.
- **Degradation is safe:** if the scope is absent, the worker's dispatch call raises, we catch it, `plan_builder_dispatched=False` comes back, and the reply falls back to the existing "Operator: dispatch the C2 plan-builder…" copy. Nothing breaks; the manual flow still works. So the code can merge/deploy before the scope is granted.

**Why the worker, not the coordinator, holds `actions: write`:** keeps the coordinator (the agent "brain", incl. the autonomous Eventarc path) free of any Actions-write capability. The dispatch is performed only by the constrained single-purpose worker, and only when the coordinator — after the `propose_apply` mode check — instructs it via the narrow `/open-pr` contract. The dispatch is a natural extension of "open a reviewable PR".

**Security framing (precise — reviewed by Codex thread 019ec6f4):** this IS a real boundary change, just not an *apply*-boundary break. What changes: the autonomous (Eventarc) path can now cause a WIF-backed `tofu plan` against live state + an artifact upload + a PR comment, where before that required a manual operator dispatch. What does NOT change: no mutation happens without a human. The apply still requires `POST /iac-approvals/{n}` — CF operator identity + CSRF/origin check + autonomy `propose_apply` + artifact re-resolution + PR-readiness — which the autonomous path cannot forge. Auto-dispatch does NOT mint GCP creds *for the agent*: the C2 workflow runs in GitHub Actions with WIF; creds never leave the runner. The agent only *triggers* a run on a `pr_number`; `iac.yml`'s guards (base==main, not cross-repo, pure-git diff-guard rejecting any non-`iac/` path, hardcoded `MODE=agent` re-gate, C1 denylist failing the build before upload) still apply. **Correct one-liner for the PR/UX:** *the agent may build the proposal automatically; the operator still approves any mutation.*

**Storm / cost posture (Codex Medium — accepted with a bounded response):** `iac.yml` concurrency is per `pr_number`, so it does NOT bound many *distinct* PRs. Mitigations relied on: the `/eventarc` handler is claim-first (one drift event → one LLM run → one PR → one dispatch; retries don't multiply), and opening the PR is itself the real decision point that already exists today. Added here: a structured log event per auto-dispatch (Task 2) for alerting, and a reused-PR skip (Task 2/4) so a re-proposal of an already-open PR doesn't re-fire. **Deferred (documented, not dropped):** a coordinator-side rate/budget cap on auto-dispatch — reasonable hardening but scope-creep for this PR given the above. Revisit if autonomous proposal volume ever climbs.

---

## Task 1: `dispatch_workflow` helper in `driftscribe_lib/github.py`

**Files:**
- Modify: `driftscribe_lib/github.py`
- Test: `tests/unit/test_github_dispatch_workflow.py` (create)

**Step 1: Write the failing test**

```python
from unittest.mock import MagicMock
import pytest
from driftscribe_lib.github import dispatch_workflow

def test_dispatch_workflow_calls_create_dispatch_with_args():
    repo = MagicMock()
    wf = MagicMock()
    repo.get_workflow.return_value = wf
    dispatch_workflow(repo, "iac.yml", "main", {"pr_number": "123"})
    repo.get_workflow.assert_called_once_with("iac.yml")
    wf.create_dispatch.assert_called_once_with("main", {"pr_number": "123"})

def test_dispatch_workflow_propagates_errors():
    repo = MagicMock()
    repo.get_workflow.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        dispatch_workflow(repo, "iac.yml", "main", {"pr_number": "1"})
```

**Step 2:** `uv run pytest tests/unit/test_github_dispatch_workflow.py -v` → FAIL (no `dispatch_workflow`).

**Step 3: Implement** (thin wrapper; raises on failure — the caller fail-softs):

```python
def dispatch_workflow(repo, workflow_filename: str, ref: str, inputs: dict[str, str]) -> None:
    """Fire a workflow_dispatch on ``workflow_filename`` at ``ref`` with ``inputs``.

    Thin PyGithub wrapper (``Workflow.create_dispatch``). Requires the token to
    carry ``actions: write``. Raises on any failure — callers decide whether to
    fail soft. ``inputs`` values must be strings (GitHub coerces workflow inputs
    from strings)."""
    repo.get_workflow(workflow_filename).create_dispatch(ref, inputs)
```

**Step 4:** `uv run pytest tests/unit/test_github_dispatch_workflow.py -v` → PASS.

**Step 5:** Commit.

---

## Task 2: tofu-editor worker — optional plan-builder dispatch on `/open-pr`

**Files:**
- Modify: `workers/tofu_editor/main.py` (request model + `open_pr` handler ~238-349)
- Test: `workers/tofu_editor/tests/` (mirror existing worker test layout; create `test_open_pr_dispatch.py`)

**Step 1: Write failing tests** — cover:
- `dispatch_plan_builder=True` + PR newly opened → `dispatch_workflow(repo, "iac.yml", "main", {"pr_number": "<n>"})` called **exactly once with those literal args** (the request body must NOT be able to choose workflow/ref/inputs — assert they are hardcoded); response `plan_builder_dispatched is True`.
- `dispatch_plan_builder=False` (or omitted) → `dispatch_workflow` NOT called; `plan_builder_dispatched is False`.
- `dispatch_plan_builder=True` but `open_iac_pr` returns a **reused** PR (already open) → `dispatch_workflow` NOT called (skip re-fire); `plan_builder_dispatched is False`.
- `dispatch_plan_builder=True` but `dispatch_workflow` raises → response still 200/`opened`, `plan_builder_dispatched is False` (fail-soft), warning logged.

**Step 3: Implement.** Add `dispatch_plan_builder: bool = False` to the `/open-pr` request model (the ONLY new field — the worker hardcodes workflow/ref/inputs so a caller can never widen the dispatch surface). After a successful `open_iac_pr(...)` returns `result`:

```python
plan_builder_dispatched = False
# Skip on a reused PR: re-proposing an already-open PR shouldn't re-fire a plan
# run (idempotency / noise control — Codex review). `open_iac_pr` signals reuse;
# confirm the exact key during impl (e.g. result.get("reused")).
if req.dispatch_plan_builder and not result.get("reused"):
    try:
        ds_github.dispatch_workflow(
            repo, "iac.yml", "main", {"pr_number": str(result["number"])}
        )
        plan_builder_dispatched = True
        log.info(
            "c2_plan_builder_auto_dispatched",
            extra={"pr_number": result["number"]},  # structured: enables alerting on volume
        )
    except Exception:  # noqa: BLE001 — fail-soft: PR is open; operator can dispatch manually
        log.warning("c2_plan_builder_dispatch_failed", exc_info=True)
# ... existing return dict, plus:
return {..., "plan_builder_dispatched": plan_builder_dispatched}
```

Use the same `repo` client already built at `workers/tofu_editor/main.py:112` (`ds_github.get_repo(GITHUB_TOKEN, TARGET_REPO)`). Comment: dispatch needs `actions: write` on `tofu-editor-github-pat`; absent → caught here. If `open_iac_pr` has no `reused` signal, add one (or detect via an existing field) so the skip is real, not aspirational.

**Step 4/5:** Tests pass; commit.

---

## Task 3: request-scoped autonomy-mode contextvar (single-agent path)

**Files:**
- Create: `agent/request_context.py`
- Modify: `agent/main.py` (`/chat` handler ~1296 and the Eventarc/recheck handler), `agent/adk_agent.py` (`run_chat_stream` entry — set the var in the same task the tools run in)
- Test: `tests/unit/test_request_context.py` (create)

**Step 1: Write failing tests** — default is `"observe"` (fail-closed → no dispatch); a token-based bind sets the value within a scope and **resets it on exit**, so a later run in the SAME task/thread does NOT inherit a stale `propose_apply` (Codex Medium — the critical correctness test). Cover: bind→get returns the bound mode; after the `reset`/context-manager exits, `get` returns the default again.

**Step 3: Implement** with a `Token`-based set/reset (NOT a bare `set` that leaks across reused tasks/threads):

```python
import contextvars
from contextlib import contextmanager

_autonomy_mode: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_autonomy_mode", default="observe"
)

def get_current_autonomy_mode() -> str:
    return _autonomy_mode.get()

@contextmanager
def autonomy_mode_scope(mode: str):
    """Bind the request's autonomy mode for the duration of the agent run,
    then reset — so a reused event-loop task / worker thread can never inherit
    a stale propose_apply (which would wrongly auto-dispatch)."""
    token = _autonomy_mode.set(mode)
    try:
        yield
    finally:
        _autonomy_mode.reset(token)
```

Wrap the agent run with `with autonomy_mode_scope(autonomy_mode):` around the `runner.run_async(...)` call in `run_chat_stream` (`agent/adk_agent.py` ~925) — the same coroutine that awaits the ADK tool calls, so the tool sees it AND it's reset before the next run. **Implementation risk to verify during build:** if ADK executes Python tools in a threadpool not sharing this context, the var won't propagate; fallback is to bind inside the tool wrapper / `_emit_event_logs` instead. Add a live-faithful test that calls the actual tool under the runner (or a representative threadpool path). Fail-safe regardless: default `observe` ⇒ no dispatch ⇒ manual-copy fallback (never a wrong-direction dispatch).

**Step 4/5:** Tests pass; commit.

---

## Task 4: coordinator threads the dispatch flag to the worker

**Files:**
- Modify: `agent/worker_client.py` (`call_open_infra_pr` ~506-540), `agent/adk_tools.py` (`_open_iac_pr_and_notify` ~704-747), `agent/fanout.py` (the `call_open_infra_pr` call ~1343-1350)
- Test: `tests/unit/test_worker_client.py`, `tests/unit/test_adk_tools_iac_pr.py` (extend existing)

**Step 1: Write failing tests:**
- `call_open_infra_pr(..., dispatch_plan_builder=True)` puts `"dispatch_plan_builder": true` in the POST body; default `False`.
- `_open_iac_pr_and_notify` computes `dispatch_plan_builder` from `get_current_autonomy_mode()` (`propose_apply` → True; `propose`/`observe` → False) and passes it through.
- fan-out passes `dispatch_plan_builder=(autonomy_mode == "propose_apply")`.

**Step 3: Implement.**
- `call_open_infra_pr(self, repo, branch, title, body, files, *, dispatch_plan_builder: bool = False)` → include in JSON body.
- `_open_iac_pr_and_notify`: `dispatch_plan_builder = get_current_autonomy_mode() == "propose_apply"` (import from `agent.request_context`); pass to `call_open_infra_pr`; read `result.get("plan_builder_dispatched")` and store on `compact_result` for the copy step (Task 5).
- fan-out (`fanout.py:1343`): pass `dispatch_plan_builder=(autonomy_mode == "propose_apply")` (already in scope); read `result.get("plan_builder_dispatched")` for `_compose_success_reply`.

**Step 4/5:** Tests pass; commit.

---

## Task 5: reply copy branches on `plan_builder_dispatched`

**Files:**
- Modify: `agent/adk_tools.py` — `iac_pr_next_steps` (~582-604), `notify_iac_pr_pending` body (~691-700), the two call sites (`_open_iac_pr_and_notify` ~737, adoption note ~899-906), and the tool docstrings (~770-772) for accuracy
- Modify: `agent/fanout.py` — `_compose_success_reply` call site (~1089)
- Test: `tests/unit/test_adk_tools_iac_pr.py` (extend; update existing copy-pinning tests)

**Step 1: Write failing tests** — `iac_pr_next_steps(123, plan_builder_dispatched=True)` contains "/iac-approvals/123", says the plan-builder was *started/kicked off* (NOT a guarantee it will succeed — Codex Low: `dispatched=True` only means GitHub accepted the request; it can still fail PR-state/denylist), and does NOT say "Operator: dispatch the C2 plan-builder"; with `plan_builder_dispatched=False` it keeps the existing dispatch instruction. Both keep the create-class re-bake clause.

**Step 3: Implement** — add keyword-only `plan_builder_dispatched: bool = False`:

```python
def iac_pr_next_steps(pr_number: object, *, plan_builder_dispatched: bool = False) -> str:
    where = (f"/iac-approvals/{pr_number}"
             if isinstance(pr_number, int) and not isinstance(pr_number, bool) and pr_number > 0
             else "/iac-approvals/<pr_number>")
    rebake = (" A PR that creates NEW resources also needs an operator "
              "re-bake (C6) before it can apply.")
    if plan_builder_dispatched:
        # "started", not "will be ready": GitHub accepted the dispatch, but the
        # run can still fail (PR state / C1 denylist / API). The approval page
        # (Task 6) covers the not-yet-there / failed case.
        return (
            f"I've started the plan-builder for this PR. When it finishes (usually a "
            f"minute or two), review & approve the plan at {where} — reload if it "
            f"isn't there yet." + rebake
        )
    return (
        "Operator: dispatch the C2 plan-builder on this PR number, then review & "
        f"approve at {where}." + rebake
    )
```

Thread the flag from the worker result into both call sites and into `notify_iac_pr_pending` (its audit-log body should say "the plan-builder has been dispatched; review & approve at …" when dispatched). Keep the adoption note append (~899-906) unchanged — it is additive and still correct.

**Step 4/5:** Tests pass; commit.

---

## Task 6: soften the approval-page "no artifact yet" copy

**Files:**
- Modify: `agent/templates/iac_approval.html` (the "No C2 tofu plan artifact found … Run the C2 plan-builder workflow first" block) and/or the Python string at `agent/adk_tools.py:961` if that is the source
- Test: `tests/integration/test_ui_iac_approval.py` (or the existing approval-page test) — assert the reworded copy; update any test pinning the old string

**Step 1:** Locate the exact "Run the C2 plan-builder workflow first, then reload this page." string (template vs Python). Write/adjust a test pinning the new wording.

**Step 3:** Reword to not imply the operator must always run it manually, e.g.:

> *No plan has been built for PR #N yet. If you just opened this PR with the autonomy dial at Propose + Apply, the plan-builder is running — give it a minute and reload. Otherwise, dispatch the C2 plan-builder for this PR, then reload.*

Keep it factual for both the auto-dispatched and manual cases. No new API calls (do not query Actions run status — out of scope).

**Step 4/5:** Test passes; commit.

---

## Task 7: full verification

**Test matrix that must exist (Codex finding 6 — gather across Tasks 2–5):**
- No dispatch on `propose` and on `observe`; dispatch only on `propose_apply`.
- No dispatch when the contextvar is unset (default `observe`).
- Contextvar resets between runs (stale `propose_apply` from a prior run in the same task/thread does NOT trigger a later dispatch).
- Worker dispatches exactly `iac.yml` / `main` / `{pr_number}` — request body cannot influence workflow/ref/inputs.
- Dispatch failure → fail-soft → `plan_builder_dispatched=False` → manual-copy fallback.
- Reused/already-open PR → no dispatch.

- `uv run pytest -q` (whole suite green; expect updated copy-pin tests).
- Worker tests: run the tofu-editor worker test subset.
- `cd frontend && npm run check` — **no frontend source changes expected** (the SPA `IacApprovalCta` copy stays accurate: you still "Review & approve" at the same link). Confirm no incidental drift.
- Confirm `ruff`/lint clean.
- Note in the PR body: **two services deploy** (coordinator + tofu-editor worker) and the **operator PAT prerequisite**.

---

## Deploy & rollout (after merge)

1. Operator grants `Actions: write` to `tofu-editor-github-pat` (prereq above). Until then: graceful fallback to manual copy.
2. Deploy the **tofu-editor worker** (its image changed) — `infra/cloudbuild.tofu-editor.yaml` equivalent; verify new revision serves and `update-traffic` if pinned.
3. Re-bake/deploy the **coordinator** — `infra/cloudbuild.coordinator-update.yaml`, then `update-traffic --to-revisions=<new>=100` (traffic is pinned).
4. Live-verify: with the dial at Propose + Apply, ask the agent to open a small infra PR → confirm (a) the reply says "I've dispatched the plan-builder…", (b) a new `iac` workflow run appears for that `pr_number`, (c) `/iac-approvals/<N>` renders the plan once the run finishes, (d) the Approve gate still requires the operator click. With the dial at Propose, confirm NO dispatch and the manual copy.

## Out of scope (deferred)

- The "Pending infra PRs" management panel (links + Cancel button) — separate, still-deferred.
- Live Actions-run-status polling on the approval page (would need `actions: read` on the page-render PAT).
- Any change to the apply authorization / approval signing. Untouched by design.
