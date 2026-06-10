# Pause Button / Kill Switch Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> to implement this plan task-by-task.

**Goal:** One operator click suspends all agent activity — `/chat`, `/recheck`,
Eventarc-triggered rechecks, and approval-driven applies/rollbacks refuse with a
calm "DriftScribe is paused" state until an operator resumes. ClickOps-audience
roadmap Wave 2 item 5 (anxiety B: "will the AI run rampage?").

**Architecture:** A singleton Firestore pause document read fail-closed at the
top of every coordinator mutation entrypoint. New `GET/POST /pause` endpoints
(operator credentials) toggle it with who/when audit. SPA gains a `PauseControl`
status banner + toggle; the two server-rendered approval pages reuse their
existing gate machinery to show the paused state. No worker changes (see
"Enforcement boundary" — the decision this doc was required to make).

**Tech stack:** FastAPI + Firestore (StateStore pattern), Jinja templates,
Svelte 5 SPA, pytest + vitest.

---

## Grounding facts (verified against the repo 2026-06-10)

- **Mutation entrypoints in `agent/main.py` (complete list — 5):**
  - `POST /recheck` (main.py:1430, `verify_token`) → `_do_recheck()` at :1444.
  - `POST /eventarc` (main.py:1454, Eventarc ID-token auth) → `_do_recheck()`
    at :1673. **Eventarc retries on non-2xx** — the handler already returns
    `200 {"ignored": "non-target-service", "service":…, "region":…}` for
    off-target events (main.py:1651–1656) precisely to avoid retry storms. A
    paused refusal must use the same 200-ignored shape.
  - `POST /chat` (main.py:3763, `verify_token`) → ADK agent → tools →
    `worker_client.call()`. Content-negotiated: SSE when the client sends
    `Accept: text/event-stream` (the SPA does), JSON dict otherwise. The SSE
    `done` frame is `{reply, tool_calls, session_id, iac_pr?}` emitted via
    `_sse_frame(event="done", data=…)` (main.py:3628). The SPA's `onDone`
    handler only needs `reply` (+optional `iac_pr`); a stream with **no**
    `meta` frame is fine — `traceId` stays null and the trace backfill no-ops
    (App.svelte:267–269).
  - `POST /approvals/{approval_id}` (main.py:3491, rollback HITL, no app-level
    auth — HMAC token in form) → `worker_client.call_execute()` (:3553,
    approve) / `call_deny()` (:3545, reject). Re-renders `approval.html`.
  - `POST /iac-approvals/{pr_number}` (main.py:2607, `require_cf_operator`) →
    `call_propose()`/`call_apply()`/merge. **Precedent for a fail-closed mode
    check:** the dry-run guard at main.py:2665 raises
    `HTTPException(503, "infra apply is disabled while the coordinator runs in
    dry-run mode")` — placed AFTER Origin+CSRF "so a cross-site probe still
    gets 403, not a mode hint". The pause check goes in the same slot.
- **Everything else in main.py is read-only** (`/healthz`, `/`, `/decisions`,
  `/trace`, `/runs`, `/infra/graph`, `/capabilities`,
  `/iac-apply/reachability`, the two approval GETs, the SPA shell). Read-only
  routes are UNAFFECTED by pause — with ONE deliberate exception: the two
  approval GET pages consult the flag for *display* (suppress/disable the
  Approve affordance + calm note) so the page matches what its POST would do.
  They stay always-200 and mutate nothing.
- **StateStore** (`agent/state_store.py`): `StateStore` Protocol +
  `InMemoryStateStore` (tests/DRY_RUN) + `FirestoreStateStore` (prod;
  collections `events`, `decisions`). Singleton via `get_state()`
  (main.py:331); integration conftest resets it per test
  (`_reset_state_for_tests`, main.py:347). There is **no existing singleton
  config-doc pattern** — the pause doc creates one (`config/pause`). The
  in-memory store IS the test double (no mocks).
- **iac approval GET** (main.py:2249) computes a gate ladder —
  `can_approve` / `reason_blocked` / `reason_severity` (`""|"error"|"pending"`)
  at main.py:2275–2311; `"pending"` renders the calm (non-red) note
  `approve-pending` in `iac_approval.html:219–225`, and `can_approve=False`
  also skips the CSRF form-token mint (main.py:2366). Pause is one more rung.
- **rollback approval GET** (main.py:2116) renders `approval.html`; the form
  shows when `approval.status == "pending" and not expired`
  (approval.html:46). No gate ladder — pause needs a small template addition.
- **Auth** (`agent/auth.py`): `verify_token` (CF Access JWT OR
  `X-DriftScribe-Token`; 503 unset / 401 missing / 403 invalid) returns None —
  no identity. `require_cf_operator` returns the canonical operator email but
  MANDATES CF Access. For pause-toggle actor attribution we best-effort verify
  an optional CF JWT (same `verify_cf_access_jwt` + `canonical_operator_email`
  from `driftscribe_lib.cf_access`) and fall back to the literal
  `"operator-token"`.
- **Status-code conventions:** 409 = operational conflict/idempotency clash
  (pervasive); 423 = tofu state lock held (only via `_map_tofu_apply_error`,
  main.py:2004); 503 = not-configured/not-deployed. **Pause refusals use 423**
  ("Locked" — semantically right, not overloaded on these routes; the detail
  string disambiguates from the tofu state-lock 423 which can only surface
  mid-approve from the worker). **`/chat` is the deliberate exception** — see
  decision 4: it returns 200 with a calm reply (+ `paused: true`), never 423.
- **Workers** (enforcement-boundary input — verified in the deploy configs,
  NOT assumed): all three mutating workers deploy
  `--no-allow-unauthenticated` (Cloud Run IAM ID-token gate;
  cloudbuild.tofu-apply.yaml:94, cloudbuild.tofu-editor.yaml:103, rollback in
  infra/cloudbuild.yaml ~:465) AND verify callers in-app via
  `driftscribe_lib.auth.verify_caller` — audience-bound Google-signed ID
  token + `ALLOWED_CALLERS` allowlist containing only the coordinator SA
  (e.g. workers/tofu_apply/main.py:146, workers/tofu_editor/main.py:84–101,
  which refuses to boot on an empty allowlist). `--ingress=internal` is an
  ADDITIONAL hardening layer on **tofu-apply only**
  (cloudbuild.tofu-apply.yaml:98); rollback and tofu-editor do NOT pin it
  (tofu-editor's runbook treats it as later hardening). The pause boundary
  argument rests on IAM + `verify_caller`, NOT on ingress. tofu-apply
  additionally requires a plan-bound HMAC approval minted by the
  coordinator's approve POST and re-verifies operator CF identity at `/apply`;
  rollback requires the operator-presented HMAC token at `/execute`.
- **GitHub-Actions C2 plan-builder** (`.github/workflows/iac.yml`,
  plan-builder job): operator-dispatched, WIF-authenticated, runs `tofu plan`
  and uploads artifacts to GCS. It NEVER applies and never calls the
  coordinator at runtime — out of band by design.
- **SPA** (`frontend/src/App.svelte`): components receive the token-aware
  `call(path, init?) => Promise<Response>` wrapper (App.svelte:103) —
  `InfraDiagram {call}` / `CapabilityCard {call}` precedent. Token =
  sessionStorage + `X-DriftScribe-Token` header with one 401-retry
  (`lib/api.ts`). Component tests inject a fake `call`
  (CapabilityCard.test.ts precedent); jsdom quirks (no native `<details>`
  toggling) documented there. **Svelte 5 whitespace rule** (PR #83/#84
  lessons): no text seams across `{#if}` boundaries — render label strings as
  single expressions; space siblings with CSS gap.
- **Gates (CI-equivalent commands, per .github/workflows/ci.yml):** backend
  `uv run pytest -q` (2249 green today) + `uv run ruff check .` (locally
  `.venv/bin/python -m pytest -q` is equivalent); frontend `npm run test:unit`
  (349), `npm run check`, `npm run build` (scripts in frontend/package.json).

---

## Settled design decisions

### 1. Enforcement boundary: coordinator-only (the decision this doc owes)

The pause flag is enforced **only at the coordinator's mutation entrypoints**.
Workers do NOT read the flag in v1. Rationale, stated honestly:

- **Why coordinator checks suffice for the runtime flow:** the three mutating
  workers deploy `--no-allow-unauthenticated` (Cloud Run IAM rejects
  unauthenticated invocation) and additionally verify, in-app, an
  audience-bound Google-signed ID token whose email must be in an
  `ALLOWED_CALLERS` allowlist containing only the coordinator SA
  (`driftscribe_lib.auth.verify_caller`; tofu-editor refuses to boot on an
  empty allowlist, tofu-apply/rollback fail-closed differently — an empty set
  makes `verify_caller` reject every caller). `--ingress=internal` exists on
  tofu-apply only and is extra
  hardening, not what this argument rests on. Every runtime mutation path
  therefore enters through one of the five gated routes above. This
  assumption is now explicit and load-bearing: *if a future change ever
  widens a worker's IAM invoker bindings or its caller allowlist, the pause
  boundary must be revisited.*
- **Why worker-side checks would NOT buy "defense against a compromised
  coordinator":** the flag lives in a Firestore document the coordinator SA
  can write. An attacker holding coordinator credentials flips the flag before
  mutating — worker-side reads of a coordinator-writable flag are theater
  against that adversary. What worker-side checks WOULD defend against is a
  coordinator **code regression** (a future mutation route that forgets the
  check). That is real but thin, and the protections that actually bind the
  high-blast-radius workers hold regardless of pause: tofu-apply refuses
  anything without a plan-bound HMAC approval (mintable only via the now-paused
  approve POST) + re-verifies the operator's CF identity at `/apply`; rollback
  `/execute` requires the operator-presented HMAC token.
- **Deferred follow-up (explicit, out of scope):** worker-side honoring of the
  flag becomes worth its cost if/when the flag moves to a store the
  coordinator cannot write (separate Firestore database + IAM, mirroring the
  C5f `PLAN_APPROVALS_DB` separation). Until then it adds two high-stakes
  worker redeploys for a control that doesn't change the threat model.
- **Out of band by design (documented, not gated):**
  - The GitHub-Actions C2 plan-builder — operator-dispatched, builds plans,
    never applies. The apply gate (`POST /iac-approvals` → worker `/apply`)
    IS paused, so a plan built during a pause cannot land.
  - A human running `gcloud`/`tofu` by hand. Pause is an **agent** kill
    switch, not an org-wide change freeze.
- **In-flight semantics:** pause gates request **entry**; an already-running
  turn (e.g. a provision fan-out that started seconds before the click) is
  NOT interrupted and may complete **any tool it already reached** — that
  includes mutation tools such as `provision_open_infra_pr`,
  `drift_patch_docs`, `upgrade_close_pr`, and `upgrade_merge_pr`
  (agent/adk_tools.py:401, :431), not just "opening a PR". What stays bounded
  regardless: anything **infra-mutating** still funnels through the approval
  POST → tofu-apply HMAC gate, and that POST is paused. An in-flight
  *approved apply* likewise completes — it was human-approved before the
  pause. The SPA copy says "no NEW agent activity will start", deliberately
  not claiming running activity halts.

### 2. Storage: `config/pause` document via StateStore

New collection `config`, document id `pause` (no collision: coordinator uses
`events`/`decisions`; workers own `plan_approvals`/`approvals` in their own
stores). Two new Protocol methods on `StateStore`, implemented by both stores:

- `get_pause() -> dict | None` — raw doc, or None if never written.
- `set_pause(*, paused: bool, reason: str | None, actor: str) -> dict` — full
  overwrite `{paused, reason, actor, updated_at}`; Firestore writes
  `SERVER_TIMESTAMP` then reads the doc back so the caller returns the real
  server time (one extra read per toggle — toggles are rare).

Absent doc = not paused (the system predates the feature; default-running).

### 3. Fail-closed read in one place: `agent/pause.py`

```python
PauseState(paused, reason, actor, updated_at, read_error=False)
read_pause_state(state) -> PauseState   # ANY exception → paused=True, read_error=True
```

Mutation routes call `read_pause_state(get_state())` per request (no caching —
a kill switch must take effect on the next request; one Firestore point-read
per *mutation* request is noise). Read-only routes never call it, so a
Firestore outage degrades reads not at all and mutations to "paused" — exactly
the roadmap's fail-closed requirement. `423` is the JSON refusal code
(constant `PAUSED_DETAIL`) for `/recheck` and the two approval-POST approve
paths; the two HTML-page POSTs follow the dry-run-precedent shape
(HTTPException after CSRF) rather than custom HTML. **Two deliberate
exceptions to 423:** `/eventarc` returns 200-ignored (retry-storm safety) and
`/chat` returns 200 with a calm reply on BOTH the JSON and SSE paths (the
operator-facing chat surface gets a readable answer, not an error toast;
machine callers detect `paused: true` in the body).

### 4. Per-route behavior when paused (or read_error)

| Route | Behavior while paused |
|---|---|
| `POST /recheck` | 423 `PAUSED_DETAIL`. `force=true` does NOT bypass — pause outranks force. |
| `POST /eventarc` | After the service/region whitelist (so off-target events skip the Firestore read), return `200 {"ignored": "paused", "service":…, "region":…}` — same retry-storm-safe shape as `non-target-service`. The event is acknowledged and dropped, NOT queued. |
| `POST /chat` | Checked right after the `use_adk` 503 (misconfigured deploys keep their existing error) and BEFORE workload resolution / ADK boot — no LLM call happens. **Returns 200, not 423 — the deliberate exception** (calm reply in the chat surface; `paused: true` for machine callers). JSON: `{"reply": <calm paused reply>, "tool_calls": [], "session_id": req.session_id or "", "paused": true}`. SSE: a stream emitting exactly one `done` frame with the same data (no `meta` frame — SPA-safe, see grounding). Entire /chat is blocked, not just mutation tools: "suspends all agent activity" means the agent doesn't run, and an LLM turn IS agent activity. |
| `POST /approvals/{id}` | `decision=="approve"` → 423 before `call_execute`. **`decision=="reject"` is ALLOWED** — denying a pending rollback is safety-direction (it prevents action); blocking it would keep a live approval pending, the opposite of what a kill switch is for. |
| `POST /iac-approvals/{n}` | Approve → 423 in the dry-run slot (after Origin+CSRF, before `_resolve_iac_plan`). Reject → ALLOWED (it is already a coordinator-side audit no-op that mutates nothing). |
| `GET /iac-approvals/{n}` | New rung in the gate ladder after dry-run: `reason_blocked = "DriftScribe is paused (operator kill switch active)"`, `reason_severity = "pending"` → calm existing `approve-pending` note, form + CSRF token never minted. Read errors take the same rung (fail-closed display matches fail-closed POST). |
| `GET /approvals/{id}` | New `paused` template var: calm `ds-note` above the form + `disabled` attr on the Approve button only; Reject stays active (matches the POST asymmetry). |
| All other read-only routes | Untouched — never read the flag. (The two approval GETs above are the deliberate display-gate exception.) |

### 5. `GET /pause` + `POST /pause`

- `GET /pause` — `Depends(verify_token)`, `Cache-Control: no-store` (the
  `/capabilities` precedent). Returns
  `{"paused": bool, "reason": str|null, "actor": str|null, "updated_at": iso-str|null, "read_error": bool}`.
  A read failure is NOT an error response — it returns the fail-closed view
  (`paused: true, read_error: true`) with 200, because that IS the system's
  effective state.
- `POST /pause` — `Depends(verify_token)`, i.e. **operator credentials**: the
  shared `X-DriftScribe-Token` OR a verified Cloudflare Access identity — the
  same dual credential every operator endpoint (`/chat`, `/recheck`,
  `/decisions`) accepts, and the SPA may legitimately run token-less behind
  CF Access (lib/api.ts supports this). This satisfies the roadmap's "resume
  requires the operator token" in spirit — resume requires *an authenticated
  operator*, and CF Access is the stronger of the two credentials (it names
  the human). A token-only dependency would lock out CF-only operators for no
  security gain. Body
  `PauseToggleRequest(BaseModel, extra="forbid")`: `paused: bool`,
  `reason: str | None = None` (stripped, max 500 chars). Actor attribution
  best-effort: if CF Access is configured AND a `Cf-Access-Jwt-Assertion`
  header verifies → canonical email; else `"operator-token"`. Persist via
  `set_pause`, return the GET shape. A WRITE failure raises 502 — the operator
  must KNOW the toggle didn't take (note: if Firestore is down, mutations are
  already fail-closed paused, so a failed "pause" write still leaves the
  system safe; a failed "resume" write leaves it paused — both fail safe).
- One structured log line per toggle:
  `log.info("pause_toggled", extra={"paused":…, "actor":…, "reason":…})` —
  with the doc's who/when fields this is the audit trail.

### 6. SPA: `PauseControl.svelte`

Mounted as the FIRST child of the chat column (above `InfraDiagram` in
App.svelte:357) — the safety status reads before anything else. Receives
`{call}` like InfraDiagram/CapabilityCard.

- **Eager fetch** of `GET /pause` on mount (this is safety status, not a lazy
  detail panel). Fetch failure → amber "Pause state unknown — DriftScribe
  fails closed: changes are blocked until this resolves." + Retry button.
- **Running state:** quiet one-line card — green dot, "DriftScribe is active —
  it can act only within the guardrails below." + a `Pause` ghost button.
- **Paused state:** prominent (but calm — this is the user feeling in control,
  not an alarm) full-width banner: "⏸ DriftScribe is paused — no new agent
  activity will start." + who/when/reason from the doc (+ "pause state could
  not be read — failing closed" when `read_error`) + `Resume` button.
- **Two-step inline confirm** (no modal): clicking Pause/Resume expands a
  confirm row in place — for Pause: optional reason `<input>` + "Confirm
  pause" + "Cancel"; for Resume: "Confirm resume" + "Cancel". Disabled buttons
  + "Saving…" while the POST is in flight; POST failure renders an inline
  error and keeps the old state.
- data-testids: `pause-control`, `pause-state`, `pause-toggle`,
  `pause-confirm`, `pause-cancel`, `pause-reason`, `pause-error`,
  `pause-retry`.
- Svelte 5 whitespace rule: meta line ("paused by X · <time> · reason") built
  as sibling elements with CSS `gap`, label strings as single expressions.
- Out of scope (deliberate): disabling ChatForm while paused — the server's
  calm paused reply already handles a submitted turn truthfully, and coupling
  PauseControl state into ChatForm is polish that can ride a later item.

---

## Visual contract (PauseControl)

```
┌────────────────────────────────────────────────────────────┐  running
│ ● DriftScribe is active — it can act only within the       │
│   guardrails below.                                [Pause] │
└────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────┐  confirm step
│ ● DriftScribe is active …                                  │
│   Pause all agent activity? New chats, rechecks, and       │
│   approvals will be refused until you resume.              │
│   reason (optional) [____________] [Confirm pause] [Cancel]│
└────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────┐  paused
│ ⏸ DriftScribe is paused — no new agent activity will start.│
│   Paused by ops@example.com · 2026-06-10 14:02 · reason:   │
│   investigating alert                             [Resume] │
└────────────────────────────────────────────────────────────┘
```

---

## Task 1 — StateStore pause doc + `agent/pause.py` + `/pause` endpoints

**Files:**
- Modify: `agent/state_store.py` (Protocol + both impls)
- Create: `agent/pause.py`
- Modify: `agent/main.py` (two new routes, placed near `/capabilities`)
- Test: `tests/unit/test_pause.py` (new), `tests/integration/test_pause_endpoints.py` (new)

**Step 1 — failing tests first** (`tests/unit/test_pause.py`):

```python
from agent.pause import PauseState, read_pause_state
from agent.state_store import InMemoryStateStore


def test_inmemory_pause_round_trip():
    s = InMemoryStateStore()
    assert s.get_pause() is None
    doc = s.set_pause(paused=True, reason="drill", actor="operator-token")
    assert doc["paused"] is True and doc["reason"] == "drill"
    assert doc["actor"] == "operator-token" and doc["updated_at"] is not None
    assert s.get_pause() == doc
    doc2 = s.set_pause(paused=False, reason=None, actor="operator-token")
    assert doc2["paused"] is False and s.get_pause()["paused"] is False


def test_read_pause_state_absent_doc_means_running():
    st = read_pause_state(InMemoryStateStore())
    assert st == PauseState(paused=False)


def test_read_pause_state_paused_doc():
    s = InMemoryStateStore()
    s.set_pause(paused=True, reason="drill", actor="a@b.c")
    st = read_pause_state(s)
    assert st.paused is True and st.reason == "drill" and st.actor == "a@b.c"
    assert st.read_error is False


def test_read_pause_state_fail_closed_on_store_error():
    class Boom:
        def get_pause(self):
            raise RuntimeError("firestore down")

    st = read_pause_state(Boom())
    assert st.paused is True and st.read_error is True
    assert st.reason  # human-readable fail-closed explanation
```

**Step 2 — implement.** `agent/state_store.py`: add to the Protocol

```python
def get_pause(self) -> dict[str, Any] | None: ...
def set_pause(
    self, *, paused: bool, reason: str | None, actor: str
) -> dict[str, Any]: ...
```

InMemory: `self._pause: dict[str, Any] | None = None` in `__init__`;
`set_pause` stores `{paused, reason, actor, updated_at: datetime.now(timezone.utc)}`
(defensive copy on get). Firestore: `self._config = client.collection("config")`
in `__init__`; `set_pause` does `self._config.document("pause").set({...,
"updated_at": firestore.SERVER_TIMESTAMP})` then reads the doc back and
returns `to_dict()`; `get_pause` is a point read returning `to_dict()` or
None.

`agent/pause.py`:

```python
"""Operator pause flag — the ClickOps-audience kill switch (Wave 2 item 5).

Fail-closed contract: read_pause_state NEVER raises. Any storage error
returns paused=True/read_error=True so mutation entrypoints refuse while
the flag is unreadable. Read-only routes must not call this at all.
"""
import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("driftscribe.agent.pause")

PAUSED_DETAIL = (
    "DriftScribe is paused — an operator suspended agent activity. "
    "Resume from the operator UI to allow changes again."
)
FAIL_CLOSED_REASON = "pause state could not be read — failing closed"


@dataclass(frozen=True)
class PauseState:
    paused: bool
    reason: str | None = None
    actor: str | None = None
    updated_at: Any = None
    read_error: bool = False


def read_pause_state(state: Any) -> PauseState:
    try:
        doc = state.get_pause()
    except Exception:  # noqa: BLE001 — fail-closed by contract, never raise
        log.warning("pause_state_read_failed", exc_info=True)
        return PauseState(paused=True, reason=FAIL_CLOSED_REASON, read_error=True)
    if not doc:
        return PauseState(paused=False)
    return PauseState(
        paused=bool(doc.get("paused")),
        reason=doc.get("reason"),
        actor=doc.get("actor"),
        updated_at=doc.get("updated_at"),
    )
```

**Step 3 — endpoint tests** (`tests/integration/test_pause_endpoints.py`,
following the conftest `_agent_settings` + TestClient pattern; auth tests use
the `no_auth_override` marker like `test_token_guard.py`): GET default
`{paused: false, …, read_error: false}`; POST pause→GET paused round-trip with
reason + actor `"operator-token"` + non-null iso `updated_at`; POST resume;
POST with ONLY a CF Access JWT (monkeypatch `verify_cf_access_jwt` per the
existing CF test pattern) succeeds AND records the canonical email as `actor`;
`extra="forbid"` 422 on unknown field; non-bool `paused` 422; reason >500
chars 422; `Cache-Control: no-store` on GET; 401 without token (marker test);
GET returns fail-closed view when the store's `get_pause` raises
(monkeypatch); POST returns 502 when the store's `set_pause` raises
(monkeypatch) — the operator must know the toggle didn't take.

**Step 4 — implement the routes** in `agent/main.py` (pydantic model near the
other request models):

```python
class PauseToggleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paused: bool
    reason: str | None = Field(default=None, max_length=500)
```

`GET /pause`: `read_pause_state(get_state())` → serialize (`updated_at` via
`.isoformat()` when datetime-like, else `str()`/None) + no-store header.
`POST /pause`: resolve actor (optional `Cf-Access-Jwt-Assertion` header →
best-effort `verify_cf_access_jwt` + `canonical_operator_email` when CF is
configured, silent fallback to `"operator-token"`), strip reason to None if
empty, `set_pause(...)` in try/except → 502 on failure, log `pause_toggled`,
return the GET shape from the as-written doc.

**Step 5:** run the new tests + full `uv run pytest -q` + `uv run ruff check .`; commit.

---

## Task 2 — wire the five mutation gates + two approval pages

**Files:**
- Modify: `agent/main.py` (5 route checks + iac GET ladder rung + rollback GET context)
- Modify: `agent/templates/approval.html` (paused note + disabled Approve)
- Test: `tests/integration/test_pause_gates.py` (new)

**Step 1 — failing tests.** All toggle pause through the real
`POST /pause` (the in-memory store makes this an honest end-to-end path):

- `/recheck` paused → 423 with `PAUSED_DETAIL`; `?force=true` still 423;
  patched `_do_recheck` NOT called.
- `/eventarc` in-scope event while paused → `200 {"ignored": "paused", "service":…, "region":…}`;
  `_do_recheck` not called; off-target event still returns
  `non-target-service` (whitelist precedes the flag read) — pinned hard by a
  test where the store's `get_pause` is patched to RAISE and an off-target
  event still returns `non-target-service` (proves the flag is never read on
  the off-target path).
- `/chat` paused, JSON: 200 `{reply: <paused copy>, tool_calls: [], paused: true}`;
  patched `agent.adk_agent.run_chat` NOT called. SSE
  (`Accept: text/event-stream`): exactly one `done` frame carrying the same
  reply + `paused: true`, no `meta` frame.
- `POST /approvals/{id}` paused: approve → 423, patched `call_execute` not
  called; reject → `call_deny` IS called (allowed).
- `POST /iac-approvals/{n}` paused: approve → 423 (use the existing
  test_iac_approval_orchestration.py fixtures to get past Origin+CSRF);
  reject → 200 render unchanged.
- `GET /iac-approvals/{n}` paused → page shows `approve-pending` note
  containing "paused", NO `token-field`/`approve-button` in the HTML.
- `GET /approvals/{id}` paused (pending approval) → `paused-note` testid
  present, approve button `disabled`, reject button NOT disabled.
- Fail-closed: monkeypatch the store's `get_pause` to raise → `/recheck` 423.
- Read-only unaffected while paused: `/decisions` and `/capabilities` 200.

**Step 2 — implement.** Each gate is two lines built on Task 1:

```python
from agent.pause import PAUSED_DETAIL, read_pause_state
# /recheck, /chat (JSON+SSE fork), approvals approve paths:
if read_pause_state(get_state()).paused:
    raise HTTPException(status_code=423, detail=PAUSED_DETAIL)
```

with the per-route shapes from the table above (`/eventarc` returns the
200-ignored dict; `/chat` returns the calm reply / single-`done`-frame
stream). iac GET ladder rung (after the dry-run `elif`, before `else:
can_approve = True`):

```python
elif (_pause := read_pause_state(get_state())).paused:
    reason_blocked = (
        "DriftScribe is paused (operator kill switch active)"
        if not _pause.read_error
        else "DriftScribe is paused (pause state unreadable — failing closed)"
    )
    reason_severity = "pending"
```

rollback GET passes `"paused": read_pause_state(get_state()).paused` into the
template context (both GET and the POST's re-render context);
`approval.html` adds the note + `{% if paused %}disabled{% endif %}` on the
Approve button only.

**Step 3:** new tests green, then FULL `uv run pytest -q` (existing recheck /
eventarc / chat / approval suites prove the not-paused path is untouched) +
`uv run ruff check .`; commit.

---

## Task 3 — SPA `PauseControl` + tests

**Files:**
- Create: `frontend/src/components/PauseControl.svelte`
- Modify: `frontend/src/App.svelte` (import + mount above InfraDiagram)
- Test: `frontend/tests/unit/PauseControl.test.ts` (new)

**Step 1 — failing tests** (inject fake `call` per CapabilityCard.test.ts):
running state renders active copy + Pause button; paused state renders banner
with actor/reason/time and Resume; fetch failure renders the fail-closed
unknown state + Retry (and Retry refetches); Pause click → confirm row
(reason input + confirm/cancel), confirm POSTs `{"paused":true,"reason":…}`
to `/pause` and the UI flips to paused from the response; cancel collapses
without a POST; POST failure shows `pause-error` and keeps running state;
Resume confirm POSTs `{"paused":false}`; `read_error: true` response renders
the fail-closed copy. Assert POST bodies via the fake's captured requests.

**Step 2 — implement** the component per the visual contract (ds-tokens,
self-styled like CoverageMeter; status copy as single expressions; sibling
spans + flex gap). Mount `<PauseControl {call} />` as the first child of
`.chat-area`.

**Step 3:** `npm run test:unit` (all green incl. existing 349), `npm run
check` (0/0), `npm run build`; commit.

---

## Final gates

- Backend: full `pytest -q` + `ruff check` clean.
- Frontend: vitest + svelte-check + build clean.
- Final whole-branch review subagent, then PR → CI → Codex completed-work
  review on the plan-review thread → squash-merge → coordinator rebake +
  pinned-traffic cutover → live verify (markers at `/static/`, plus a real
  pause→verify-423→resume round-trip against prod with operator credentials).

## Plan-review record (Codex thread 019eb163-694b-7620-8d0e-4ad55d8f0429)

First round: **NO-GO**, six must-fixes — all folded into the text above:

1. **Worker ingress claim was false** — only tofu-apply pins
   `--ingress=internal`; rollback/tofu-editor do not. Boundary argument
   rewritten around the verified facts: `--no-allow-unauthenticated` (IAM) +
   in-app `verify_caller` audience-bound ID token + coordinator-SA-only
   `ALLOWED_CALLERS` on all three mutators; ingress called out as
   tofu-apply-only hardening, explicitly NOT load-bearing.
2. **`POST /pause` auth wording contradicted `verify_token`'s dual
   credential** — resolved as: operator credentials = shared token OR CF
   Access (the SPA may run token-less behind CF); CF-only POST test added
   (which also pins actor attribution).
3. **Approval-GET wording contradiction** ("read-only unaffected" vs "GET
   pages show paused state") — fixed: the two approval GETs are the named
   display-gate exception, always-200, mutate nothing.
4. **/chat status-code contradiction** (423-everywhere vs 200-calm-reply) —
   resolved: /chat returns 200 on both JSON and SSE paths, documented as the
   deliberate exception alongside Eventarc's 200-ignored.
5. **In-flight semantics understated** — rewritten: an already-running turn
   may complete ANY tool it already reached (incl. `upgrade_close_pr` /
   `upgrade_merge_pr`), not just "open a PR"; the infra-apply funnel through
   the paused approval POST is what stays bounded.
6. **Ingress tests** — moot after (1): the claim is removed rather than
   newly enforced; no deploy changes in this item.

Nice-to-haves folded: `set_pause`-raises → 502 POST test;
off-target-Eventarc-never-reads-the-flag test (get_pause patched to raise);
CI-equivalent gate commands (`uv run pytest -q`, `uv run ruff check .`,
`npm run test:unit`, `npm run check`, `npm run build`).

## Out of scope (deliberate)

- Worker-side pause checks (see Enforcement boundary — deferred until the
  flag lives outside coordinator-writable storage).
- Gating the GitHub-Actions plan-builder (out of band; builds, never applies).
- Disabling ChatForm / other SPA inputs while paused (server reply is honest).
- Notifications on pause/resume (Wave 2 item 7 owns notifications).
- Auto-unpause / TTL on the pause (explicitly NOT wanted — a kill switch that
  un-kills itself betrays the operator).
- `/capabilities` mentioning the pause feature (candidate polish for later).
