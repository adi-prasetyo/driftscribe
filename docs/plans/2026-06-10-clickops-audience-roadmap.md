# ClickOps-to-IaC Audience Roadmap (rough plan)

**Status:** rough plan / feature backlog — each wave gets its own detailed spec before implementation.
**Detailed spec exists for:** Wave 1, item 1 → [`2026-06-10-plain-language-plan-summary.md`](2026-06-10-plain-language-plan-summary.md).

## Who this roadmap is for

The target operator: knows infrastructure and networking cold, manages GCP by
hand in the console ("ClickOps"), wants to move to IaC but doesn't know how to
start, can't read HCL/diffs fluently, and is afraid an AI agent with apply
rights will run rampage. DriftScribe's safety architecture already answers the
fear *technically* — this roadmap makes it answer the fear *legibly*, and adds
the missing migration on-ramp.

Three audience anxieties, and where each feature lands:

| # | Anxiety | Features |
|---|---------|----------|
| A | "How do I migrate my hand-built infra?" | Adopt/import flow, coverage meter, guided adoption order |
| B | "Will the AI run rampage?" | Capability card, autonomy dial, pause button, blast-radius line |
| C | "I can't read the code it writes" | Plain-language plan summary, before/after diagram, ask-about-this-change, cost estimate |

---

## Wave 1 — comprehension + trust quick wins

### 1. Plain-language change summary on the IaC approval page (anxiety C) — **spec written**

The single worst moment for this audience today: `/iac-approvals/<N>` asks them
to approve based on raw `tofu show` text. Render the already-fetched,
integrity-checked `plan.json` as a deterministic plain-language summary:
per-resource CREATE/UPDATE/DESTROY/REPLACE badges, human resource-type labels
("Cloud Storage bucket", "Pub/Sub topic"), attribute-level before→after diffs,
and a prominent "No resources will be destroyed or replaced" / "⚠ N resources
will be DESTROYED" line. Deterministic (not LLM) so it is trustworthy;
sensitive values masked exactly as `tofu` itself masks them.

- **Approach:** new pure lib `driftscribe_lib/iac_plan_summary.py`; surfaced as
  a cached property on `agent/iac_artifacts.IacPlanView` (the plan JSON is
  already parsed there); new card in `agent/templates/iac_approval.html`;
  ds-* classes in `frontend/src/styles/base.css`.
- **Size:** S/M. **Dependencies:** none. **Ships via:** coordinator rebake.
- Recognizes OpenTofu `importing` changes from day one → groundwork for Wave 3.

### 2. Migration coverage meter (anxiety A)

`InfraDiagram.svelte` already shows a small `{managed}/{resources} managed`
count — this item is the *progress treatment*, not a new metric: a percentage,
a progress bar, and headline placement ("26% of your infrastructure is under
IaC management") so the migration reads as a journey with a number that goes
up.

- **Approach:** frontend-only change to `frontend/src/components/InfraDiagram.svelte`
  (or its parent panel); derive % from the existing graph payload totals.
- **Size:** S. **Dependencies:** none. **Ships via:** coordinator rebake (SPA bundle).

### 3. Capability card — "what this agent cannot do" (anxiety B)

A UI panel that renders the existing cage: per-workload tool allowlists (from
`workloads/*/workload.yaml`), the C1 denylist rules (secrets, foundations,
IAM), per-worker IAM scopes in plain language, and the human-gate map (what
always requires an Approve click). Today this lives only in
`docs/architecture/` — invisible to the person who most needs it.

- **Approach:** new `GET /capabilities` endpoint that serializes workload tool
  registries + denylist rule descriptions from the same constants the
  enforcement code uses (never a hand-maintained copy that can drift); new SPA
  panel. Phase 2 (optional, separate): live `testIamPermissions` probes to
  show *proof* rather than claims.
- **Size:** S/M (phase 1), M (live-proof phase 2). **Dependencies:** none.

### 4. Hygiene: collapse duplicate `iac_apply` rail rows (deferred from PR #81)

One row per PR in the decisions rail, expandable to the faithful 3-doc
lifecycle. Frontend-only; supersession logic already exists
(`resolvedIacPrNumbers`).

- **Size:** S. **Dependencies:** none.

---

## Wave 2 — trust controls + visibility

### 5. Pause button / kill switch (anxiety B)

One operator click suspends all agent activity: `/chat` mutations, `/recheck`,
Eventarc triggers, and approval POSTs return a calm "DriftScribe is paused"
state. Stored as a Firestore flag with who/when audit; resume requires the
operator token. Fail-closed: flag-read errors treat the system as paused for
mutation paths.

- **Approach:** `StateStore` pause document + a check at the top of each
  mutation entrypoint in `agent/main.py`; SPA toggle with confirm dialog;
  rollback/IaC approval pages show paused state.
- **Enforcement boundary (decide in the design doc):** coordinator-level
  checks cover the runtime mutation flow only because workers are private and
  accept calls solely from the coordinator — the design must state that
  assumption explicitly and decide whether high-value workers (`tofu-apply`,
  `rollback`) ALSO honor the pause flag (defense-in-depth against a
  compromised coordinator), and what happens to the GitHub-Actions C2
  plan-builder (out of band by design — document it).
- **Size:** S/M. **Dependencies:** none.

### 6. Before/after on the infra diagram (anxiety C)

The approval flow rendered in the language this audience actually thinks in:
the Mermaid resource map with ghost nodes — green-dashed "will be created",
amber "will be modified", red "will be destroyed" — derived from the same
`iac_plan_summary` entries as Wave 1 item 1.

- **Approach:** extend `/infra/graph` (or a new `/infra/graph/preview?pr=N`)
  to overlay plan-summary entries onto the live graph; render in
  `InfraDiagram.svelte`; link from the approval page ("see this change on the
  map") — the approval page itself stays static-HTML/strict-CSP, so the
  overlay lives in the SPA.
- **Size:** M. **Dependencies:** Wave 1 item 1 (reuses the summary lib).

### 7. Pending-approval notifications (anxiety B, operationally)

The `notifier` worker already exists for drift. Reuse it: when a rollback
approval or IaC approval becomes pending, notify (email/chat webhook) with the
approval link. Operators live in chat tools, not dashboards.

- **Size:** M. **Dependencies:** none.

### 8. Blast-radius line on every proposal (anxiety B)

On the approval page and in the `done` stream event: "This change can affect
at most: 1 Pub/Sub topic. It cannot touch: secrets, IAM, networks, databases
(denylist-enforced)." Derived from the plan summary (affected types) + the
static denylist (protected classes).

- **Size:** S. **Dependencies:** Wave 1 item 1.

---

## Wave 3 — the migration flagship

### 9. Adopt/import flow — "bring this resource under management" (anxiety A)

The product thesis for this audience. Every unmanaged node on the infra graph
gets an **Adopt** button → prefilled `provision` chat prompt → the agent
authors the resource block *plus an OpenTofu `import` block* → PR → the plan
provably reads "1 to import, 0 to add/change/destroy" → the approval page
(via the Wave 1 summary, which already recognizes `importing`) shows a green
"this does not change the live resource — it records it in OpenTofu state so
DriftScribe can manage it" banner. ClickOps→IaC one resource at a time, with
the plan as proof. (Precise wording matters: an import IS a real state
mutation behind the same gated apply pipeline — never claim "changes
nothing"; claim "does not change the remote resource".)

- **Approach (phased):**
  - 9a. Editor + prompt support: confirm `tofu-editor` policy admits `import`
    blocks (they are plain HCL under `iac/`); extend the `provision` system
    prompt with an adopt recipe grounded in the CAI inventory + the Phase-2
    declared-identity templates (`driftscribe_lib/iac_hcl`).
  - 9b. Plan-classification: `iac_plan_classify`/`tofu-apply` gating semantics
    for import-only plans (import is create-class for routing? decide + test);
    summary lib renders the zero-change banner.
  - 9c. UI: Adopt button on unmanaged nodes in `InfraDiagram.svelte` →
    prefilled chat; coverage meter (Wave 1 item 2) ticks up on success.
- **Size:** L. **Dependencies:** Wave 1 item 1 (banner), item 2 (meter).
- **Risk to de-risk early:** import-block behavior through the C2 plan-builder
  → C4 apply pipeline end-to-end (state mutation on import is real — the
  apply worker's resource_set_guard and denylist must admit it deliberately).

### 10. Guided adoption order (anxiety A)

After a scan, the agent suggests *which* resources to adopt first — leaf,
low-blast-radius resources (buckets, topics) before load-bearing ones
(networks, IAM). Heuristic ranking over the CAI graph; no new mutation
surface.

- **Size:** M. **Dependencies:** item 9.

### 11. Autonomy dial (anxiety B)

Explicit per-workload modes: **Observe** (report only — no mutation tools in
the registry), **Propose** (PRs/issues, no apply pipeline), **Propose+Apply**
(current, still human-gated). A scared adopter starts in Observe and turns the
dial up as trust builds. Enforcement at the tool-registry layer (Layer 0), not
prompt-level; default = current behavior; fail-closed to the *more*
restrictive mode on config errors.

- **Size:** M/L. **Dependencies:** none, but pairs with capability card (3).

---

## Wave 4 — exploratory / larger bets

### 12. "Ask about this change" on the approval page (anxiety C)

A read-only `explore`-workload chat scoped to the plan artifact ("what does
`uniform_bucket_level_access` mean?"). Strict-CSP approval page → link out to
the SPA with the PR context prefilled rather than embedding chat in the page.

- **Size:** M. **Dependencies:** none hard; better after Wave 1 item 1.

### 13. Cost estimate per change (anxiety C)

"~¥120/month estimated" next to creates/updates. No first-party GCP API gives
arbitrary-resource estimates; requires a pricing-heuristic table or an
Infracost-style integration. Exploratory — validate demand first.

- **Size:** M/L, high uncertainty.

### 14. Onboarding wizard (anxiety A)

First-run guided flow: connect project → scan (explore workload) → "here is
your estate, N resources, M unmanaged" → adopt your first resource in 5
minutes. Front door for new operators; today onboarding is shell scripts
(`docs/runbooks/deploy.md`).

- **Size:** L. **Dependencies:** items 2, 9.

### 15. Dry-run pill (deferred Phase 2 of decision-artifact-links)

Carried forward as previously scoped.

---

## Sequencing rationale

- Wave 1 is all S/M, zero new mutation surface, and every later wave's UX
  (zero-change banner, ghost nodes, blast radius) builds on item 1's summary
  lib — hence item 1 ships first.
- Wave 2 adds visible *controls* (pause, notifications) before Wave 3 asks the
  user to trust the agent with their real estate.
- Wave 3 item 9 is the flagship but touches the apply pipeline's gating
  semantics — it deserves its own design doc + Codex review before any code.
- Wave 4 items are validated (or dropped) against real operator feedback from
  Waves 1–3.
