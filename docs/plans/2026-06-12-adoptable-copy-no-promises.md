# Adoption copy: state present capability, promise nothing ("not YET adoptable" papercut)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the future-promising "not **yet** adoptable" / "adoptable **today**" copy — most visibly on KMS key/keyring rows in the adopt list — with present-capability wording, and add a repo-wide banned-phrase pin so the promise can't creep back.

**Architecture:** Copy-only change across five surfaces (one Svelte chip, one tour sentence, one lib rejection message, two workload prompts) plus their existing test pins, plus ONE new "no future-promising adoption copy" scan test. No behavior changes anywhere. Coordinator-only rebake (SPA + prompts + lib are all baked into the coordinator image).

**Tech Stack:** Svelte/vitest, `driftscribe_lib`, workload prompt `.md` files, pytest.

---

## Background (the papercut)

Recorded after PR #108: "KMS key/keyring rows say 'not yet adoptable' (pre-existing copy for all non-adoptable types)." The roadmap's copy discipline (items 10/14/15: confidence-framing never safety; approve-scope and tour overclaim fixes) is that operator-facing copy states **present capability** and never implies futures. "Not *yet* adoptable" and "adoptable *today*" frame non-support as a pending roadmap item — a commitment DriftScribe has not made. For KMS specifically the implied promise is doubly wrong: importing key/keyring config would surface sensitive material, and there is no plan to support it.

## Grounding facts (every surface, found by repo-wide grep)

1. `frontend/src/components/InfraDiagram.svelte:577` — the adopt-list fallback chip `not yet adoptable` (`data-testid="adopt-unavailable"`), rendered for every unmanaged row that is neither adoptable nor control-plane (KMS keys/keyrings are the prominent live case). Pinned by `frontend/tests/unit/InfraDiagram.test.ts:551`; a comment at `:976` also quotes the phrase.
2. `frontend/src/lib/tour.ts:221` — tour estate fallback: "Your remaining unmanaged resources are not adoptable types **yet**." Pinned by `frontend/tests/unit/tour.test.ts:263` (`toContain('not adoptable types yet')`).
3. `driftscribe_lib/adopt_recipe.py:266-271` — the unknown-type `AdoptRecipeError`: "…If the operator's resource is none of these, it is not **yet** adoptable."
4. `workloads/provision/system_prompt.md:51-52` — "Anything else: explain it is not **yet** adoptable."
5. `workloads/explore/system_prompt.md:87` — "Only these four types are adoptable **today**."
6. NOT involved: the pinned lib constants (`adoption_order_sentence()`, `ADOPTION_ORDER_HONESTY`, `ADOPTION_CONTROL_PLANE_NOTE` end before these sentences — verified), the capability card ("Adoptable (import) types: …" is already present-tense), and the FINAL_REFUSAL_MARKER bullet. No pytest test pins the adopt_recipe sentence (only the frontend pins exist).
7. `agent/static/` contains the BUILT bundle with the old string — build output, not a source surface; excluded from the scan test and refreshed by the rebake.

## Replacements (exact)

| Surface | Old | New |
|---|---|---|
| InfraDiagram chip | `not yet adoptable` | `not an adoptable type` |
| tour.ts estate fallback | `…are not adoptable types yet. The Infrastructure panel…` | `…are not adoptable types. The Infrastructure panel…` |
| adopt_recipe.py error | `If the operator's resource is none of these, it is not yet adoptable.` | `If the operator's resource is none of these, DriftScribe cannot adopt it.` |
| provision prompt | `Anything else: explain it is not yet adoptable.` | `Anything else: explain that DriftScribe cannot adopt that type.` |
| explore prompt | `Only these four types are adoptable today.` | `Only these four types are adoptable.` |

(The chip wording `not an adoptable type` mirrors the lib's own vocabulary — `"…is not an adoptable resource type"` — present-tense, no roadmap implication, short enough for a row chip.)

## Design decisions

1. **One new banned-phrase scan test** (pytest, `tests/unit/test_adoption_copy_no_promises.py`): walk the SOURCE copy surfaces — `workloads/**/*.md`, `frontend/src/**/*.svelte` + `**/*.ts`, `driftscribe_lib/**/*.py`, `agent/**/*.py` excluding `agent/static/` — and assert none contains the phrases `not yet adoptable`, `adoptable today`, or `not adoptable types yet` (case-insensitive). Tests directories are deliberately NOT scanned (this test must be allowed to name the phrases). This is the re-introduction guard; the per-surface positives stay where they live (frontend pins updated in place).
2. **Existing pins updated, not weakened:** `InfraDiagram.test.ts:551` asserts the NEW chip text exactly; `tour.test.ts:263` asserts `'not adoptable types.'`; the `:976` comment wording updated.
3. **No prompt golden involved** — provision/explore prompts have no byte-equal golden (only the constant substring pins, untouched). The two prompt edits are free text.
4. **No behavior change:** the `AdoptRecipeError` type/flow is identical; only the sentence differs. Nothing reads these strings programmatically (verified: the frontend pins and the one adopt_recipe test region don't match on the changed fragment — implementer re-verifies with a full-suite run).
5. **Ship surface:** frontend + lib + prompts ⇒ coordinator-only rebake (no gate/denylist change ⇒ no worker rebakes).
6. **Live verify:** served bundle contains `not an adoptable type` and does NOT contain `not yet adoptable`; explore chat asked "can I adopt the KMS keyring?" answers in present-capability terms (qualitative — the prompt rule changed); graph still serves (`degraded` falsy).

## Out of scope

- Making any new type adoptable. Per-type explanations of WHY a type isn't adoptable (a richer future copy decision). The infra-reader timeout-edge residual (recorded under backlog 3). Historical docs/plans (they quote old copy as history; not scanned).

## Tasks

### Task 1: banned-phrase scan test (fails on current tree)

**Files:** Create `tests/unit/test_adoption_copy_no_promises.py`.

```python
"""No future-promising adoption copy ("not YET adoptable" papercut, 2026-06-12).

Operator-facing copy states PRESENT capability — the roadmap's copy
discipline (items 10/14/15) — so "not yet adoptable" / "adoptable today"
style promises are banned from every source copy surface. Tests dirs are
not scanned (this file must be free to name the phrases); agent/static/
is build output, refreshed by the coordinator rebake.
"""
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

_BANNED = ("not yet adoptable", "adoptable today", "not adoptable types yet")

_SCAN_GLOBS = (
    ("workloads", "**/*.md"),
    ("frontend/src", "**/*.svelte"),
    ("frontend/src", "**/*.ts"),
    ("driftscribe_lib", "**/*.py"),
    ("agent", "**/*.py"),
)


def test_no_future_promising_adoption_copy():
    offenders: list[str] = []
    for base, glob in _SCAN_GLOBS:
        for path in sorted((_REPO_ROOT / base).glob(glob)):
            if "static" in path.parts:
                continue
            text = path.read_text(encoding="utf-8").lower()
            for phrase in _BANNED:
                if phrase in text:
                    offenders.append(f"{path.relative_to(_REPO_ROOT)}: {phrase!r}")
    assert not offenders, "\n".join(offenders)
```

Run `.venv/bin/pytest tests/unit/test_adoption_copy_no_promises.py -q` → expect FAIL listing exactly the five grounding-fact surfaces (4 files: InfraDiagram.svelte, tour.ts, adopt_recipe.py, provision + explore prompts — five hits). If it lists MORE surfaces than the grounding facts, those are real additional offenders: fix them too and note the deviation.

### Task 2: apply the replacements + update frontend pins

1. Make the five replacements from the table (exact old/new).
2. `frontend/tests/unit/InfraDiagram.test.ts:551` → `toContain('not an adoptable type')`; update the `:976` comment to quote the new chip text.
3. `frontend/tests/unit/tour.test.ts:263` → `toContain('not adoptable types.')`.
4. Run: scan test PASS; `cd frontend && npm run test:unit` PASS; `npm run check` clean; full `.venv/bin/pytest -q`; `.venv/bin/ruff check --no-cache .`
5. Commit: `fix(copy): adoption copy states present capability — drop "not yet adoptable" promises`

## Ship steps

1. Branch `fix/adoptable-copy-no-promises`, PR, CI, Codex completed-work review (same thread), squash-merge.
2. Coordinator rebake at squash SHA → revision by digest → traffic 100%.
3. Live verify per decision 6.
4. Memory + closing report (backlog COMPLETE — 4/4).
