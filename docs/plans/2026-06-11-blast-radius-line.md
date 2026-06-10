# Blast-Radius Line (ClickOps Wave 2, item 8)

> **For Claude:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.
> Two small tasks, two-stage review each, final review before PR.

**Goal:** Every IaC proposal answers the operator's real question — "what's
the worst this can do?" — in one line: what the plan touches (counted, by
type) and what it can NEVER touch (denylist classes), on the approval page;
plus a short static cage-teaser on the first-authoring CTA.

**Audience anxiety served (roadmap item 8, anxiety B):** the denylist is
real but invisible at the decision moment. The capability card (item 1 wave
"what this agent cannot do") lists the whole cage; this puts the relevant
one-liner exactly where the operator's finger hovers over Approve.

**Honesty correction vs the roadmap sketch (same discipline as items 6/7):**
the roadmap's example line says "It cannot touch: secrets, IAM, networks,
databases" — the actual denylist does NOT protect "networks" or "databases"
as classes, and protects *DriftScribe's own* secrets (control-plane set),
not all secrets. The shipped line claims ONLY what
`driftscribe_lib/iac_plan_denylist.py` enforces, and a drift-pin test
forces a copy re-review whenever the rule set changes. Also: the roadmap
wants the line "in the `done` stream event," but at done-time (PR just
opened) NO plan exists yet (C2 is maintainer-dispatched) — so the CTA gets
only the static cannot-touch teaser, never a fabricated "at most" count.

---

## Grounding facts (verified 2026-06-11 against `main` @ 9b3d31b)

1. **The denylist's ACTUAL protected classes**
   (`driftscribe_lib/iac_plan_denylist.py`, `RULE_DESCRIPTIONS` — 14 rule
   IDs, exported + already drift-pinned by the capability-card AST test):
   control-plane Cloud Run services, control-plane SAs, state/artifact
   buckets (+objects), DriftScribe's own secrets (+versions), the KMS
   key/ring, WIF pools/providers; ALL IAM changes (v1 floor); ALL
   deletes / forgets / replaces (v1 floor); unknown action shapes
   (fail-closed). The denylist re-runs in the tofu-apply worker
   immediately before `tofu apply` (C4) — "re-checked before apply" is a
   true claim.
2. **`PlanSummary`** (`driftscribe_lib/iac_plan_summary.py`) has per-VERB
   counts computed over ALL entries pre-truncation, but NO per-type
   counts; `entries` is capped at `MAX_ENTRIES = 40`, so a template-side
   aggregation over `entries` would under-count large plans. A new
   per-type aggregation must be computed inside `summarize_plan` next to
   the verb `Counter` (pre-truncation), additive field.
3. **Approval page** (`agent/templates/iac_approval.html`): the
   "What this change does" card is double-gated (route `show_summary` +
   template Gate 2); inside it: verb-count pills → destructive/safe note →
   `preview-map-link` (item 6) → entries list. `pr_number` int-typed.
   Template tests live in `tests/integration/test_iac_approval_get.py`.
4. **First-authoring CTA** (`frontend/src/components/IacApprovalCta.svelte`):
   renders from the `done` frame's `iac_pr.pr_number` only; amber card,
   title + "Review & approve →" button. Tests in
   `frontend/tests/unit/approval.test.ts` cover `iacApprovalHref`; the
   component itself is exercised in the App-level/CTA tests (find the
   exact file before editing — `grep -rn "iac-approval-cta" frontend/tests`).
5. **House rule (items 1/3):** trust-surface copy must be derived from /
   pinned against enforcement constants, never hand-maintained prose that
   can silently drift.

---

## Settled decisions

### Decision 1 — the cannot-touch sentence is a lib constant, drift-pinned

New module constant in `driftscribe_lib/iac_plan_summary.py` (it is
presentation text for the plan summary surface; the denylist module stays
pure policy):

```python
# One-line operator-facing summary of the denylist cage, rendered next to
# the per-plan blast radius on the approval page. HONESTY CONTRACT: this
# sentence may claim ONLY what driftscribe_lib/iac_plan_denylist.py
# enforces. test_blast_cannot_touch_note_matches_rule_set pins the exact
# RULE_DESCRIPTIONS key set — any denylist rule change fails that test and
# forces a re-review of this copy.
BLAST_CANNOT_TOUCH_NOTE = (
    "It cannot touch DriftScribe's own control plane (its services, "
    "service accounts, state/artifact buckets, secrets, or encryption "
    "keys), cannot change IAM anywhere, and cannot delete, replace, or "
    "un-manage any resource — denylist-enforced, re-checked by the apply "
    "worker before apply."
)
```

("re-checked by the apply worker" — Codex should-fix: the C4 worker
re-runs the denylist before apply, but other verifications sit between
the re-check and the apply; "immediately before" overstated adjacency.)

Drift pin (in `tests/unit/test_iac_plan_summary.py`): assert
`set(RULE_DESCRIPTIONS) == {…the 14 current rule IDs, written out…}` with
a comment that the assertion exists to force a `BLAST_CANNOT_TOUCH_NOTE`
re-review on any change (same mechanism as the capability-card pin, scoped
to this consumer). Plus a sanity pin that the note mentions neither
"networks" nor "databases" (the roadmap's overstated classes must never
creep in).

### Decision 2 — per-type counts + phrase builder (additive lib change)

- `PlanSummary.type_counts: tuple[tuple[str, int], ...] = ()` — pairs of
  `(type_label, count)` aggregated over ALL entries pre-truncation
  (compute next to the verb `Counter` in `summarize_plan`), sorted by
  `(-count, type_label)` for deterministic rendering.
- New pure helper in the same module:

```python
def _pluralize(label: str) -> str:
    """Pluralize a type label's final word. +'es' after s/x/z/ch/sh
    (covers the humanized fallback for e.g. google_compute_address →
    'compute address' → 'compute addresses' — Codex should-fix: a bare
    +'s' would emit 'addresss'), else +'s'. Every _TYPE_LABELS value and
    the fallback end in a regular noun; no irregular plurals needed."""
    if label.endswith(("s", "x", "z", "ch", "sh")):
        return label + "es"
    return label + "s"


def blast_radius_phrase(summary: PlanSummary) -> str:
    """'1 Pub/Sub topic, 2 Cloud Storage buckets' — the can-affect-at-most
    half of the blast-radius line. '' for an empty plan (the empty card
    already says 'no changes'; the line is suppressed there)."""
    if not summary.type_counts:
        return ""
    return ", ".join(
        f"{n} {label}" if n == 1 else f"{n} {_pluralize(label)}"
        for label, n in summary.type_counts
    )
```

### Decision 3 — approval-page line (the full two-part claim)

In `iac_approval.html`, inside the non-empty `change-summary` card,
directly AFTER the destructive/safe-note block and BEFORE the
`preview-map-link` paragraph (the blast radius is decision-critical; the
map link is auxiliary):

```html
<p class="ds-note" data-testid="blast-radius">
  This change can affect at most: {{ blast_phrase }}.
  {{ cannot_touch_note }}
</p>
```

Route side (`iac_approval_get` ctx): `blast_phrase =
blast_radius_phrase(s)` and `cannot_touch_note = BLAST_CANNOT_TOUCH_NOTE`,
computed only when the summary is non-None and has entries (mirror the
template's own arm structure — compute in the route, gate in the template
on `blast_phrase`, so an empty phrase renders nothing). "At most" is the
honest qualifier: the plan IS the exact change set; an apply can only do
less (partial failure), never more.

POST re-renders that reuse the template must not crash when the new ctx
keys are absent — use `| default("")` guards in the template (match how
`show_summary` is guarded).

### Decision 4 — CTA static teaser (the done-event half, honestly scoped)

`IacApprovalCta.svelte` gains one static sentence under the title (same
amber card, `data-testid="iac-cta-cage-note"`, muted styling consistent
with the card):

```
Before anything applies, this change must pass the self-protection
denylist — no DriftScribe control-plane changes, no IAM changes, no
deletes, replacements, or un-managing — and your explicit approval.
```

(Codex should-fix: "no control-plane changes" alone was ambiguous about
WHOSE control plane, and "no deletes" under-described the v1 floor.)

Static frontend constant; drift risk accepted and documented in a comment
(the authoritative, pinned copy lives server-side; this is a teaser whose
three claims are the denylist's stable v1 floor). NO per-plan counts here
— no plan exists at done-time (grounding: C2 is `workflow_dispatch`).

### Decision 5 — out of scope

- No notification-body changes (item 7 just shipped; its copy already
  points at the approval page).
- No capability-card changes (it already lists all 14 rules).
- No rollback-approval blast radius (different surface, already shows
  env-diffs; revisit with Wave 3).
- No "affected types vs denylist classes" cross-highlighting (YAGNI).

---

## Task 1 — lib + approval page (implementer: Sonnet 4.6)

**Files:** `driftscribe_lib/iac_plan_summary.py`, `agent/main.py` (ctx
keys), `agent/templates/iac_approval.html`,
`tests/unit/test_iac_plan_summary.py`,
`tests/integration/test_iac_approval_get.py`.

Both new public names (`BLAST_CANNOT_TOUCH_NOTE`, `blast_radius_phrase`)
go into the module's `__all__` (Codex nit). The POST-re-render guard test
may live in whichever existing iac-approval test file already exercises
POST renders (Codex nit — don't force it into test_iac_approval_get.py).

TDD steps:
1. Failing unit tests: `type_counts` aggregation (multi-type plan →
   sorted (-count, label) pairs; counts survive MAX_ENTRIES truncation —
   42 creates of one type → `(label, 42)`; empty plan → `()`);
   `blast_radius_phrase` (singular/plural/join/empty; the
   `_pluralize` sibilant case — a label ending in "address" →
   "addresses", pinned); the
   `RULE_DESCRIPTIONS` key-set drift pin (exact 14 IDs, with the
   forcing-function comment); the note never says "networks"/"databases";
   `BLAST_CANNOT_TOUCH_NOTE` non-empty + mentions "denylist".
2. Failing route tests: non-empty summary page renders
   `data-testid="blast-radius"` containing both the phrase ("1 Pub/Sub
   topic") and the cannot-touch note verbatim; empty-plan page does NOT
   render it; unverifiable/error pages do NOT (card itself absent); POST
   re-render path does not 500 (default guards).
3. Implement; full `uv run pytest -q` (baseline 2387) + ruff.
4. Commit `feat(blast-radius): per-type plan counts + approval-page blast-radius line`.

## Task 2 — CTA teaser (implementer: Sonnet 4.6)

**Files:** `frontend/src/components/IacApprovalCta.svelte`, the CTA's
existing test file (locate first).

TDD steps: failing test (CTA with valid prNumber renders
`iac-cta-cage-note` with the exact sentence; CTA hidden → note absent) →
implement (one `<p>`, muted ink token, comment documenting the
static-teaser tradeoff) → `npm run test:unit` (baseline 420) +
`npm run check` + `npm run build` → commit
`feat(blast-radius): denylist cage teaser on the first-authoring CTA`.

## Plan-review record (Codex thread 019eb2aa-b2f1-7693-b887-01b47f04e48c)

**GO** first round, 0 must-fix; 3 should-fix + 2 nits all folded:
apply-worker re-check wording (adjacency overstated); `_pluralize`
sibilant helper (bare +'s' would emit "addresss" via the humanized
fallback); CTA teaser names DriftScribe's control plane + the full v1
floor; `__all__` exports; POST-guard test placement freed. Codex verified
both honesty corrections (denylist classes; no plan exists at done-time)
and that the drift pin doesn't conflict with the capability-card pins.

## Post-review deltas (as shipped)

1. `_pluralize` gained a consonant+y → "ies" rule beyond the spec (the
   implementer caught "Artifact Registry repository" → "repositorys" —
   missed by both the spec and the Codex review); pinned by tests.
2. The POST-re-render guard test's comment corrected: POST renders never
   set `show_summary`, so the blast block is skipped wholesale — the
   `| default("")` guards protect future refactors/StrictUndefined, not
   that path (the original comment overclaimed what the test falsifies).
3. Final-review confirmations worth recording: WIF is covered via the
   IAM claim (under-claiming, honest); "at most" is defensible because
   the apply worker applies the SAVED plan.tfplan (never a re-plan) and
   the freshness gate can only block, never expand, the change set.

## Final gates

`uv run pytest -q` (+~12) · ruff · `npm run test:unit` (+~2) ·
`npm run check` 0/0 · `npm run build` · then the usual: final review → PR
→ CI → Codex completed-work → merge → coordinator rebake + traffic →
live-verify (approval page of a historical PR renders the card only on
non-terminal pages — so verify via route tests + bundle markers
`iac-cta-cage-note` / `blast-radius`; a real render arrives with the next
live plan).
