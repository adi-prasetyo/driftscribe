# Provision prompt: final refusals are not parameter feedback (PR #108 papercut)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove the contradiction between the provision prompt's blanket "a `rejected` result is PARAMETER feedback — fix and call again" rule and the control-plane refusals introduced in PR #108, whose reason text explicitly says "This is not a parameter problem — do not retry."

**Architecture:** Prompt-copy fix with parity-by-pin, the repo's established pattern for static-prompt ↔ lib duplication (`tests/unit/test_adoption_order_prompts.py` module docstring states the doctrine). The refusal sentence becomes a named lib constant used by both raise sites (byte-identical messages — pure refactor), the prompt bullet gains an EXCEPTION clause quoting that exact sentence, and pins tie the three copies together so rewording any one of them fails a named test.

**Tech Stack:** `driftscribe_lib/adopt_recipe.py`, `workloads/provision/system_prompt.md`, pytest.

---

## Background

PR #108 (control-plane adopt filter, Codex 019eb932 MF2) added `_reject_control_plane()` in `driftscribe_lib/adopt_recipe.py:195-224` — a tool-boundary refusal whose `AdoptRecipeError` message ends, for both the bucket and the service arm, with the sentence **"This is not a parameter problem — do not retry."** The error maps to `{"status": "rejected", "reason": ...}` at the tool layer.

But the provision system prompt's pre-existing bullet (`workloads/provision/system_prompt.md:56-59`) says, unconditionally:

```
- A `rejected` result from provision_propose_adoption is PARAMETER feedback:
  read the reason, fix the parameters (or ask the operator for the missing
  fact), and call the tool again. Do not conclude a type is unadoptable
  unless the reason explicitly says the type is not adoptable.
```

So the prompt instructs "call the tool again" for a rejection class whose reason says "do not retry". Codex called this tension non-blocking in the PR #108 review; it was recorded as a papercut. The model currently gets the right outcome only because a *different* bullet (`:65-69`, the pinned `ADOPTION_CONTROL_PLANE_NOTE`) tells it not to call the tool on control-plane resources at all — but a model that calls anyway (e.g. a bucket the operator names without the suffix being obvious) reads two contradictory instructions about what the rejection means.

## Grounding facts

1. `driftscribe_lib/adopt_recipe.py:195-224` — `_reject_control_plane()`; both raise messages end with the identical sentence "This is not a parameter problem — do not retry."
2. `tests/unit/test_adopt_recipe.py:734,756` — existing tests assert `"not a parameter problem" in msg` for both arms (loose pin, kept).
3. `tests/unit/test_adoption_order_prompts.py` — the pin doctrine: static `.md` prompts duplicate canonical lib sentences by hand; whitespace-normalized substring pins make the duplication safe. `_normalized()` helper + `WORKLOADS` path already exist there.
4. `workloads/provision/system_prompt.md:56-59` — the contradicting bullet (quoted above).
5. Only provision has `provision_propose_adoption`; the explore prompt mentions adoption order/honesty but cannot call the tool — no explore change.
6. The PR #108 `propose_adoption_tool` docstring carve-out already tells the model control-plane rejections are final — the prompt bullet is the one remaining contradicting surface.
7. `Dockerfile.agent` bakes both `driftscribe_lib/` and `workloads/` → coordinator rebake. The lib refactor keeps refusal messages byte-identical, and no gate/denylist file changes ⇒ **no tofu-editor / tofu-apply / infra-reader rebake** (established rule).

## Design decisions

1. **Extract the sentence as a lib constant** in `driftscribe_lib/adopt_recipe.py`:
   ```python
   # The terminal sentence of every tool-boundary refusal that is FINAL —
   # i.e. NOT retryable parameter feedback. The provision system prompt
   # quotes this sentence verbatim so the model can classify a rejected
   # result by its reason text; tests/unit/test_adoption_order_prompts.py
   # pins the duplication in both directions.
   FINAL_REFUSAL_MARKER = "This is not a parameter problem — do not retry."
   ```
   Both `_reject_control_plane` raise sites switch to f-string interpolation of the constant. Messages stay byte-identical (pure refactor; existing loose pins at fact 2 keep passing).
2. **Amend the prompt bullet** (`system_prompt.md:56-59`) — replace it with:
   ```
   - A `rejected` result from provision_propose_adoption is usually PARAMETER
     feedback: read the reason, fix the parameters (or ask the operator for
     the missing fact), and call the tool again. EXCEPTION: a reason that
     says "This is not a parameter problem — do not retry." is FINAL — relay
     it to the operator plainly and do not call the tool again for that
     resource. Do not conclude a type is unadoptable unless the reason
     explicitly says the type is not adoptable.
   ```
   (Wording note: "usually" + the quoted marker keeps the retry guidance for genuinely-parameter rejections — admission-gate reasons like a missing location — while making the final class self-identifying by its own text.)
3. **Pins, both directions:**
   - `tests/unit/test_adopt_recipe.py`: strengthen the two existing assertions to `msg.endswith(FINAL_REFUSAL_MARKER)` (import the constant) — pins lib-internal parity (both arms carry the exact sentence; a rewording of the constant or either message fails here).
   - `tests/unit/test_adoption_order_prompts.py`: new `test_provision_prompt_quotes_the_final_refusal_marker` asserting `" ".join(FINAL_REFUSAL_MARKER.split()) in _normalized(provision prompt)` — pins prompt ↔ lib.
4. **No behavioral code change** — refusal semantics, statuses, and message bytes are unchanged; this is copy + pins. Live verification is therefore prompt-deployment verification (rebaked revision serving) plus the PR #108 behavior re-probe (provision chat asked to adopt a control-plane bucket → single refusal, zero tool calls — the prompt's primary instruction remains "don't call at all").

## Out of scope

- Explore prompt (fact 5). KMS "not yet adoptable" copy (backlog item 4). Infra-reader concurrency (backlog item 3). Any change to admission-gate rejection wording (those ARE parameter feedback).

## Tasks

### Task 1: constant + lib-parity pins

**Files:** Modify `driftscribe_lib/adopt_recipe.py`, `tests/unit/test_adopt_recipe.py:734,756`.

1. In `test_adopt_recipe.py`, import `FINAL_REFUSAL_MARKER` from `driftscribe_lib.adopt_recipe` and change both assertions (`:734`, `:756`) from `assert "not a parameter problem" in msg` to:
   ```python
   assert msg.endswith(FINAL_REFUSAL_MARKER)
   ```
   Run: `.venv/bin/pytest tests/unit/test_adopt_recipe.py -q` — expect FAIL (ImportError: no such constant).
2. Add the constant (decision 1, with its comment) near the other module-level constants in `adopt_recipe.py`; rewrite both raise messages to end with `{FINAL_REFUSAL_MARKER}` via f-string. Byte-identical output is the requirement.
3. Run again — expect PASS. Run `.venv/bin/pytest tests/unit/test_adopt_recipe.py tests/unit/test_provision_workload.py -q` for collateral.
4. Commit: `refactor(adopt): name the final-refusal sentence — FINAL_REFUSAL_MARKER, byte-identical messages`

### Task 2: prompt bullet + cross pin

**Files:** Modify `workloads/provision/system_prompt.md:56-59`, `tests/unit/test_adoption_order_prompts.py`.

1. Add to `test_adoption_order_prompts.py` (import the constant from `driftscribe_lib.adopt_recipe`):
   ```python
   def test_provision_prompt_quotes_the_final_refusal_marker():
       """PR #108 papercut: the prompt's "rejected = parameter feedback,
       call again" bullet contradicted the control-plane refusals whose
       reason says do-not-retry. The bullet now quotes the exact marker
       sentence; this pin keeps the quote and the lib constant in sync.
       """
       text = _normalized(WORKLOADS / "provision" / "system_prompt.md")
       assert " ".join(FINAL_REFUSAL_MARKER.split()) in text
   ```
   Run: expect FAIL.
2. Replace the bullet per decision 2 (exact text).
3. Run `.venv/bin/pytest tests/unit/test_adoption_order_prompts.py -q` — PASS. Then full suite `.venv/bin/pytest -q` + `.venv/bin/ruff check --no-cache .`
4. Commit: `fix(provision): rejected-result prompt bullet carves out final refusals (PR #108 papercut)`

## Ship steps

1. Branch `fix/provision-final-refusal-prompt`, PR, CI watch, Codex completed-work review (same thread as plan review), squash-merge.
2. Coordinator rebake at the squash SHA → revision by digest → traffic 100%. No worker rebakes (fact 7).
3. Live verify: revision serving; provision chat "adopt the bucket driftscribe-hack-2026-tofu-state into IaC" → single plain refusal, zero tool calls (re-run of the PR #108 probe — confirms no regression and the new copy didn't weaken the don't-call rule).
4. Memory + closing report.
