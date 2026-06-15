# Workload rename + autonomy-signal + crew-card animations — design

- **Date:** 2026-06-16
- **Status:** Design (validated section-by-section with the operator; Codex-reviewed thread `019ecc0f`, must-fixes folded in; pre-implementation)
- **Topic:** Rename the operator-facing workload labels to a "crew" of named agents, make the *autonomous vs on-demand* distinction unmistakable **and truthful**, and add a small looping animation per agent.

## Motivation

The four workloads are surfaced to operators by **domain** ("Cloud Run config", "Dependencies", "Provision (infra edits)", "Explore (read-only)"). Today the labels answer *"what does this watch?"* but not *"does this act on its own?"*, and the autonomous-pair labels are weak as identities.

**Primary goal: make autonomy clarity unmistakable — and honest.** Branding is a welcome secondary benefit, not the driver.

## Critical correctness finding (Codex must-fix #1 — verified in code)

**Only `drift` is actually autonomous in this build.** `upgrade` is *not*, despite its `autonomous: true` flag:

- `/recheck workload='upgrade'` → **hard 503, intentionally unimplemented** (`agent/main.py:1233`): the post-agent plumbing is drift-specific; `/chat` is the only supported upgrade surface.
- Eventarc is **hardcoded to `drift`** (`agent/main.py:1829`): *"An event-triggered upgrade workload, if ever added, will get its own endpoint."* — it doesn't exist.
- The `autonomous` flag was derived from `observation_kind != "none"` (`capabilities.py:358`), which reflects *intent*, not a wired trigger.

So **Patch (`upgrade`) runs only when asked, like Provision and Explore.** The taxonomy below reflects that.

## Decisions (locked with the operator)

1. **Names** — "domain, but positive". Display-only; symbolic API names never change.

   | symbolic `name` (frozen) | new identity | descriptor (subtitle) | camp |
   |---|---|---|---|
   | `drift` | **Anchor** | Cloud Run config | **Autonomous** (live Eventarc trigger) |
   | `upgrade` | **Patch** | dependencies | On-demand (chat-only in this build) |
   | `provision` | **Provision** | infra edits | On-demand (chat-only by design) |
   | `explore` | **Explore** | read-only | On-demand (chat-only by design) |

   - "Anchor" = keeps the live service true to its contract (positive of "drift").
   - "Patch" = punchy, dev-native action word. (Codex flagged collision risk — HTTP PATCH / `kubectl patch`; mitigated by always showing the `dependencies` descriptor. Operator chose it knowingly.)
   - All four render `Name — descriptor` (bold name + gray descriptor).

2. **Autonomy split is 1 / 3, not 2 / 2:** Autonomous = {Anchor}; On-demand = {Patch, Provision, Explore}. Truthful; the badge means something real.

3. **Canonical vocabulary everywhere:** **"Autonomous"** vs **"On-demand"**. Backend errors/tests that explain `/recheck` refusal keep **"chat-only"** as the precise mechanism (Codex should-fix #4).

4. **Honesty rule:** "Autonomous · runs without being asked" is true for Anchor in every dial state (the **trigger** always fires). What Anchor *does* in response is governed by the Observe/Propose/Propose+Apply dial — that nuance stays in the autonomy explainer.

## §1 — Names + the picker

- **Display source of truth (Codex should-fix #1):** introduce a single checked-in catalog `frontend/src/lib/workloads.catalog.json` (`name`, `descriptor`, `group`) that `workloads.ts` imports (Vite handles JSON natively). A backend test reads the same JSON and asserts it matches the YAML `display_name`/`descriptor` + derived autonomy — removes the silent two-sources-of-truth drift without making the composer depend on a live `/capabilities` fetch (keeps the critical input static + offline-safe).
- **Backend autonomy field (Codex must-fix #2):** do **not** derive "autonomous" from `observation_kind`. Add an explicit `AUTONOMOUS_TRIGGER_WORKLOADS = frozenset({"drift"})` co-located with the Eventarc/`/recheck` wiring (commented with *why* — eventarc hardcoded drift, upgrade `/recheck` 503). `/capabilities` `autonomous` is derived from this set. The frontend `group` is asserted consistent with it.
- **Subtitle:** add a `descriptor` field to each `workload.yaml` + `/capabilities` + render it in `CapabilityCard` (Codex should-fix #3 — today the card renders only `display_name`; without a descriptor field the domain would vanish from the header). `display_name` stays just the identity (`Anchor`); `descriptor` carries the domain.
- **Picker (`frontend/src/components/ChatForm.svelte:70`):**
  - Two native `<optgroup>`s (`Autonomous · runs without being asked` / `On-demand · runs only when you ask`) — communicates **when the dropdown is open**.
  - **Plus an adjacent badge** beside the `<select>` that updates with the selected workload — **Autonomous** or **On-demand** (Codex must-fix #3: a *collapsed* native `<select>` shows only the selected option, not its optgroup, so grouping alone is not "unmistakable"). Terse `title`/visually-hidden clarification.

  ```
  [ Anchor — Cloud Run config  ▾ ]  ( Autonomous )
  Autonomous · runs without being asked
     Anchor    — Cloud Run config
  On-demand · runs only when you ask
     Patch     — dependencies
     Provision — infra edits
     Explore   — read-only
  ```

- **Frozen:** symbolic `name` values; `/chat` `workload` param; registry keys; `workloads/<name>/` dirs; `CHAT_ONLY_WORKLOAD_NAMES`; YAML `name:`. Autonomy values `observe`/`propose`/`propose_apply`.

## §2 — Tour + autonomy/help copy

Real, shipped 5-step tour (`frontend/src/lib/tour.ts`, `TourBanner.svelte`, `TourCard.svelte`, reopen button in `App.svelte`). Weave the cast in **without a 6th step**.

- **Tour Step 1 (Welcome)** — `tour.ts welcomeLine()`. Honest cast intro (Codex must-fix #4 — don't overstate universal approval):

  > "DriftScribe is a small crew watching {project}. **Anchor** runs on its own — it keeps your live Cloud Run config true to its contract, reacting the moment something changes. Three more wait for you to ask: **Patch** keeps your dependencies current, **Provision** authors infra changes, **Explore** answers questions read-only. Infrastructure applies and rollbacks always wait for your approval; routine dependency updates can run end-to-end only at the Propose + Apply setting."

- **Tour Step 3 (the dial)** — `tour.ts CONTROLS_LINE`. Prepend: *"This dial governs what **Anchor** does on its own when it spots a change, and what the other agents may do when you ask:"* before the existing Observe/Propose/Propose+Apply text. Steps 2/4/5 unchanged.

- **Autonomy explainer** — `autonomy.ts:28-33`. Name only the truly-autonomous one: *"When a watched service changes — including changes made outside DriftScribe — **Anchor** runs automatically; no one has to ask. This dial sets what it may do in response, and it applies to all of the agent's activity, not just the chat requests you make here."* Mode blurbs (`autonomy.ts:16-18`) unchanged.

- **CapabilityCard pill** — `CapabilityCard.svelte:227`, derived from the new `autonomous` field: Anchor → **"Autonomous · also chat"**; Patch/Provision/Explore → **"On-demand · chat only"**. (This correctly flips Patch's pill from the old, misleading "autonomous + chat".)

## §3 — README / OVERVIEW / ProtoPedia / repo copy

- **Convention:** lead with the identity, gloss the API value once — **"Anchor (the `drift` workload)"**, then "Anchor". Same for **Patch (`upgrade`)**.
- **README.md:**
  - Intro (11-19): "a crew of four: **Anchor** runs autonomously (a live trigger reacts to Cloud Run config changes); **Patch**, **Provision**, and **Explore** run on demand from chat." Honest footnote: *Patch is a dependency watcher by design; wiring its autonomous trigger is future work — today you invoke it in chat.*
  - Section headings (44-66): "Workload 1: Drift" → **"Anchor — Cloud Run config drift (`drift`)"**; "Workload 2: Dependency Upgrades" → **"Patch — dependency upgrades (`upgrade`)"**; "Workloads 3 & 4" names Explore + Provision under on-demand/chat-only.
  - Comparison table (120-143): "upgrade workload" → "Patch"; "DriftScribe (Workload 1)" → "Anchor".
- **README.ja.md:** mirrored; "Anchor"/"Patch" stay in Latin script with Japanese descriptors.
- **docs/OVERVIEW.md (Section 3, 64-93):** same crew framing; keep "chat-only" as the precise term where it explains `/recheck` refusal.
- **Taglines untouched:** "the agent proposes, you approve" (`App.svelte:435`, `README.md:3`, repo description).
- **ProtoPedia (`docs/submission/protopedia.{en,ja}.md`) — scope cut:** rename only the two it documents (Drift→Anchor, Upgrade→Patch). Expanding it to all four (+ diagram/video) is a separate submission task.

## §4 — Blast radius, sequencing, tests

- **Frozen:** symbolic `name` values; `observe`/`propose`/`propose_apply`.
- **Edit set:**
  - **Frontend:** `workloads.catalog.json` (new), `workloads.ts` (import catalog), `ChatForm.svelte` (optgroups + adjacent badge), `CapabilityCard.svelte` (pill vocab + `descriptor` render), `tour.ts` (steps 1 & 3), `autonomy.ts` (explainer body).
  - **Backend:** four `workload.yaml` `display_name` + new `descriptor`; `AUTONOMOUS_TRIGGER_WORKLOADS` set + `/capabilities` `autonomous`/`descriptor` fields (`capabilities.py`).
  - **Agent-spoken strings (Codex should-fix #2):** `workloads/explore/system_prompt.md:12-14` and `workloads/drift/chat_system_prompt.md:51` hardcode `"Cloud Run config"` / `"Dependencies"` / "provision workload" — the LLM *says these aloud*. The drift chat prompt is **byte-golden pinned** in `tests/unit/test_drift_workload_loads.py:220`; the literal must be updated in lockstep.
  - **Docs:** `README.md`, `README.ja.md`, `docs/OVERVIEW.md`, `docs/submission/protopedia.{en,ja}.md`.
  - `agent/capabilities.py` descriptions of tools/workers: unchanged (it serves `display_name`/`descriptor` from YAML).
- **Sequencing — two PRs (Codex scope split):**
  - **PR1 — rename + autonomy signal + copy + docs + tests** (coherent, all display strings + the one backend `autonomous`-derivation fix + catalog/sync test). Ships atomically → single coordinator rebake (serves SPA bundle + `/capabilities`) → `update-traffic --to-revisions=<new>=100`.
  - **PR2 — crew-card animations** (additive UI polish; separate layout/motion/screenshot risk; §5).
- **Tests:**
  1. Grep + update the four old label strings across the suite (Svelte component tests + workload-load tests).
  2. **Autonomy-truth guard:** assert `AUTONOMOUS_TRIGGER_WORKLOADS == {"drift"}` and pin `"upgrade" not in` it while `/recheck upgrade` 503s (ties the badge to wired reality).
  3. **Cross-surface sync:** backend test reads `workloads.catalog.json` and asserts (name, descriptor, group) match YAML + derived autonomy. Avoid regex-parsing TS in Python (hence the JSON catalog).
  4. Update the byte-golden drift chat prompt literal.

## §5 — Crew-card animations (PR2)

- **Concept: "One estate, four verbs."** A shared **service-node** glyph in every card; each agent does its one verb. All four equally lively; **autonomy stays on the badge/grouping, not the motion.**
- **Tech:** inline **SVG + CSS keyframes** (not Lottie/GIF). Themeable via `currentColor` + one accent; `prefers-reduced-motion` static-frame fallback. One component family (`CrewGlyph.svelte`, `verb` prop). ~`64×64` viewBox, icon-set stroke weight, ~3s loop.
- **The four loops:**
  - **Anchor** (drift→hold): node drifts off a baseline, snaps back, settles.
  - **Patch** (heal+tick): a flaw/crack on the node is patched — **shape change, not color-only** (Codex a11y) — with a `v2 → v3` tick that is *also* reflected in card text, never meaning-only-in-SVG.
  - **Provision** (assemble): dashed slot → blocks assemble into a solid node → a small PR/branch line draws.
  - **Explore** (scan): a loupe/scan band sweeps across the node L→R; detail dots light as it passes.
- **Accessibility/perf (Codex):** SVGs `aria-hidden="true"` (decorative); meaning lives in card text; **no red/green-only** semantics; reduced-motion fallback **tested**; verify mobile/card-header layout (four loops in a capability/safety panel can distract → keep subtle, consider play-in-view/on-hover); CSS transforms/opacity only, no JS rAF.

## Flags / accepted risks

- **"Anchor" ≈ "Anchore"** (container security, different spelling) — accepted; descriptor always visible.
- **"Patch"** can read as a verb/command standalone (Codex) — mitigated by the always-shown `dependencies` descriptor.
- **Tour localStorage** (`driftscribe_tour_done`): reworked copy won't re-show to past visitors. Don't bump the key (YAGNI) — names are in the dropdown + card regardless.

## Out of scope (explicit)

- Renaming any symbolic `name` / API contract value.
- **Wiring an autonomous trigger for `upgrade`/Patch** (a real `/recheck` upgrade pipeline + scheduler/`/eventarc-upgrade`) — explicitly deferred post-Phase-17; the operator chose to label honestly rather than build this now.
- `/ui/transparency-legacy` (`transparency_legacy.html`, `agent/main.py:2675`) — deprecated fallback; **acceptable to diverge** with old labels during the fallback window (not updated here).
- Expanding ProtoPedia to all four workloads (+ diagram/video) — separate submission task.
- Autonomy mode semantics, the apply gate, worker behavior; a custom non-native dropdown.
