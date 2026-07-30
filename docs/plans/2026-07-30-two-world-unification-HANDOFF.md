# ds-qbo two-world unification — handoff + Codex review (2026-07-30)

**Status: PR #274 open, CI fully green, Codex reviewed. ONE merge-blocking fix outstanding.**

## Where the work is

- Worktree: `/home/adi/driftscribe/.worktrees/two-world`
- Branch: `ui/two-world-unification`, based on `origin/main` @ `749843b` (= #272's `e3ed992` + #273)
- PR: <https://github.com/adi-prasetyo/driftscribe/pull/274>
- Plan: `docs/plans/2026-07-30-two-world-unification.md` (committed on the branch)
- Codex thread to continue: **`019fb16f-7add-7ae0-9429-f02a1550d6bf`**
  (the plan's original thread `019fb101-…` was already expired/unresumable)

## Commits (all 9 pushed)

```
5653a55 refactor(tokens): retire the paper-world duplicates — one vocabulary (ds-qbo)
c6c75d3 fix(spa): paper-world audit — Mermaid palette, navy fills, blue classified
6831885 feat(type): mincho display face for page titles (ds-qbo)
c848552 feat(styles): cards rest on rules, not shadows (ds-qbo)
787a3e4 feat(header): paper ground, navy-filled active nav (ds-qbo)
28bfd3a fix(tokens): muted must clear AA on the neutral chip too, not just the page
48c5b6a fix(styles): sweep hard-coded legacy hexes behind the re-valued tokens
dee61ac feat(tokens): one world — legacy tokens take the paper values (ds-qbo)
b41fb1a docs(plan): two-world unification (ds-qbo)
```

## Gates (all green, run locally)

| Gate | Result |
|---|---|
| `npx svelte-check` | 0 errors, 0 warnings, 675 files |
| `npx vitest run` | 1659 passed (2 new) |
| `npm run test:smoke` | 28 passed (required `ui-smoke`) |
| `pytest tests/unit tests/integration` | 3618 passed |
| GitHub CI on #274 | frontend / lint-test / static-gate / tofu / ui-smoke / worker / GitGuardian **all pass**; plan-builder skipped (not an infra PR) |

## Deviations from the plan (both recorded in commit bodies + PR body)

1. `--ds-muted` shipped as **`#656c7a`**, not the mapping table's `#6a7180`. Codex independently
   confirmed the table value fails on SIX non-white grounds (surface-2, neutral chip, and all four
   semantic tints). `#656c7a` clears 4.5:1 on all eight grounds muted lands on.
2. `--ds-ring` NOT fixed — inherited WCAG 1.4.11 failure (1.40:1 now, 1.46:1 before). Codex agrees
   with the deferral. Bead it.

## Regression found and fixed during implementation

The token re-value silently broke four link hover/active states that brighten text from
`--ds-stream-ink` to raw `--ds-stream` (4.51:1 → 3.42:1). Fixed to underline without recoloring in
`DecisionsRail` (×2), `ConversationThread`, `TokenStatus`. Codex confirmed complete among `color:`
rules.

---

# WORK AFTER THE REVIEW

## 1. ~~MERGE-BLOCKING~~ — FIXED in `e613caf`: raw stream blue on small approval-desk text

`frontend/src/components/ApprovalDesk.svelte`:

- **`:534` `.approval-desk__who`** — meaningful pending/unresolved status, 11.5px
- **`:589` `.approval-desk__why-btn`** — interactive "view reasoning" control, 12px

Both now read `var(--ds-stream)` = `#4285f4` = **3.416:1 on paper**, under the 4.5:1 floor. They were
`--ds-gblue` during the Phase 2 classification pass, so the grep for `var(--ds-stream)` did not see
them; Phase 3's rename turned them into canonical stream consumers *after* the audit.

**FIXED** in `e613caf` — both → `var(--ds-stream-ink)` (`#2a63c9`, 5.402:1), as a new commit on top
of `5653a55` so the retirement commit stays a pure zero-delta mechanical rename. Codex's line numbers
were 534/589; the actual declarations were at 538/597. Also added a `styles.test.ts` guard that fails
on ANY raw-stream text `color:` (allowlisting only InstrumentBand's 44px numeral), and verified the
guard bites by reverting one fix and watching it name the offender. Recount: 17 raw-stream refs,
exactly one a text color, and it is the permitted numeral.

Verify the 44px `.approval-desk__await` numeral (or equivalent) is left alone — large-text
non-text-accent use of raw stream is permitted.

## 2. ~~PR body corrections~~ — DONE (pushed via `gh api PATCH`; `gh pr edit` aborts on this repo
with a projects-classic GraphQL error)

- "14 raw-stream consumers remain, zero are text" — **false after Phase 3**; source has 17
  occurrences including the two above. Recount and restate after fixing #1.
- "same bounding boxes" in the control-experiment claim is **false**: e.g. `en-stamped-rollback.png`
  was `(69,498,238,751)` pre→post but `(69,175,592,751)` post→control, and the control introduced
  three extra tiny JA desk diffs. Restate as "the control run differs in MORE files (38 vs 35),
  establishing the rig is nondeterministic"; the decisive evidence is value-identity + the verified
  mechanical mapping, which Codex accepts as sufficient.
- The muted explanation should say **six** non-white grounds fail at `#6a7180`, not "three".

## 3. ~~Stale comments~~ — FIXED in `e613caf` (Codex finding 4)

- `frontend/src/components/InfraDiagram.svelte:1316` — says Adopt's hover "mirrors the Send button's
  deepen-on-hover". Send now **lifts** (`color-mix` 88% navy + white). Correct the comment.
- `frontend/src/styles/tokens.css:74` — says `--ds-seal` is "declared once below"; it was promoted
  **above** in Phase 3. Correct to "above".

## 4. Beads — FILED 2026-07-30 (index was empty; bd swept nothing)

- **`ds-dce` (P1) — `--ds-ring` focus-contrast audit.** Translucent ring measures 1.36–1.54:1 across grounds and
  fails 1.4.11's 3:1; `base.css:89` suppresses the native outline and relies on it. Opaque `#4285f4`
  clears every ground Codex checked (lowest: neutral chip 3.069:1; navy 4.395:1). Tractable,
  high priority, deliberately not absorbed by this PR.
- **`ds-b52` (P3) — desk `unresolved` state boxes DriftDiffCard.** `ApprovalDesk.svelte:470` lacks the
  `.approval-desk__diff` wrapper that lines 411 and 483 have, so it renders as a boxed white panel
  while its siblings render as borderless rules. Pre-existing; fixing it is a DOM change this plan
  forbade and the acceptance matrix pinned the desk as unchanged.
- **`ds-16e` (P2) — `InstrumentBand.svelte:284` opacity fade** (Codex finding 2). `opacity: .75` on interactive
  numerals takes the awaiting numeral to ≈2.46:1 (44px/600 needs 3:1); resting is 3.416:1. Needs
  ≈`0.91`, or just drop the numeral fade since the hover hint already signals interactivity.
  **Pre-existing** — old `--ds-gblue` was the same value — so not a regression from this PR.
- **`ds-b42` (P3) — `--ds-shadow-md` is undefined.** `AutonomyPill:506`, `PausePill:248`, `DemoNoticeBell:309` use
  `var(--ds-shadow-md, var(--ds-shadow-sm))`, so popovers get the LIGHTEST shadow. Polish debt, not
  a correctness bug (border + overlap + z-position still separate them). `--ds-shadow` would express
  the intended middle elevation.
- **`ds-836` (P3) — retired-token guard coverage** (Codex finding 3). `styles.test.ts:183` walks only `.svelte`/`.css`
  under `src/`. It does not inspect `.ts` (`style.setProperty`), `agent/templates/*.html`, or future
  `.scss`. Codex found no current straggler in any of those, so this is coverage, not a live bug.

## 5. Then

- Merge #274, deploy the coordinator per the **`driftscribe-deploy`** skill: the build lands at **0%
  traffic** and `update-traffic` is a REQUIRED second step. Verify `cpu-throttling=false` survives
  (eventarc fast-ack invariant, #268).
- Live-verify per **`driftscribe-live-probe`**.
- `git status` must show an **EMPTY INDEX** before `bd close ds-qbo` (mutating `bd` commits whatever
  is staged, and the main worktree is shared).
- **Announce the video re-shoot is unblocked** — that is the downstream consumer of this work.

## Verification artifacts (scratchpad, session-scoped — regenerate if gone)

`/tmp/claude-1000/-home-adi-driftscribe/975ecc23-8661-4da3-9cff-e8db10a77ee4/scratchpad/`

- `render_approval_pages.py` — renders both Jinja approval pages in 8 states × 2 locales through the
  real Jinja env, rewriting the `/static/` CSS href to an absolute `file://` path so the BUILT
  stylesheet applies. `shoot.mjs` screenshots them. Requires `npm run build` first.
- `two-world-before/` (pre-plan baseline), `two-world-p1/`, `two-world-p2/`, `pre-p3/`, `post-p3/`,
  `post-p3b/` (control) — 93 screenshots each from the mocked Playwright visual rig, both locales.
- `pxdiff.py` — pixel diff with per-file differing-pixel counts and bounding boxes.
- `contrast.mjs`, `c2–c8.mjs` — WCAG contrast arithmetic for every value in the mapping.

**Note on the visual rig:** its desk mocks stamp real clock time, so back-to-back runs of identical
code differ in ~38 of 93 screenshots. Dimension parity proves no reflow; pixel identity is only
meaningful for non-desk shots.

---

# Codex review, verbatim (thread `019fb16f-7add-7ae0-9429-f02a1550d6bf`)

> I found one merge-worthy implementation issue, one pre-existing interaction issue, and several
> documentation/test-guard corrections. The rest of the implementation matches the plan.
>
> ## Findings
>
> **1. Raw stream blue returned as small approval-desk text**
>
> `ApprovalDesk.svelte:534` and `ApprovalDesk.svelte:589` now use `--ds-stream` for:
> - `.approval-desk__who`: meaningful pending/unresolved status, 11.5px.
> - `.approval-desk__why-btn`: an interactive "view reasoning" control, 12px.
>
> On paper: `#4285f4 / #fbfaf8 = 3.416:1`. Both require 4.5:1. This directly contradicts the token
> contract at `tokens.css:18` and Phase 2's "raw stream is non-text only" classification. The Phase 2
> audit missed them because they still used `--ds-gblue` then; Phase 3 made them canonical stream
> consumers.
>
> `--ds-stream-ink` gives `#2a63c9 / #fbfaf8 = 5.402:1`.
>
> I would fix both before merge in a post-retirement commit. That preserves commit `5653a55` itself
> as a zero-delta mechanical rename.
>
> The PR body's claim that 14 raw-stream consumers remain and zero are text is now false: current
> source has 17 occurrences, including these two small-text declarations and the permitted 44px
> numeral.
>
> **2. The interaction audit missed opacity-based lightening**
>
> `InstrumentBand.svelte:284` fades every interactive numeral to `opacity: 0.75`. For the awaiting
> numeral: `#4285f4` at 75% over `#fbfaf8` ≈ `#70a2f5`, contrast ≈ 2.46:1. The numeral is 44px/600,
> so it qualifies as large text but still needs 3:1. Resting contrast is 3.416:1. Approximately
> `opacity: 0.91` is needed to clear 3:1; removing the numeral fade is cleaner because the hover hint
> already supplies the interaction.
>
> This predates this PR — the old `--ds-gblue` was the same value — so I would not call it a
> token-shift regression or block this PR on it. But A's requested interaction audit did miss it.
>
> **3. The retired-token guard does not cover the whole project contract**
>
> The prefix test at `styles.test.ts:183` correctly catches every normal `--ds-paper*` occurrence in
> `.svelte` and `.css`. A valid CSS custom-property name cannot normally be split across lines.
>
> It does not inspect: `.ts`, which can contain generated styles or `style.setProperty(...)`;
> `agent/templates/*.html`, despite those templates sharing the bundle; future style-bearing
> extensions such as `.scss`.
>
> It can also be deliberately evaded with CSS identifier escapes or JS string concatenation, though
> those are not plausible accidents. I found no current missed reference in any `.svelte`, `.css`,
> `.ts`, or template; this is guard coverage, not a current straggler.
>
> **4. Comments/PR text needing correction**
>
> - `InfraDiagram.svelte:1316` says Adopt's deepening "mirrors the Send button's deepen-on-hover."
>   Send now lifts.
> - `tokens.css:74` says `--ds-seal` is "declared once below"; it was promoted above.
> - The PR body's muted explanation understates the failures. `#6a7180` fails on six enumerated
>   non-white grounds: surface-2, neutral, and all four semantic tints — not "three other grounds/all
>   three tints."
> - The PR body's control-run "same bounding boxes" claim is false. For example,
>   `en-stamped-rollback.png` was `(69,498,238,751)` in pre→post but `(69,175,592,751)` in
>   post→control; the control also introduced three extra tiny JA desk diffs.
>
> ## Requested judgments
>
> **Muted.** Your `#656c7a` change is correct. I independently get: paper 5.058950, white 5.277186,
> surface-2 4.837346, neutral 4.545432, ok tint 4.656831, warn tint 4.665168, stream tint 4.728852,
> danger tint 4.690312. I found no fifth opaque ground. Transparent consumers resolve onto one of
> these; disabled opacity is not another background and disabled controls are exempt. Your token
> comment's eight-ground enumeration is accurate.
>
> **Ring deferral.** I agree with the deferral. It is a serious existing focus defect, but not
> introduced here, and the plan explicitly specified the re-value. For reference, the current
> translucent ring measures only 1.36–1.54:1 across the known grounds. Opaque `#4285f4` clears every
> one I checked, with neutral the lowest at 3.069:1 and navy at 4.395:1. The follow-up looks tractable
> and should remain high priority, but I would not make this PR absorb it days before the freeze.
>
> The unresolved DriftDiffCard DOM fix was also correctly deferred.
>
> **Hover-state fixes.** The four fixes are correct and complete among `color:` changes that brighten
> stream text. The other genuine lightening rule, DecisionsRail's `ok-ink → ok`, still gives 6.159:1
> on paper and 6.424:1 on white. The opacity issue above is the only missed failing interaction state
> I found.
>
> **Elevation.** All seven keeps are genuine overlays/acting surfaces; none is a resting card. None of
> the removals floats over content. The three undefined `--ds-shadow-md` references resolve to the
> very weak `--ds-shadow-sm`, but their border, overlap, and z-position still separate them. I regard
> that as polish debt, not a new correctness problem. Using `--ds-shadow` later would better express
> the intended middle elevation.
>
> **Navy Send hover.** Acceptable: `88% #0e1b5f + white ≈ #2b3672`, white contrast = 11.210:1.
> `color-mix()` is already used elsewhere in this project and the tested browser floor is modern
> Chromium. An unsupported browser merely ignores the hover declaration and retains accessible navy.
> It differs from green/amber filled controls, which deepen, but the near-black base justifies
> lifting. Only the InfraDiagram comment needs correcting.
>
> **Phase 3 proof and rename.** The rename is mechanically sound: 70 replacements = 68 CSS references
> plus two comments; all mappings are one-for-one and correctly ordered; no retired or mangled token
> remains in current source/templates; SealStamp uses only the surviving `--ds-seal` and genuinely
> required no change.
>
> A max channel delta of 168 is consistent with a clock digit disappearing/reappearing in constant
> ink, and the inspected crops show exactly that. But 168 alone could hide a color change; it is not
> diagnostic. The identical-code control establishes that the rig is nondeterministic, but "more
> differing files" is not a formal null hypothesis. The decisive evidence is the literal value
> identity plus the verified mechanical source mapping. Together, those are sufficient to accept
> Phase 3 as zero visual delta.
>
> **Remaining checks.**
> - Tint text is safe: ok 8.028:1, warn 6.592:1, stream 5.050:1, danger 6.756:1 on their respective
>   surfaces.
> - Tint-border/surface pairs remain low — 1.25–1.41:1 — but they were similarly low before and did
>   not cross a threshold. Essential boundaries use the raw accent: ok 5.669:1, warn 4.786:1,
>   stream 3.193:1, danger 4.825:1.
> - InfraDiagram arithmetic is correct: border-strong/drift row 1.3326:1; warn/drift row 4.7857:1.
> - Removing the seal literal from the `tokens.css` comment instead of relaxing the test was correct.
> - The crew identity/status comment is stronger and accurate.
> - I found no unswept old-world paint. The remaining `#1f6feb` is the deliberate crew identity;
>   `#1858c0` occurs only in historical prose; the legacy template remains the explicit exemption.
> - Apart from the approval-desk stream classification issue, every Phase 2 task is present, with no
>   DOM/testid/tour-target changes.
>
> I independently reran Vitest — 1659/1659 — and Svelte check — 675 files, zero errors/warnings. The
> worktree remained clean.

## My assessment of the review

Finding 1 is a genuine hit and exactly the failure mode this repo has been bitten by before (a value
renders through more paths than you think; the audit ran before the rename that created the
consumers). Findings 2–4 are accurate but correctly scoped as pre-existing / coverage / prose.

Codex is right that "more differing files" is not a formal null hypothesis, and right that 168/255 is
not diagnostic on its own. The zero-delta claim should rest on value-identity + verified mechanical
mapping, with the pixel work as corroboration. The PR body needs restating on that point.
