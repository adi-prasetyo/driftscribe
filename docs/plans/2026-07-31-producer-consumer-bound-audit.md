# ds-thm — bound every producer to the cap its consumer declares

Follow-on to ds-j0i (#278), which fixed this bug class on `/propose` after it
took autonomous self-heal down on prod. The class: **a value bounded at the
CONSUMER and unbounded at the PRODUCER.** It is invisible in review because the
two halves live in different deployables.

Two Codex rounds. Round 1 found a missing instance and an audit method that
defined its own completeness. Round 2 found that my replacement scope criterion
rested on a false belief about the runtime. Both corrections are recorded below
rather than quietly folded in, because the second one is the interesting part.

## 0. Scope — and the wrong turn I took getting here

**First criterion (wrong):** "can a producer send something the consumer
rejects?" Useless — under it every trust boundary is a bug, since refusing bad
input is what a trust boundary is *for*.

**Second criterion (right idea, false premise):** recoverability — a rejection
matters when ordinary operation triggers it and nothing in the loop can recover.
ds-j0i qualified because a 581-char rationale is ordinary Gemini output and the
autonomous lane has no model in it to read the 422 and retry.

I then measured recoverability with the wrong instrument. I grepped each tool
for an `except worker_client.WorkerClientError` handler and read *absence* as
"the worker's error propagates to the model as feedback" — pointing at
`WorkerClientError.__str__`, which does embed the worker's reason, and at three
tool docstrings that describe exactly that feedback loop.

**The opposite is true.** `Runner` is built with no `on_tool_error`
(`agent/adk_agent.py:1434`), so ADK re-raises an uncaught tool exception and
**aborts the turn**; `/chat` maps it to a 502. This repo already documents it,
in a test written for ds-q38:

> `read_live_env_tool` does not catch `WorkerClientError`, and ADK re-raises a
> tool exception when no `on_tool_error` callback handles it — `build_agent`
> installs none. So a Reader that is down during the agent's own read ABORTS
> the turn.
> — `tests/integration/test_eventarc_cache_coherence.py:1057`

So "no `except`" is not the feedback path, it is the *worst* path: the model
never receives a function response at all. The three docstrings promising a
"feedback loop" (`adk_tools.py:607`, `:866`, `:1271`) describe an intent the
runtime does not implement. I had read those docstrings as evidence and they
were the thing needing verification.

**Final criterion** — four tests, not one proxy:

1. Does the failure become a structured function response rather than an exception?
2. Is that response actionable and safe to expose?
3. Can a retry preserve the requested semantic action?
4. Is retrying allowed and side-effect-safe?

Diagnosis and recovery are separate axes — `upgrade_close_pr_tool` forwards the
worker's reason (diagnosable) but its docstring says "do not retry within the
same turn" (not recoverable). My previous draft contradicted itself on exactly
that case.

Measured against the real runtime:

| producer | on worker rejection | verdict |
|---|---|---|
| `_notify_rollback_approval` | suppressed, autonomous lane | in scope |
| `main.py:7045` merge alert | `contextlib.suppress`, silent | in scope |
| `notify_tool` | soft dict, status only, reason discarded | in scope |
| `propose_rollback_tool` | soft dict, fully laundered (ds-y5i) | in scope |
| `upgrade_close_pr_tool` | soft dict, forwards `e.body`; retry discouraged | in scope |
| `open_infra_pr_tool`, `propose_adoption_tool` | **turn aborts → 502** | in scope |
| `patch_docs_tool` | turn aborts | deferred — see §1b |
| `upgrade_propose_pr_tool` | turn aborts | deferred — see §1b |

## 1a. Audit — in scope

| consumer bound | producer | defect | fix |
|---|---|---|---|
| `notifier.NotifyRequest.body` `1..10000` | `_notify_rollback_approval(render_rollback_body(...))` | unbounded | 1 |
| ″ | `notify_tool(body=...)` model-authored | unbounded, can be empty | 2 |
| ″ | merge-failure alert interpolating `{e}` — `main.py:7045` | unbounded | 4 |
| `upgrade_docs.ClosePrRequest.reason` `1..1000` | `upgrade_close_pr_tool(reason=...)` | unbounded, can be empty | 3 |
| `rollback.ProposeRequest.target_revision` `1..64` + pattern | `propose_rollback_tool(target_revision=...)` | unvalidated | 5 |
| `iac_editor_policy`: title ≤200, body ≤20 000, 1–32 files, non-blank content | `_open_iac_pr_and_notify` (both authoring tools) | unvalidated | 6 |

### Fix 5 — the catch that matters

The autonomous lane validates the revision-name pattern before calling the
worker (`agent/validator.py:216`). The chat lane takes a bare `str` from the
model and does not. Worse, `propose_rollback_tool` is the **one** tool that
deliberately launders the worker's error (ds-y5i: `WorkerClientError.__str__`
embeds the response body, which a compromised worker could use to smuggle a
credential). So the model gets `"The rollback service refused the request"` with
no reason and cannot self-correct. Two independently correct safety decisions
compose into a lane where a bad revision name fails opaquely.

The value is also interpolated into `safe_reason` (`adk_tools.py:456`), so my
first draft's claim that `safe_reason` "derives from a bounded value" was false.

**Validate and refuse, never clamp.** Truncating an identifier is not a
degradation, it is a *different identifier* — a clipped name could match another
real revision and shift traffic somewhere nobody proposed.

**Validate syntax only — not membership in `previous_revisions`.** Tempting,
and wrong: the reader returns at most five candidates and degrades a listing
failure to `[]`, while the worker checks the authoritative revision list
(`workers/rollback/main.py:655`). A coordinator membership check would reject
valid older revisions the worker accepts, and would convert a best-effort
candidate-list outage into a rollback-proposal outage.

Instead, restore recoverability at the other end: map worker `400`/`404` onto
**coordinator-authored** messages ("that revision is currently active", "not
found — re-read the candidate list") keyed off the status code alone. Never
`e.body`. ds-y5i's laundering stays intact; the model gets a usable next step.

### Fix 6 — apply the validator we already share

`open_infra_pr_tool` and `propose_adoption_tool` both funnel through
`_open_iac_pr_and_notify`, which pre-validates nothing; a 201-char title or a
>20 000-char body aborts the turn with a 502. Meanwhile `agent/fanout.py:1063`
already calls `validate_file_writes` from
`driftscribe_lib/iac_editor_policy.py` before its editor call.

So this is not new validation — it is applying the **existing shared policy
module** on the path that skips it, exactly as the other authoring path already
does, and mapping `EditorPolicyError` to a structured refusal. That also serves
`_open_iac_pr_and_notify`'s own stated purpose: "the two authoring paths can
never drift."

**Never clamp file content.** Truncating HCL changes what infrastructure is
proposed. Refuse.

## 1b. Audit — out of scope, with honest reasons

**`patch_docs_tool`** (branch tail ≤200 derived from a model filename) and
**`upgrade_propose_pr_tool`** (branch tail from `package_name` +
`target_version`). Both abort the turn on rejection, so the feedback argument I
originally made for them is dead. They stay deferred on *concrete-target*
grounds instead: the docs path must first satisfy `^demo/docs/[^/]+\.md$` and
the supported target is `demo/docs/runbook.md`; the upgrade registry's only
target lockfile contains only `lodash`. Reaching either bound needs a ~190-char
filename or package name that also has to exist.

Recorded honestly: the general recovery story for upgrade *is* illusory — a
package name that genuinely exists cannot be shortened without naming a
different package, and the accepted semver regex permits an arbitrarily long
prerelease/build suffix. If the registry ever gains a real target set, these
come back.

**Trust-boundary inputs** — approval tokens arriving as URL query params. The
worker rejecting a tampered token *is* the security control. Out permanently.

**Our own workers' response fields** — IaC `pr_url`, tofu-apply `approval_id`
(checked non-empty, not UUID-shaped), and rollback `approval_url` / `expires_at`
which feed the chat notification body. The producer-side types genuinely do not
prove the bounds, but a malformed value here means our own worker is broken or
compromised: different threat model, different fix. Recorded, not fixed.

**Already correct, listed because "every field" must mean every field:**
`contract_hash` (≤64; always 16 from `agent/contract.py:75`), `contract_env`
(≤64 keys; ds-j0i omits the whole preview above 64 rather than truncating),
`propose_adoption_tool`'s rendered file (deterministic, recipe-bounded), and
`tofu_apply.ProposeRequest.approver` (≤320) — the last is the house pattern done
right: `canonical_operator_email` fails closed above 320 with the comment "the
RFC forward-path max", matching the worker exactly. `agent/fanout.py` is clean.

## 2. Why this is not theoretical

`tests/integration/test_notify_preserves_approval_url.py` already **documents
the 10000 boundary and then works around it**:

```python
_MAX_REALISTIC_RATIONALE = 8000   # "the worst case the coordinator can actually mint"

def test_the_rendered_body_still_fits_the_notifier_schema():
    assert len(body) <= 10000
```

Nothing enforces 8000 — it is an assumption about how verbose the model feels
today. The `10000` is a hand-copied literal, not read from the notifier's
schema. The suite asserts the invariant *for a fixture it chose to keep under
the limit*: primitive proven, seam assumed.

A second instance found by this audit: `agent/validator.py:16` mirrors the
worker's revision regex under a comment saying "update this constant in
lockstep" — and **no test pins them equal**.

Severity: fix 1 is advisory (ds-hdt), 2–4 soft-fail, 5 and 6 abort or opaquely
fail an operator-requested action. None is a P0. The reason to fix them is that
the class is proven to reach production and each fix is small.

## 3. Design

### 3a. Shared arithmetic, separate policy wrappers

One policy-free clamp is not enough, for a sharper reason than "different
fields": **the notifier does not repair the payload the coordinator would
damage.** `_discord_safe_content` repairs orphaned fences only on the `content`
field; the handler sets `text` to the full uncapped body, deliberately — *"Only
`content` is capped. `text` deliberately carries the FULL body … so a
non-Discord receiver loses nothing to a limit that isn't theirs."* A
coordinator-side cut that orphans a fence reaches Slack and generic receivers as
malformed Markdown with no repair anywhere.

- `clamp_middle_out(text, cap)` — pure arithmetic, no policy. Must define
  `cap <= 0` and cap-smaller-than-marker behavior: the existing fallback would
  slice from the *end* on a negative cap (Python negative indexing).
  **Returns head and tail as separate fragments**, not a joined string — a
  Markdown wrapper cannot recover the boundary afterwards, especially if model
  text contains the marker itself.
- `normalize_rollback_reason` — plain text, empty → `ROLLBACK_REASON_ABSENT`.
  Rewritten over the helper with **byte-identical output**: characterization
  tests are written and passing *before* the refactor. This is
  production-proven code and abstraction is not worth behavioral drift.
- `normalize_notifier_body` — clamp, then **neutralize** every code delimiter
  in the retained fragments (backtick runs of any length, tilde runs of three
  or more). Guarantees `len(result) <= cap` and that the approval URL is not
  inside code. It needs no repair reserve *because* neutralizing only removes
  characters — see §5c for the four repair designs that came first and why
  each was wrong.
- `normalize_close_reason` — plain text, empty → disclosing fallback.

Fix 1 clamps the **assembled body**, not the rationale. Bounding rationale and
evidence table separately would keep the template provably intact but needs its
own budget split, and the whole path is a last resort (2000-char cap, 581
observed). "The fix can be riskier than the bug" applies.

The fence handling stays **duplicated** across the deploy boundary rather than
hoisted into `driftscribe_lib`, which is split coordinator/worker and would
force deploying both (prior incident: the infra-drift worker-skew canary). This
is the house pattern, stated at `agent/validator.py:11`: mirror the constant,
never take a build-time dependency on a worker. Test-time imports are fine and
are what pins the mirrors. The two copies also now differ deliberately — the
Notifier *repairs* fences for Discord (it can, since it is cutting to 2000 with
a fixed tail budget), while the coordinator *neutralizes* them.

### 3b. Constants pinned to the consumer — both directions

```python
NOTIFIER_BODY_MAX_CHARS = 10000        # mirrors workers/notifier
UPGRADE_CLOSE_REASON_MAX_CHARS = 1000  # mirrors workers/upgrade_docs
```

My draft claimed validating a captured payload against the real pydantic model
is "strictly stronger" than comparing integers. **Wrong** — a coordinator
constant of 9000 against a worker cap of 10000 passes model validation happily.
Both tests are needed, proving different things:

- **introspect** the consumer model's declared `min_length`/`max_length` and
  assert equality with the mirrored constant → catches drift either direction
  (pin the minimum too: it documents that the empty-input policy exists because
  of a real consumer constraint);
- **validate** captured seam payloads with the consumer's real model.

Extended to `agent/validator._REVISION_NAME` vs the worker's, which today is
mirrored by comment alone.

### 3c. Producer fixes

1. `_notify_rollback_approval` — `normalize_notifier_body`. Extend
   `test_notify_preserves_approval_url.py` past its 8000 ceiling; replace the
   hardcoded `10000` with the notifier's own constant.
2. `notify_tool` — clamp; empty → soft `{"delivered": False, "error": ...}`,
   **not** fabricated text: a notification's body is its entire substance.
3. `upgrade_close_pr_tool` — clamp; empty → fallback that **discloses the
   omission**. Closing the PR is the operator's requested action and the reason
   is auxiliary audit context: preserve the action, never invent a motive.
4. `main.py:7045` — clamp the assembled alert ("apply SUCCEEDED but merge
   failed" — the one an operator most needs delivered).
5. `propose_rollback_tool` — local syntax validation + refusal; 400/404 mapped
   to coordinator-authored messages (§1a).
6. `_open_iac_pr_and_notify` — apply `iac_editor_policy` validators, refuse
   structurally (§1a).

### 3d. Seam tests

ds-j0i's lesson: normalizer, schema and call site can each be correct while the
wiring between them is not — unwiring the normalizer left all 78 unit tests
green. Each fix gets a test driving the **real producer** with
`worker_client.call` patched, capturing the payload and validating it against
the consumer's real model. Each is verified by unwiring the fix and confirming
the seam test goes red while unit tests stay green.

## 4. Deploy

Every change clamps, validates or refuses at the **producer**; no consumer bound
moves. Coordinator-only, no ordering constraint against any worker — the inverse
of ds-j0i.

One caveat: fix 7 touches `frontend/src/styles/base.css`, which is a **built**
asset, so the frontend build is part of the gates and of the image. Still one
deploy, not a Python/template-only one.

## 5. The approval-page whitespace fix (fix 7)

ds-j0i deferred structured `reason_truncated` / `reason_original_chars` fields
plus a worker-rendered banner. The premise was wrong: the rollback worker only
*stores* `req.reason` (`workers/rollback/main.py:686`); **the coordinator renders
the page** (`agent/main.py:5648` → `agent/templates/approval.html:50`). No schema
change, no `extra="forbid"` coupling.

Minimal fix: wrap the reason in a dedicated span with `white-space: pre-wrap`.
Constraints discovered while checking this:

- The rule lives in the built stylesheet under a `ds-*` class, enforced by
  `test_approval_pages_link_shared_css_no_inline_style`. **Correction from an
  earlier draft of this plan:** I wrote that a CSP would block an inline style
  here. It would not — `approval.html` gets only
  `_apply_approval_security_headers` (no CSP); `style-src 'self'` with no
  `unsafe-inline` applies to the *IaC* approval page via `_apply_iac_csp`. The
  no-inline-style rule is a convention shared by both templates so the page
  that does have the CSP keeps satisfying it.
- `.ds-field` is `display: flex; flex-wrap: wrap`, so the span needs
  `flex: 1 1 0; min-width: 0` to wrap instead of overflowing.
- Keep `{{ approval.reason }}` immediately inside the element — with
  `pre-wrap`, Jinja's own template indentation would otherwise render as
  visible leading whitespace.

Deliberately **not** doing: a truncation-detection sniff on our own marker.
Matching it in model-authored text is a guess, and a banner that fires because a
rationale quoted the marker is worse than no banner. The structured-fields half
stays deferred.

## 5b. Known residual found while building this — `OWN_URL` import races

Worker modules read config at import and fail closed, so every test file that
imports one sets env at module scope with `setdefault`. That makes the value
first-import-wins for the whole pytest process, and it is not hypothetical: this
change broke `workers/rollback/tests/test_rollback.py::test_propose_happy_path`
in the full-suite run only, because a new file's `COORDINATOR_URL` won the race
against the value that test asserts on. Fixed by aligning every fixture value
with the canonical one each worker's own tests use.

`OWN_URL` cannot be fixed the same way: three workers pin three different values
(`rollback.` / `notifier.` / `upgrade-docs.example.com`) and only one can win.
It is harmless today because `OWN_URL` feeds `verify_caller`'s audience check,
which the suites override — but it is latent, it predates this change, and the
next test that asserts on an audience will hit it. Filed rather than fixed:
correcting it means changing how every worker test file bootstraps its env,
which is a much larger change than this bead.

## 5c. Codex round 3 — three defects in the fix itself

Reviewed after implementation. All three were mine, all three verified before
fixing, and all three are the same shape as ds-q38's lesson: the fix carrying
its own bug.

1. **Near-cap premature truncation.** `clamp_middle_out` tested
   `len(text) <= cap - reserve`, so bodies of 9993–10000 characters were cut
   even though the worker accepts them. The reserve pays for repairs a *cut*
   makes; it has no bearing on whether to cut. Now tested against `cap`.
2. **The fence repair was wrong — three times.** This one took four attempts
   and is the most useful thing in this bead:

   1. *Append ``` ``` ``` to a head with an odd delimiter count.* CommonMark
      closes a fence only with a run at least as long as the opener, so a four-
      or six-backtick block is not closed by three.
   2. *Drop the last unmatched run instead.* Still wrong: it decides which runs
      are unmatched by **parity**, and parity cannot see length. Codex's
      counterexample — a 12-backtick opener followed by a too-short
      three-backtick line — counts as "even", so the block stays open through
      the URL. Reproduced against `markdown-it`: zero link tokens.
   3. *Prepend an opener to a tail with an odd count.* Wrong in the other
      direction: an inline ``` ``` ``` in ordinary prose is not a fence, so the
      prepended opener never closes and the "repair" **creates** the failure.

   Each delivers the notification while silently disabling the one thing it
   exists to deliver — strictly worse than the 422 it replaces.

   4. *Delete backtick runs of three or more.* Right idea, **incomplete
      alphabet**: CommonMark has two fence characters, so a `~~~yaml` block
      whose closer fell in the omitted middle still swallowed the URL —
      reproduced through the real rollback renderer. Backtick runs of one and
      two were also left live on the argument that the marker's blank lines
      stop a span crossing from the head. That argument is true and beside the
      point: it says nothing about two delimiters that both survive *inside*
      the tail.

   **Shipped: no inference, and no partial alphabet.** Both retained fragments
   have every code delimiter deleted — backtick runs of any length, tilde runs
   of three or more (`_neutralize_fences`). Nothing can be left open if nothing
   can open; correct for every arrangement; only ever shrinks, so the cut needs
   no repair reserve at all. The cost is that a *truncated* notification loses
   code formatting in the fragments that survive — a body on that path has
   already lost its middle, and monospace is worth far less than a clickable
   link. A body that fits is untouched. This is what the Notifier's own comment
   recommended as the fallback: "neutralize backticks in the retained fragments
   instead of deepening the inference."

   5. *Delete both alphabets in one alternation.* Wrong because `re.sub` never
      rescans its own output: removing a backtick can **join** tilde fragments
      into a fence that did not exist. `"~~`~"` → `"~~~"` — the neutralizer
      manufacturing the delimiter it exists to remove. Fixed with two ordered
      passes (backticks, then tildes), which is provably sufficient rather than
      merely likelier to work: pass 1 leaves no backtick for pass 2 to
      re-create, and a tilde *run* is bounded by non-tildes, so deleting one
      cannot merge two others.

   6. *Delete only CODE delimiters.* Wrong because removing a fence
      **activates** what was inert inside it. An `<!--` in a fenced block whose
      `-->` and closing fence both fall in the cut becomes a live comment and
      eats the URL. "Nothing can open if no code delimiter survives" was true
      only of *code*. Closed by `_HTML_SWALLOWER`, applied after the fences are
      gone — and it is not another guessed alphabet: CommonMark defines exactly
      seven HTML block start conditions, and this is precisely the subset whose
      end condition is a specific terminator rather than a blank line (types
      1–5). Types 6 and 7 end at a blank line, which the marker supplies.

   **The test fixtures were the deeper problem.** Every fence fixture in the
   first four rounds opened a block and never closed it — so the URL was inside
   code in the **original** too, and the test could not have shown anything
   about truncation either way. They passed or failed for reasons unrelated to
   the code under test.

   Fixed structurally: `_assert_link_survives` now asserts the fixture renders a
   link *before* truncation, and every fixture is a balanced document whose
   closer is placed where the cut deletes it — which is the actual mechanism.
   Two more fixtures were thrown out on the same grounds after `.enable(
   "linkify")` turned out to be a silent no-op (`linkify-it-py` is not
   installed), so bare URLs never link at all; the renderer emits `<...>`
   autolink form, and the fixtures now match it.

   Two further coverage gaps Codex caught in the rebuilt fixtures: `mid` was
   being placed in the deleted region (so the 12-vs-3-backtick case never had
   both runs in the head), and the "run split by the cut" fixture used an
   8-backtick run that sat entirely inside the head and never split. Both now
   assert their own geometry.

   The rebuilt suite discriminates **every** prior implementation: 58, 11, 8, 2
   and 6 failures for attempts 1–4 and 6 respectively, and 82 green for the
   shipped one. Before the rebuild, three of the four then-existing attempts
   passed.

   7. *Escape the swallowers to `&lt;`.* Wrong because it replaced one
      character with four, so neutralization stopped being shrink-only and the
      output could exceed the cap: `"<!--" * 3000` came out at 17 460 against
      10 000. **This bead's own bug class, recreated inside its own fix.**

   **DESIGN CHANGED — the string surgery is gone.** Seven wrong attempts is not
   bad luck; "cut arbitrary Markdown and guarantee one element still renders"
   is an open-ended sanitization problem, and attempt 7 proved it could
   reintroduce the very defect being fixed. So the budget is now spent **before
   assembly**: `render_rollback_body(..., max_chars=...)` fits the rationale and
   the evidence table into whatever the fixed template leaves over, measured
   against the real template rather than an estimate. The template — approval
   URL, expiry note, traffic warning — is never cut and never sanitized.

   The evidence table is bounded by **dropping whole rows**, never by a
   character cut: half a row is malformed Markdown that degrades the table to
   prose, which on an approval page reads as though the evidence were something
   other than a table.

   Neutralization survives, but only over the truncated *rationale*, and only
   because a stranded fence there can still swallow the link below it. Its
   scope is now one paragraph of prose instead of an assembled document.

   **The claims are tested separately**, which is the clearest way to see what
   each mechanism actually buys. Disable `_neutralize_fences` entirely and:

   - the ten *structural* assertions still pass (footer text present verbatim,
     cap held, `NotifyRequest` valid) — that is the budget-before-assembly fix;
   - the ten *at-least-one-link-renders* assertions still pass — that is the
     leading link;
   - only the six *footer-link-also-renders* assertions fail — which is all
     neutralization is now responsible for.

   An earlier draft of this section claimed disabling the neutralizer broke
   seven rendering assertions. That was true when the only link was in the
   footer and became false the moment the leading link landed; Codex caught it
   by replacing the neutralizer with an identity function. Recorded rather than
   silently corrected, because a test or a doc that describes a guarantee it no
   longer provides is exactly how six broken repairs stayed green.

   8. *Bound the sections, but leave the link only in the footer.* Incomplete,
      and it exposed a defect **older than this bead**: neutralization runs
      only when the rationale is TRUNCATED, so a model emitting an unclosed
      ``` in a 900-character rationale broke the approval link in every
      notification — bounded or not, and long before ds-thm. Budgeting cannot
      help; the body was never over the cap.

      Fixed structurally, per Codex: an approval link now appears **above** the
      model's rationale. Markdown parses forwards, so a link already rendered
      cannot be captured by anything written below it. That cures the old bug
      and demotes neutralization from safety-critical to a readability nicety.
      The footer link stays — it carries the expiry and traffic warning.

   9. *Emit the pathological fallback unchecked.* The "template does not fit"
      branch returned a body without measuring it: 172 chars against a cap of
      100, and 20 173 against 10 000 for an absurd URL. Now every branch is
      measured, degrading to a link-only body and finally to a hard bound.
      It deliberately does **not** raise: this runs after the approval is
      minted and before the decision row is written, so an exception would
      strand the approval for real (ds-hdt).

  10. *Fall back by slicing the assembled body.* The "template does not fit"
      chain went straight to a head slice, which preserved the `## DriftScribe`
      heading and dropped the URL — so a cap that could physically hold the
      whole autolink still produced a notification with nothing to click.
      There is now an explicit bare-autolink tier before the unavoidable
      hard slice: prose is not worth a link.

  11. *Assume the approval URL is absolute.* `_approval_url_matches`
      deliberately accepts the relative `/approvals/{id}?t=…` form (a worker
      whose `COORDINATOR_URL` has drifted would otherwise lose rollbacks
      entirely), but a CommonMark autolink requires an absolute URI —
      `</approvals/…>` renders as inert text. The shapes the validator admits
      are wider than the shapes the renderer can make clickable: this bead's
      mismatch again, in URL *shape* rather than length, and independent of
      truncation. Canonicalized against the coordinator's own configured origin
      rather than rejected, because rejecting after the approval is minted
      would strand it (ds-hdt).

   **What this cost.** Eleven wrong implementations and two rounds of invalid
   fixtures, on a path that fires only when a model writes a ~9400-character
   rationale. Without any clamp that path is a guaranteed 422 and *no*
   notification, so every version was an improvement on the status quo — but
   five of them would have shipped a notification whose link silently did not
   work, which is the worse failure because it looks like success.

   The test oracle is now **`markdown-it`**, declared as a dev dependency. Each
   broken repair had shipped with a hand-rolled assertion that shared its blind
   spot — a parity check cannot detect a parity bug — so the oracle must not be
   ours. Re-injecting attempts 1 and 2 fails 51 and 4 tests respectively.
3. **The guard accepted exactly what the worker rejects.** `re.match` with a
   `$`-anchored pattern also matches just before a *final newline*, so
   `"payment-demo-00024-f6v\n"` passed the coordinator and was rejected by
   pydantic-core (Rust regex, `$` = strict end-of-text) with
   `string_pattern_mismatch` — straight back onto the opaque-422 path fix 5
   exists to remove. **ds-j0i reproduced inside the fix for ds-j0i.** Both
   lanes now use `fullmatch`, including `agent/validator.py`, where the same
   defect predates this bead and would have hit the autonomous lane with no
   model in the loop to recover.

A differential test now asserts the coordinator's guard and the worker's real
model reach the **same** verdict on every candidate — testing each side alone
cannot catch a disagreement, and a disagreement is the whole bug class.

Also corrected: an inline style on `approval.html` would **not** be blocked by
CSP (see §5), and the stale "the worker's 403/422 reaches the model as a
feedback loop" claims in `open_infra_pr_tool` and `patch_docs_tool`.

## 6. Gates

`uv run --extra dev ruff check .` · `uv run --extra dev pytest -q tests/ workers/`
· `cd frontend && npm run test:unit -- --run && npm run check && npm run build`
