# Cold-open v4 round 3: the file dictates, the values disagree, the census closes (ds-22b.3)

Operator round 3, green-lit 2026-08-06. Three tasks, one commit each, on branch
`cold-open-v4` (worktree `.worktrees/cold-open-v4`). File:
`docs/submission/video/cold-open-boundary.html`. No retime — every change fits
inside the existing 44.7s clock; MARKS and the C_* chain are untouched.

Standing rules that bind every task: one animation per element per beat
(pseudo-elements are their own element); base state owns the settled state;
per-property transition-delay lists; `?hold=N` end states must survive
`body.no-anim` (which kills transitions AND animations, including pseudos);
no line or flight annotation during the *edit* — color belongs to detection.

---

## Task A — the file dictates the value (reverses the round-2 flight)

**Problem (operator):** the chip appears, an identical twin materializes on it
and flies to the census — two identical texts co-visible at birth and forever
after. And nothing explains what PAYMENT_MODE has to do with payment-demo.

**Design:** the value starts in the *card* and the chip is its *arrival*.
After payment-demo's name lands (~3490 into beat 2), an indented child line
types itself under the name — typing is the piece's "the file speaks" verb
(beat 9). Then a copy flies down to the square and *becomes* the chip. At no
instant do two identical static strings coexist; the only duplicate is in
motion, and a moving copy reads as delivery. Containment (indent) + delivery
(flight) carry the relation wordlessly. File shows `"mock"` (HCL string);
square shows `mock` (runtime value) — the quotes are the file-ness.

**Build:**
1. Card height 264 → **285** (base — the card is sized for the value row from
   the start; names land progressively anyway so an unfilled bottom row during
   assembly is the existing norm). Bottom edge 455 vs gate top 470: 15px, no
   overlap (gate is beats 5–7 only).
2. New line `.cn-lbl.cn-cfg` (markup-side JS append like the ten):
   `--x0`=`--x1`=1003 (986 + 2ch indent), `--y0`=`--y1`=423 (402 + 21),
   `--ci:13.3` — same coords for x0/x1 means the beat-2 flight rules are a
   no-op on it and the generic cn-lbl cascade (beats 2–7, and Task C's 8–10)
   applies for free. Content: `<span>PAYMENT_MODE = </span><span
   class="cv2">"mock"</span>` inside a width-typing wrapper.
3. Typing: wrapper span `inline-block; overflow:hidden; white-space:pre`,
   base width **21ch** (base owns settled), animation `cfgtype` from 0ch,
   `steps(21,end) 700ms 3550ms both`, beat 2 only. Mini caret span, one
   finite blink animation 3550–4250, base opacity 0.
4. Flyer `.cn-drop` (own class, NOT cn-lbl — it must not obey the census
   cascade): text `PAYMENT_MODE = mock` (no quotes — it becomes the chip),
   base at (1003,423) 14px, opacity 0.
   - beat 2: opacity 1 @4350, left/top → (532,610) @4350 (700ms flight),
     font-size → 12px along the ride.
   - beat 3: restate endpoint; opacity → 0 with delay 350ms (fade after the
     ~350ms-into-beat-3 landing). Beats 4+: base absence.
5. Chip: beat-2 reveal delay 3550 → **4650** (fades in under the descending
   flyer; the beat-3 chip rule completes it across the cut — same
   restated-endpoint pattern the round-2 cn-val used). Edit still starts
   beat-3+800 (abs 5500): ~450ms of settled chip before the caret.
6. **Remove:** the `.cn-val` CSS rule + comment block, the JS `val` append,
   and rewrite the chip comment (the flight now arrives, not departs).
7. Comments: header WHAT-V4/ACCURACY + timeline beat-2 line get the new
   numbers (type 3550–4250 · flyer 4350–5050 · chip 4650).

**Verify:** hold-2 (complete card incl. value line, chip landed), scrub
frames at 11000/11500/12000/12300; hold-3 unchanged flare end-state;
playback probe across the 2→3 cut, zero page errors.

## Task B — the disagreement is two words (values-only emphasis)

**Problem (operator):** whole-line amber dilutes the point; the drift reads
too subtle. Emphasize only `live` and `mock`, marker-sweep them, try ズレ.

**Build:**
1. **Remove** the `.cn-subj` amber (beats 3–6) and green (beat 7) rules —
   the name line stays neutral ink forever. Remove the whole-chip amber
   (beats 3–6) and green (beat 7) color/weight; keep the chip's scale-1.15
   summon and the square's own flare/fixflare (that's Anchor on the
   *resource*).
2. Spans get `transition:color 400ms ease, font-weight 200ms ease` (base)
   and `position:relative; z-index:0` (for the sweep pseudo behind the ink):
   - beat 3: `.chip .v-drift` amber-ink + 700, delay 3000/3000; `.cn-cfg
     .cv2` same, delay 3200. Beats 4–6 restate plain (delays must not
     replay). Width animations on these spans are `width` — color/weight
     transitions don't collide, and ch recomputes under bold so no clipping.
   - beat 7: `.chip .v-old` + `.cv2` green-ink + 700, delay 2500 (with the
     square's flip). Beat 8+: base neutral (the story has moved on).
3. Marker sweep: `::after` on `.v-drift` (beat 3), `.cv2` (beats 3 and 7),
   `.v-old` (beat 7) — inset block, translucent highlight via `--swp`
   (amber rgba at beat 3, green rgba at beat 7), `transform-origin:left`,
   keyframes `swp{0%{opacity:.9;scaleX(0)} 45%{opacity:.9;scaleX(1)}
   100%{opacity:0}}`, 900ms, delays 3000 (`live`) / 3200 (`"mock"`) at
   beat 3 and 2500/2700 at beat 7. Order = the story: anomaly first, then
   the reference it was checked against. Base pseudo opacity 0 = settled
   absence; `body.no-anim *::after` already kills it in holds. One
   animation per element: the spans' own `animation` slot stays free
   (width anims live on the same spans — the sweep lives on the pseudo).
4. ズレ tag: world element `.zure`, mincho 15px amber-ink at (655,632)
   (under the scaled chip's `live`), base opacity 0, one animation
   `zurelife 1300ms ease 3150ms both` at beat 3 only (in 18%, hold to
   72%, gone) — caption D's own word landing where the eye already is,
   detection-phase only. If the screenshot says crowded: delete one rule.
5. Timeline comment beat-3/beat-7 lines updated.

**Verify:** screenshots at flare (abs ~15400), ズレ peak (~15700), settle
(~16000); hold-3/4/6 (amber values persist, lines neutral); hold-7/8
(green values / neutral); crowding judgment call on the ズレ frame.

## Task C — the census closes the loop (movement B, the point-4 gap)

**Problem (operator):** the adopted bucket never joins the census; the card
is gone before the import happens. Accepted design: the card persists, and
after approval swallows the bucket, its name flies in as the 11th line and
*then* the counter rolls.

**Build:**
1. **Dock:** `.census` beats 8–10 opacity 1, `top:170px → 60px` (pure
   vertical — at wide frame nothing occupies (975–1405)×(60–345); the
   write badge (top 356) and `.tf` (430) are beat-9-only and clear it by
   11px+). Add `top 700ms cubic-bezier(.16,1,.3,1), height 300ms ease` to
   the card's transition list. All `.cn-lbl` (ten names + cn-cfg) get
   beats-8–10: `opacity:1; left:var(--x1); top:calc(var(--y1) - 110px)`.
   Value line drops its beat-7 green en route (base neutral) — fine.
2. **Red discovery (beat 8):** the sweep flags the bucket in the *other*
   drift sense, so it gets the *other* color (red = cap I's 定義がない,
   never the amber of config drift — #194 on camera): beat-8 `.tgt-halo`
   border-color → `var(--red-ink)`; new `discover` animation on
   `.mk.tgt`'s **border-color only** (never box-shadow — the :384 trap):
   ink-soft → red-ink @25% → ink-soft, 1600ms delay 800, landing exactly
   on the existing halo pulse. End state = the rule's own ink-soft, so
   holds need nothing. Quiet: a flush, not a siren — the sweep is
   on-demand reading, not an alarm.
3. **The 11th line (beat 10):** element `.cn-new` — own class, visual
   clone of `.cn-lbl`, deliberately NOT cn-lbl (the beats-2–7 census
   cascade must never touch it; adding `:not()` guards there would break
   the source-order specificity ties). Text
   `driftscribe-hack-2026-receipts`, base opacity 0 at the square
   (914,444); beat 10: opacity delay 800 (after the 41400+620 swallow
   completes at ~720), left/top delay 1250 → docked slot (986, 334)
   [= 423+21 undocked − 110]. Same materialize→fly grammar as beat 2.
4. **Card grows for it:** beat 10 height 285 → 306, delay 1150 (the card
   makes room as the name lifts off — never before, or it telegraphs).
5. **Counter waits for the census:** beat-10 `.cnt .cflap` gets
   `transition-delay:2050ms` — swallow (≈720) → name lands (≈1950) →
   count rolls (2050–2830). The ledger records, then the number moves.
6. Beat-10 rig is scale(1.06) translate(−57.6,−32.4): card top renders at
   screen y≈31 — on-screen, verified by screenshot.
7. Comments: beats-table lines 8/9/10, WHAT-V4-CHANGED bullet, ACCURACY
   note (the 11th line is the post-merge state the approved PR produces —
   same dramatization the beat-9 file and the 10→11 counter already
   carry; the real bucket stays unmanaged and on-camera per the demo-ops
   constraint).

**Verify:** holds 8/9/10 against pre-change same-hold captures (standing
gate: every changed beat vs `?hold=N` on the previous commit — expect the
card as the only diff in 8/9); screenshots at discovery flush (~32900),
dock settle (~32800), lift-off (~42550), landing + roll (~43400); full
playback probe (MutationObserver on data-beat + chain classes) — all cuts
on marks, zero page errors; `cmp` beat-9 chain checkpoints unchanged.

---

## Order & commits

A → B → C, one commit each (B touches spans A creates). After C: full
44.7s playback probe, hold sweep 0–10, memory + ds-22b.3 notes update,
then push `cold-open-v4` (operator-authorized 2026-08-06 — continuing on
another machine).
