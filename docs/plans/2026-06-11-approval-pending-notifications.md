# Pending-Approval Notifications (ClickOps Wave 2, item 7)

> **For Claude:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.
> One implementer task (size S), two-stage review, final review before PR.

**Goal:** When a change starts waiting on a human, the human finds out —
in their chat tool, not by polling a dashboard. Two notification gaps are
closed: (A) an agent-authored IaC PR awaiting `/iac-approvals/{N}` review,
and (B) a chat-proposed rollback approval. Both reuse the existing
`notifier` worker verbatim — no worker changes, no new secrets, no new env.

**Audience anxiety served (roadmap item 7, anxiety B):** safety machinery
that fires silently doesn't reduce fear. Operators live in chat tools; an
approval that sits invisible until someone happens to open the SPA reads
as "the AI did something and nobody was told."

**Architecture:** best-effort `worker_client.call("notifier", …)`
side-effects at the coordinator's un-notified "approval became pending"
chokepoints — a shared helper in `agent/adk_tools.py` wired into
`open_infra_pr_tool`, the D5 multi-slice orchestrator in
`agent/fanout.py`, and `propose_rollback_tool`. Everything else already
exists.

---

## Grounding facts (verified 2026-06-11 against `main` @ d0686cc)

1. **The notifier worker (`workers/notifier/main.py`) needs NO changes.**
   `POST /notify` takes `{channel, severity, body}` with `channel` Literal
   already including `"approval"`, severity Literal `low|medium|high|critical`,
   body 1–10000 chars, `extra="forbid"`. The webhook URL is the worker's
   boot-time secret (`driftscribe-webhook-url`); callers cannot influence
   it. Auth = `verify_caller` audience+allowlist (coordinator SA already
   allowed — the rollback flow uses this path in prod today).
2. **Prod env is already wired:** the live coordinator revision carries
   `NOTIFIER_URL=https://driftscribe-notifier-….a.run.app` AND
   `COORDINATOR_ORIGIN=https://driftscribe.adp-app.com`
   (`settings.coordinator_origin`, used by the iac-approval POST Origin
   check). No deploy-time config change needed.
3. **What ALREADY notifies (do not duplicate):**
   - Autonomous rollback proposals (drift recheck / eventarc):
     `agent/main.py` ~962–982 sends
     `{channel: "approval", severity: "high", body: render_rollback_body(...)}`
     — and there the notify is LOAD-BEARING: failure releases the event
     claim and 502s, because the webhook is the ONLY surface that carries
     the approval URL to the operator in that flow.
   - IaC apply failures/ambiguous outcomes: several
     `contextlib.suppress(Exception)`-wrapped notifier calls in the
     iac-approval POST orchestration (~3207/3252/3296/3412/3738).
4. **Gap A — IaC pending: TWO call sites, not one (Codex plan-review
   must-fix — the original single-chokepoint claim was FALSE).** The
   single-agent path goes through `open_infra_pr_tool`
   (`agent/adk_tools.py` ~587; the fanout registry's
   `provision_open_infra_pr` symbol also resolves to this callable for
   the single-slice fallback). But the D5 MULTI-SLICE fan-out
   orchestrator opens its convergent PR by calling
   `worker_client.call_open_infra_pr` DIRECTLY
   (`agent/fanout.py` ~1269, via `asyncio.to_thread`) and then gates on
   `iac_pr_pointer(result)` itself (~1301). Both sites already share
   `iac_pr_pointer` (adk_tools ~560: positive non-bool int `pr_number` +
   non-empty str `pr_url`) and `iac_pr_next_steps` — the notification
   must be a third SHARED helper called from both, after the pointer
   confirms. NOTHING notifies at this moment today; the operator only
   learns about the pending PR if they are watching the chat stream or
   the rail. Note the C2 plan-builder is **maintainer-dispatched**
   (`workflow_dispatch` in `.github/workflows/iac.yml`) — the approval
   page shows "No verifiable C2 plan artifact" until someone dispatches
   it, so the notification copy must not imply automatic plan readiness.
5. **Gap B — chat-proposed rollback:** `propose_rollback_tool`
   (`agent/adk_tools.py` ~72) calls the rollback worker, which mints the
   approval and returns `{approval_id, approval_token, approval_url,
   expires_at, …}` (15-min TTL). The tool returns this to the LLM (the
   operator sees `approval_url` in the chat reply) but NOTHING webhooks.
   SECURITY stance already in place: the model-authored `reason` is NOT
   forwarded to the worker (a safe `safe_reason` derived from
   `target_revision` is sent instead) because the model sees live env
   unredacted — any new notification body must honor the same rule.
6. **`agent/adk_tools.py` already imports** `worker_client`,
   `get_settings`; `contextlib` is NOT yet imported there (add it).
   Existing unit tests: `tests/unit/test_adk_tools.py` (fixture style to
   reuse: monkeypatching `agent.worker_client.call` /
   `call_open_infra_pr` seams).
7. **Pause/dry-run doctrine (item 5):** in-flight turns complete tools
   already reached; `propose_rollback_tool` deliberately runs its worker
   calls under dry-run too ("so the demo can show the approval URL").
   The PR / approval these notifications describe is REAL in both modes
   — no pause or dry-run gate on the notify.

---

## Settled decisions

### Decision 1 — two tool-level notifies, both BEST-EFFORT (deliberate contrast with the autonomous path)

Both new sites wrap the notifier call in `contextlib.suppress(Exception)`
plus a WARNING log (`iac_pending_notify_failed` /
`rollback_propose_notify_failed`, each carrying identifying extras but
never the body). Rationale, documented in code: unlike the autonomous
rollback flow (grounding fact 3) where the webhook is the only surface,
BOTH these flows already show the link to the operator in the chat
reply/CTA — failing the tool (and with it the operator's chat turn) to
protect an advisory side-channel would invert the priorities. At-least-
once is NOT promised here; at-most-once per confirmed PR/approval is.

No pause gate, no dry-run gate (grounding fact 7).

### Decision 2 — Gap A: ONE shared helper, called from BOTH authoring sites

New public helper in `agent/adk_tools.py` (next to its siblings
`iac_pr_pointer` / `iac_pr_next_steps`, which the same two sites already
share): `notify_iac_pr_pending(pr_number: int, pr_url: str, title: str)
-> None` — best-effort per Decision 1, builds the body and calls the
notifier. Call sites:

1. `open_infra_pr_tool`: after the compact result dict is built, gate on
   `iac_pr_pointer(compact_result) is not None` — the exact "confirmed
   opened PR" predicate the CTA uses, so a malformed/unconfirmed worker
   response never notifies (the two surfaces agree by construction).
2. `agent/fanout.py` multi-slice orchestrator: immediately after the
   existing `iac_pr is None` fail-closed return (~1316), i.e. only for a
   CONFIRMED pointer. The orchestrator is async and `worker_client.call`
   is sync httpx — invoke via `await asyncio.to_thread(notify_iac_pr_pending, …)`
   exactly like the surrounding worker calls (the helper itself never
   raises, so no extra error handling at the call site).

- `channel="approval"`, **`severity="medium"`** — a pending review with
  no TTL is calmer than the 15-min-TTL rollback (high) and the failed
  applies (high). First use of "medium"; the Literal already allows it.
- Body (exact template; `_NOTIFY_TITLE_CAP = 200` clamp on the
  LLM-authored title — it is already public on GitHub/the rail, the clamp
  is hygiene for the 10k body cap, suffix `…` when clamped). The copy is
  HONEST about C2 being maintainer-dispatched (Codex should-fix — "the
  plan may take a minute" would overstate readiness):

  ```python
  approve_url = (
      f"{s.coordinator_origin}/iac-approvals/{pr_number}"
      if s.coordinator_origin
      else f"/iac-approvals/{pr_number}"
  )
  body = (
      f"Infrastructure change awaiting review: {clamped_title!r} "
      f"(PR #{pr_number}). Next: dispatch the C2 plan-builder for "
      f"PR #{pr_number}, then review & approve: {approve_url}. "
      f"GitHub: {pr_url}"
  )
  ```

  The relative-path fallback (empty `coordinator_origin`) is dev-only and
  documented as such in a comment; prod carries the origin (grounding 2).
- Each confirmed `open_infra_pr` result notifies once — if a turn opens
  two PRs (model retry after a 422), each real PR gets its own
  notification, which is correct (each is genuinely pending).

### Decision 3 — Gap B: notify inside `propose_rollback_tool` after worker success

Insertion: after `worker_client.call("rollback", …)` returns (an
exception already propagates to the model — no approval, no notify).

- `channel="approval"`, `severity="high"` (15-min TTL urgency — parity
  with the autonomous rollback notify).
- Body built ONLY from `target_revision` (caller arg, already validated
  vocabulary: a Cloud Run revision name) and the worker-returned
  `approval_url` / `expires_at` — the model-authored `reason` NEVER
  appears (grounding fact 5; pinned by a test asserting the reason
  string is absent from the notified body):

  ```python
  body = (
      f"Rollback approval pending: roll back {s.target_service} to "
      f"{target_revision}. Approve or deny (expires {expires_at}): "
      f"{approval_url}"
  )
  ```

  `approval_url`/`expires_at` are read defensively: a value counts ONLY
  when `isinstance(v, str) and v` — never `str()`-coerce (a careless
  `str(None)` would interpolate the literal `"None"`, Codex nit). If
  EITHER is missing/non-str/empty the notify is SKIPPED entirely with the
  same WARNING log — never send a body with a placeholder hole, and the
  coordinator must not crash on a malformed worker response it previously
  tolerated.
- Yes, `approval_url` embeds the single-use token (`?t=…`): sending it to
  the webhook is the ESTABLISHED model (the autonomous path has done
  exactly this since Phase 13 — the webhook URL is itself the capability).

### Decision 4 — out of scope

- Plan-ready (C2 artifact posted) notifications — that moment is
  GitHub-Actions-side, out-of-band by design; the PR-open notification
  instead tells the operator the next step is dispatching the C2
  plan-builder.
- Upgrade-workload PRs, docs PRs (different lifecycle, no approval page).
- Notification preferences/routing/digest (the notifier is single-channel
  by design — its URL is the capability).
- SPA/UI changes: none. Worker changes: none. New tests only at the
  coordinator tool layer.
- De-duplication beyond at-most-once-per-call (no Firestore ledger of
  sent notifications — YAGNI for a single-operator deployment).

---

## Task 1 (the only task) — shared helper + three call sites + tests (implementer: Sonnet 4.6)

**Files:**
- Modify: `agent/adk_tools.py` (helper + `open_infra_pr_tool` +
  `propose_rollback_tool`)
- Modify: `agent/fanout.py` (multi-slice call site)
- Test: `tests/unit/test_adk_tools.py`, plus the fanout streaming test
  file that covers the multi-slice success path (find it — likely
  `tests/unit/test_fanout_parallel_author.py` or a sibling; reuse its
  existing orchestrator fixtures)

**Step 1 — failing tests.** Read the existing file's fixture idioms first
(how `worker_client.call` / `call_open_infra_pr` are monkeypatched) and
reuse them. Required tests:

`open_infra_pr_tool` notify:
- confirmed PR → exactly ONE `worker_client.call("notifier", …)` with
  `channel="approval"`, `severity="medium"`, body containing
  `/iac-approvals/<N>`, the (clamped) title, the GitHub `pr_url`, and the
  honest "dispatch the C2 plan-builder" instruction.
- `coordinator_origin` set → body contains the ABSOLUTE
  `https://…/iac-approvals/<N>`; empty → relative path (both pinned).
- title longer than 200 chars → clamped with `…` in the body; body stays
  ≤ notifier cap comfortably.
- unconfirmed results (each of: missing `pr_number`, bool `pr_number`,
  `0`, missing/empty `pr_url`) → ZERO notifier calls; tool return value
  unchanged from today.
- notifier raises `WorkerClientError` (and a generic `Exception`) → tool
  returns its normal compact result, nothing propagates, WARNING
  `iac_pending_notify_failed` logged (caplog).
- `call_open_infra_pr` itself raising → no notifier call (exception
  propagates as today — order pin).
- the tool's return value is byte-identical to today in ALL cases (the
  notify is a pure side-effect — assert deep-equality against the
  pre-change shape).

Fanout multi-slice path (in the fanout streaming tests, reusing their
orchestrator fixtures — Codex should-fix):
- confirmed PR through the DIRECT `call_open_infra_pr` path → exactly ONE
  notifier call (same payload contract as above), and the stream's
  `result` item is unchanged (still carries `iac_pr`).
- malformed PR result (pointer None) → ZERO notifier calls (the existing
  fail-closed reply path is unchanged).
- editor worker error → ZERO notifier calls.
- notifier failure → suppressed; the stream completes normally (the
  helper never raises — one test at the helper level may stand in for
  re-proving this per-site, but the fanout happy-path notify test is
  mandatory).

`propose_rollback_tool` notify:
- worker success with `approval_url` + `expires_at` → exactly ONE
  notifier call, `severity="high"`, body contains both values and
  `target_revision`; the model-supplied `reason` string does NOT appear
  in the notified body (use a sentinel reason like
  `"SECRET-SENTINEL-do-not-leak"`).
- the worker payload still carries the existing `safe_reason` (existing
  behavior pin — must not regress).
- worker response missing/empty `approval_url` (or `expires_at`) → ZERO
  notifier calls + WARNING `rollback_propose_notify_failed`.
- worker call raising → no notify, exception propagates as today.
- notifier raising → suppressed, tool returns the worker response
  unchanged.

**Step 2 — verify failures** (`uv run pytest tests/unit/test_adk_tools.py
tests/unit/test_fanout_orchestrator.py tests/unit/test_fanout_parallel_author.py -q`
— whichever fanout file hosts the multi-slice success-path fixtures; run
the one you extend).

**Step 3 — implement.** `agent/adk_tools.py` has NO module logger today
— add one mirroring `agent/adk_agent.py`'s
`logging.getLogger("driftscribe.agent…")` convention (note: `fanout.py`
has no logger either — don't look there). Warning extras are identifier-only (pr_number /
target_revision) and NEVER include the body (it may embed a tokened
approval URL). One shared best-effort wrapper keeps the sites readable:

```python
_NOTIFY_TITLE_CAP = 200


def _notify_approval_pending(body: str, *, event: str, **log_extra: object) -> None:
    """Best-effort operator notification — advisory side-channel, never
    load-bearing here (the chat reply/CTA already carries the link; contrast
    agent/main.py's autonomous rollback flow where notify failure 502s).
    Suppresses EVERYTHING; logs one WARNING with identifying extras only
    (never the body — it may embed a tokened approval URL)."""
    try:
        worker_client.call(
            "notifier",
            {"channel": "approval", "severity": ..., "body": body},
        )
    except Exception:
        log.warning(event, extra=log_extra)
```

(Implementer: severity differs per site — pass it as a parameter; keep
the docstring's contrast comment.) Wire per Decisions 2–3; the fanout
call site is `await asyncio.to_thread(notify_iac_pr_pending, …)` placed
right after the existing `iac_pr is None` fail-closed return.

**Step 4 — gates:** targeted files → full `uv run pytest -q` (baseline
2360) → `uv run ruff check .`.

**Step 5 — commit** `feat(notify): pending-approval notifications for IaC PRs + chat-proposed rollbacks` + `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

## Final gates

- `uv run pytest -q` (2360 baseline, expect +~14) · `uv run ruff check .`
- Frontend untouched: run `npm run test:unit` once to confirm 420 still.
- Deploy after merge: coordinator rebake + `update-traffic` (traffic
  pinned). NO notifier redeploy (worker unchanged). Live verify: trigger
  is agent-authoring-dependent, so verify via (a) bundle/route untouched,
  (b) unit coverage, and (c) optionally a real chat-driven
  `propose_rollback` round-trip if an idle revision exists to target —
  otherwise verify the rollback-notify path stays observable in logs on
  next real use. The honest live check: `gcloud logging read` for the
  notifier's `notify:` line on the next pending approval.

---

## Plan-review record (Codex thread 019eb283-2ad7-7250-ab84-06fdcfb89bee)

First round: **NO-GO**, 1 must-fix + 2 should-fix + 2 nits — all folded:

1. **MUST-FIX — the single-chokepoint claim was FALSE for D5 fanout:**
   the multi-slice orchestrator calls `worker_client.call_open_infra_pr`
   DIRECTLY (`agent/fanout.py` ~1269) and gates on `iac_pr_pointer`
   itself; only the single-agent path (and the registry's single-slice
   fallback) goes through `open_infra_pr_tool`. → shared
   `notify_iac_pr_pending` helper called from BOTH sites (Decision 2,
   grounding fact 4).
2. SHOULD-FIX — the body copy implied automatic plan readiness, but the
   C2 plan-builder is maintainer-dispatched (`workflow_dispatch`):
   → "Next: dispatch the C2 plan-builder for PR #N, then review &
   approve: …" (Decision 2).
3. SHOULD-FIX — fanout direct-path tests added as mandatory (Task 1).
4. NIT — defensive reads must be `isinstance(v, str) and v`, never
   `str()`-coercion (Decision 3).
5. NIT — `adk_tools.py` has no module logger; add one per repo
   convention; warning extras identifier-only, never the body (Task 1
   Step 3).

Codex also verified the scope-narrowing claims: autonomous rollback
notify is real + load-bearing; iac apply-failure alerts are real;
eventarc/recheck rollback is covered by `_do_rollback`; the C6 resume
mints-and-immediately-applies inside the operator's POST (no separate
waiting moment); upgrade PRs are `requires_approval: false` (not this
HITL class); best-effort is sound because chat/SSE already carries the
CTA on both new surfaces.

Second round: **GO** (no remaining must-fix) + 1 should-fix and 3 nits,
all folded: fanout test target added to Step 2; architecture line names
both modules; out-of-scope "plan may take a minute" wording corrected;
logger convention reference fixed to `adk_agent.py` (fanout has none).

## Post-review deltas (as shipped)

1. `_notify_approval_pending` restructured so NOTHING in it can
   propagate — the `except Exception` block wraps `_log.warning` in
   `contextlib.suppress(Exception)` (quality review: the original shape
   left a pathological raising log handler able to escape, contradicting
   the docstring contract).
2. Fanout call-site comment carries the latency bound
   (`_HTTPX_TIMEOUT` 30 s — verified against worker_client) so the
   stream-stall ceiling is documented.
3. The fanout happy-path test also pins the plan title + pr_url in the
   notified body (closes a pass-empty-title-at-the-call-site hole).
4. Final-review observations (accepted, no change): `mint_id_token` has
   no timeout before the httpx block — pre-existing and shared with every
   worker call; the fanout single-slice fallback's no-double-notify
   property is structural (`return` before the orchestrator notify), not
   integration-tested.
