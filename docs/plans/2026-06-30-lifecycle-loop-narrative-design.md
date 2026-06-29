# Lifecycle-loop narrative — design

> 2026-06-30. Give the four crews a single unifying frame — a stewardship
> **loop** (create → guard → maintain → explain) — so they read as one coherent
> system instead of four separate tools. Motivation: a cleaner demo narrative
> for hackathon judges. We are NOT merging crews (the autonomy boundary is the
> crew boundary); we are reframing the *story*.

## Precursor (already done in this branch)

Stripped the internal workload-ID parentheticals (`drift`/`upgrade`/`provision`/
`explore`) from judge-facing prose + the README intro tables, keeping them only
in `###` headings and folder-mapping rows (where the ID names a real folder) and
in `Reader (drift)` (a worker label). Files: README.md, README.ja.md,
docs/OVERVIEW.md §3, protopedia.en.md, protopedia.ja.md.

## The canonical loop

One job, four crews, around one cloud estate, kept honest from creation onward:

- **Provision** — *create.* You describe a change in chat; it authors the
  OpenTofu and opens ONE gated `iac/` PR. Never touches live infra.
- **Anchor** — *guard.* Once live, it runs on its own (the only crew that does),
  watching Cloud Run config and reacting the moment it drifts from its contract.
- **Patch** — *maintain.* Proposes dependency upgrades.
- **Explore** — *explain.* Read-only answers across the whole system, including
  how DriftScribe itself works.

**The point is the handoff: you provision once, then Anchor guards it for you.**

### Honesty guardrails (this repo is strict about overclaim)

- Only Anchor is autonomous. "Runs on its own / acts without being asked" =
  the **trigger** axis (Eventarc), NOT the mutation axis.
- Anchor *detects* drift autonomously; its remediations (rollback) stay behind
  HITL approval. Loop copy uses "guards / catches / reacts to" drift, never
  "auto-fixes." The existing approval caveat stays wherever it already is.
- Patch is NOT autonomous (chat-only today). Provision/Patch/Explore wait to be
  asked. Avoid the word "safe."

## Per-surface placement

| # | Surface | File | Change |
|---|---------|------|--------|
| 1 | Tour welcome | `frontend/src/lib/tour.ts` `welcomeLine()` | Reframe as the loop; keep `${subject}` interpolation + the existing approval/autonomy caveats verbatim at the end. |
| 2 | Tour test | `frontend/tests/unit/tour.test.ts` | Update the welcomeLine "honesty" assertions to match new phrasing (keep: 4 names, autonomy distinction, "how DriftScribe itself works", "wait for your approval", not "safe"). |
| 3 | CapabilityCard | `frontend/src/components/CapabilityCard.svelte` | New per-crew one-liner in the workload **body** (after `cap-workload__desc`): `In the loop · <role>`. NOT the summary (glued-exact-string test). |
| 4 | Crew copy const | `frontend/src/lib/workloads.ts` | New `CREW_LIFECYCLE: Record<Workload,string>` (pure, testable). |
| 5 | CapabilityCard test | `frontend/tests/unit/CapabilityCard.test.ts` | Assert provision body shows its loop line + testid. |
| 6 | README | `README.md` | Compact loop paragraph after the intro table. |
| 7 | README JA | `README.ja.md` | JA parity (flag for native review). |
| 8 | OVERVIEW | `docs/OVERVIEW.md` §3 | Loop framing sentence after "Four workloads…", before the numbered list. |
| 9 | Demo script | `docs/demo-script.md` | Opening "positioning" line framing the demo as the loop. |
| 10 | ProtoPedia EN | `docs/submission/protopedia.en.md` | Loop frame at the top of `## Demo`. |
| 11 | ProtoPedia JA | `docs/submission/protopedia.ja.md` | JA parity. |

## Draft copy

### Tour `welcomeLine()` (keep subject + caveats)

> DriftScribe is a small crew keeping ${subject} honest, from creation onward,
> and it works as a loop. Provision stands infrastructure up: you describe a
> change, it opens the IaC pull request. Anchor then guards what is live. It
> runs on its own, the only crew that does, watching your Cloud Run config and
> responding the moment it strays from its contract. Patch keeps your
> dependencies current, and Explore answers questions read-only, including how
> DriftScribe itself works. Provision, Patch, and Explore wait for you to ask.
> Infrastructure applies and rollbacks always wait for your approval. Only
> routine dependency updates can run end-to-end, and only at the Propose +
> Apply setting.

### `CREW_LIFECYCLE`

- provision: "Stands infrastructure up. You describe a change; it opens the IaC PR."
- drift: "Guards what is live. Runs on its own, responding when config drifts."
- upgrade: "Keeps it current. Proposes dependency upgrades."
- explore: "Explains it. Read-only answers across the whole system."

Rendered as: `In the loop · <value>`.

### README (after the intro table)

> **How the crew fits together:** it runs as a loop. Provision stands new
> infrastructure up (you ask, it opens the IaC PR). Anchor then guards what's
> live, catching drift the moment it appears, on its own. Patch keeps
> dependencies current, and Explore answers anything read-only. The handoff is
> the point: you provision once, and Anchor keeps it honest.

### OVERVIEW §3 (after "Four workloads exist today, organised as a crew:")

> The four read as a loop around one estate: **Provision** stands infrastructure
> up, **Anchor** guards what's live (the only one that acts on its own),
> **Patch** keeps it current, and **Explore** explains it. You provision once;
> Anchor keeps it true after the handoff.

### ProtoPedia `## Demo` (opening frame)

> DriftScribe's crews form a loop around one cloud estate: Provision stands
> infrastructure up, Anchor guards what's live on its own, Patch keeps it
> current, and Explore explains it. The demo follows that arc.

## Verification

- `cd frontend && npm run test` (vitest) — tour + CapabilityCard suites green.
- `npm run check` (svelte-check) — 0 errors.
- Markdown: re-grep confirms no overclaim ("auto-fix", stray "safe" in tour).
- No backend change → no pytest impact expected; run capabilities cross-surface
  test to be sure the catalog wasn't disturbed.
- Codex review of copy honesty before + after implementation (per workflow).

## Codex review — incorporated (thread 019f13eb)

Accepted: (2) "acts on its own" → "runs on its own" / judge-facing "the only
autonomous trigger"; (3) closing taglines "keeps it honest/true" → "keeps
watch for drift"; (4) "responding the moment it strays" → "reacting the moment
it drifts" + CapabilityCard "reacting when it detects drift"; (5) "stewardship
loop" in OVERVIEW + ProtoPedia.

Rejected: (1) "drop the routine-dependency-end-to-end line" — that line is a
**deliberate honesty disclosure** already in the tour (tour.ts Codex-MF1: a
blanket "nothing applies until you approve" overclaims, since Propose+Apply may
merge a dependency PR). Removing it lowers honesty. Kept verbatim.

Final copy = the drafts above with those substitutions.

## Out of scope / deferred

- Explore crew's baked-in "About DriftScribe" prompt (backend, anchor-tested) —
  could later mirror the loop, but the user did not ask for it. Note only.
- Crew-picker card reordering to create→guard→maintain→explain — optional.
