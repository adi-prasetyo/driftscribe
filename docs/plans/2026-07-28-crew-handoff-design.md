# Crew handoff — one door, four crews

**Date:** 2026-07-28
**Status:** design, validated in conversation + Codex-reviewed (2026-07-28, thread `019fa7fd`)
**Related:** `docs/plans/2026-07-28-composite-redesign-implementation.md`

## Problem

Today a chat cannot begin until the operator picks one of four crew cards. The
composer defaults to Anchor, and the thread then hard-locks to that crew
(`_resolve_chat_conversation` → 409). Explore's system prompt already knows its
siblings, but routes **terminally**: "start a new chat with Provision."

Two costs:

1. **A decision before a sentence.** The operator must know DriftScribe's
   internal workload split before they are allowed to describe their problem.
   This is the shape the judges named — 「フロントエンドが後付けのようである」.
2. **Dead ends mid-task.** The moment a read-only investigation finds something
   worth fixing, the flow stops and the operator restarts elsewhere.

## Is Anchor even distinct in chat?

Yes. Explore holds Anchor's *reader* (`drift_read_live_env`) plus more read
surface (`read_project_inventory`, `load_iac_plan`, `read_team_log`), so it sees
every drift Anchor sees. What it lacks is every remedy:

| Anchor-only chat tool | Effect |
|---|---|
| `drift_propose_rollback` | Mints the single-use HMAC approval — the HITL gate |
| `drift_patch_docs` | Opens the docs PR for sanctioned drift |
| `notify` | Operator notification |
| `search_recent_prs` | Rides the write-capable GitHub token (deliberately excluded from Explore) |

Explore can *see* everything and *do* nothing. Anchor is a real destination.

## Shape

Explore becomes the primary door. When the work needs a remedy, the crew
proposes a handoff, the operator confirms one chip, and the thread's crew
changes.

```
You:      the ORDER_TIMEOUT on payment-api looks wrong
[Explore] reads live env + contract
          "90s, contract pins 30s, allow_manual_change=false.
           Fixing this needs Anchor."
          ┌──────────────────────────────────────────┐
          │ Bring in Anchor to propose a rollback?   │
          │                             [Yes]  [No]  │
          └──────────────────────────────────────────┘
                    ↓ click
──────── ◆ Anchor joined ────────
[Anchor]  runs immediately, proposes rollback → HMAC approval gate
```

### Decisions taken

| Decision | Choice | Rejected alternative |
|---|---|---|
| Transition | **Crew swap in-thread** — next turn runs the target crew's own agent with its own scoped tools | Explore delegating to a sub-agent (Explore's turn would transitively reach a mutation surface, voiding the read-only pin); one merged crew with per-turn tool modes (collapses four auditable prompts into one, guts the in-app prompt viewer) |
| Picker | **Removed from the composer**; crews stay visible as identities in the transcript | Hidden behind `⌘K`/`/` (one more thing to explain on stage); keep + additive handoff (does not simplify first run, which was the point) |
| Stickiness | **Sticky; every crew can hand off**, including back to Explore. Crew changes *only* on a confirmed transition | Auto-return to Explore (a transition the operator never clicked); Explore-only handoff (the dead end just moves one hop later) |
| First turn | **Confirm is the turn** — the chip submits on the new crew's behalf, carrying replayed history + brief | Join-then-wait (reintroduces the friction being removed); fresh context (crew re-asks what Explore established) |
| Merge | **Never reachable from the confirm click** — enforced server-side, not by prompt wording | Trusting the prompt to keep `upgrade_merge_pr` out of the first turn |

## Mechanic

### The tool

New capability `request_crew_handoff` → callable `request_crew_handoff_tool`,
enabled in all four manifests. Signature `(target, reason, brief)` — **no
conversation id**. The model must never name the thread it writes to, or it can
write proposals onto arbitrary conversations.

### Proposal is request-scoped, committed with the turn

The obvious implementation — "the tool updates the conversation doc" — **cannot
work on the first Explore turn, which is the most common case.** A new
conversation does not exist until its turns persist: `_resolve_chat_conversation`
only mints an id (`agent/main.py:430`), and creation is lazy via `create_with` on
`append_turns` (`agent/main.py:497`).

So:

1. The tool validates `target` / `reason` / `brief` and records the intent in
   **server-bound request context**. It returns no nonce to the model.
2. At final-result persistence, the user turn, the crew turn, and
   `pending_handoff` are written in **one transaction**.
3. The nonce is minted and delivered to the client only after that transaction
   commits.

A newly proposed handoff **supersedes and burns any prior unredeemed nonce** on
that conversation. Exactly one `pending_handoff` may exist at a time. Without
this, an old proposal to Anchor stays redeemable after the conversation has moved
on — a genuine replay path, and one that `pending.from == conversation.workload`
does not catch, since both proposals come from Explore.

The proposal rides the terminal `done` payload rather than a dedicated
`handoff_proposed` SSE frame. It is not valid until persisted, and persistence
happens at the end of the stream, so a mid-stream frame would only advertise a
nonce that does not exist yet.

### Redemption

`POST /chat/handoff {conversation_id, nonce, accept}`. The body carries **no
target** — `from` and `to` are read from persisted server state.

`ChatRequest.workload` stays a closed `Literal` and `_resolve_chat_conversation`'s
409 stays exactly as written. An ordinary `/chat` POST still cannot move a locked
thread.

This matters because the lock is scar tissue. From `ChatRequest`'s own docstring:

> a workload-less POST once defaulted to the mutation-capable drift workload and
> an out-of-domain probe prompt became fabricated docs PR #109

Redemption validates, in one transaction: nonce digest matches, unexpired,
`pending.from == conversation.workload`, expected conversation generation, and no
active run. Then it burns the nonce, flips the workload, and appends the
`crew_change` turn.

Store the nonce's **digest**, compare in constant time, and burn transactionally.
This is a *narrative* parallel to the HMAC rollback gate — propose, mint, redeem,
burn — not shared machinery. Both ends live inside the coordinator, so an opaque
hashed token is sufficient; do not import the worker's HMAC architecture.

### Concurrency — the 409 is a check, not a lock

`_resolve_chat_conversation` validates at `agent/main.py:453`, the
`StreamingResponse` returns at `:6448`, and the agent actually runs later inside
the generator (`:6243`). Persistence then appends using the *captured* workload
(`:489`) without rechecking. So a `/chat` run and a redemption can interleave, and
turns can land on a conversation that now claims a different crew.

This is **audit integrity, not capability escalation** — the racing caller
already held the crew it is running, and no ordering of these requests grants
tools the caller could not otherwise reach. But for a system whose entire pitch is
a trustworthy decision record, a transcript that misattributes which crew acted is
a real defect.

Fix: a per-conversation run lease. `/chat` acquires it before the LLM starts;
redemption requires it free. Add narrow `begin_chat_run` / `finish_chat_run` /
`propose_handoff` / `redeem_handoff` operations to the `StateStore` protocol
(`agent/state_store.py:21`), following the existing transactional turn-allocation
pattern (`:665`). **Do not expose a generic "set conversation workload" method** —
the only writer of that field should be redemption.

### `workload` becomes mutable — and three readers assumed it never would

Restricting the *writer* to redemption is only half of it. The moment that write
exists, an invariant nothing ever wrote down stops holding:

> **A conversation belonged to exactly one crew for its entire life.**

Nothing declared it, so nothing defended it, and three readers had quietly
encoded it. Each degrades silently — no error, just a wrong answer:

- `list_conversations(workload=...)`, which backs `read_conversations_tool(crew=…)`
  and so decides what a crew can recall. Explore loses ten turns of its own work
  the moment it hands off, and Patch inherits threads that are mostly Explore's.
- `build_conversations_breadcrumb`, the always-on pointer block prepended to
  **every** chat agent's instruction. It renders a crew name beside a title, and
  the title is the FIRST user prompt — asked of the ORIGINATING crew. So it
  prints one crew's name over another crew's question, in the one surface whose
  entire job is saying what *other* crews did.
- The rail's "N messages", which means the operator's own prompts and derived
  them as `ceil(turn_count / 2)`. That holds only while every exchange writes a
  user turn and a crew reply. A handoff breaks it from both ends: an accepted
  transition appends a `crew_change` row, and the joining turn writes a reply
  with no prompt in front of it, because the operator confirmed a suggestion
  rather than typing one.
- `matchesConversation` (`frontend/src/lib/conversations.ts`), behind the rail's
  search modal — the only one of the four an operator sees directly. Its
  docstring states the assumption outright: the haystack is "the raw workload
  value, and the crew display name". Searching `explore` stops finding a thread
  Explore ran and handed away.

Four, and the fourth was found only by sweeping a second time from a different
angle. Assume there is a fifth: the failure mode is silent, so a reader that
still assumes immutability looks exactly like one that does not.

The fix is to stop overloading one field with three questions:

| Field | Answers | Written by |
|---|---|---|
| `workload` | who is bound RIGHT NOW — crew-lock authority, the 409 | redemption, and nothing else |
| `crews` | who has taken part | appended on accept, never rewritten |
| `user_turn_count` | what the operator actually typed | every turn append |

Conversations predating the two new fields fall back exactly rather than
approximately — the single bound workload IS their whole participant history,
and their turns landed strictly paired — so neither needs a backfill.

Two consequences worth stating so they are not rediscovered:

- The breadcrumb must exclude on the **bound** crew, not on participation. That
  is what stops a crew being shown its own live thread as someone else's
  history, and it now holds for a non-obvious reason: redemption flips
  `workload` before the joining crew runs, so the bound crew is always the
  running one. Pin it with a test.
- **The rail's count cannot be fixed in the frontend.** The rail receives only
  metadata, so once the pairing invariant is gone the real number is not
  recoverable from `turn_count` at all. The store has to carry it.

Rule for anything added later: reading `workload` to answer "whose thread is
this?" is a bug. It answers "who is bound right now" and nothing else.

### Merge exclusion must be code, not prose

`build_chat_agent` filters by autonomy tier, then applies a demo-anon denylist
that **explicitly preserves `upgrade_merge_pr`** as a risk-accepted carve-out
(`agent/adk_agent.py:746-757`). Under the default `propose_apply` mode there is no
denylist at all. So absent new code, the confirm click *can* merge a PR, and the
"floor is a PR exists" claim is false.

The first turn after a handoff runs with a server-enforced denylist —
`upgrade_merge_pr` and `upgrade_close_pr`, both of which mutate an existing PR on
a single click. Patch joins and says the PR is ready; merging takes a second,
deliberate operator turn.

### "A human clicked" is not a cryptographic property

The endpoint proves possession of the operator credential plus the proposal
nonce. In demo mode anonymous visitors deliberately hold the operator seat
(`operator_seat_demo_window`), so the honest guarantee is **"a separate,
authenticated confirmation request bound to a specific proposal"** — not "only a
human can redeem." The nonce buys intent binding, expiry, and replay prevention.
State it that way in the pitch.

## Prompt surface

The joining crew receives replayed history plus the brief. Two corrections to how
that must work:

**Replay attribution.** `_seed_event_from_turn` authors *every* crew turn with the
**current** agent's name (`agent/adk_agent.py:1175`), deliberately, because ADK
otherwise rewrites foreign authors into user-role "For context: … said" messages.
For same-crew replay that suppression is correct. For cross-crew replay it is
exactly backwards — after Explore→Anchor, Explore's words would read to Anchor as
its own prior output. The fix is to let ADK do what this code currently suppresses:
author foreign-crew turns under their own crew name and accept the "For context"
rewrite. That is precisely the attributed-quotation framing a handoff wants.
`crew_change` and `handoff_declined` rows replay as trusted server event text, not
as model-authored history.

**Not the "full thread."** Replay is capped at `MAX_SEED_TURNS = 20`
(`agent/adk_agent.py:1170`). The brief is what carries intent across that cap.

**Explore.** Its terminal sibling block is rewritten into a handoff block, with a
capability boundary so it routes on what is *needed* rather than on keywords:

| Operator wants | Crew | Because Explore lacks |
|---|---|---|
| undo an unsanctioned env change | Anchor | `drift_propose_rollback` |
| record a sanctioned change | Anchor | `drift_patch_docs` |
| bump or pin a dependency | Patch | `upgrade_propose_pr` |
| author new infra | Provision | `provision_open_infra_pr` |
| adopt an untracked resource | Provision | `provision_propose_adoption` |

Plus a restraint rule: **do not hand off for anything you can answer by
reading.** Explore sees every drift Anchor sees; "is this drifted?" is Explore's
own question. Only the *remedy* needs a handoff.

**Anchor / Patch / Provision.** Each gets a short reciprocal block: if the ask is
outside your domain, call `request_crew_handoff_tool`; never claim you will do it
yourself.

Constraints:

- Tools named by callable `__name__` throughout (`test_prompt_tool_names.py`).
- **Anchor's `chat_system_prompt.md` is byte-golden** — a two-file commit with
  `_DRIFT_CHAT_SYSTEM_PROMPT_GOLDEN` in `tests/unit/test_drift_workload_loads.py`.
- `workloads/drift/system_prompt.md` is **untouched**: the `/recheck` path is
  autonomous Eventarc Anchor, which has no operator to confirm anything.
- Tool docstrings and return strings reach the model, so the handoff tool's
  docstring is part of the routing prompt in practice and gets the same
  operator-register wording as the `.md` files.

## Tool classification

`request_crew_handoff` is **not** an external mutation tool: no PR, no rollback,
no notification, no write-capable credential. But it does write server state and
mint a transition credential, so claiming Explore's read-only test "keeps exactly
the same meaning" would be false. `MUTATION_TOOL_NAMES` is documented
(`agent/fanout.py:248`) as covering anything that mutates *or* rides a write
credential, and Explore's manifest currently promises it "cannot change anything"
(`workloads/explore/workload.yaml:37`).

Split the taxonomy so the escalation stays visible to reviewers:

- `EXTERNAL_MUTATION_TOOL_NAMES` — PRs, rollbacks, notifications, write-capable
  credentials. **Explore stays disjoint.**
- `CONTROL_PLANE_PROPOSAL_TOOL_NAMES = {"request_crew_handoff"}`.
- Assert Explore's control-plane intersection is *exactly* that singleton.
- Assert no redemption callable exists in `TOOL_REGISTRY` / `COORDINATOR_TOOLS` —
  the model can propose, never redeem.

Explore's manifest header prose must be updated to match; it currently overclaims.

## Registration pins

Adding one tool touches, in lockstep:

`COORDINATOR_TOOLS` · `TOOL_REGISTRY` · `TOOL_TIERS` (+ `test_tool_tiers.py`
exact pins) · all four `*_WORKLOAD_TOOL_NAMES` order tuples · all four YAML
manifests · `EXPECTED_TOOL_NAMES` · **`CHAT_ONLY_TOOL_NAMES`** · the fan-out
exclusion set.

Two of these are load-bearing:

- **`CHAT_ONLY_TOOL_NAMES`** — `build_agent` strips only names in that set
  (`agent/adk_agent.py:511`). Omit it and autonomous Anchor `/recheck` gets a
  handoff tool with no operator to confirm it.
- **`_FANOUT_EXCLUDED_READ_TOOL_NAMES`** (`agent/fanout.py:302`) — anything not in
  the mutation set is handed to Provision's fan-out slice agents, which run
  without the operator-facing guards.

No new `worker_names` entry: this is coordinator-local, and a fake worker would
trip URL resolution at `agent/workloads/registry.py:987`.

## Frontend

- `CrewPicker` comes out of `ChatForm`. New threads send `workload: "explore"`.
- `ConversationThread` renders every non-`user` role as a crew bubble
  (`ConversationThread.svelte:39`) — it needs explicit branches for
  `crew_change` and `handoff_declined`. Per-bubble crew identity via `CrewGlyph`
  and `crewName` already exists (`:55`), so that part is mostly done.
- The chip renders from persisted `pending_handoff`, so a reload or a
  `?conversation=` deep link restores it rather than stranding a dead nonce.
- **Decline posts too** (`accept: false`): burns the nonce, records a note the
  crew reads next turn. Without it the crew re-proposes every turn and no
  prompt-level restraint can stop it.
- **The crew catalog stays.** `CapabilityCard` and `GET /workloads/{name}/prompts`
  are untouched; judges still read all four real system prompts in-app. Only the
  composer gate is removed, not the crews' visibility.

Known consumers of the old shape:

| Site | Breaks how |
|---|---|
| `ConversationsRail.svelte:85` | `ceil(turn_count / 2)` misreports once transition rows count as turns — exclude them from `turn_count` |
| `frontend/src/lib/types.ts:135` | Types a conversation as having one workload |
| `frontend/src/locales/conversations.ts:9` | Help copy promises the thread stays with its starting crew |
| `tests/unit/test_crew_redirect_block.py:68` | Pins the terminal-routing wording being replaced |
| `CrewPicker.test.ts`, `ChatForm` crew-lock tests | Rewritten or removed |

### Adopt stays a second door

`App.svelte:323` starts a new **Provision** thread from the Adopt button, and
`ChatForm:63` applies both prefill text and prefill workload. Forcing every new
thread to Explore would turn one deliberate click into an Explore turn plus a
confirmation.

Adopt therefore remains a direct entry into Provision. This is not a picker — it
is a deep link carrying explicit intent from a specific resource, and it creates a
*new* conversation, so it never bypasses an existing lock. "Explore is the door
you walk through when you don't already know where you're going" is the honest
framing.

## Safety

Preserved unchanged: one crew per turn with manifest-scoped tools; Explore
disjoint from external mutation; the `/chat` 409; `/recheck` and Eventarc.

Blast radius of a mis-clicked chip:

| Crew | Worst auto-run first action | Floor |
|---|---|---|
| Anchor | `drift_propose_rollback` | No live change; still needs the HMAC redeem |
| Anchor | `drift_patch_docs` | A PR exists |
| Provision | `provision_open_infra_pr` / `provision_propose_adoption` | An `iac/`-only PR exists; apply stays gated |
| Patch | `upgrade_propose_pr` | A PR exists |
| Patch | `upgrade_merge_pr`, `upgrade_close_pr` | **Denylisted on the handoff turn, in code** |

Floor across every crew: *a GitHub PR exists*. Nothing merges, nothing applies,
nothing touches live infra.

**Prompt injection is unchanged in kind, wider in reach.** Operator-pasted log
text could steer a crew into proposing a handoff the operator did not intend. The
mitigation is the one already in the system: the chip names the concrete action
and a human reads it before it runs. Say this plainly in the pitch rather than
claiming it away.

## Demo path

`POST /chat/handoff` must join the CF worker `DEMO_ALLOWLIST` or anonymous judges
hit the token modal (`demo-allowlist-gap-pr208`).

**That is necessary but not sufficient.** The worker's rate limiter matches the
exact path `/chat` (`infra/cloudflare/worker/src/proxy.js:65,126`). Redemption
immediately starts a Gemini run, so the new endpoint is a second cost-amplification
route and must share the limiter — otherwise the anonymous-window budget rail has
a hole in it. The demo-anon marker must also reach the joining run so Provision's
demo restrictions and the current autonomy mode are re-evaluated at redemption
rather than inherited from the proposing turn.

This is the one respect in which this design differs from the composite plan,
which deliberately added no new endpoints.

## Tests

- New `test_chat_handoff.py`: nonce single-use; expiry; supersession burns the
  prior nonce; `pending.from` must equal current workload; wrong conversation
  rejected; decline burns; plain `/chat` with a different `workload` still 409s;
  merge/close denied on the handoff turn; concurrent `/chat` + redeem cannot
  interleave.
- `test_coordinator_tool_inventory.py`: the split taxonomy above.
- `test_tool_tiers.py`: new tier pin.
- `test_drift_workload_loads.py`: golden literal, same commit.
- `test_crew_redirect_block.py`: rewritten for handoff wording.
- `test_prompt_tool_names.py`: covers the new tool automatically.
- Frontend: chip render + deep-link restore + decline; transition row; rail count.

## Slices

Strictly ordered — slice 2 fails coordinator boot without slice 1.

1. **Backend** — capability, tool, request-scoped proposal sink, transactional
   persistence, run lease, `POST /chat/handoff`, first-turn denylist, tests. No
   UI. Ships dark.
2. **Prompts** — four crews' handoff blocks, Explore's terminal block rewritten,
   Explore manifest prose corrected, Anchor's golden literal.
3. **Frontend** — chip, transition rows, picker removal, rail count fix.

## Sequencing

Slices 1 and 2 run **in parallel with** the composite redesign — they are
invisible, touch no shared component, and cannot destabilize it.

Slice 3 merges **only after** the composite redesign's composer baseline lands,
and rebases its tests onto it then. Composite already rewrites `App.svelte` and
the composer; racing it there buys nothing.

The gate is the **video, not a date.** The deck must embed a demo video that
cannot be paused mid-playback, so whatever the video shows is what judges see on
8/19. The 8/3 freeze in the composite plan is a self-imposed backstop derived from
that (re-shoot 8/4–8/6, deck due 8/10 10:00 — the organizer's only hard date
besides the 8/19 pitch). If slice 3 is not clean when recording starts, record
without it and ship after the pitch. A half-migrated composer on camera re-proves
「後付け」 louder than the picker does.

## Deliberately deferred

- **Rail stacked-glyph trail.** Needs a denormalized `crew_history` field and an
  atomic update, because list responses carry no turns. Show the current crew;
  transitions are visible inside the thread.
- **Conversation schema versioning.** Not needed — treat missing
  `pending_handoff` / lease fields as old-schema defaults. (The run lease is a
  concurrency generation, which is a different thing.)

## Rejected from review

- **Dropping the model-authored `brief`.** The review argued it duplicates
  replayed history and adds an injection surface. But replay is capped at 20 turns
  and, once foreign-crew turns become attributed quotations, the brief is the one
  clean first-person statement of *what is being asked* — and it is what the chip
  text derives from. Kept, length-capped, rendered strictly as quoted data.
- **"Confirm joins, next operator turn runs."** Correctly identified as the single
  biggest simplification: it removes the denylist, the synthetic prompt, and the
  failure-after-flip path. But the immediate run *is* the feature — a join that
  then waits is the friction being removed. Documented here as the fallback if
  slice 3 runs out of runway.

## Open questions

- Chip placement: inline in the transcript, or docked above the composer? The
  composite redesign's chat frame decides this; defer to it.
- Nonce TTL — match the existing approval gate's rather than inventing a number.
- Is a declined handoff visible in the transcript, or silent? Visible is more
  honest about what the agent wanted; silent is calmer.
- What happens if the joining crew's first run *fails* after the workload has
  already flipped? Leaning: the flip stands, the failure renders as a normal
  error turn, and the operator retries by typing.
