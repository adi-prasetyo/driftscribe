# Rollback honesty: ds-d4z, ds-b3m, ds-uwc

Three P3 beads that all say the same thing from different angles: **the rollback
lane tells the operator less than it knows, and gates on less than it could.**

| Bead | Surface | Deploy |
|---|---|---|
| `ds-d4z` | `DecisionsRail` keeps a dead Approve CTA on a `used` approval | coordinator (SPA is in the image) |
| `ds-b3m` | the `/recheck` rollback gate reads the model's diff array, not live env | coordinator |
| `ds-uwc` | nothing anywhere compares the ROLLBACK TARGET's env to the contract | rollback worker + coordinator |

Three PRs. Revision 2 — the first draft was reviewed by Codex and **three of its
load-bearing claims were wrong**; what changed is recorded inline rather than
edited away, because two of the corrections are the same defect class this repo
keeps shipping.

---

## ds-d4z — the rail re-derives its own answer

### What is wrong

`DecisionsRail.svelte` resolved a rollback row's Approve CTA from
`approval.approval_url` alone and branched only on `isExpired`. It never read
`approval.status`. A `used` approval kept a live-looking Approve link until its
15-minute TTL ran out, and the click dead-ended at the rollback worker's refusal.

`approval.ts::isRollbackAwaitingOperator` already answers this, and
`desk.ts::selectPendingRollback` + `ledger.ts::classify` already share it. Its own
doc comment says it exists so *every other surface agrees instead of re-deriving*
— the rail was the one caller it had never been applied to. The same omission
also dropped the ds-mml rule: `status_unavailable` read as "live".

### The change (SHIPPED)

`rollbackCta(d)` returns `'live' | 'expired' | null`.

**Ordering the two predicates was not enough, and the first draft got this
wrong.** A `used` approval whose TTL has *also* passed fails
`isRollbackAwaitingOperator` for the right reason and then matches a bare
`isExpired` anyway — so it rendered "expired", a second wrong answer replacing
the first, and one that misreports a rollback the operator really did approve. My
tests covered only the unexpired forms of `used` / `denied` /
`status_unavailable`, so they passed. Caught by Codex.

The fix splits the STATUS half out of `isRollbackAwaitingOperator` as
`isRollbackApprovalUnresolved(approval)` — no clock — and the expired branch gates
on it too. Shared, not re-derived: re-deriving is the exact habit this bead exists
to end.

### Tests (12 new, all mutation-checked)

Both forms — unexpired AND expired — of `used`, `denied`, `status_unavailable`;
pending live; absent-status live; pending-expired badge; absent-status-expired
badge; off-origin; no approval. Plus 5 direct `isRollbackApprovalUnresolved` cases.

Mutation checks run: reverting to `approveHref`-only fails 3; dropping the status
gate from the expired branch fails 3.

---

## ds-b3m — a gate that reads the model's homework

### What is wrong

`agent/validator.py`'s rollback branch derives its deviation set by looping over
`proposal.env_diffs`, which the LLM authors, so it cannot detect **omission**. Its
own comment is already honest about this and names this bead as the fix.

### Correction 1: the gate is not the only door

`agent/adk_agent.py:531` hands the **/recheck** agent every non-`CHAT_ONLY` tool
that survives the autonomy tier filter — and `drift_propose_rollback` is tier
`propose`, so in prod's `propose_apply` mode the autonomous agent **holds
`propose_rollback_tool` and can mint an approval without `validate()` ever
running**. `workloads/drift/system_prompt.md:66` already tells it not to:

> The /recheck path only emits a DecisionProposal — do NOT call
> `propose_rollback_tool` ... the LLM only outputs the JSON decision and never
> mints approval tokens directly.

The prompt asserts a property the tool set contradicts, and
`test_adk_agent_autonomy.py:313` pins the contradiction. A gate that can be walked
around is not a gate, so **hardening the gate has to include closing the second
door**: `drift_propose_rollback` joins `CHAT_ONLY_TOOL_NAMES`. That set means
exactly this, the prompt already documents the intent, and no prompt edit is
needed — so no byte-golden churn. The autonomous rollback path does not use the
tool (it goes proposal → `validate()` → `_do_rollback` → worker `/propose`), so
nothing legitimate loses a capability.

`drift_patch_docs` is named in the same prompt sentence and has the same gap. NOT
fixed here — a bypassed docs PR mints no credential and changes no live state.
Filed separately.

### Correction 2: current-vs-contract is the wrong predicate for half the rule

The first draft derived BOTH questions from `(live_env, contract)`. Only one of
them can be:

| Question | Correct authority |
|---|---|
| Is there a hard violation to revert? | current vs contract |
| Would this rollback undo an operator's `allow_manual` change? | current vs **TARGET** |

Codex's counterexamples, both correct:

- current `FEATURE_NEW_CHECKOUT=true`, target `true` → the rollback **preserves**
  the operator's value, but a current-vs-contract check refuses. A false refusal
  — the exact ds-2f5 availability shape.
- current `false`, target `true` → the rollback **changes** it, and a
  current-vs-contract check passes.

Revision 2 note: I accepted this and then **wrote the rejected rule anyway** —
revision 1 still carried an `allow_manual` veto in the new layer, one paragraph
below the correction saying it did not belong there. Codex caught it a second
time. It is now gone, and ds-b3m's scope is exactly one question: *is there a
hard contract violation to revert?*

**The gap this leaves is stated, not papered over.** A model can still change the
outcome by OMITTING a deviating `allow_manual` diff, because the pre-existing
reported-diff loop rejects one that is reported. Grounding that rule here would
not fix it — the grounded predicate would still be the wrong one. It is pinned
as a known gap by `test_the_omitted_allow_manual_hole_is_NOT_closed_here`, and
closed properly by ds-uwc's source→target comparison.

### Correction 3: `live_env` is not complete ground truth

`driftscribe_lib/cloud_run.py:15` **skips Secret-Manager-backed entries**, and
flattens all containers last-one-wins. So a var missing from `live_env` is either
genuinely deleted or present-but-opaque.

Checked hard, because unknown-laundered-into-absent is this repo's recurring
defect. It does not bite in the direction that survives: **both readings are "not
observably at the declared value"**, and a var the contract pins to the literal
`"mock"` that now resolves through Secret Manager has departed from the declared
config as surely as one that was deleted. The ambiguity limits what we may
*claim*, not which way the verdict falls. It would have bitten in the veto
direction — which no longer exists here.

Single-container is assumed. Stated rather than silently relied on.

### The change, purely ADDITIVE

The existing per-diff loop stays **exactly as is**, including `seen_live` and the
undeclared-var rejection. A new live-env-grounded check runs after it. Two layers
with different jobs:

1. *(existing)* the proposal's reported evidence must be internally coherent;
2. *(new)* the authorization must be grounded in state we actually observed.

The first draft deleted `seen_live` and the undeclared-var rule, arguing
`env_diffs` had become "presentation only". **That is false**: `agent/main.py:1428`
passes `proposal.env_diffs` to `scrub_rationale_text`, which is what keeps a
secret out of the `reason` the rollback worker renders on the operator approval
page. The diffs are a security input and an audit record. Both checks stay.

`validate()` gains keyword-only `live_env: dict[str, str] | None`, defaulted to
`None`. Defaulted rather than required (contrast `autonomy_mode`) because
forgetting it can only ever **tighten** this gate — a call site that omits it
refuses rollbacks; it can never permit one.

### `live_env` provenance

`agent/main.py` has TWO sources (lines 1816-1832): the reader worker, and — on the
ADK path when that call fails — **a reconstruction from `proposal.env_diffs`**.
Passing that into a gate claiming to read ground truth is the ds-qua defect
verbatim. The call site now keeps them as separate variables: `live_env` (which
may be a reconstruction, and still feeds the idempotency key exactly as before)
and `observed_env` (`None` unless the reader really answered). `validate()` gets
`observed_env`.

Revision 1 justified the refusal by claiming a failed reader read means the
model's diffs were never grounded either. **Not true** — the model's tool call and
the coordinator's call are independent, so the first can succeed and the second
transiently fail. The refusal is still right; it bites on one-off blips and deploy
skew, not only sustained outages.

The reader response is shape-validated by `_observed_env_or_none`. A malformed
payload degrades to `None`, never to `{}` — an empty dict would make every
declared var read as "not at its contract value" and would **manufacture** the
violation that authorizes the rollback. That is a fail-OPEN degradation and it has
its own parametrized test.

### Known residual: the idempotency cache short-circuits the gate

`validate()` runs at `agent/main.py:1891`, *after* the cached-decision return at
`:1854`. So a rollback decision already cached under the same event key is served
without re-validating. Bounded, and left as-is:

- `_cached_rollback_is_expired` evicts a cached rollback past its 15-minute TTL,
  so the window is one TTL;
- the event key is derived from `live_env`, so a reconstruction and a real read
  produce *different* keys — a cache hit on the reconstruction path comes from a
  prior run that was equally ungrounded, not from a grounded one being reused;
- nothing executes without the operator's click either way.

Moving `validate()` ahead of the cache lookup would change the idempotency
semantics of every action, not just rollback. Not worth doing inside a P3.

### Demo-path check

Contract is 2 vars: `PAYMENT_MODE` (`mock`, disallow) and `FEATURE_NEW_CHECKOUT`
(`false`, allow). Injected drift is `PAYMENT_MODE=live`, `FEATURE_NEW_CHECKOUT` at
its contract value → 1 confirmed violation → **passes**. Pinned by an end-to-end
test using that exact env, because ds-2f5 is the standing reminder of what an
over-refusing gate costs.

### Tests (26 new, all mutation-checked)

Model omits the real violation but live env shows it → accepted. Model reports a
fabricated violation while live env is clean → rejected. `live_env=None` and
`live_env` omitted → rejected; every non-rollback action unaffected. Five
malformed reader payload shapes → rejected, never read as an empty env. The
known `allow_manual` omission gap → pinned as allowed, with the reason.
`drift_propose_rollback` absent from /recheck in all three dial modes, present on
chat. All 25 pre-existing rollback tests still green, each now carrying a
`live_env` that matches the scenario it describes — so a rejection test cannot
pass merely because the NEW layer refused, which would be a test proving nothing
about its own subject.

Mutations verified to fail tests: passing `live_env` instead of `observed_env`;
deleting the new gate; leaving `drift_propose_rollback` on /recheck; coercing a
malformed reader payload to `{}`.

## ds-uwc — nothing looks at the revision we are rolling back TO

### What is wrong

A rollback reverts the target revision's **entire** env, and nothing reads that
revision's config. The worker validates only existence + not-active
(`workers/rollback/main.py:468-486`); `agent/validator.py` reasons only about
current env, even after ds-b3m. The operator sees a revision NAME and nothing else.

### Correction 4: propose-time, not GET-time

The first draft computed the diff at approval-page GET time. Codex argued for
propose-time and is right, decisively on the second point:

- GET-time is **unbound presentation** — state can move between render and click,
  so the click can perform a different transition than the one shown.
- The reader's `previous_revisions` is a **capped (5), READY-only** candidate
  list. A valid approval target can be older than five or no longer READY, so the
  lookup would miss constantly and the page would say "unknown" most of the time.
- Re-reading live state on every anonymously reachable GET is a downstream-call
  amplification surface.
- A `used`/`denied` page would recompute against *today's* current revision —
  rewriting history.
- Fatally for the original plan: `read_live_env_tool` (`agent/adk_tools.py:76`)
  returns the **entire** reader response to the LLM, so a new
  `previous_revision_env` key would immediately reach the model and put historical
  env values in its context. The claimed "unchanged tool contract" was wrong, and
  the claimed reader-first deploy order was the unsafe one.

**Propose-time instead.** `workers/rollback/main.py::_list_revisions` already
iterates full `run_v2.Revision` protos, so source and target env are available
with no extra API call and no new IAM.

### The change

**1. `workers/rollback/main.py::/propose`** computes the source→target change set
and stores it on the approval doc as additive optional fields: `source_revision`,
`env_observed_at`, `env_change_names` (names only), plus disclosed values and the
target's contract-var values (below).

**2. Values are disclosed by an explicit allowlist, not a heuristic.** Codex is
right that `is_secret_name()` catches only conventional names and cannot see a
secret under an innocuous one — on an anonymously reachable page that is not good
enough. So `ProposeRequest` gains `disclose_env_names: list[str]`, which the
coordinator populates from `contract.expected_env` (both call sites: `_do_rollback`
and `propose_rollback_tool`). The worker stores values **only** for those names.
Contract-declared values are already public in `demo/ops-contract.yaml`; every
other var contributes its NAME and change kind and nothing else. Raw env never
lands in Firestore outside that allowlist.

**3. Target compliance scans every contract-declared var, not just changed ones.**
Another Codex catch: if source and target hold the *same* violating value it is
not in the change set, so a change-set-only scan misses the target's violation
entirely. The worker stores the target's value for every disclosed name.

**4. `agent/main.py::approval_get`** renders it, with the contract verdicts:
`violates` (target value ≠ contract on a `disallow_manual` var) and
`reverts_operator_change` (an `allow_manual` var whose value differs
source→target — the blast-radius question ds-b3m could not answer).

**5. `agent/templates/approval.html`** — a "What this rollback will change"
section, EN + JA, labeled with `source_revision` + `env_observed_at` so staleness
is visible rather than assumed away.

### Deploy skew

Either order is safe, which is why this design is better than the reader one.
`_drop_unknown_fields` (`driftscribe_lib/approvals.py:119`) means an old
coordinator ignores the new fields; a new coordinator reading an old doc (or one
written before the worker deployed) finds them absent and renders the
unknown-state note. The fields are display-only — never authoritative for
execution — which is exactly the scope that projection rule permits.

### Three things this must not get wrong

**Unknown is not empty.** Absent fields, an empty change set, a missing target
key, empty-string values vs absent keys, and malformed payloads must all be
distinguishable. An empty table that reads as "nothing will change" is the worst
possible outcome here. Never decide availability by truthiness.

**The page is anonymously reachable.** Hence the allowlist above. Tests pin that a
non-disclosed var's value appears nowhere in the response body, and that HTML
escaping holds.

**Always 200.** The docstring promises probe-safety, but `store.get()` at
`agent/main.py:4572` is **currently unguarded** — a Firestore failure 500s the page
today. Pre-existing; this change lands on that line and fixes it, with tests for
approval-store failure, contract-load failure, and malformed data. A
contract-unavailable state is labeled as such, never as "target configuration
unavailable".

### Criterion 3: flag, with an acknowledgment — not refuse, not a bare badge

**Decision: neither extreme.** A categorical refusal could leave an operator with
no valid rollback target mid-incident. A badge beside an unchanged live Approve
button is too weak for a target that provably violates the contract.

So: a contract-violating target renders a **required acknowledgment checkbox**
that the POST verifies. The break-glass lever stays; taking it becomes deliberate.

Dropping the first draft's "the human who is accountable for it" framing: this
page authenticates **possession of a token**, not operator identity, and is
anonymously reachable during demo windows. The acknowledgment is a speed bump on
an unattributed click, and the plan should not claim more than that.

### Deliberately out of scope, filed instead

- Binding `source_revision` at `/execute` and refusing when the active revision
  moved since the proposal. Real (the HMAC binds only approval id + target —
  `approvals.py:202` — and `/execute` never rechecks source state), but it can
  refuse a legitimate rollback whenever a second drift lands mid-window, which is
  exactly what the demo does. Needs its own design.
- `drift_patch_docs` escaping the /recheck gate the same way
  `drift_propose_rollback` does.
- `workloads/drift/system_prompt.md` is **stale and self-contradicting**: line 54
  tells the model to find a contract-compliant previous revision, line 62 says the
  reader returns no previous-revision list. It does — `previous_revisions` has
  shipped. Byte-golden, so it is a deliberate two-file edit, not a drive-by.

## Deploy order

1. **rollback worker** (`infra/cloudbuild.rollback.yaml` — verify it exists and
   whether it uses `--set-env-vars`, which REPLACES; the ds-wjw / #259 lesson).
2. **coordinator** (`infra/cloudbuild.coordinator-update.yaml`, `_NO_TRAFFIC`),
   digest-verify, smoke on an isolated tag, then `update-traffic`.

Order is not safety-critical here (see skew above), but writer-first means no
window where every approval page shows the unknown-state note.
