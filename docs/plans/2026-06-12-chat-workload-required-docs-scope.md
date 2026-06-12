# /chat requires explicit `workload` + drift docs-tool scope rules (PR #109 follow-up)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Close the two defects behind junk PR #109 — `/chat` silently routing a workload-less request to the mutation-capable drift agent, and the drift chat agent authoring a fabricated "this bucket is IaC-managed" doc because nothing scopes what its docs tool is for.

**Architecture:** Two independent, layered fixes. (1) Deterministic: `ChatRequest.workload` loses its `"drift"` default and becomes required — a workload-less POST gets a standard 422 instead of a capability assignment it never asked for. (2) Honesty-pinned prompt + tool-docstring scope rules for `patch_docs_tool`, following the roadmap's established pattern (prompt rule + model-facing docstring carve-out + pin tests). Backend-only; coordinator rebake required (prompt file is baked into the image, `Dockerfile.agent:36`).

**Tech Stack:** FastAPI/Pydantic (`agent/main.py`), workload prompt files (`workloads/drift/chat_system_prompt.md`), pytest.

---

## Incident (what actually happened, 2026-06-12)

A live probe POSTed `/chat` with only `prompt` — no `workload` field. `ChatRequest` defaulted to `"drift"` (`agent/main.py:4275`). The prompt was adoption-flavored (about `driftscribe-hack-2026-tofu-artifacts`, an **unmanaged** bucket that is also, since PR #108, a flagged control-plane identity). The drift chat agent has no adoption tools, so it answered with the one content-authoring tool it has — `patch_docs_tool` — and opened **PR #109 "Document IaC management for driftscribe-hack-2026-tofu-artifacts"** (file `demo/docs/iac_storage_bucket_management.md`): a doc fabricating IaC-managed status for a resource nothing manages. Notably it *followed* the existing cite-developer-docs prompt rule while fabricating the substance (the PR body dutifully cites a Cloud Storage docs URL) — external-doc citation is not observed-state grounding. The PR was closed by hand.

Two distinct defects:

1. **Ambiguous routing.** `workload` selects which agent and which tool set answer the request — the single most capability-relevant field in the API — and it silently defaults. The default's recorded rationale ("pre-17 callers that omit the field route as they always did", `main.py` ChatRequest docstring) is dead: the SPA always sends `workload` explicitly (`frontend/src/App.svelte:224`), and no script/CI caller in the repo posts `/chat` without it. The only beneficiaries of the default are ad-hoc curl probes — exactly the path that produced PR #109.
2. **Unscoped docs authoring.** Nothing in `workloads/drift/chat_system_prompt.md` says what `patch_docs_tool` is *for* (documenting the observed env-var configuration of the drift target service). Faced with an out-of-domain request, the agent's "helpful" move was to author fiction inside its path allowlist.

## Grounding facts

1. `agent/main.py:4256-4279` — `ChatRequest` (`extra="forbid"`), `workload: Literal["drift", "upgrade", "explore", "provision"] = "drift"`. Docstring records the back-compat rationale for the default.
2. `frontend/src/App.svelte:224` — the SPA always sends `JSON.stringify({ prompt, workload })`. No frontend change needed.
3. `agent/main.py:1819-1827` — `/eventarc` hardcodes `workload="drift"` deliberately (documented Codex blocker). `RecheckRequest` (`main.py:1521`) keeps its `"drift"` default — `/recheck` is the autonomous drift surface with its own chat-only-workload guard (`main.py:1127`). Neither is touched.
4. `agent/adk_tools.py` `patch_docs_tool` — LLM authors `file_path`, `new_content`, `title`, `body` freely; tool does NOT pre-validate (deliberate: worker 403 is the feedback loop). Branch is computed, not model-chosen.
5. `workers/docs/main.py:72-82` — Layer-2 gates are **shape**, not content: `ALLOWED_PATH = demo/docs/[^/]+\.md`, branch prefix `driftscribe/`, base `main`, caller allowlist. No content gate exists, and a deterministic "fabrication classifier" is not buildable honestly.
6. `workloads/drift/workload.yaml:44` — `drift_patch_docs` enabled for drift; `agent/workloads/registry.py:437` — tier `"propose"`, so under prod's `propose_apply` autonomy it runs without approval. Docs PRs are still human-merged (the action's blast radius is "a PR exists").
7. `workloads/upgrade/chat_system_prompt.md:52-54` — upgrade chat has NO docs tool (`docs_pr` explicitly "out of scope for /chat today"); explore/provision don't wire `drift_patch_docs`. The fabrication fix is drift-only.
8. `tests/unit/test_drift_workload_loads.py` `_DRIFT_CHAT_SYSTEM_PROMPT_GOLDEN` — byte-equal golden pin on the chat prompt; intentional edits must update file + literal together (the test's own docstring says so).
9. Workload-less `/chat` POSTs in tests (must gain `"workload": "drift"`): `tests/integration/test_chat_endpoint.py:42,65,91,111,138` and `tests/integration/test_pause_gates.py:281,329,353`. `test_chat_endpoint.py:161` POSTs `{}` expecting 422 — stays valid (now doubly-422). All other `/chat` tests already pass `workload`.
10. `Dockerfile.agent:36` — `COPY workloads/ ./workloads/`: prompt edits ship only via coordinator rebake.

## Design decisions

1. **`workload` becomes required on `/chat`.** Delete the default; keep the closed `Literal`. Missing field → FastAPI's standard 422 (`loc: ["body","workload"]`, "Field required"); invalid value → the Literal's 422 listing permitted values, unchanged. No custom error machinery — the standard 422 names the field, and the Literal error teaches the values. Rewrite the ChatRequest docstring: the old "pre-17 callers" rationale is replaced by the PR #109 rationale (capability selection must be explicit; the SPA always sends it).
2. **`/recheck` and `/eventarc` defaults untouched.** Different surfaces with documented semantics (facts 3). This plan changes only the operator-facing chat API.
3. **Prompt scope rule** appended to the `Rules:` section of `workloads/drift/chat_system_prompt.md` (exact text in Task 2). Substance: the docs tool documents the observed env-var configuration of the drift target service, grounded in this conversation's tool results; never author a doc claiming a resource is IaC-managed/adopted/imported — that is the provision workload's pipeline; redirect adoption asks instead of substituting a docs PR; unverifiable claims don't go into docs. Golden literal updated in lockstep (fact 8) **plus** a focused substring pin test so a future wholesale prompt rewrite can't silently drop the rule (the golden alone can't distinguish "intentional evolution" from "rule lost").
4. **Model-facing docstring carve-out** on `patch_docs_tool` (item-15's `propose_adoption_tool` pattern — the docstring is what the model reads at tool-choice time), with its own substring pin test.
5. **No deterministic content gate — explicitly rejected.** A string-match fabrication classifier would be dishonest: trivially bypassed by paraphrase, and its existence would imply content is vetted when it isn't. The deterministic component of this fix is the 422 (the misrouted prompt that caused #109 now never reaches an agent); residual risk for an in-domain drift chat that fabricates is bounded by the worker's path allowlist (fact 5) and the human merge gate on every docs PR (fact 6).
6. **Ship surface:** backend + prompt only. No frontend change, no worker rebakes, coordinator rebake + traffic cutover required. Live verify is directly probeable (unlike item 15): the incident's own shape becomes the regression probe — workload-less POST → 422; `workload="drift"` adoption-flavored prompt → refusal/redirect with zero `patch_docs_tool` calls and no new PR.

## Out of scope

- `/recheck` & `/eventarc` workload semantics (fact 3 — deliberate, documented).
- Secret-content scanning of chat-authored docs (`secret_guard` covers the autonomous validator path; the chat path's exposure is a separate question — note for backlog, do not bundle).
- Upgrade/explore/provision prompts (fact 7 — no docs tool there).
- KMS "not yet adoptable" copy, infra-reader concurrency (separate backlog items).
- Any UI change.

## Tasks

### Task 1: `workload` required on `/chat`

**Files:**
- Modify: `agent/main.py:4256-4279` (ChatRequest)
- Test: `tests/integration/test_chat_endpoint.py`
- Modify: `tests/integration/test_chat_endpoint.py:42,65,91,111,138`, `tests/integration/test_pause_gates.py:281,329,353` (add explicit `"workload": "drift"`)

**Step 1: Write the failing test** (in `tests/integration/test_chat_endpoint.py`, near the existing 422 test at :161):

```python
def test_chat_missing_workload_is_422(client):
    """PR #109 follow-up: ``workload`` selects which agent and tool set
    answer the request — it must be explicit. A workload-less POST used
    to silently default to the mutation-capable drift workload; that
    default routed an adoption-flavored probe to an agent whose only
    authoring tool is docs, which fabricated junk PR #109. Now: 422.
    """
    r = client.post("/chat", json={"prompt": "hi"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert any(
        e.get("loc") == ["body", "workload"] and e.get("type") == "missing"
        for e in detail
    )
```

**Step 2: Run it — expect FAIL** (currently 200/502-ish, not 422): `.venv/bin/pytest tests/integration/test_chat_endpoint.py::test_chat_missing_workload_is_422 -q`

**Step 3: Implement.** In `ChatRequest`: change

```python
    workload: Literal["drift", "upgrade", "explore", "provision"] = "drift"
```

to

```python
    workload: Literal["drift", "upgrade", "explore", "provision"]
```

and replace the docstring's Phase-17.A.3 default paragraph with:

```
    Phase 17.A.3 introduced ``workload``; the PR #109 follow-up made it
    REQUIRED. ``workload`` selects which agent — and therefore which
    tool set, including mutation tools — answers the request, so it
    must be explicit: a workload-less POST once defaulted to the
    mutation-capable drift workload and an out-of-domain probe prompt
    became fabricated docs PR #109. The Literal closes the set; pydantic
    422s both a missing field and an unknown value before the handler
    body runs. The SPA always sends workload (App.svelte); /recheck and
    /eventarc keep their own documented drift defaults — those are
    autonomous surfaces, not this one.
```

**Step 4: Update the eight legacy call sites** (fact 9) — add `"workload": "drift"` to each `client.post("/chat", json={...})` body. Do NOT touch `test_chat_endpoint.py:161` (`json={}` — still 422).

**Step 5: Run the affected files, expect PASS:** `.venv/bin/pytest tests/integration/test_chat_endpoint.py tests/integration/test_pause_gates.py -q` — then the full suite: `.venv/bin/pytest -q` (catches any straggler call site the grep missed).

**Step 6: Commit:** `feat(chat): require explicit workload on /chat — no silent drift default (PR #109 follow-up)`

### Task 2: drift docs-tool scope rule (prompt + golden + pin)

**Files:**
- Modify: `workloads/drift/chat_system_prompt.md` (append to `Rules:`)
- Modify: `tests/unit/test_drift_workload_loads.py` (`_DRIFT_CHAT_SYSTEM_PROMPT_GOLDEN` + new pin test)

**Step 1: Write the failing pin test** (in `test_drift_workload_loads.py`):

```python
def test_drift_chat_prompt_pins_docs_scope_rule():
    """PR #109 follow-up: the docs tool's scope rule must stay in the
    drift chat prompt. The byte-equal golden above pins *every* edit;
    this test pins *this rule specifically*, so a future intentional
    prompt rewrite (which legitimately updates the golden) still can't
    drop the fabrication guard without failing a named test.
    """
    text = (
        _REPO_ROOT / "workloads" / "drift" / "chat_system_prompt.md"
    ).read_text(encoding="utf-8")
    assert "NEVER author a doc that claims a resource is managed by" in text
    assert "adoption or import" in text
    assert "do not write it into a doc" in text
```

**Step 2: Run — expect FAIL.**

**Step 3: Append to the `Rules:` section of `workloads/drift/chat_system_prompt.md`** (before the final "Be concise" line, matching the list style):

```
- patch_docs_tool documents ONLY the observed env-variable configuration
  of the target Cloud Run service (the one read_live_env_tool reports
  on), grounded in what your tools returned in THIS conversation. NEVER
  author a doc that claims a resource is managed by, adopted into, or
  imported into IaC — adoption and import run through the provision
  workload's human-approved pipeline, and a docs PR is not a state
  change. If the operator asks about adoption or import, say this is
  the drift workload and point them at the provision workload instead
  of opening a docs PR. If you cannot verify a claim with a tool result
  from this conversation, do not write it into a doc.
```

**Step 4: Update `_DRIFT_CHAT_SYSTEM_PROMPT_GOLDEN`** in the same test file — insert the identical block at the identical position (the golden test's docstring mandates lockstep edits).

**Step 5: Run, expect PASS:** `.venv/bin/pytest tests/unit/test_drift_workload_loads.py -q`

**Step 6: Commit:** `feat(drift): scope patch_docs_tool in the chat prompt — never document adoption/IaC status (PR #109 follow-up)`

### Task 3: `patch_docs_tool` docstring carve-out

**Files:**
- Modify: `agent/adk_tools.py` (`patch_docs_tool` docstring)
- Test: `tests/unit/test_adk_tools.py` (or the file that already tests `patch_docs_tool`; place it with its siblings)

**Step 1: Write the failing pin test:**

```python
def test_patch_docs_tool_docstring_pins_scope_carve_out():
    """The docstring is the model-facing tool description (ADK reads it
    at tool-choice time) — same pattern as propose_adoption_tool's
    control-plane carve-out (PR #108). Pin the PR #109 scope language.
    """
    doc = adk_tools.patch_docs_tool.__doc__ or ""
    assert "observed env-variable configuration" in doc
    assert "never" in doc.lower() and "IaC-managed" in doc
```

**Step 2: Run — expect FAIL.**

**Step 3: Append to `patch_docs_tool`'s docstring** (after the branch-name paragraph):

```
    Scope carve-out (PR #109 follow-up): this tool documents the
    observed env-variable configuration of the drift target service —
    nothing else. Never use it to describe a resource as IaC-managed,
    adopted, or imported; adoption runs through the provision
    workload's human-approved pipeline, and a docs PR must never be
    offered as a substitute for a state change.
```

**Step 4: Run, expect PASS.**

**Step 5: Full suite + lint:** `.venv/bin/pytest -q` and `.venv/bin/ruff check --no-cache .`

**Step 6: Commit:** `feat(drift): model-facing scope carve-out on patch_docs_tool docstring (PR #109 follow-up)`

## Ship steps

1. Branch `fix/chat-workload-required-docs-scope`, PR, CI watch (plan-builder "skipping" expected).
2. Codex completed-work review on the plan-review thread.
3. Squash-merge → coordinator rebake (`gcloud builds submit --config=infra/cloudbuild.coordinator-update.yaml --substitutions=_TAG=<short-sha>`) → find revision by image digest → `update-traffic --to-revisions=<new>=100`.
4. Live verify (directly probeable): (a) POST `/chat` `{"prompt":"hi"}` → 422 naming `body.workload`; (b) POST with `workload="drift"` + an adoption-flavored prompt about the tofu-artifacts bucket → reply redirects to provision, **zero** `patch_docs_tool` calls, no new PR on the repo; (c) SPA chat unaffected (sends workload explicitly).
5. Memory update + closing report.
