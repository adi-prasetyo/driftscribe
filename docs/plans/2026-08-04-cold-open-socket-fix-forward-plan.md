# Cold open — socket build, fix-forward round (implementation plan)

**Bead: ds-22b.1.1 (child of ds-22b.1). Status: BUILT 2026-08-04, NOT COMMITTED,
NOT RECORDED.** All of A–H plus the §3 decision are in the working tree
(`docs/submission/video/cold-open-boundary.html`, uncommitted). Runtime 34.6 →
**34.9s**. See §9 for what the build changed relative to this plan and what it
found that the plan did not predict.

Written 2026-08-04 after the operator's first honest viewing of `252772b` and a
three-way review (operator + two agents). Self-contained: everything needed to
build is here.

Subject file: **`docs/submission/video/cold-open-boundary.html`** (tracked, on
`main`, pushed).
Baseline for before/after comparison: **`fa7d024`** (the verified 34.6 s
pre-socket build). Socket commit under repair: **`252772b`**.

Prior rounds: [socket build plan](2026-08-03-cold-open-socket-build-plan.md) ·
[the debate that produced it](2026-08-03-cold-open-movement-a-example-debate.md)
(review CLOSED) · [rework design](2026-08-01-cold-open-boundary-rework-design.md).

---

## 0. Posture: fix forward, do not revert

Every failure found is an execution failure with a known fix, not a disproof of
the socket grammar. Beats 6, 7 and 8 are better than the baseline; beat 2 is
mixed; beats 1, 3 and 4 are worse. A revert would also be a new commit, so it
buys nothing mechanically and would discard the wins.

**Why the last round shipped a regression:** both GO/NO-GO gates tested single
frames in isolation. Nothing compared a frame against the frame it replaced.
That is the process fix in §5.

## 1. Invariants — new this round, write them into the source

Carried invariants from the previous round still hold (amber only where a
definition is violated; a vacated socket stays lit and tethered; solid vs dashed
stays semantic; no cryptographic vocabulary on screen; every resource name
verified live). These are added:

1. **The lock is on the ACTION, never on the approver.** Nothing may cage,
   restrain, gate or delay the human. The machines are the constrained parties;
   the human is the one party that is not. A 判子-in-a-cage was proposed and
   rejected for exactly this reason: it makes a machine-minted key the grantor
   of human authority, which is the thesis upside down.
2. **The key never travels from one agent to another.** Worker → lock only. The
   earlier "key flies from the 実行側 badge to the gate, link drawn between
   them" staging was retired because it read as the worker handing the agent a
   credential — machine-to-machine authorization, the exact inverse of the claim.
3. **Whatever opens the lock is the authority the audience remembers.** The key
   is placed in the keyhole and sits *unturned*. It turns only after the 判子
   lands. If the key opens the lock on its own, the human becomes decoration.
4. **The refusal must not touch its target.** `cold-open-boundary.html` is
   explicit: *"The residual gap between the tip and the x IS the refusal."*
   Anchor's reach stops short. It never lands.
5. **Vermilion (`--seal`) is the human's alone** — the only vermilion in the
   piece. The ✕ stays `--ink`: a refusal here is the system *working*, `--red`
   already means "the subject is wrong" at beats 3–4, and the seal is spent once.
6. **Cartoon choreography yes, cartoon rendering no.** Approach / rebuff /
   retreat / place / withdraw read instantly and cost the register nothing.
   Drawn hands, motion lines and rattle marks would fight every other frame.
   The vocabulary already exists: `.lnk-act` is glossed in-source as the
   worker's *hands*.
7. **Legibility is a spec, not a polish pass.** The predicate must read at frame
   scale, not just be present in the DOM.

## 2. The work

### A. Beat 0/1 — the label arrives with its square

Operator point 1 from the previous round; planned in that plan's §3 and never
built. `.mklbl` and `addLabel` were never touched by `252772b`.

- Today: `:469` is `body[data-beat="1"] .mklbl{opacity:1;
  transition-delay:calc(1500ms + var(--ls,0ms))}`. Beat 1 opens at 3200 ms, so
  `payment-demo · Cloud Run` starts fading at **4700 ms** and is up at 5.2 s.
  The last square finishes blooming at **2340 ms** (`MAXD` 1500 + 220 jitter +
  620 duration).
- Change: the subject's label fades in *with* its square during beat 0's bloom.
- **Delete the comment at `:467`** arguing the opposite (*"at beat 0 a label only
  says 'this is a resource'; at beat 1 it argues about which side it is on"*).
  That comment is why an agent read the rule, found a justification, and left it.
  Replace it with the operator's decision so the next reader does not re-litigate.
- The three *outside* labels may keep the beat-1 timing — the argument in the old
  comment is true for them and only for them: they exist to say "on the other
  side of the line," which needs the line.

### B. The hinge has to read at frame scale

Measured: mark 20 px world → 34 px on screen; socket 30 px → 51 px; displacement
`translate(90px,-24px)` = 93 world px → 158 px, on a 1920 px frame. The whole
predicate occupies roughly 8 % of frame width.

- **Do not tighten the camera.** `252772b` widened beat 2 from `scale(2.06)` to
  `scale(1.7)` (`:199`). Going tighter than 2.06 was proposed and is rejected:
  at 1.7 the HCL card, the 見張る側 badge and the pair already span nearly the
  full width. Tightening 35 % cannot hold all three, and that is precisely the
  constraint that produced the old rig's contortions (the `translate(460px,-6px)`
  pair relocation, the `.pair::before` paper backdrop, the `.lnk-lead` amber
  leader) — all of which the socket redesign just retired.
- **Scale the pair locally instead.** ~1.6× on `.mk.subj` and `.sock.subj` at
  beats 2–5 puts the peg at ~54 px and the hole at ~82 px with zero
  recomposition — and it works at beats 3–5, where the void is worst and where no
  camera change helps because the cards that filled those frames are gone.
- **Recessed hole, raised peg.** Today both are outlined rounded squares and the
  socket is *larger*, so the vacated socket reads as the resource and the
  displaced square reads as a chip. The hole wants no fill and an inset shadow;
  the peg wants fill and a drop shadow.

### C. Beats 3–5 are a void — refill them

`hold-3` is a large empty blob with a ~25 px socket and a ~20 px square in the
dead centre, for 3.1 s, then 3.9 s more at beat 4. The baseline carried those
same seconds with full-size legible cards.

Two causes, both fixable:
- Deleting the 実際 card also emptied beats 3–4, which nobody checked. The old
  plan predicted beat 2 could "go wider and calmer" (§6) and stopped there.
- `.sock` is `opacity:0` at beats 2–5 (`:295-298`) and `.mk.in` is hidden there
  too, so the other eight managed resources and all their sockets are off screen
  exactly when the drama happens. **The predicate the whole redesign is built on
  is absent during its own payoff.** Keep the siblings and their sockets alive
  through beat 5, dimmed rather than hidden.

### D. Kill the red peg

`:377-378` turns `.mk.subj` red at beats 3–4 while its vacated socket stays amber
(`:304-306`), so the tether now joins two different semantics. This is the
dormant-state trap the previous plan flagged (§5 trap 2) and then did not check
in the built file. Displacement stays **one colour** for its whole duration:
amber from beat 2 until the re-seat. The green at beat 5 is the re-seat and stays.

### E. Beat 4 — the refusal

Measured today, live, with computed styles:

| t (ms) | `.lnk-blocked` opacity | `.stopx` opacity |
|---|---|---|
| 15300 | 0 | 0 |
| 15700 | **0** | 0.15 |
| 16100 | 0.73 | 1 |

The ✕ appears from nothing and *then* grows a tail. Root cause:
`body[data-beat="4"] .lnk-blocked{stroke:var(--muted); transition-delay:1450ms}`
has no property list, so the delay lands on `opacity` too — and the base rule at
`:698` transitions both. The `reach` animation draws the dashoffset from
250–1150 ms on an element that is still transparent.

New choreography:

1. **Retarget the reach at the padlock**, not the 実行側 badge. This is a
   correctness fix as well as a legibility one: the current arc reads "Anchor
   cannot reach the worker," which is false — Anchor proposes to the worker
   routinely. What it cannot get is the key.
2. **Fix the delay** so it applies to `stroke` only. `tipjab` (`:711-718`) then
   becomes visible for the first time: it extends to the contact point, is
   stopped, and settles ~9 px short. That is the recoil, already built, never seen.
3. **The padlock shakes** 2–3 px for ~200 ms at the contact instant. This is the
   "it cannot be opened" beat.
4. **The ✕ pops with a label: 鍵は出せない.** Muted ink, small. Not 拒否 /
   "rejected" — that says Anchor asked and was told no, when the truth is
   stronger: there is no code path by which it could have succeeded
   (`agent/main.py`). 出せない echoes caption E's own 出せない, so the caption at
   the bottom and the mark at the top reinforce instead of competing for a 3.9 s
   beat.
5. **Anchor's line withdraws.** Today it greys and lingers, which is why a dead
   ✕ and a live navy line share the frame at beat 5. The retreat fixes that too.

Do **not** stack a third shake on the ✕: `xpop` already overshoots
(`cubic-bezier(.3,1.6,.4,1)`, scale .35 → 1). Jab + pop + shake inside 300 ms is
too much. Fix the sequencing, look at it, then decide.

### F. Beat 5 — the key, and who turns it

Today the key materializes on the lock at beat 4 (`keyset`, 1700 ms) and crumbles
at beat 5 (`keycrumble`, 1200 ms) **without ever turning**. The gate header icon
at `:1177-1180` is a closed padlock — `<rect>` body, closed shackle — and no rule
ever opens it. There is a key, there is a lock, and neither does anything to the
other.

| ms | what |
|---|---|
| 0–700 | 実行側 extends its line to the lock, **places the key in the keyhole, withdraws**. The key sits unturned. |
| 700–1320 | the 判子 lands — **alone**: badges still, key inert, nothing else moving |
| 1320–1580 | the key **turns** ~90° |
| 1500–1700 | the shackle **lifts**; the padlock is open |
| 1700–2350 | `actdraw`: 実行側 extends again, to the square |
| ~2350 | the square re-seats in its socket |

Proposer cannot, minter will not, human does. **The withdrawal at 700 ms is the
most valuable gesture in the piece** — it is the only moment that says the party
who minted the credential does not get to spend it — and nothing on screen
currently says it.

Restoring provenance (worker → lock) is a deliberate change from `252772b`,
which staged the key as materializing from nowhere and noted *"this is VISUAL
STAGING, not a provenance claim."* It is safe under invariant 2 because the key
goes to the **lock**, never to an agent, and safe under invariant 3 because it
sits unturned until the stamp. It is worth restoring because "the key comes out
of a different box than the one that proposed" is the least-privilege thesis
made visible.

Beat 5 needs ~4.3 s to hold this (from 3.7). See §4.

### G. The 判子 reads as a person

Already half-built and worth knowing before touching it:
- `--seal` vermilion is reserved — `:836`, *"the only vermilion in the piece."*
- `stampin` (`:845-849`) goes `scale(2.1) → 1` at a fixed `rotate(-9deg)`. It
  does not travel from any badge; it descends toward the paper from above the
  plane. It is already the only element that does not come out of a labelled box.
- Caption F at `:1274` is 人が<span class="accent-seal">承認</span>して、はじめて戻る
  — the word 人 and the vermilion fire in the same second as the stamp.

What defeats all three is staging: 92 px tucked into a card's bottom-right
corner, so it reads as a widget the interface produced rather than an act a
person performed. Two changes:

1. **Its own instant** (already in §F's table) and **let it break the gate card's
   edge.** Things that come from inside a card obey the card; things that come
   from outside do not. That overlap is most of the difference between "the UI
   showed a badge" and "a person pressed this."
2. **Lean on imperfection.** Everything else in this world is geometrically
   derived — a computed blob, exact sockets, measured béziers. Keep the stamp the
   only thing on screen that is not axis-aligned: the 9° is there, add a little
   ink spread and pressure. The only crooked thing in a precise world is the human.

The audience is Japanese and a 判子 is a *personal* seal, not a generic approval
badge, so the cultural read of "a specific person put their name to this" is
already doing work no new element has to. The gesture does not need explaining;
it needs room.

### H. The ring becomes the socket

`.tgt-sock` is **new in `252772b`** (0 occurrences at `fa7d024`, 12 now) and its
own comment admits it is a rhyme, not a morph — the ring fades out and a dashed
socket fades in. Identity does not carry, so on first viewing a crisp new square
outline materializes outside the line. That is the exact misread the debate
scrapped beat-6 materialization to avoid, and it is what the operator reacted to.

The ring must visibly **contract into** the landed socket: one object, continuous,
no crossfade.

## 3. The one open decision

**The gate card's change preview.** Keep `live → mock` (`:1187`), or de-text it
to 定義値に戻す with the specifics demoted to small mono, as `HMAC 署名付き`
already is.

Recommendation: **de-text.** With the beat-2 実際 card gone, the gate is the last
surviving string diff and it arrives with less setup than it used to. De-texting
it means the piece shows the *defined* value once (the beat-2 HCL card) and never
shows the drifted value at all — because the displaced square **is** the drifted
state. That is the socket grammar finally doing the job it was adopted for, it
satisfies "get the mock/live example off screen" without swapping the drift
subject, and the approval stays accurate: a human approves a rollback to the
defined value.

Items A–H do not depend on this. Build them either way.

## 4. Runtime

34.6 s → **~35.2 s**: beat 4 gains Anchor's withdrawal, beat 5 goes 3.7 → ~4.3 s.
The 35 s cap was already waived once. This beat is the thesis — proposer cannot,
minter will not, human does — so it is the right place to spend, but the trade
should be made knowingly here rather than discovered in the recording.

Captions are sized to JA reading time (~4 chars/s, 5.2 ceiling; budget =
`chars / (duration − 0.5 s stagger)`). Beats may be **lengthened** freely;
shortening past a caption is forbidden.

## 5. Build order and gates

**New standing gate, and the reason this round exists:** every changed beat gets
a **before/after pair** — `?hold=N` on the working file *and* on `fa7d024` — at
1920×1080, compared side by side. A frame reviewed alone tells you whether it is
good; only the pair tells you whether it is better. Both of the previous round's
GO/NO-GO gates passed on single frames while the piece regressed.

1. **A** (labels) — smallest, independently verifiable, and the operator's
   longest-standing request.
2. **D** (red peg) — one rule, removes a semantic contradiction.
3. **E** (refusal) — the delay fix first, *then* look at `tipjab` before adding
   the shake or the label. GATE: does the recoil carry the beat on its own?
4. **B** (hinge scale + recessed/raised) — GATE: `?hold=2` and `?hold=3` against
   the baseline. Does displacement read across a room?
5. **C** (refill beats 3–5) — depends on B's sizing decision.
6. **F + G** (key, lock, stamp) — the largest piece. GATE: after the stamp lands,
   ask a fresh viewer who opened the lock. If the answer is "the worker," the
   choreography failed invariant 3.
7. **H** (ring → socket morph).
8. **§3 decision** if answered, then retime MARKS, re-measure runtime, update the
   header beat table and the three doc surfaces.

## 6. Not in scope

- **A third badge for the human** (決める側 · 人). Deferred by the operator
  2026-08-04. It is the most literal answer to "how do we say 判子 = human" and
  removes all doubt, but it puts a third card on screen at beat 5 and risks
  reading as a fourth crew. Hold it in reserve: build §G first, and add this only
  if a test viewer still misses the link.
- **The match-cut ending** into the live desk panel.
- **The recorder script.** None exists. Shooting needs a fresh scratch Playwright
  + `ffmpeg-static` install; Playwright is vendored at
  `frontend/node_modules/playwright`, which is enough for `?hold=` review.

## 7. Traps verified at source 2026-08-04

1. **`transition-delay` with no property list applies to every transitioned
   property.** That is the ✕ bug's root cause (§E). Grep for the same pattern
   elsewhere before assuming it is the only instance.
2. **`.mk.subj` already uses `transform`** for the displacement (`:375`) *and*
   for the beat-5 re-seat (`:382`, `translate(0,0)`). Adding a scale means every
   one of those rules must carry both functions, or the scale silently vanishes
   at the re-seat.
3. **`.sock` is positioned by `left`/`top`**, so scaling it needs
   `transform-origin:center`.
4. **CSS specificity ties are decided by source order.** A reveal rule must
   follow its hide rule. This cost a whole beat two rounds ago.
5. **`?hold=` cannot review transient gestures** — their settled end state is
   absence. Anchor's withdrawal and the worker's withdrawal join the ripple and
   the sweep on that list. They need timeline sampling, not hold frames.
6. **`no-anim` kills animations**, so anything an animation *delivers* must be
   restated for hold mode, and `body.no-anim *` does not match pseudo-elements.
7. **The mark field is deterministic** (LCG seeded 20260801). Consuming an extra
   `rnd()` re-scatters everything; any cull must happen after both draws.
8. **Camera rig**: `transform-origin:0 0`, world point P lands at `T + s*P`.
   Measure with `offsetLeft`/`offsetTop`, never `getBoundingClientRect()`.
   Beat 2 is now `scale(1.7)`; beats 3–5 are `1.35/1.40/1.45`; beats 6–7 are 1.
9. **The counter must agree with the visible marks** (9 inside → 10) and takes
   real counts at record time via `?managed=N&grown=N+1`.

## 8. Facts that bind (do not re-derive)

- Anchor's Eventarc trigger fires on config changes to one Cloud Run service and
  **never** for untracked resources (`docs/OVERVIEW.md:127`). Untracked resources
  reach the system through `infra-reader` via CAI, eventually consistent. Never
  depict Anchor discovering an unmanaged resource — PR #194 shipped
  byte-golden-pinned prompt copy to keep these senses apart.
- The proposing crew never mints its own approval. The token is minted by the
  worker that will act, bound to one revision, single-use, TTL 15 min
  (`driftscribe_lib/approvals.py:13,41,221`; `workers/rollback/main.py:718`).
- `payment-demo` is declared in `iac/cloudrun.tf`; `PAYMENT_MODE` is locked to
  `"mock"` by `demo/ops-contract.yaml:7-8`.
- `driftscribe-hack-2026-receipts` is a real, live, unmanaged bucket and the
  returning character from `seg-crew.mp4`. Beat 7 depicts a genuine pending
  adoption; only the *file* is depicted, because the adoption is not approved.
- "Inside the line = has a definition" is an honest simplification at the
  resource grain, blessed by the rework design's §8.

---

## 9. Build log — 2026-08-04

All of A–H shipped, plus §3. Every changed beat was rendered against the same
`?hold=N` on `fa7d024` at 1920×1080 before moving on, per §5's new standing gate.
Runtime **34.9s** (+300ms, all of it beat 5). Nine hold states and a full
playback pass run clean with no console errors; `?managed=/&grown=` still work.

### Where the build departed from the plan

- **B — the magnification is on the whole inside group, not on the subject pair.**
  Scaling only `.mk.subj`/`.sock.subj` would have made the drifted resource a
  different *size* from its siblings, which says "this is a different kind of
  thing" where the beat needs "this one has moved." All nine marks and sockets
  scale 1.55× at beats 2–5 together, so the file's own "the difference is ink,
  not scale" rule survives inside the field. The outside marks stay at 1× and
  that mismatch was accepted: at beats 2–5 they are peripheral texture, and
  dimming them to hide it would invert beat 6's sweep, which needs them bright
  enough to visibly *dim*.
- **B — the displacement had to widen 90 → 155px.** Not in the plan. At 1.55×
  the peg's edge landed ~5px from the socket of the neighbour at (712,592), so
  the displaced square read as sitting on the lip of somebody else's hole. 155px
  along the same heading clears it by ~31px and keeps the peg 167px from the
  blob centre, comfortably inside the line. This dragged four coupled numbers
  with it: `.teth`, `.lnk-lead`, `.ripple`'s origin and `ripple-travel`'s end
  offset. All four are now cross-referenced in source.
- **E — the shake and the label were both kept.** §5's gate said to fix the
  sequencing first and then decide. With the sequencing fixed the recoil is
  visible for the first time but is far too quiet to carry a 3.9s beat on its
  own at that framing, so `鍵は出せない` and the padlock rattle both stayed.
  Nothing was stacked on the ✕ itself, per the plan.
- **F — beat 5 needed +300ms, not +600.** The plan estimated ~4.3s; the built
  choreography lands the re-seat at 3350ms and settles by 4000ms.
- **H — `.tgt-sock` was deleted outright.** The plan said the ring must contract
  into the landed socket; the cheapest honest way to do that is to stop having
  two objects. The ring's own path is now lerped point-for-point into a
  superellipse at the target (`ringLand()`), so the hand-drawn wobble anneals
  out on the way and a drawn circle visibly becomes a printed socket. The socket
  is the ring from then on — dashed at beat 7, snapped solid by
  `boundary-committed` at beat 8, `drop-shadow` instead of `box-shadow` because
  it is an SVG path now.
- **§3 — decided: de-texted.** `live → mock` became `定義値に戻す`, with the
  specifics demoted to the small-mono line above. This was the operator's own
  complaint 2 of 4 and the plan's own recommendation. `live` no longer appears
  on screen anywhere; the one surviving `mock` is the *defined* value in the
  beat-2 HCL card, which is what that card exists to show. Reversible in one
  line — the markup note says how.

### Three bugs the plan did not predict, found while building

1. **`.lnk-act`'s dasharray was 330 against a 468px path**, so the last 138px of
   the execution stroke — the stretch that arrives at the resource — was drawn
   through the whole `backwards` fill. A navy line was touching the resource
   before the human had approved anything. Pre-existing; merely less visible at
   the old 1150ms delay than at the new 2000ms one. Both this and the new
   `.lnk-key` now measure with `getTotalLength()`.
2. **A single shared `raf` slot.** Beat 7 runs two rAF loops at once (the ring
   landing and the lobe growing); the second overwrote the first's handle, so a
   replay mid-beat cancelled only one and left an orphan writing to a path the
   new run had already reset. Handles are keyed now.
3. **The gate header padlock at `1em` is unopenable.** ~17 world px, ~25 on
   screen at beat 5's `scale(1.45)` — big enough to be an icon, far too small to
   be a thing you watch open. It is `1.9em` with `overflow:visible`, and the
   shackle is its own element rotated about its right foot, because a 4px lift
   on a 32px icon reads as a rendering artefact where a 20° swing reads as open.

### Lesson worth keeping

**A gesture the file already pays for is not a gesture the file performs.** Three
were already built and none had ever fired: the padlock that never opened, the
key that never turned, and a complete extend-stop-recoil hidden behind a
`transition-delay` with no property list. Reading what is already there was worth
more than anything added this round.

### Still open

- Not committed and not recorded. A recorder script still does not exist (§6).
- The match-cut ending (§6) and the third badge for the human (§6, deferred by
  the operator) are both untouched.
- `.mk.tgt` renders in full managed styling at beat 7 while its socket is still
  only *dashed*, i.e. the resource looks adopted one beat before approval.
  Pre-existing, predates the socket build, out of scope for this round.
