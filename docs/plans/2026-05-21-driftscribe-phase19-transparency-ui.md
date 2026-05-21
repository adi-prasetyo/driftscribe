# DriftScribe Phase 19 — Transparency UI for Multi-Agent Reasoning (v3)

> **For Claude:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` (or `superpowers:subagent-driven-development`) to implement this plan task-by-task. Each numbered task is its own commit.

**Goal:** Ship a "glass-box" same-origin web UI on the coordinator that visualizes the reasoning timeline for any `/chat` invocation and any historical drift / upgrade decision. Surface per-group reasoning (coordinator thoughts + token usage / tool calls grouped by worker / MCP citations grouped by server), tool args + results, and the final response — all collapsible. Same UI also lets an operator scroll back through Firestore-recorded decisions and open the trace for any past run.

**Architecture:** Phase 18 lays most of the required data plane — `llm_thought`, `tool_call`, `llm_usage`, and `mcp_call` events are emitted as structured JSON log lines keyed by `trace_id` (bound by the FastAPI middleware in `driftscribe_lib/logging.py`) and held in Cloud Logging `_Default` for 365 days. Phase 19 is (a) **redact-at-source** so the durable log copy never carries credentials, (b) two small log-shape extensions (`tool_call.args`, new `tool_result` event, new `final_response` event), (c) one Cloud Logging IAM grant, (d) one Cloud Logging read endpoint + one Firestore decisions listing, and (e) a single static HTML page served by the existing coordinator. No new service. No new datastore.

**Tech stack:** `google-cloud-logging` Python client (promoted from transitive to direct dep), FastAPI + Jinja2 templates (existing pattern), vanilla JS + `fetch()` (no SPA framework — same posture as `approval.html`), in-process `time`-based TTL cache (`functools.lru_cache` is keyed wrong for TTL — use a tiny dict helper).

---

## Changelog from v1

This plan replaces the v1 that was reviewed by Codex on 2026-05-21 (thread `019e4497-3615-7390-9847-8a6eb39bf464`). The following CRITICAL and IMPORTANT findings are addressed in-line:

| # | finding (severity) | how this plan addresses it |
| - | ------------------ | -------------------------- |
| 1 | `_is_complete` heuristic was wrong — `llm_usage` fires per iteration, would freeze caches on partial timelines (**CRITICAL**) | New 19.A.3.x: emit explicit `final_response` event in the ADK loop on `event.is_final_response()`. Completion + caching now gated on `final_response` presence AND an ingestion grace window. |
| 2 | Redaction was render-only — raw secrets sat in Cloud Logging for 365 days (**CRITICAL**) | Moved to **redact-at-emit**. New 19.A.2 `redact_event(payload)` recursively walks every string field and runs `redact_text`, with a small metadata allowlist (`event`, `trace_id`, `workload`, `tool_name`, `mcp_tool`, `mcp_server`, token counts, timestamps). Applied at every `_log.info(...)` call site in `agent/adk_agent.py`. Applied AGAIN at render in `/trace` as defense-in-depth. |
| 3 | Plan claimed "No new IAM" — false; Cloud Logging read needs `logging.viewer` (**CRITICAL**) | New 19.A.0: grants `roles/logging.viewer` on the coordinator runtime SA via `infra/scripts/setup_secrets.sh` (idempotent describe-then-act pattern from Phase 18.A.1). Parse-only regression test. Documented in `docs/runbooks/deploy.md`. |
| 4 | Cloud Logging ingestion latency promised 2-5s; reality 10-30s for Cloud Run resource logs (**IMPORTANT**) | UI now shows the `/chat` final response IMMEDIATELY in a "Final response" card at the top. The reasoning timeline fills in underneath with an explicit "waiting for logs (typical lag ~15s)" state when a poll returns no new events. |
| 5 | SSE token-in-query leaked the long-lived `DRIFTSCRIBE_TOKEN` (**IMPORTANT**) | Superseded by v3 row #16 — entire 19.C section descoped, see "Phase 19.C — DESCOPED" below. One-shot HMAC design noted for future re-incorporation. |
| 6 | Cloud Logging client is sync — would block the FastAPI event loop in an `async def` (**IMPORTANT**) | `/trace/{id}` is `def` (FastAPI runs sync defs on a threadpool). Fetch has a 5s timeout + bounded result size. |
| 7 | "Per-agent grouping" rendered flat-chronological + per-event-type only (**IMPORTANT**) | UI now groups events into three collapsible top-level sections (per Codex): **Coordinator reasoning** (`llm_thought` + `llm_usage`), **Tools & workers** (grouped by `tool_name`, pairs `tool_call` + `tool_result`), **MCP** (grouped by `mcp_tool` / `mcp_server`). Honest labeling — workers don't "think." |
| 8 | Cache stored raw payloads (**MINOR**) | Cache stores the post-redaction render payload only. |
| 9 | Sort tie-breaker missing — same-ms events shuffle (**MINOR**) | Order key: `(timestamp_asc, insert_id_asc)`. `insert_id` is a Cloud Logging `LogEntry` field; stub fetcher mirrors with a monotonic seq. |
| 10 | Module-global cache state was undocumented (**MINOR**) | Comment + reset hooks; explicit "per-process, best-effort, not a correctness boundary" docstring. |

Cells refuted by Codex (kept noted so reviewers know they were considered): `jsonPayload.trace_id` filter syntax IS correct for DriftScribe's `JSONFormatter` (no change); Firestore single-field auto-index on `trace_id` is fine — no composite-index pre-create needed (Phase 19 uses single-field `where(trace_id)` only).

## Changelog from v2 → v3 (Codex review 2 — 2026-05-21)

| # | finding (severity) | how this plan addresses it |
| - | ------------------ | -------------------------- |
| 11 | v2's completion gate used **log timestamps**; out-of-order Cloud Logging delivery could mark a partial trace complete on first poll (**CRITICAL**) | 19.A.6 completion now uses **observed-stability** in process state: first observation of `final_response` records `(monotonic_seen_at, sha256_signature_of_all_events)`; the signature hashes every event's `(timestamp, insert_id, event)` tuple via stable JSON encoding (Codex v3.1 strengthened from the original `(count, last_id)` shortcut to catch same-count replacement). Only cache once the same signature has held for `_STABILITY_GRACE_S = 30s` (aligns with Cloud Logging's documented 0-60s tailing buffer). |
| 12 | v2's `tool_result` did `json.dumps(response)` BEFORE `redact_event`, dropping key context so `should_redact("DATABASE_URL", ...)` no longer fired on nested secret-keyed values (**CRITICAL**) | 19.A.3 now redacts the **structured** response first, then serializes: `safe = redact_event(response); preview = json.dumps(safe)[:2000]`. Added a regression test for secret-keyed nested result fields. |
| 13 | v2's `final_response` emit could fire with empty preview (in `run_agent`'s no-text edge) and referenced a non-existent `final_text` variable in `run_chat` (**IMPORTANT**) | 19.A.3 emit now guarded by `final_response_logged = False` flag + non-empty-accepted-text precondition, with per-loop variable names made correct (`reply_chunks` in `run_chat`, `parts_text` in `run_agent`). Test pins zero `final_response` emit on the no-text error path. |
| 14 | v2's `CloudLoggingFetcher.timeout_s=5.0` was a false claim — `Client.list_entries()` has no timeout param in 3.15.x (**IMPORTANT**) | 19.A.5 now bounds via `concurrent.futures.ThreadPoolExecutor.submit(...).result(timeout=5.0)` at the call site, raising `HTTPException(503)` on timeout. Underlying iteration may continue briefly on the worker thread but the request returns deterministically. Combined with `max_results=500` as the data bound. |
| 15 | v2's `list_decisions()` ordered by Firestore `__create_time__` — metadata, not query-able as a normal field (**IMPORTANT**) | 19.A.7 now stores explicit `created_at = firestore.SERVER_TIMESTAMP` on every new decision record. Listing fetches snapshots and sorts CLIENT-SIDE by `snapshot.create_time` — Codex v3 caught that Firestore's `order_by(field)` filters OUT documents missing the field rather than sorting them last, so a server-side `order_by("created_at")` would hide every pre-Phase-19 decision. Client-side sort works for both old and new docs without a backfill. |
| 16 | v2's SSE design opened `EventSource` AFTER `/chat` returned — by then the ADK run had already finished and all `publish_event` calls had happened against an unregistered queue (**IMPORTANT**) | 19.C **descoped entirely**. The polling experience plus the immediate final-response card is good enough for the demo. Salvaging SSE would require either (a) a background-run endpoint or (b) client-supplied `X-Trace-Id` with the EventSource opened BEFORE the `/chat` POST — both are bigger restructures than the stretch budget supports. Documented in the new "Out of scope" entry. |
| 17 | v2's SSE secret `SSE_HMAC_KEY` was named but never wired into `setup_secrets.sh` or Cloud Run `--set-secrets` (**IMPORTANT**) | Moot — SSE descoped (see #16). |
| 18 | v2 grouping by tool name alone is opaque to judges (**MINOR**) | 19.B.4 now maps internal tool names to friendly worker labels: `read_live_env_tool → Reader (drift)`, `propose_rollback_tool → Rollback (drift)`, `upgrade_read_dependencies → Upgrade Reader`, `notify_tool → Notifier`, etc. MCP tools labeled `Developer Knowledge MCP`. Mapping table lives in the JS as `_WORKER_LABELS`. |
| 19 | v2 didn't set `Cache-Control: no-store` on operator surfaces (**MINOR**) | 19.A.6 and 19.A.7 add `Cache-Control: no-store` to `/trace`, `/decisions`, and 19.B.1 to the `/ui/transparency` HTML response. |
| 20 | v2's Firestore composite-index pre-create in 19.A.0 was scope creep (no compound query in Phase 19) (**MINOR**) | 19.A.0 simplified — only the IAM grant block remains. Composite-index work deferred to whichever future task introduces a compound query. |

---

## Context

Phase 17 made DriftScribe a multi-agent system (drift + upgrade workloads, MCP grounding, two ADK personalities). Phase 18 made every step the agent takes durably observable: each `llm_thought`, `tool_call`, `llm_usage`, and `mcp_call` is logged as a structured JSON line through `driftscribe_lib.logging`, the FastAPI middleware threads `trace_id` from request entry to every downstream log call, and `_Default` Cloud Logging retention is 365 days.

The story those two phases tell is invisible from the outside. Operators today have to open Cloud Logs Explorer and paste a filter. Hackathon judges will not do that. Phase 19 turns the same data into a one-click visual narrative.

---

## Architecture diagram

```mermaid
flowchart LR
  subgraph Browser
    UI[transparency.html<br/>vanilla JS + fetch]
  end

  subgraph Coordinator [Coordinator — agent/main.py]
    GETUI[GET /ui/transparency]
    POSTCHAT[POST /chat]
    GETTRACE[GET /trace/{id}<br/>sync def + threadpool<br/>+ Future timeout]
    GETDECISIONS[GET /decisions]
    Cache[("in-proc TTL cache<br/>5 min, completed only,<br/>after observed-stability grace")]
  end

  subgraph Agent [agent/adk_agent.py]
    Runner[ADK Runner]
    Loop[event loop emits REDACTED<br/>llm_thought / tool_call+args /<br/>tool_result / mcp_call / llm_usage /<br/>final_response]
  end

  CL[(Cloud Logging _Default<br/>365-day retention<br/>roles/logging.viewer on SA)]
  FS[(Firestore<br/>decisions collection<br/>+ trace_id field)]

  UI -->|page load| GETUI
  UI -->|prompt + token| POSTCHAT
  POSTCHAT --> Runner
  Runner --> Loop
  Loop -.->|redacted JSON| CL
  POSTCHAT -.->|writes trace_id| FS
  POSTCHAT -->|X-Trace-Id + final response| UI
  UI -->|poll every 2s| GETTRACE
  GETTRACE --> Cache
  Cache -.miss.-> CL
  GETTRACE -->|enrich| FS
  GETTRACE -->|redact_event again| UI
  UI --> GETDECISIONS
  GETDECISIONS --> FS
```

The dashed arrows are the only new write paths the existing data plane needs (redacted `tool_args` + `tool_result` + `final_response` in 19.A.3, `trace_id` on the decision doc in 19.A.4). Everything else is read.

---

## Background facts that shaped this plan

1. **Cloud Logging ingestion latency for Cloud Run is 10-30s typical, not 2-5s.** Polling every 2s on the UI is fine — what matters is that the UI doesn't *promise* 2-5s. Show the final response immediately from the `/chat` response body; let the reasoning timeline arrive when it arrives, with an explicit "waiting for logs" state.
2. **`event.is_final_response()` returns True on exactly one event per run.** `agent/adk_agent.py:407` and `agent/adk_agent.py:523` already gate the final-text collection on it. We co-emit a `final_response` log line in that same branch — one record per run, the deterministic completion signal Phase 19 needs.
3. **`llm_usage` fires per iteration.** `agent/adk_agent.py:421-422` and `agent/adk_agent.py:533-534` emit `llm_usage` whenever an event carries `usage_metadata`. Multi-turn ADK runs emit multiple `llm_usage` records — these are great cost data but **must not** be used as a completion signal.
4. **`google-cloud-logging` is sync.** FastAPI handles this cleanly if the route is `def` (not `async def`) — the framework runs sync defs on a threadpool. The route must also bound the fetch (timeout + max_entries) so a slow query doesn't tie up a threadpool worker indefinitely.
5. **`google-cloud-logging` is transitive today via OTEL.** `uv.lock` carries it via `opentelemetry-exporter-gcp-logging`. Promote to a direct dep in `pyproject.toml` so a future ADK version dropping OTEL doesn't silently break `/trace`.
6. **The HMAC-signed approval URL is its own security boundary.** Phase 19 endpoints use `X-DriftScribe-Token` via `verify_token` (`agent/auth.py:29`), same as `/chat` and `/recheck`. The static UI HTML is served *without* auth (it's just markup); every backend call from the page carries the token from `sessionStorage`. The approval HMAC is touched by nothing in this phase — 19.C (SSE) is descoped.
7. **`InMemoryStateStore` has no list method.** 19.A.7 adds `list_decisions(limit)` to the `StateStore` Protocol and both implementations. New decisions carry an explicit `created_at = firestore.SERVER_TIMESTAMP`; listing uses **client-side sort by `snapshot.create_time`** (Firestore `order_by(field)` filters out documents missing that field, so a server-side order_by on `created_at` would hide every pre-Phase-19 decision — Codex v3 review IMPORTANT).
8. **Cost guard.** Cloud Logging `list_entries` is billed per read; the UI polls. A 5-minute TTL cache on completed traces (where completed = `final_response` event seen AND observed-stability signature has held for ≥ `_STABILITY_GRACE_S = 30s`) keeps the bill bounded. In-flight traces are NOT cached — they're refetched every poll. Codex v2 review CRITICAL: the v2 plan used log-event timestamps for the grace window; out-of-order Cloud Logging delivery could mark a partial trace complete on first poll, so v3 tracks observation time in process state instead.
9. **`secret_guard` today is name+value, not free-text or recursive.** `agent/secret_guard.py:14-39` works on `(name, value)` pairs. 19.A.1 adds `redact_text(s)` (URL-userinfo regex applied to arbitrary strings). 19.A.2 adds `redact_event(d)` (recursive walker that runs `redact_text` over every string-valued field at any depth, with a tiny allowlist of safe metadata keys).

---

## Phase 19.A — Backend

### Task 19.A.0: IAM — grant `logging.viewer` on coordinator runtime SA

**Files:**
- Modify: `infra/scripts/setup_secrets.sh`
- Modify: `docs/runbooks/deploy.md`
- Test: `tests/integration/test_iam_logging_viewer_setup.py` (new — parse-only regression mirroring `test_log_retention_setup.py` from Phase 18.A.1)

**Why first:** every other task in 19.A depends on the coordinator being able to call `logEntries.list`. Without this grant, `/trace` works locally (where ADC is the operator) and 403s in Cloud Run.

**Step 1: Add a section to `infra/scripts/setup_secrets.sh`**

Follow the existing describe-then-act pattern (see Phase 18.A.1 "log retention" section for the template). Pseudocode:

```bash
# 12. Cloud Logging read access for /trace endpoint
sa_email="<coordinator runtime SA email>"
role="roles/logging.viewer"
existing="$(gcloud projects get-iam-policy "$PROJECT" \
  --flatten='bindings[].members' \
  --format="value(bindings.members)" \
  --filter="bindings.role=${role} AND bindings.members=serviceAccount:${sa_email}" \
  2>/dev/null || true)"
if [[ -z "$existing" ]]; then
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${sa_email}" \
    --role="${role}" --condition=None --quiet
  echo "  logging.viewer: granted to ${sa_email}"
else
  echo "  logging.viewer: already bound to ${sa_email} — skipping"
fi
```

**Step 2: Parse-only test**

Pattern from `tests/integration/test_log_retention_setup.py` — assert that `setup_secrets.sh` contains a properly-anchored regex match for the `logging.viewer` grant block, with negative tests for commented-out or alternate-role variants.

**Step 3: Document in `docs/runbooks/deploy.md`**

Add a "Step 1c — verify the Cloud Logging viewer role" between Step 1b (log retention) and Step 2. Include a sample `gcloud projects get-iam-policy` query that confirms the binding.

**Step 4: Commit**

```bash
git commit -m "feat(infra): grant roles/logging.viewer to coordinator SA for /trace (19.A.0)"
```

---

### Task 19.A.1: Add `redact_text` to `agent/secret_guard.py`

**Files:**
- Modify: `agent/secret_guard.py`
- Modify: `tests/unit/test_secret_guard.py`

```python
def redact_text(text: str) -> str:
    """Return ``text`` with credentialed URLs replaced.

    Targets the same pattern as :func:`value_looks_credentialed` —
    URLs of the form ``scheme://user:pass@host`` — but operates on
    arbitrary free-form strings (thought summaries, tool-result
    previews, MCP errors). Replaces only the userinfo segment so the
    URL stays parseable for the reader (host + path remain) but the
    secret is gone.
    """
    if not text:
        return text
    return _CREDENTIALED_URL.sub(
        lambda m: m.group(0).split(":", 1)[0] + "://<redacted>@", text
    )
```

Tests:

```python
def test_redact_text_strips_url_userinfo():
    assert redact_text("connect to postgres://u:p@host/db now") == \
        "connect to postgres://<redacted>@host/db now"

def test_redact_text_passes_through_plain_text():
    assert redact_text("no secrets here") == "no secrets here"

def test_redact_text_handles_multiple():
    s = "a postgres://u:p@h1 then mysql://x:y@h2 done"
    out = redact_text(s)
    assert "u:p@" not in out and "x:y@" not in out
```

Commit:

```bash
git commit -m "feat(secret_guard): add redact_text for free-form payloads (19.A.1)"
```

---

### Task 19.A.2: Add `redact_event` + `redact_dict` recursive walker

**Files:**
- Modify: `agent/secret_guard.py`
- Modify: `tests/unit/test_secret_guard.py`

**Why:** the previous v1 plan only redacted three named fields. Codex flagged this as a CRITICAL hole — `mcp_call.query_or_names`, error blobs, and any future structured field would leak. `redact_event` walks the whole payload recursively with a small allowlist of metadata keys known never to carry secrets.

```python
# Metadata keys known never to carry secrets — passed through as-is.
# Adding to this allowlist is a security review decision, not a casual edit.
_SAFE_METADATA_KEYS: frozenset[str] = frozenset({
    "event", "trace_id", "workload", "tool_name", "mcp_tool", "mcp_server",
    "prompt_token_count", "candidates_token_count", "thoughts_token_count",
    "total_token_count", "doc_count", "latency_ms", "timestamp", "level",
    "logger", "result_ok", "insert_id",
})


def redact_dict(payload: dict | None) -> dict:
    """Key-aware shallow redaction for dicts (e.g. tool args).

    Applies :func:`should_redact` to each (k, v) pair so secret-keyed
    entries get replaced with '<redacted>'. Non-secret-keyed entries
    pass through as-is. Used at the boundary of structured tool args.
    """
    if not payload:
        return {}
    out: dict = {}
    for k, v in payload.items():
        s = v if isinstance(v, str) else None
        out[k] = "<redacted>" if should_redact(str(k), s) else v
    return out


def redact_event(payload: object) -> object:
    """Recursively redact every string in a structured log payload.

    Strings outside the metadata allowlist are run through
    :func:`redact_text`. Dicts redact the WHOLE value (regardless of
    type) when the KEY name looks secret-like — so
    `{"PASSWORD": {"raw": "abc"}}` becomes `{"PASSWORD": "<redacted>"}`,
    not `{"PASSWORD": {"raw": "abc"}}` after recursion. (Codex v3
    review CRITICAL: the previous version checked `should_redact`
    only on string values, letting structured-container secrets like
    `{"PASSWORD": {"raw": "abc"}}` leak through.) Lists recurse.
    Non-string scalars (int, float, bool, None) pass through.

    Call this BEFORE `_log.info(..., extra=...)` in the ADK event
    loop. Also call again at render time as defense-in-depth in case
    a future emit site forgets.
    """
    if isinstance(payload, dict):
        out: dict = {}
        for k, v in payload.items():
            if k in _SAFE_METADATA_KEYS:
                out[k] = v
                continue
            # KEY-name check FIRST — applies regardless of value type.
            # If the key looks secret-like, the value is gone, even
            # if it's a nested dict, a list, a number, or None.
            if is_secret_name(str(k)):
                out[k] = "<redacted>"
                continue
            if isinstance(v, str):
                # Key isn't secret-like but the value might still
                # match credentialed-URL or value-side rules.
                if should_redact(str(k), v):
                    out[k] = "<redacted>"
                else:
                    out[k] = redact_text(v)
            else:
                out[k] = redact_event(v)
        return out
    if isinstance(payload, list):
        return [redact_event(v) for v in payload]
    return payload
```

**Tests** (extend `tests/unit/test_secret_guard.py`):

1. `test_redact_event_recurses_into_nested_dicts` — assert a nested credentialed URL gets userinfo stripped.
2. `test_redact_event_allowlists_metadata` — `{"trace_id": "abc"}` passes through unchanged.
3. `test_redact_event_lists` — `{"errors": ["postgres://u:p@h"]}` recurses into the list.
4. `test_redact_event_key_aware_takes_precedence` — `{"DATABASE_URL": "anything"}` becomes `{"DATABASE_URL": "<redacted>"}` (full mask, not just URL-userinfo) because the key matches `should_redact`.
5. `test_redact_event_passes_through_numbers` — token counts stay numeric.
6. **NEW (v3)**: `test_redact_event_secret_named_container_redacts_whole_value` — `{"PASSWORD": {"raw": "abc"}}` becomes `{"PASSWORD": "<redacted>"}`. Mirror with `{"AUTH": {"header": "Bearer abc"}}` and `{"API_KEY": ["k1", "k2"]}`.

```bash
git commit -m "feat(secret_guard): recursive redact_event walker + safe metadata allowlist (19.A.2)"
```

---

### Task 19.A.3: ADK event loop — emit `tool_call.args`, `tool_result`, `final_response`, all redacted

**Files:**
- Modify: `agent/adk_agent.py` — both `run_chat` and `run_agent` event loops
- Test: `tests/unit/test_adk_agent_tool_event_logging.py` (new)
- Test: `tests/unit/test_adk_agent_final_response_event.py` (new)

**Step 1: Confirm ADK part shape**

`grep -n 'function_response\|FunctionResponse' /home/adi/.venv/lib/*/site-packages/google/genai/types.py | head` — confirm a `Part` carries `function_response.name` + `function_response.response`. (Phase 18 already confirmed `function_call.name`; `function_call.args` is on the same struct.)

**Step 2: Write the failing tests**

`test_adk_agent_tool_event_logging.py` pins three invariants:

a. `tool_call` log lines now carry `tool_args` (a redacted dict).
b. A `tool_result` log line is emitted for every `function_response` part, with `result_preview` (a redacted JSON string, ≤2000 chars) and `result_ok` (bool — `False` iff response dict has `error` or `errors` key).
c. Both fields run through `redact_event` before emit — verify with a stub that returns a credentialed URL in `function_response.response`.

`test_adk_agent_final_response_event.py` pins:

d. Exactly one `final_response` event is emitted per `run_chat` / `run_agent`, only on the event where `event.is_final_response()` is True and the event has content parts AND the collected text is non-empty.
e. The `final_response` log line carries `response_preview` (redacted, ≤2000 chars) and `response_kind` (`"json"` / `"text"`).
f. Multi-turn runs (multiple `llm_usage` events) still produce exactly one `final_response`.
g. **NEW (v3)**: zero `final_response` events emit on the no-text error path (where the loop raises `RuntimeError("ADK agent produced no final response")`). Stub a run that yields a final event with empty `parts` → assert no `final_response` log line + the expected `RuntimeError`.
h. **NEW (v3)**: a `tool_result` whose `response` contains `{"DATABASE_URL": "postgres://u:p@h/d", "nested": {"PASSWORD": "abc"}}` emits a `result_preview` where BOTH the `DATABASE_URL` and `PASSWORD` values are `<redacted>` (key-aware), not just the URL userinfo (regex-aware). This pins the structured-then-serialize redaction order.

Reuse `tests/unit/_adk_stubs.py` — extend with a `function_response` kwarg on `StubPart` if missing.

**Step 3: Implement**

In `agent/adk_agent.py`, inside both `run_chat` and `run_agent` event loops:

```python
from agent.secret_guard import redact_event, redact_dict, redact_text

# (existing) part-iteration block inside the partial-gate
fc = getattr(part, "function_call", None)
if fc and getattr(fc, "name", None):
    args = getattr(fc, "args", None) or {}
    tool_calls.append(fc.name)  # run_chat only
    _log.info(
        "tool_call",
        extra=redact_event({
            "event": "tool_call",
            "trace_id": current_trace_id_or_new(),
            "workload": current_workload(),
            "tool_name": fc.name,
            "tool_args": redact_dict(args),  # key-aware first
        }),
    )
    continue

fr = getattr(part, "function_response", None)
if fr and getattr(fr, "name", None):
    response = getattr(fr, "response", None) or {}
    # CRITICAL (Codex v2 review): redact the STRUCTURED response
    # BEFORE serializing. Otherwise `should_redact("DATABASE_URL", ...)`
    # never fires on nested secret-keyed values — the dict-key context
    # is gone once you json.dumps. The double-redact-after-dumps
    # approach only catches credentialed URLs by regex, missing the
    # name-based half of the redaction surface.
    safe_response = redact_event(response)
    preview = json.dumps(safe_response, default=str)[:2000]
    result_ok = not (
        isinstance(response, dict) and ("error" in response or "errors" in response)
    )
    _log.info(
        "tool_result",
        extra=redact_event({
            "event": "tool_result",
            "trace_id": current_trace_id_or_new(),
            "workload": current_workload(),
            "tool_name": fr.name,
            "result_preview": preview,
            "result_ok": result_ok,
        }),
    )
    continue
```

In both `run_chat` and `run_agent`, add a `final_response_logged = False` flag at the start of the event loop. Inside the existing `event.is_final_response() and event.content and event.content.parts` branch — but ONLY after the collected final text is confirmed non-empty (the loop already raises `RuntimeError("ADK agent produced no final response")` when it would be empty; we emit BEFORE that error path would fire, but only when there's something to log):

```python
# run_chat: the collected text variable is named `reply_chunks` and
# joined later as ''.join(reply_chunks). Build a one-shot preview.
# run_agent: the collected text variable is named `parts_text` (a
# list of strings). Same shape.
accepted_text = "".join(reply_chunks)  # run_chat
# accepted_text = "".join(parts_text)   # run_agent (mirror — pick
                                          # the right name per loop)
# Codex v3 review MINOR: .strip() — whitespace-only "text" shouldn't
# count as accepted; the downstream RuntimeError would fire anyway.
if accepted_text.strip() and not final_response_logged:
    response_preview = accepted_text[:2000]
    response_kind = "json" if accepted_text.lstrip().startswith("{") else "text"
    _log.info(
        "final_response",
        extra=redact_event({
            "event": "final_response",
            "trace_id": current_trace_id_or_new(),
            "workload": current_workload(),
            "response_preview": response_preview,
            "response_kind": response_kind,
        }),
    )
    final_response_logged = True
```

This guards against three v2 bugs Codex flagged:
1. **Wrong variable name in `run_chat`** — v2 wrote `final_text` which doesn't exist in `run_chat` (only `run_agent`). Each loop uses its own accumulator name.
2. **Empty preview emit on no-text error path** — v2 would emit `final_response` with `response_preview=""` and then immediately raise. The accepted-text precondition closes this.
3. **Duplicate emits** — if a malformed ADK runner ever yields multiple final events, the `final_response_logged` flag prevents a second emit. Test asserts exactly one across all paths.

Also wrap the existing `llm_thought` and `llm_usage` emits in `redact_event(...)` so the redact-at-source invariant holds uniformly. (The `mcp_call` site in `agent/mcp/developer_knowledge.py:_log_call` gets the same treatment in a follow-up patch within this task — single commit, both sites updated.)

**Step 4: Run tests + ruff + commit**

```bash
pytest tests/unit/test_adk_agent_tool_event_logging.py \
       tests/unit/test_adk_agent_final_response_event.py \
       tests/unit/test_secret_guard.py -v
ruff check agent/ tests/
git commit -m "feat(agent): redact-at-emit + tool_call.args + tool_result + final_response (19.A.3)"
```

---

### Task 19.A.4: Embed `trace_id` in decision records

**Files:**
- Modify: `agent/main.py` — `_do_rollback` (~line 540) and `_do_recheck` (~line 907)
- Test: `tests/integration/test_decision_trace_id.py` (new)

```python
# in both _do_rollback and _do_recheck response dicts:
response = {
    "decision_id": decision_id,
    "event_key": event_key,
    "trace_id": current_trace_id_or_new(),  # NEW (19.A.4)
    ...
}
```

Test:

```python
def test_recheck_decision_carries_trace_id():
    fixed_trace = "a" * 32
    with patch.object(...):  # stub Reader Worker
        resp = client.post(
            "/recheck",
            headers={"X-Trace-Id": fixed_trace},
            json={"workload": "drift"},
        )
    assert resp.status_code == 200
    assert resp.json().get("trace_id") == fixed_trace
```

```bash
git commit -m "feat(agent): record trace_id on every decision document (19.A.4)"
```

---

### Task 19.A.5: Promote `google-cloud-logging` to a direct dep + add `TraceFetcher` abstraction

**Files:**
- Modify: `pyproject.toml`, `uv.lock`
- New: `agent/trace_fetcher.py`
- Test: `tests/unit/test_trace_fetcher.py` (new)

**Step 1: Add dep**

```toml
# [project] dependencies
"google-cloud-logging>=3.11",
```

Run `uv lock` and commit the updated `uv.lock`.

**Step 2: Protocol + two implementations**

```python
import re
from typing import Protocol

_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")


class TraceFetcher(Protocol):
    def fetch(self, trace_id: str, *, limit: int = 500) -> list[dict]:
        """Return entries ordered by (timestamp asc, insert_id asc).

        Each entry is a dict from the structured JSON payload — Phase
        18's `JSONFormatter` puts our extras at the top of
        `jsonPayload`, and Cloud Run's stdout parser turns that into
        `entry.payload` on the client side.
        """


class CloudLoggingFetcher:
    """Production. Reads from Cloud Logging via the sync Python client.

    Per-process singleton; instantiated lazily so tests that don't go
    near GCP don't pull in google-cloud-logging at import time.
    Caller MUST hold a service account with roles/logging.viewer
    (granted in 19.A.0).

    Note: `Client.list_entries()` in google-cloud-logging 3.15.x has
    NO timeout parameter — Codex review v2 caught that the v2 plan
    claimed otherwise. Time-bounding happens at the endpoint level
    via `concurrent.futures.Future.result(timeout=...)`, not here.
    The data-size bound is `max_results=limit` (default 500).
    """

    def __init__(self, project: str, service_name: str = "driftscribe-agent"):
        from google.cloud import logging as cloud_logging
        self._client = cloud_logging.Client(project=project)
        self._service = service_name

    def fetch(self, trace_id: str, *, limit: int = 500) -> list[dict]:
        if not _HEX32_RE.match(trace_id):
            return []  # fail-closed against filter-string injection
        # Filter syntax confirmed correct for our JSONFormatter — Cloud
        # Run's structured-stdout pipeline puts our extras under
        # jsonPayload.* (Codex review 2026-05-21).
        filter_str = (
            f'resource.type="cloud_run_revision" '
            f'AND resource.labels.service_name="{self._service}" '
            f'AND jsonPayload.trace_id="{trace_id}"'
        )
        entries_iter = self._client.list_entries(
            filter_=filter_str,
            order_by="timestamp asc",
            page_size=limit,
            max_results=limit,
        )
        return [_entry_to_dict(e) for e in entries_iter]


class StubTraceFetcher:
    """In-memory. Used by tests via app.dependency_overrides."""
    def __init__(self, entries: list[dict] | None = None):
        self.entries = entries or []
        self.calls = 0  # so tests can assert cache behavior

    def fetch(self, trace_id: str, *, limit: int = 500) -> list[dict]:
        self.calls += 1
        return [e for e in self.entries if e.get("trace_id") == trace_id][:limit]


def _entry_to_dict(entry) -> dict:
    """Convert a google-cloud-logging LogEntry to our payload dict.

    JSONFormatter writes every field at the top of jsonPayload, so
    `entry.payload` is already the structured event dict we want.
    Sort tie-breaker uses `entry.insert_id` (a unique Cloud Logging
    string per entry — see Codex review).
    """
    d = dict(entry.payload) if isinstance(entry.payload, dict) else {"text": entry.payload}
    d.setdefault("timestamp", entry.timestamp.isoformat() if entry.timestamp else "")
    d.setdefault("insert_id", entry.insert_id or "")
    return d
```

**Step 3: Factory in `agent/main.py`**

```python
_trace_fetcher_singleton: TraceFetcher | None = None

def get_trace_fetcher() -> TraceFetcher:
    # NOTE: per-process, best-effort. Not a correctness boundary —
    # multi-process workers each have their own singleton. Acceptable
    # because /trace's source of truth is Cloud Logging; the
    # singleton just amortizes client construction.
    global _trace_fetcher_singleton
    if _trace_fetcher_singleton is None:
        s = get_settings()
        if s.dry_run or not s.gcp_project:
            _trace_fetcher_singleton = StubTraceFetcher()
        else:
            _trace_fetcher_singleton = CloudLoggingFetcher(project=s.gcp_project)
    return _trace_fetcher_singleton


def _reset_trace_fetcher_for_tests() -> None:
    global _trace_fetcher_singleton
    _trace_fetcher_singleton = None
```

Wire `_reset_trace_fetcher_for_tests` into the integration conftest teardown (mirror `_reset_state_for_tests`).

**Step 4: Tests**

```python
def test_stub_filters_by_trace_id():
    f = StubTraceFetcher(entries=[
        {"trace_id": "a"*32, "event": "llm_thought", "timestamp": "..."},
        {"trace_id": "b"*32, "event": "llm_thought", "timestamp": "..."},
    ])
    assert len(f.fetch("a"*32)) == 1


def test_cloud_logging_fetcher_rejects_bad_trace_id(monkeypatch):
    # Patch the cloud client so we never touch network; pin the
    # format guard returns [] on non-hex32 input.
    ...


def test_cloud_logging_fetcher_filter_string_shape():
    # Snapshot the filter string built for a known trace_id —
    # protects against accidentally regressing to labels.* or
    # textPayload-based filtering.
    ...
```

**Step 5: Commit**

```bash
git commit -m "feat(agent): TraceFetcher abstraction + google-cloud-logging direct dep (19.A.5)"
```

---

### Task 19.A.6: `GET /trace/{trace_id}` — sync def, ingestion-grace caching, defense-in-depth redaction

**Files:**
- Modify: `agent/main.py`
- Modify: `agent/state_store.py` — add `find_decision_by_trace_id` to Protocol + both impls
- Test: `tests/integration/test_trace_endpoint.py` (new)
- Test: `tests/unit/test_state_store_find_by_trace.py` (new)

**Step 1: StateStore extension**

```python
# Protocol
def find_decision_by_trace_id(self, trace_id: str) -> dict[str, Any] | None: ...

# InMemory
def find_decision_by_trace_id(self, trace_id: str) -> dict[str, Any] | None:
    for d in self._decisions.values():
        if d.get("trace_id") == trace_id:
            return d
    return None

# Firestore
def find_decision_by_trace_id(self, trace_id: str) -> dict[str, Any] | None:
    snaps = self._decisions.where("trace_id", "==", trace_id).limit(1).stream()
    for s in snaps:
        return s.to_dict()
    return None
```

**Step 2: Endpoint — sync def + threadpool execution + real timeout via Future**

```python
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeout

# Module-level — NOT per-request — so threads are reused. Single
# worker is enough because get_trace runs on FastAPI's own
# threadpool (sync def); this nested executor exists solely to
# provide a real `.result(timeout=...)` boundary that the
# google-cloud-logging client lacks natively.
_TRACE_FETCH_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="trace-fetch")
_TRACE_FETCH_TIMEOUT_S = 5.0


@app.get("/trace/{trace_id}")
def get_trace(
    trace_id: str,
    response: Response,
    _: None = Depends(verify_token),
    fetcher: TraceFetcher = Depends(get_trace_fetcher),
    state: StateStore = Depends(get_state),
) -> dict:
    """Return the redacted reasoning timeline for a trace.

    Sync def — FastAPI runs sync routes on a threadpool, which is the
    right shape for the SYNC google-cloud-logging client. An async
    def here would block the event loop.

    Response shape:
      { "trace_id": "<hex32>",
        "events": [<redacted event dicts, sorted ascending>],
        "decision": { ... } | None,
        "complete": bool,
        "fetched_from_cache": bool }

    Errors:
      400 on non-hex32 trace_id (fail-closed before any filter built)
      401/403 from verify_token
      503 if the Cloud Logging fetch exceeds _TRACE_FETCH_TIMEOUT_S
    """
    if not _HEX32_RE.match(trace_id):
        raise HTTPException(status_code=400, detail="trace_id must be 32-char lowercase hex")

    # Operator surface — never cache in the browser.
    response.headers["Cache-Control"] = "no-store"

    cached = _cache_get(trace_id)
    if cached is not None:
        return {**cached, "fetched_from_cache": True}

    # Real timeout — Codex v2 review caught that the v2 plan claimed
    # `timeout_s=5.0` on the fetcher but `Client.list_entries` has
    # no timeout param. Bound it here with Future.result(timeout=).
    fut = _TRACE_FETCH_EXECUTOR.submit(fetcher.fetch, trace_id, limit=500)
    try:
        events = fut.result(timeout=_TRACE_FETCH_TIMEOUT_S)
    except _FutureTimeout:
        # Cooperative cancellation request — best-effort, since
        # the underlying google-cloud-logging generator can't
        # actually be cancelled mid-network-call. The Future is
        # marked cancelled if the worker thread hasn't started,
        # otherwise just no-ops. Either way THIS request returns
        # deterministically. Codex v3 review IMPORTANT: add the
        # cancel() so we free up the executor slot when possible.
        fut.cancel()
        raise HTTPException(status_code=503, detail="trace fetch timed out") from None

    # Stable tie-breaker: same-millisecond events shuffle without insert_id.
    events.sort(key=lambda e: (e.get("timestamp", ""), e.get("insert_id", "")))

    # Defense-in-depth: redact again at render. Source emits ARE
    # redacted (19.A.3) — this catches any future emit site that
    # forgets, and any pre-Phase-19 historical logs in the bucket.
    events = [redact_event(e) for e in events]

    decision = state.find_decision_by_trace_id(trace_id)
    complete = _observe_and_check_stability(trace_id, events)
    payload = {
        "trace_id": trace_id,
        "events": events,
        "decision": decision,
        "complete": complete,
        "fetched_from_cache": False,
    }
    if complete:
        _cache_put(trace_id, payload)
    return payload
```

**Step 3: Observed-stability completion + cache**

Codex v2 review CRITICAL: the v2 completion used log-event timestamps to gate "the timeline has settled," which fails because Cloud Logging can deliver entries out of order. If `final_response` arrives first with a 30-second-old timestamp, the v2 logic returned `complete=True` on the FIRST poll and cached an incomplete timeline. The fix is to track stability in **process state** — how long the timeline has held the same signature in our own observations, not how old the events claim to be.

```python
from datetime import datetime, timezone

_TRACE_CACHE: dict[str, tuple[float, dict]] = {}
_TRACE_CACHE_TTL_S = 300.0

# How long we need to observe a STABLE timeline (same event count
# + same final (timestamp, insert_id)) before we trust completion.
# Aligned with Cloud Logging's documented 0-60s live-tail buffer:
# 30s catches typical late-arrival drift; 60s is the safe upper
# bound. We pick 30 for demo friendliness, with a comment that
# anyone seeing repeat truncation should bump to 60.
_STABILITY_GRACE_S = 30.0

# Per-trace observation state. (Process-local. Not a correctness
# boundary across processes — single-instance Cloud Run today;
# multi-process would need to externalize this AND the cache.)
# Each entry: { trace_id -> (first_seen_complete_monotonic, signature_hash) }
_TRACE_OBSERVATIONS: dict[str, tuple[float, str]] = {}


def _signature_of(events: list[dict]) -> str:
    """Hash over every event's identity tuple.

    Codex v3 review IMPORTANT: `(count, last_(timestamp, insert_id))`
    misses rare same-count replacement cases under `max_results`
    or reordered same-count results. Hashing every event's
    `(timestamp, insert_id, event)` tuple catches any reordering or
    swap without growing the count.

    Codex v3.1 review MINOR: use JSON-encoded tuples rather than a
    hand-joined string to eliminate delimiter ambiguity — e.g. a
    timestamp legitimately containing `|` could otherwise produce
    the same signature as two adjacent shifted fields.
    """
    import hashlib
    import json
    h = hashlib.sha256()
    for e in events:
        h.update(json.dumps([
            e.get("timestamp", ""),
            e.get("insert_id", ""),
            e.get("event", ""),
        ], separators=(",", ":")).encode())
    return h.hexdigest()


def _observe_and_check_stability(trace_id: str, events: list[dict]) -> bool:
    """Determine completion via OBSERVED stability, not log timestamps.

    The trace is complete when:
      1. A final_response event exists in the timeline, AND
      2. The full-event signature has held unchanged for at least
         _STABILITY_GRACE_S since we first saw a final_response
         with that signature.

    Codex v2 review CRITICAL fix: using `last_event_timestamp_age`
    let the first poll cache a partial timeline if Cloud Logging
    delivered `final_response` before some earlier entries. This
    rewrite tracks our own observation time + a full-event signature
    hash (Codex v3 review IMPORTANT), so out-of-order log arrival
    and same-count replacements can't trick the cache.
    """
    if not any(e.get("event") == "final_response" for e in events):
        # No final yet — definitely not complete; clear any stale obs.
        _TRACE_OBSERVATIONS.pop(trace_id, None)
        return False

    sig = _signature_of(events)
    obs = _TRACE_OBSERVATIONS.get(trace_id)
    if obs is None or obs[1] != sig:
        # First-time-complete OR signature changed → reset clock.
        _TRACE_OBSERVATIONS[trace_id] = (time.monotonic(), sig)
        return False

    first_seen_at, _sig = obs
    return (time.monotonic() - first_seen_at) >= _STABILITY_GRACE_S


def _cache_get(trace_id: str) -> dict | None:
    hit = _TRACE_CACHE.get(trace_id)
    if hit is None:
        return None
    written_at, payload = hit
    if time.monotonic() - written_at > _TRACE_CACHE_TTL_S:
        _TRACE_CACHE.pop(trace_id, None)
        return None
    return payload


def _cache_put(trace_id: str, payload: dict) -> None:
    # Already redacted upstream. Cache the redacted payload — never
    # the raw fetch result.
    _TRACE_CACHE[trace_id] = (time.monotonic(), payload)


def _reset_trace_state_for_tests() -> None:
    _TRACE_CACHE.clear()
    _TRACE_OBSERVATIONS.clear()
```

**Step 4: Tests**

```python
def test_trace_endpoint_400_on_bad_trace_id():
    resp = client.get("/trace/not-hex", headers=_TOKEN_HEADER)
    assert resp.status_code == 400


def test_trace_endpoint_404_returns_empty_events_for_unknown_trace():
    # Not 404 — empty timeline IS a valid response.
    resp = client.get("/trace/" + "f"*32, headers=_TOKEN_HEADER)
    assert resp.status_code == 200
    assert resp.json()["events"] == []


def test_trace_endpoint_redacts_credentialed_urls_in_thought_text():
    # Stub a llm_thought entry with "postgres://u:p@h" in thought_text;
    # assert response has "<redacted>@".
    ...


def test_trace_endpoint_requires_final_response_AND_stability_grace(monkeypatch):
    # CRITICAL (Codex v2): observed-stability — not log timestamps.
    # 1. First poll WITH final_response event present: returns
    #    complete=False (no observation history yet), NOT cached.
    # 2. Second poll same signature, monotonic time freshly bumped
    #    past _STABILITY_GRACE_S via monkeypatch: returns complete=True,
    #    cached. Third poll: fetcher.calls unchanged (cache hit).
    # 3. If between polls 1 and 2 a NEW event arrives (signature
    #    changes), the stability clock resets — complete=False even
    #    though we'd previously seen final_response.
    ...


def test_trace_endpoint_503_on_fetch_timeout():
    # Stub fetcher whose fetch() sleeps > _TRACE_FETCH_TIMEOUT_S.
    # Endpoint must return 503, not 500 / 200-with-empty.
    ...


def test_trace_endpoint_sets_cache_control_no_store():
    resp = client.get("/trace/" + "f"*32, headers=_TOKEN_HEADER)
    assert resp.headers.get("Cache-Control") == "no-store"


def test_trace_endpoint_orders_by_timestamp_then_insert_id():
    # Two events at the same timestamp, different insert_ids — assert
    # the ordering is by insert_id ascending.
    ...


def test_trace_endpoint_enriches_with_decision_when_present():
    # Pre-load state with a decision carrying matching trace_id;
    # assert response["decision"] is not None.
    ...
```

**Step 5: Commit**

```bash
git commit -m "feat(agent): GET /trace/{id} with redact-at-render + ingestion-grace cache (19.A.6)"
```

---

### Task 19.A.7: `GET /decisions` past-decisions listing

**Files:**
- Modify: `agent/state_store.py` — add `list_decisions(limit)` to Protocol + both impls
- Modify: `agent/main.py`
- Test: `tests/unit/test_state_store_list.py`, `tests/integration/test_decisions_endpoint.py`

**Schema change:** every new decision record gets a `created_at` field set to `firestore.SERVER_TIMESTAMP` (server-side authoritative time, immune to client clock skew). Update `record_decision` in BOTH StateStore implementations to set this field on write. Codex v2 review IMPORTANT: ordering by `__create_time__` was wrong — that's Firestore metadata, not a queryable field. Always store an explicit timestamp column when you need creation-date ordering.

```python
# agent/state_store.py — FirestoreStateStore.record_decision
def record_decision(
    self, decision_id: str, event_key: str, decision: dict[str, Any]
) -> None:
    from google.cloud import firestore
    record = dict(decision)
    record["created_at"] = firestore.SERVER_TIMESTAMP  # NEW (19.A.7)
    self._decisions.document(decision_id).set(record)
    ...

# InMemoryStateStore.record_decision — use a real datetime (no
# SERVER_TIMESTAMP equivalent) so the ordering test can pin it.
def record_decision(
    self, decision_id: str, event_key: str, decision: dict[str, Any]
) -> None:
    from datetime import datetime, timezone
    record = dict(decision)
    record.setdefault("created_at", datetime.now(timezone.utc))
    self._decisions[decision_id] = record
    ...
```

Listing:

```python
# Protocol
def list_decisions(self, *, limit: int = 50) -> list[dict[str, Any]]: ...

# InMemory — sort by created_at desc, missing field sorts last.
def list_decisions(self, *, limit: int = 50) -> list[dict[str, Any]]:
    from datetime import datetime, timezone
    sentinel = datetime.min.replace(tzinfo=timezone.utc)
    by_time = sorted(
        self._decisions.values(),
        key=lambda d: d.get("created_at") or sentinel,
        reverse=True,
    )
    return by_time[:limit]

# Firestore — fetch ALL + client-side sort. Codex v3 review IMPORTANT:
# Firestore's order_by(field) EXCLUDES documents where the field is
# missing (not "sorts them last"), so a server-side order_by on
# created_at would hide every pre-Phase-19 decision. Client-side
# sort using snapshot.create_time (always present, server-managed)
# gives a stable union of old + new docs without backfilling.
#
# Codex v3.1 review IMPORTANT: do NOT apply `limit()` before sort —
# Firestore's default order without order_by is BY DOCUMENT ID, so
# `limit(N)` picks an arbitrary subset that may exclude the newest
# decisions. Fetch all, sort, then trim. Documented assumption:
# hackathon decision volume is hundreds, not millions — if this
# scales past that, replace with a server-side ordered query
# (with backfill of `created_at` on old docs as a prerequisite).
def list_decisions(self, *, limit: int = 50) -> list[dict[str, Any]]:
    snaps = list(self._decisions.stream())
    snaps.sort(
        key=lambda s: s.create_time,  # google.api_core.datetime_helpers.DatetimeWithNanoseconds
        reverse=True,
    )
    out: list[dict[str, Any]] = []
    for s in snaps[:limit]:
        d = s.to_dict() or {}
        # Codex v3.1 polish: snapshot.create_time isn't on to_dict(),
        # but pre-Phase-19 decisions don't have an explicit
        # created_at field either. Backfill so the UI can show a
        # timestamp for every row uniformly.
        d.setdefault("created_at", s.create_time)
        out.append(d)
    return out
```

Endpoint:

```python
@app.get("/decisions")
def list_decisions_endpoint(
    response: Response,
    limit: int = 50,
    _: None = Depends(verify_token),
    state: StateStore = Depends(get_state),
) -> dict:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be 1..200")
    response.headers["Cache-Control"] = "no-store"  # operator surface
    return {"decisions": state.list_decisions(limit=limit)}
```

**Tests:**

1. `test_list_decisions_orders_by_create_time_desc` — write three decisions with explicit `create_time` snapshot values via the test harness; assert the listing returns newest-first.
2. `test_list_decisions_includes_pre_phase_19_decisions` — write one decision without an explicit `created_at` payload field (mimicking a pre-Phase-19 doc); assert it APPEARS in the listing (would NOT appear under a server-side `order_by("created_at")`), at the position implied by its `snapshot.create_time`.
3. `test_record_decision_sets_created_at_server_timestamp` — Firestore stub captures the `.set()` payload; assert `SERVER_TIMESTAMP` sentinel is present.
4. **NEW (v3.1)**: `test_list_decisions_doesnt_truncate_before_sort` — write `limit * 4` decisions with intentionally non-monotonic doc IDs (e.g. random UUIDs) and `snapshot.create_time` set so the newest IDs sort last alphabetically; assert `list_decisions(limit=10)` returns the 10 newest BY `create_time`, not the alphabetic head.

```bash
git commit -m "feat(agent): /decisions listing endpoint + StateStore.list_decisions (19.A.7)"
```

---

## Phase 19.B — Transparency UI (HTML/JS)

### Task 19.B.1: Template skeleton + `GET /ui/transparency` route

**Files:**
- New: `agent/templates/transparency.html`
- Modify: `agent/main.py`
- Test: `tests/integration/test_ui_transparency.py` (new)

```python
@app.get("/ui/transparency", response_class=HTMLResponse)
def transparency_ui(request: Request) -> Response:
    """Serve the transparency UI. No auth on the HTML itself; every
    API call the page makes carries X-DriftScribe-Token, prompted
    from the operator and held in sessionStorage."""
    resp = _TEMPLATES.TemplateResponse(request, "transparency.html", {})
    resp.headers["Cache-Control"] = "no-store"  # operator surface
    return resp
```

Template skeleton (single file, inline `<style>` + `<script>`, same posture as `approval.html`):
- Header: "DriftScribe — Reasoning Timeline" + token-status pill
- Left rail: "Past decisions" list (loaded from `/decisions`)
- Main area: prompt + workload selector + Send button, trace_id badge + status pill, **immediate final-response card (filled from `/chat` body)**, then the timeline grouped into three collapsible sections

```bash
git commit -m "feat(ui): scaffold transparency UI template + route (19.B.1)"
```

---

### Task 19.B.2: Token prompt + sessionStorage helper

**Files:** modify `agent/templates/transparency.html`.

Same shape as in v1: `getToken()` prompts once, stores in `sessionStorage` under `driftscribe_token`. Every `fetch` wraps through `api(path, init)` which auto-attaches the header and clears the token on 401/403.

```bash
git commit -m "feat(ui): token prompt + sessionStorage helper (19.B.2)"
```

---

### Task 19.B.3: `/chat` form → immediate final-response card → polling → "waiting for logs" state

**Files:** modify `agent/templates/transparency.html`.

This task implements the user-facing fix for the Cloud Logging ingestion lag finding.

1. POST `/chat` with `{prompt, workload}`. The response carries the agent's final answer in the body AND an `X-Trace-Id` header.
2. **Render the final answer IMMEDIATELY** in a "Final response" card at the top of the main area. The timeline below shows "waiting for reasoning logs (typical lag ~15s)…" until the first poll returns events.
3. Start a 2-second polling loop on `/trace/{trace_id}`.
4. Each poll replaces (NOT appends — events can arrive out of order from Cloud Logging) the timeline DOM by re-rendering the grouped view. Preserve scroll position by anchoring on `insert_id` of the user's last-expanded `<details>`.
5. Status pill states: `pending` (no events yet) → `streaming` (events arriving) → `complete` (response says `complete=true`) → `stalled` (last 10 polls returned identical event counts and `complete=false` — soft warning, polling continues).
6. Cancel polling on a fresh Send.

```bash
git commit -m "feat(ui): chat → immediate final-response card → trace polling (19.B.3)"
```

---

### Task 19.B.4: Three-group timeline rendering (Coordinator / Tools & workers / MCP)

**Files:** modify `agent/templates/transparency.html`.

This task implements the per-agent grouping fix.

Render events into three top-level collapsible sections:

1. **Coordinator reasoning** — `llm_thought` + `llm_usage`, chronological within the group. Each `llm_thought` is a `<details>` with the first 80 chars italicized as summary; expanded body shows the full thought. `llm_usage` shows token-count breakdown.
2. **Tools & workers** — sub-grouped by `tool_name`. Each sub-group is a `<details>` whose summary shows **the worker-friendly label** (see mapping below) and `<N calls> · <latency_span>`. Inside, `tool_call` and `tool_result` events pair up chronologically. `tool_args` and `result_preview` render inside their respective `<pre>` blocks.
3. **MCP** — sub-grouped by `mcp_tool` (or `mcp_server` when present). Each sub-group shows the friendly label (`Developer Knowledge MCP`) and `<N calls> · <doc_count> docs · <latency_span>`. Inside, each call shows `query_or_names`, doc count, latency, and any error.

**Worker-friendly label mapping** (Codex v3 review MINOR — judges read "Reader (drift)" not `read_live_env_tool`). Define inline in the JS:

```javascript
const _WORKER_LABELS = {
  // Drift workload
  "read_live_env_tool":        "Reader (drift)",
  "patch_docs_tool":           "Docs (drift)",
  "propose_rollback_tool":     "Rollback (drift) — HITL",
  // Upgrade workload
  "upgrade_read_dependencies": "Upgrade Reader",
  "upgrade_patch_docs_tool":   "Upgrade Docs",
  // Shared
  "notify_tool":               "Notifier",
  // MCP
  "answer_query":              "Developer Knowledge MCP — answer",
  "search_documents":          "Developer Knowledge MCP — search",
  "get_documents":             "Developer Knowledge MCP — get",
};
function workerLabel(toolName) {
  return _WORKER_LABELS[toolName] || toolName;
}
```

Final response sits in its own card ABOVE these three groups (filled by 19.B.3 from the `/chat` body, not from the timeline poll).

Color/icon legend in a small footer so a judge can read the page without explanation.

```bash
git commit -m "feat(ui): three-group timeline render (coordinator / tools / MCP) (19.B.4)"
```

---

### Task 19.B.5: Inline HITL approval link rendering

**Files:** modify `agent/templates/transparency.html`.

When a `tool_result` event has `tool_name == "propose_rollback_tool"` and `result_preview` contains an `approval_url`, render an in-timeline call-to-action card with a button linking to that URL. Same treatment for decisions in the past-decisions list (with expired URLs rendered with a strikethrough + "expired" badge — derived from the decision doc's `expires_at`).

```bash
git commit -m "feat(ui): inline HITL approval CTA in timeline + past decisions (19.B.5)"
```

---

### Task 19.B.6: Past-decisions pane + open-trace navigation

**Files:** modify `agent/templates/transparency.html`.

Left rail loads `/decisions?limit=50` on page mount and on every Send completion. Each row:

```
<time> · <workload> · <action> · <trigger>
[open trace →]   (button — only when decision.trace_id is present)
```

Clicking [open trace] fetches `/trace/{trace_id}` and renders into the main timeline WITHOUT polling (historical traces are immutable). Prompt input is dimmed and labeled "viewing historical trace `<id>`" with a "← new chat" button to return to interactive mode.

Decisions written pre-Phase-19 lack `trace_id` — the button is hidden, the row still appears.

```bash
git commit -m "feat(ui): past-decisions pane + open-trace navigation (19.B.6)"
```

---

### Task 19.B.7: Demo flow walkthrough + verify + docs

**Files:**
- Modify: `docs/demo-script.md`, `docs/demo-script.ja.md`
- Modify: `README.md`, `README.ja.md` (one-line link to the UI)

Verify end-to-end (use the `verify` skill or a manual headless-browser walk):

1. Page loads → token prompt → enter test token.
2. Send "what is the current drift?" with workload=drift → final response appears in the top card immediately → trace_id badge populates → after ~15s the three reasoning groups fill in.
3. Each of the three groups renders with distinct visual treatment.
4. Click a past decision → timeline switches to historical mode → no polling.
5. For a rollback decision, click the inline "Approve →" CTA → land on `/approvals/{id}?t=…`.

Add a "Transparency UI demo" section to `docs/demo-script.md` + screenshot to `docs/submission/`.

```bash
pytest -q
ruff check agent/ tests/
git commit -m "docs: transparency UI demo walkthrough + screenshot (19.B.7)"
```

---

## Phase 19.C — DESCOPED (live SSE)

**Codex v2 review (2026-05-21) flagged this stretch as broken-by-design under the v2 flow.** Reasoning: `/chat` only returns after the ADK runner has finished its full loop. Opening an `EventSource` on the client AFTER `/chat` returns means the queue is registered AFTER `publish_event(...)` has already fired for every event — the live stream catches nothing and the polling backstop does all the work anyway.

Two salvage paths exist and were both deferred:

1. **Background-run endpoint** — restructure `/chat` so the LLM runs asynchronously and the response is just an acknowledgement carrying the `trace_id`. The browser then opens `EventSource` to watch. Major restructure: changes the operator-facing semantics of `/chat`, breaks the immediate-final-response card in 19.B.3, requires response-storage to deliver the final answer out-of-band.
2. **Client-supplied `X-Trace-Id`** — browser generates a hex32 trace_id, opens `EventSource` FIRST, then POSTs `/chat` with the matching `X-Trace-Id` header. The middleware (Phase 15.2) already honors inbound `X-Trace-Id` so this works in principle, but there's a race: if the runner emits before the SSE connection is established, those events are lost and only polling catches them. Mitigations exist but the complexity-vs-payoff ratio is poor for a hackathon timebox.

**For Phase 19, the polling experience plus the immediate final-response card is the shipped UX.** Reasoning-event arrival time is dominated by Cloud Logging's 10-30s ingestion lag — going sub-second via SSE buys little if Logging itself is the slow link.

If SSE returns as a future phase, the one-shot HMAC key design from v2 is sound — re-incorporate it then, alongside whichever salvage path is chosen. The `SSE_HMAC_KEY` secret, the `sse_keys` module, and the `--set-secrets` Cloud Run wiring all need to land together at that time.

---

## Out of scope (explicit non-goals)

- **Multi-user auth / RBAC.** Single shared operator token, same as the rest of DriftScribe.
- **Persistence beyond Cloud Logging + Firestore.** The 365-day retention from Phase 18.A is the durable copy.
- **Search-by-text across traces.** Use `/decisions` filters or Logs Explorer.
- **PII scanning beyond credentialed URLs.** Input space is Cloud Run env vars + lockfile contents (Phase 18 §"Out of scope" applies).
- **Worker-side reasoning.** Workers don't run an LLM today; the UI surfaces only the coordinator's reasoning. Workers appear in the "Tools & workers" group as tool-call destinations.

---

## Sanity-check checklist before merge

- [ ] `pytest -q` is green (baseline + every new test from 19.A and 19.B).
- [ ] `ruff check agent/ tests/ infra/` is clean.
- [ ] `google-cloud-logging` appears in `pyproject.toml`'s `[project] dependencies`.
- [ ] `roles/logging.viewer` binding exists on the coordinator runtime SA (verify with `gcloud projects get-iam-policy`).
- [ ] Every emit site in `agent/adk_agent.py` and `agent/mcp/developer_knowledge.py` passes the payload through `redact_event` BEFORE `_log.info`.
- [ ] A `final_response` log line is emitted exactly once per `/chat` run (smoke test against a live deployment).
- [ ] `/trace/{trace_id}` returns 400 on non-hex32, 401 without token, 200 + redacted timeline with token.
- [ ] `/trace` is `def` (sync), not `async def`.
- [ ] Cache stores only completed-AND-stable traces; in-flight traces refetch every poll.
- [ ] Decision documents written post-deploy carry `trace_id`; UI hides "open trace" on pre-Phase-19 decisions.
- [ ] UI renders the `/chat` final response IMMEDIATELY, doesn't wait on Cloud Logging.
- [ ] UI groups events into Coordinator / Tools & workers / MCP, not flat-chronological.
- [ ] Cache stores **only** completed-AND-observed-stable traces (`final_response` present AND signature has held ≥ `_STABILITY_GRACE_S`). First-poll completion is impossible by construction.
- [ ] `/trace` fetch is bounded by `_TRACE_FETCH_EXECUTOR.submit(...).result(timeout=5.0)`; 503 on timeout is tested.
- [ ] `tool_result` events redact STRUCTURED response before `json.dumps`, so secret-keyed nested fields like `{"PASSWORD": "..."}` are masked.
- [ ] `final_response` event emits exactly once per run AND only after collected text is non-empty; zero emits on the `RuntimeError("ADK agent produced no final response")` path.
- [ ] `created_at = SERVER_TIMESTAMP` is set on every new decision record; `list_decisions` sorts CLIENT-SIDE by `DocumentSnapshot.create_time` (not server-side `order_by` — that filters out missing-field docs).
- [ ] All three operator surfaces (`/trace`, `/decisions`, `/ui/transparency`) return `Cache-Control: no-store`.
- [ ] UI uses friendly worker labels (`Reader (drift)`, `Notifier`, `Developer Knowledge MCP`, etc.) — not raw tool function names.

---

## Verification

After all of 19.A and 19.B land:

1. **Local boot:** `USE_ADK=true DRIFTSCRIBE_TOKEN=test GCP_PROJECT=test-proj uvicorn agent.main:app --port 8080` (StubTraceFetcher kicks in without real GCP creds).
2. Open `http://localhost:8080/ui/transparency`. Enter token "test".
3. Send "What's the current drift on payment-demo?" with workload=drift.
4. Final response card populates immediately. Three reasoning groups fill in over ~15s.
5. Click a past decision → historical mode renders without polling.
6. For a rollback proposal, click the inline "Approve →" → existing approval flow works unchanged.
7. Stub a tool with `tool_args={"DATABASE_URL": "postgres://u:p@h/d"}`; rendered cell shows `<redacted>`.
8. Open a completed trace twice → second GET hits cache (assert via `StubTraceFetcher.calls` counter).
9. Tail Cloud Logging while running step 3; confirm the structured log lines carry NO credentialed URLs (redact-at-source invariant).

SSE-related verification steps removed — 19.C descoped (see Phase 19.C section).
