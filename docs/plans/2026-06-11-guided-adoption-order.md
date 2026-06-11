# Guided Adoption Order (roadmap item 10) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** After a scan, DriftScribe suggests *which* unmanaged resources to adopt
first — the simplest types to recognize and review (buckets, topics) before the
largest configs (Cloud Run) — as a deterministic ranking surfaced in the adopt
list and in the agent's scan/adopt prompts. No new mutation surface.

**Architecture:** One source of truth, three read-only surfaces. A new
`ADOPTION_GUIDE` constant in `driftscribe_lib/infra_graph.py` (rank + plain-
language hint per adoptable CAI asset type, drift-pinned to
`ADOPTABLE_ASSET_TYPES`) flows (1) into the `/infra/graph` DTO as per-group
`adopt_rank`/`adopt_hint` fields, (2) into the SPA adopt list as a rank-sorted
list with a "Start here" chip + per-group hint lines, and (3) into the explore
and provision system prompts as two canonical sentences — the order
(`adoption_order_sentence()`) and the honesty note
(`ADOPTION_ORDER_HONESTY`) — pinned by whitespace-normalized substring tests so
prompt text can never drift from the constants. Stale-coordinator responses
without the new fields render exactly today's UI (fail-quiet).

**Tech Stack:** Python (driftscribe_lib, pytest), Svelte 5 runes + TypeScript
(vitest + @testing-library/svelte), static workload prompt files (`.md`).

**Codex plan review:** thread `019eb608` — all must-fixes folded: (1) copy
reworded from blast-radius/safety language to review-comfort language, (2)
honesty phrases pinned in tests (both prompts + the SPA order note), (3) test
snippets corrected to the real `test_infra_graph.py` scaffolding
(`from driftscribe_lib.infra_graph import build_graph`, `_inventory(by_type=…)`,
module-level type constants); nice-to-haves folded: strict positive-safe-integer
rank on the client boundary, "among the unmanaged resources shown" scoping.
Additional fold found while applying: the prompt `.md` files hard-wrap (~75
cols), so the pins whitespace-normalize both sides before the substring check.

**Out of scope (explicit):** batch adoption (one-adoption-per-PR stays a
structural invariant — design doc D3), any change to denylist/gate/apply
pipeline (rebake surface stays coordinator-only), edges/topology-derived
ranking (Phase-1 graph is node-only; the heuristic ranks *types*, not nodes),
`/capabilities` (ordering is guidance, not a capability claim).

**Honesty constraint (audience copy — Codex must-fix 1):** every adoption is
the same zero-change import behind the same approval gate, regardless of type.
The order is about *building confidence* — which configs are easiest to
recognize and review — never about one type being safer to adopt than another.
No "blast radius", "low-risk", "load-bearing" framing anywhere in user-facing
copy or hints. The canonical honesty sentence lives in the lib and is pinned
into every surface.

**Rebake surface:** coordinator only (`driftscribe_lib/` + `frontend/` +
`workloads/*.md` all bake into the coordinator image; `/infra/graph` passes the
`build_graph` dict through verbatim — verified, no field whitelist). No
tofu-apply / tofu-editor / infra-reader rebake (no gate, denylist, or `iac/`
change; infra-reader never imports `infra_graph`).

---

### Task 1: `ADOPTION_GUIDE`, `adoption_order_sentence()`, `ADOPTION_ORDER_HONESTY`

**Files:**
- Modify: `driftscribe_lib/infra_graph.py` (after `ADOPTABLE_ASSET_TYPES`, ~line 68)
- Test: `tests/unit/test_infra_graph.py` (append a new test class at the end)

**Step 1: Write the failing tests.** The file already has module-level constants
`RUN_TYPE`, `BUCKET_TYPE`, `TOPIC_TYPE`, `SUB_TYPE`, `SA_TYPE` and the
`_inventory(**overrides)` fixture; it imports symbols directly
(`from driftscribe_lib.infra_graph import build_graph` at top,
`ADOPTABLE_ASSET_TYPES` mid-file with `# noqa: E402`). Follow that pattern:

```python
# ---------------------------------------------------------------------------
# Item 10 (guided adoption order): deterministic "what to adopt first" ranking.
# Single source of truth, drift-pinned to the adoptable set — a new adoptable
# type CANNOT ship without a rank, a hint, and a plural label.
# ---------------------------------------------------------------------------

from driftscribe_lib.infra_graph import (  # noqa: E402
    _ADOPTION_PLURAL_LABELS,
    ADOPTION_GUIDE,
    ADOPTION_ORDER_HONESTY,
    adoption_order_sentence,
)


class TestAdoptionGuide:
    def test_guide_keys_are_exactly_the_adoptable_asset_types(self):
        assert set(ADOPTION_GUIDE) == set(ADOPTABLE_ASSET_TYPES)

    def test_plural_labels_keys_match_the_guide(self):
        assert set(_ADOPTION_PLURAL_LABELS) == set(ADOPTION_GUIDE)

    def test_ranks_are_unique_and_contiguous_from_1(self):
        ranks = sorted(rank for rank, _ in ADOPTION_GUIDE.values())
        assert ranks == list(range(1, len(ADOPTION_GUIDE) + 1))

    def test_hints_are_nonempty_and_never_safety_framed(self):
        # Honesty constraint (Codex must-fix 1): hints guide review comfort,
        # never imply one type is safer/riskier to adopt.
        for rank, hint in ADOPTION_GUIDE.values():
            assert hint and hint == hint.strip()
            lowered = hint.lower()
            for banned in ("risk", "danger", "blast", "safe"):
                assert banned not in lowered

    def test_order_sentence_is_derived_from_rank_order(self):
        assert adoption_order_sentence() == (
            "Storage buckets → Pub/Sub topics → Pub/Sub subscriptions → Cloud Run services"
        )

    def test_bucket_is_rank_1_and_run_service_is_last(self):
        assert ADOPTION_GUIDE[BUCKET_TYPE][0] == 1
        assert ADOPTION_GUIDE[RUN_TYPE][0] == len(ADOPTION_GUIDE)

    def test_honesty_note_says_zero_change_and_not_safety(self):
        # The load-bearing phrases every surface pins against.
        assert "same zero-change import" in ADOPTION_ORDER_HONESTY
        assert "not safety" in ADOPTION_ORDER_HONESTY
```

**Step 2: Run to verify failure**

Run: `pytest tests/unit/test_infra_graph.py::TestAdoptionGuide -x -q`
Expected: FAIL with `ImportError: cannot import name 'ADOPTION_GUIDE'`

**Step 3: Implement**

Insert after the `ADOPTABLE_ASSET_TYPES` block in `driftscribe_lib/infra_graph.py`:

```python
# Guided adoption order (roadmap item 10): deterministic "what to adopt first"
# ranking over the adoptable types. rank 1 = start here. The simplest configs
# to recognize and review come first; the largest (a live service definition)
# last. HONESTY: every adoption is the same zero-change import behind the same
# approval gate — the order is about building operator confidence, NEVER about
# one type being safer to adopt (tests ban safety framing in the hints).
# Drift-pinned: keys == ADOPTABLE_ASSET_TYPES, ranks unique + contiguous (a new
# adoptable type cannot ship unranked).
ADOPTION_GUIDE: dict[str, tuple[int, str]] = {
    "storage.googleapis.com/Bucket": (
        1,
        "a simple leaf resource — the easiest place to build confidence",
    ),
    "pubsub.googleapis.com/Topic": (
        2,
        "small and quick to review — a name and a handful of settings",
    ),
    "pubsub.googleapis.com/Subscription": (
        3,
        "best adopted after its topic, so the pair reads naturally in IaC",
    ),
    "run.googleapis.com/Service": (
        4,
        "the largest config to review — most operators adopt these once comfortable",
    ),
}

# Plural display labels for the canonical order sentence (prompt surface).
# Same drift-pin as ADOPTION_GUIDE.
_ADOPTION_PLURAL_LABELS: dict[str, str] = {
    "storage.googleapis.com/Bucket": "Storage buckets",
    "pubsub.googleapis.com/Topic": "Pub/Sub topics",
    "pubsub.googleapis.com/Subscription": "Pub/Sub subscriptions",
    "run.googleapis.com/Service": "Cloud Run services",
}

# Canonical honesty sentence (Codex must-fix 2): pinned verbatim (whitespace-
# normalized) into both workload prompts; the SPA order note pins the same
# phrases in its vitest. Never weaken this without updating every surface.
ADOPTION_ORDER_HONESTY = (
    "Every adoption is the same zero-change import behind the same approval "
    "gate — the order is about building confidence, not safety."
)


def adoption_order_sentence() -> str:
    """Canonical adoption-order phrase, derived from ADOPTION_GUIDE rank order.

    The explore + provision system prompts carry this string verbatim (modulo
    line wrapping — the pin test whitespace-normalizes both sides), so
    reordering the guide without updating the prompts fails CI.
    """
    ordered = sorted(ADOPTION_GUIDE, key=lambda t: ADOPTION_GUIDE[t][0])
    return " → ".join(_ADOPTION_PLURAL_LABELS[t] for t in ordered)
```

**Step 4: Run tests** — `pytest tests/unit/test_infra_graph.py -q` → all pass.

**Step 5: Commit** — `feat(lib): ADOPTION_GUIDE — deterministic guided-adoption ranking (item 10)`

---

### Task 2: `build_graph` emits `adopt_rank` / `adopt_hint` on adoptable groups

**Files:**
- Modify: `driftscribe_lib/infra_graph.py` (`build_graph`, after the `group = {...}` literal ~line 301)
- Test: `tests/unit/test_infra_graph.py`

**Step 1: Write the failing tests** (same class file; `_inventory(by_type=…)`
fixture exactly as `TestAdoptableFlag.test_adoptable_groups_for_the_four_types`
uses it):

```python
class TestAdoptRankInGraph:
    def _one_drift_group(self, atype: str) -> dict:
        return {
            atype: {
                "count": 1, "declared_in_iac": 0, "not_in_iac": 1,
                "sensitive": False,
                "sample": [{"name": "n", "location": "g", "iac": False,
                            "match_confidence": None}],
            }
        }

    def test_adoptable_group_carries_rank_and_hint(self):
        g = build_graph(_inventory(by_type=self._one_drift_group(BUCKET_TYPE)))
        grp = g["groups"][0]
        assert grp["adoptable"] is True
        assert grp["adopt_rank"] == 1
        assert grp["adopt_hint"] == ADOPTION_GUIDE[BUCKET_TYPE][1]

    def test_all_four_adoptable_types_carry_their_guide_rank(self):
        by_type = {}
        for t in (BUCKET_TYPE, TOPIC_TYPE, SUB_TYPE, RUN_TYPE):
            by_type.update(self._one_drift_group(t))
        g = build_graph(_inventory(by_type=by_type))
        got = {grp["asset_type"]: grp["adopt_rank"] for grp in g["groups"]}
        assert got == {t: ADOPTION_GUIDE[t][0]
                       for t in (BUCKET_TYPE, TOPIC_TYPE, SUB_TYPE, RUN_TYPE)}

    def test_non_adoptable_group_omits_rank_and_hint(self):
        # Omitted (not None) — mirrors the truncated_in_group convention.
        g = build_graph(_inventory(by_type=self._one_drift_group(SA_TYPE)))
        grp = g["groups"][0]
        assert grp["adoptable"] is False
        assert "adopt_rank" not in grp and "adopt_hint" not in grp

    def test_sensitive_group_omits_rank_and_hint(self):
        # adoptable is forced False on sensitive groups; rank must follow it.
        by_type = self._one_drift_group(BUCKET_TYPE)
        by_type[BUCKET_TYPE]["sensitive"] = True
        g = build_graph(_inventory(by_type=by_type))
        grp = g["groups"][0]
        assert grp["adoptable"] is False
        assert "adopt_rank" not in grp and "adopt_hint" not in grp
```

**Step 2: Run to verify failure** — KeyError `adopt_rank`.

**Step 3: Implement** — in `build_graph`, right after the `group = {...}` literal
(which already computes `adoptable`):

```python
        if group["adoptable"]:
            # Guided adoption order (item 10). .get (not [...]) keeps the
            # "never raises" contract even if the guide/adoptable drift-pin
            # were somehow violated at runtime.
            guide = ADOPTION_GUIDE.get(atype)
            if guide:
                group["adopt_rank"], group["adopt_hint"] = guide
```

**Step 4: Run** — `pytest tests/unit/test_infra_graph.py -q` → pass.

**Step 5: Commit** — `feat(lib): per-group adopt_rank/adopt_hint in the graph DTO`

---

### Task 3: prompt surface — explore + provision adoption-order guidance

**Files:**
- Modify: `workloads/explore/system_prompt.md` (new rule bullet, after the
  freshness-caveat bullet, before "Be concise")
- Modify: `workloads/provision/system_prompt.md` (new bullet at the end of the
  "Adopting existing resources" section, after the "One resource per adoption
  PR" bullet)
- Test: Create `tests/unit/test_adoption_order_prompts.py`

**Step 1: Write the failing test**

```python
"""Item 10: adoption-order + honesty copy in the prompts is PINNED to the lib.

The prompts are static .md files (no interpolation), so the canonical
sentences are duplicated by hand — these pins make the duplication safe:
changing ADOPTION_GUIDE order or the honesty note without updating both
prompts (or vice versa) fails here. The .md files hard-wrap, so both sides
are whitespace-normalized before the substring check.
"""
from pathlib import Path

import pytest

from driftscribe_lib.infra_graph import ADOPTION_ORDER_HONESTY, adoption_order_sentence

WORKLOADS = Path(__file__).resolve().parents[2] / "workloads"


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


@pytest.mark.parametrize("workload", ["explore", "provision"])
def test_prompt_carries_the_canonical_order_sentence(workload):
    text = _normalized(WORKLOADS / workload / "system_prompt.md")
    assert " ".join(adoption_order_sentence().split()) in text


@pytest.mark.parametrize("workload", ["explore", "provision"])
def test_prompt_carries_the_honesty_note(workload):
    text = _normalized(WORKLOADS / workload / "system_prompt.md")
    assert " ".join(ADOPTION_ORDER_HONESTY.split()) in text
```

**Step 2: Run to verify failure** — all four parametrized cases fail (absent).

**Step 3: Implement — explore prompt.** Append to the `Rules:` list (after the
freshness-caveat bullet, before "Be concise"):

```markdown
- When the inventory shows resources NOT declared in IaC and the operator
  wants to start bringing them under management, suggest this adoption order:
  Storage buckets → Pub/Sub topics → Pub/Sub subscriptions → Cloud Run
  services — the simplest to recognize and review first. Every adoption is
  the same zero-change import behind the same approval gate — the order is
  about building confidence, not safety. Only these four types are adoptable
  today. You cannot adopt from Explore (read-only): point the operator at the
  Adopt button on the resource map, or the Provision workload.
```

**Implement — provision prompt.** Append to the adoption section:

```markdown
- If the operator asks WHERE TO START or what to adopt first, suggest:
  Storage buckets → Pub/Sub topics → Pub/Sub subscriptions → Cloud Run
  services — the simplest to recognize and review first. Every adoption is
  the same zero-change import behind the same approval gate — the order is
  about building confidence, not safety. One resource per adoption PR,
  starting at the top of that order.
```

(Wrap lines to the file's existing column width; the pin normalizes whitespace
so wrapping is free.)

**Step 4: Run** — `pytest tests/unit/test_adoption_order_prompts.py tests/unit/test_explore_workload_loads.py tests/unit/test_provision_workload.py -q` → pass.

**Step 5: Commit** — `feat(prompts): suggest guided adoption order after a scan (item 10)`

---

### Task 4: frontend types + rank helper in `lib/infra_graph.ts`

**Files:**
- Modify: `frontend/src/lib/infra_graph.ts` (InfraGroup interface + new helper near `adoptRows`)
- Test: `frontend/tests/unit/infra_graph.test.ts`

**Step 1: Write the failing tests**

```ts
describe('adoptGroupRank', () => {
  const base = { asset_type: 't', label: 'T', count: 1, managed: 0, drift: 1,
    sensitive: false, nodes: [] } as unknown as InfraGroup;
  it('returns the rank for an adoptable ranked group', () => {
    expect(adoptGroupRank({ ...base, adoptable: true, adopt_rank: 2 })).toBe(2);
  });
  it('returns null when not adoptable, even with a rank present', () => {
    expect(adoptGroupRank({ ...base, adoptable: false, adopt_rank: 1 })).toBeNull();
    expect(adoptGroupRank({ ...base, adopt_rank: 1 })).toBeNull();
  });
  it('returns null when the field is missing (stale coordinator)', () => {
    expect(adoptGroupRank({ ...base, adoptable: true })).toBeNull();
  });
  it('rejects junk ranks: non-number, NaN, zero, negative, non-integer', () => {
    for (const junk of ['x' as never, NaN, 0, -1, 1.5]) {
      expect(adoptGroupRank({ ...base, adoptable: true, adopt_rank: junk })).toBeNull();
    }
  });
});
```

**Step 2: Run** — `npx vitest run tests/unit/infra_graph.test.ts` → FAIL (no export).

**Step 3: Implement.** In `InfraGroup`, after `adoptable?`:

```ts
  /**
   * Guided adoption order (item 10): server-assigned rank (1 = start here) and
   * plain-language hint, present ONLY on adoptable groups. Optional — a stale
   * coordinator response simply renders the unsorted list with no hints.
   */
  adopt_rank?: number;
  adopt_hint?: string;
```

New helper next to `adoptRows`:

```ts
/**
 * Effective adoption rank of a group, or null when unranked: not adoptable,
 * field missing (stale coordinator), or junk (this is a fail-quiet client
 * boundary — only a positive safe integer counts, so a malformed rank can
 * never sort ahead of the real rank 1 and steal "Start here"; Codex 019eb608).
 * Sorting with `rank ?? Infinity` keeps unranked groups after ranked ones, in
 * their original (stable) order.
 */
export function adoptGroupRank(g: InfraGroup): number | null {
  if (g.adoptable !== true) return null;
  return typeof g.adopt_rank === 'number' &&
    Number.isSafeInteger(g.adopt_rank) &&
    g.adopt_rank > 0
    ? g.adopt_rank
    : null;
}
```

**Step 4: Run** — pass. **Step 5: Commit** — `feat(ui-lib): adoptGroupRank + DTO fields`

---

### Task 5: InfraDiagram — rank-sorted adopt list, "Start here" chip, hint lines

**Files:**
- Modify: `frontend/src/components/InfraDiagram.svelte` (the `adoptGroups` derived
  ~line 119, the adopt-list markup ~line 492, and `<style>`)
- Test: `frontend/tests/unit/InfraDiagram.test.ts`

**Step 1: Write the failing tests** (testing-library, following the file's
existing adopt-list test setup — mock fetch returning a graph DTO):

```ts
// Graph fixture: FOUR groups in server (asset_type-sorted) order where rank
// order DIFFERS from server order, plus an unranked drift group:
//   pubsub Topic         (adopt_rank 2, adopt_hint 'topic hint', 1 unmanaged)
//   run Service          (adopt_rank 4, adopt_hint 'run hint',   1 unmanaged)
//   iam ServiceAccount   (not adoptable,                          1 unmanaged)
//   storage Bucket       (adopt_rank 1, adopt_hint 'bucket hint', 1 unmanaged)
it('orders adopt-list groups by adopt_rank, unranked last', ...);
  // expect adopt-row name order: bucket name, topic name, run name, SA name
it('shows the Start-here chip exactly once, on the top-ranked group', ...);
  // getAllByTestId('adopt-start-here').length === 1, inside the bucket group's hint line
it('renders one hint line per ranked group, none for unranked', ...);
  // getAllByTestId('adopt-hint').length === 3; texts contain the three hints
it('pins the honesty phrases in the order note', ...);
  // adopt-order-note text contains 'same zero-change import' and
  // 'building confidence, not safety' and 'among the unmanaged resources shown'
it('renders exactly today's list when rank fields are absent (stale coordinator)', ...);
  // same fixture minus adopt_rank/adopt_hint: queryAllByTestId('adopt-hint') is [],
  // no adopt-start-here, no adopt-order-note; row order = server order
```

**Step 2: Run** — `npx vitest run tests/unit/InfraDiagram.test.ts` → new tests FAIL.

**Step 3: Implement.**

Import `adoptGroupRank` alongside `adoptPrefill` from `../lib/infra_graph`.

In the `adoptGroups` derived: carry rank+hint and sort (JS sort is stable, so
unranked groups keep server order):

```ts
  type AdoptListGroup = {
    assetType: string;
    label: string;
    rows: AdoptListRow[];
    hiddenUnmanaged: number;
    rank: number | null; // item 10: guided adoption order (null = unranked)
    hint: string | null;
  };
  const adoptGroups = $derived.by((): AdoptListGroup[] => {
    // ... existing loop; in the out.push() add:
    //   rank,
    //   hint: rank !== null && typeof g.adopt_hint === 'string' && g.adopt_hint
    //     ? g.adopt_hint
    //     : null,
    // where `const rank = adoptGroupRank(g);` is computed beside `adoptable`.
    out.sort(
      (a, b) =>
        (a.rank ?? Number.POSITIVE_INFINITY) - (b.rank ?? Number.POSITIVE_INFINITY),
    );
    return out;
  });
  // First group is the "start here" target iff it is ranked (ranked sort
  // first, so index 0 unranked ⇒ nothing is ranked ⇒ no chip, no order note).
  const startHereAssetType = $derived(
    adoptGroups[0]?.rank != null ? adoptGroups[0].assetType : null,
  );
```

Markup — under the existing heading `<p>` (renders only when ranked data exists;
copy pins: "among the unmanaged resources shown" scoping + the honesty phrases):

```svelte
        {#if startHereAssetType !== null}
          <p class="ds-subtle infra-adopt__order" data-testid="adopt-order-note">
            Suggested order among the unmanaged resources shown: the simplest to
            recognize and review come first. Every adoption is the same zero-change
            import behind the same approval gate — the order is about building
            confidence, not safety.
          </p>
        {/if}
```

Inside the group `{#each}`, before the rows `{#each}` — a per-group hint line:

```svelte
            {#if g.hint !== null}
              <li class="ds-subtle infra-adopt__hint" data-testid="adopt-hint">
                {#if g.assetType === startHereAssetType}
                  <span class="infra-adopt__start" data-testid="adopt-start-here">Start here</span>
                {/if}
                {g.label}: {g.hint}
              </li>
            {/if}
```

Style additions (match existing `.infra-adopt__*` rules; mirror the green/ok
literal-hex precedent used by CLASS_DEFS and check `frontend/src/styles/base.css`
for existing ok/green tokens — use tokens if they exist, literals otherwise):

```css
  .infra-adopt__order {
    margin: 0 0 0.4rem;
  }
  .infra-adopt__hint {
    list-style: none;
    margin-top: 0.45rem;
  }
  .infra-adopt__start {
    display: inline-block;
    margin-right: 0.45rem;
    padding: 0.05rem 0.5rem;
    border: 1px solid #1f8a4c;
    border-radius: 999px;
    color: #176b3b;
    background: #ecf6ef;
    font-size: 0.72rem;
    font-weight: 600;
  }
```

**Step 4: Run** — `npx vitest run` (full frontend suite) → all pass, including
the existing Phase-4 adopt-list tests (their fixtures carry no rank fields, so
order is unchanged — they must pass untouched; if one asserts exact DOM order
with rank fields present, fix the FIXTURE only, never weaken an assertion).

**Step 5: Commit** — `feat(ui): rank-sorted adopt list with Start-here chip (item 10)`

---

### Task 6: full suites, docs touch-up, PR

**Step 1:** `pytest -q` from repo root (expect ≥2629 passing, +~15 new) and
`cd frontend && npx vitest run` (expect ≥453 passing, +~9 new), `npm run build`.

**Step 2:** Lint/typecheck exactly as CI runs them (`ruff check .`,
`npm run check` / `npx tsc --noEmit` — match the repo's CI workflow).

**Step 3:** Branch `feat/guided-adoption-order`, push, open PR titled
`feat: guided adoption order — what to adopt first, ranked and explained`
with the standard body (what/why/tests/rebake-surface).

**Step 4:** CI green → Codex completed-work review on thread `019eb608` →
fold any must-fix → squash-merge.

---

### Task 7: ship + live verify

**Step 1:** Coordinator rebake:
`gcloud builds submit --config=infra/cloudbuild.coordinator-update.yaml --substitutions=_TAG=<short-sha>`

**Step 2:** Pin traffic to the NEW revision (pick by image digest, NOT
`latestReadyRevisionName`):
`gcloud run services update-traffic driftscribe-agent --to-revisions=<new-rev>=100 --region=asia-northeast1`

**Step 3:** Live verify:
- `GET /infra/graph` (X-DriftScribe-Token) → bucket group carries
  `adopt_rank: 1` + hint; Cloud Run group `adopt_rank: 4`; service-account
  group has neither field.
- SPA: adopt list shows the order note, bucket rows first with the
  "Start here" chip, run-service rows later, SA rows last with
  "not yet adoptable". (The three demo nodes adopt-probe-topic /
  adopt-probe-sub / adopt-probe-svc are live unmanaged fixtures for exactly
  this check.)
- Chat (explore workload): "what should I adopt first?" → reply suggests the
  canonical order with the honesty note.

**Step 4:** Update memory (`clickops_audience_initiative.md` + MEMORY.md index).
