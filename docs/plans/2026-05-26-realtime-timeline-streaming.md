# Real-time Transparency Timeline Streaming — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stream the transparency timeline (tool_call / tool_result / llm_thought / llm_usage) to the chat UI in near-real-time over the `/chat` request via SSE, additive to the existing Cloud Logging emission.

**Architecture:** `/chat` content-negotiates: `Accept: text/event-stream` → `StreamingResponse`; else the unchanged JSON dict. A new `run_chat_stream` async generator yields each redacted event dict (same shape `/trace` returns) plus a terminal result; `run_chat` becomes a thin drain of it. The SSE wrapper re-binds the trace_id/workload ContextVars inside the generator, runs the core stream in a producer task feeding an `asyncio.Queue`, and the consumer emits frames + 15s heartbeats. The UI parses SSE frames via `fetch()`+`ReadableStream`, renders incrementally, and on `done` does one `/trace` backfill.

**Tech Stack:** FastAPI / Starlette `StreamingResponse`, `asyncio.Queue`, Google ADK runner, vanilla-JS `fetch` streaming, Cloud Run, Cloudflare Access.

**Design doc:** `docs/plans/2026-05-26-realtime-timeline-streaming-design.md`

**Key source locations (verified):**
- `agent/adk_agent.py:434` `_emit_event_logs`, `:524` `_emit_llm_usage`, `:711` `run_chat`
- `agent/main.py:1896` `/chat` handler, `:1999-2046` exception ladder
- `driftscribe_lib/logging.py` `set_trace_id`/`get_trace_id`/`reset_trace_id`/`current_trace_id_or_new`
- `agent/workload_context.py` `set_workload`/`reset_workload`/`current_workload`
- `agent/templates/transparency.html:644` `api()`, `:1245` `renderTimeline`, `:1469` `onSubmit`
- Tests harness: `tests/unit/_adk_stubs.py` (`StubEvent`/`StubPart`), patch `adk_agent.Runner.return_value.run_async = _stub_run`
- `infra/cloudbuild.yaml` Cloud Run deploy args

---

## Task 1: `_emit_event_logs` / `_emit_llm_usage` return the redacted dicts they log

Make the emit helpers return the redacted event dict(s) they emit, while still logging them byte-identically. `run_agent` ignores the return value (no behavior change). This gives `run_chat_stream` a single source of truth for redaction.

**Files:**
- Modify: `agent/adk_agent.py:434-521` (`_emit_event_logs`), `:524-549` (`_emit_llm_usage`)
- Test: `tests/unit/test_adk_agent_event_logging.py` (add cases; existing must still pass)

**Step 1: Write the failing test**

Add to `tests/unit/test_adk_agent_event_logging.py`:

```python
def test_emit_event_logs_returns_redacted_dicts():
    """_emit_event_logs returns the same redacted dicts it logs, in order."""
    from types import SimpleNamespace
    from tests.unit._adk_stubs import StubEvent as _Ev, StubPart as _P

    ev = _Ev(
        [
            _P(text="thinking about it", thought=True),
            _P(function_call=SimpleNamespace(
                name="read_drift", args={"PASSWORD": "hunter2", "ok": "v"})),
        ],
        partial=False,
    )
    out = adk_agent._emit_event_logs(ev, tool_calls=[])
    assert [d["event"] for d in out] == ["llm_thought", "tool_call"]
    assert out[0]["thought_text"] == "thinking about it"
    # redaction applied to tool_args (PASSWORD masked, plain value kept)
    assert out[1]["tool_args"]["PASSWORD"] != "hunter2"
    assert out[1]["tool_args"]["ok"] == "v"


def test_emit_llm_usage_returns_dict_or_none():
    from types import SimpleNamespace
    from tests.unit._adk_stubs import StubEvent as _Ev

    no_usage = _Ev([], partial=False)
    assert adk_agent._emit_llm_usage(no_usage) is None

    with_usage = _Ev([], partial=False, usage=SimpleNamespace(
        prompt_token_count=10, candidates_token_count=5,
        thoughts_token_count=2, total_token_count=17))
    d = adk_agent._emit_llm_usage(with_usage)
    assert d["event"] == "llm_usage"
    assert d["total_token_count"] == 17
```

**Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/test_adk_agent_event_logging.py::test_emit_event_logs_returns_redacted_dicts tests/unit/test_adk_agent_event_logging.py::test_emit_llm_usage_returns_dict_or_none -v`
Expected: FAIL (helpers return `None`).

**Step 3: Minimal implementation**

In `_emit_event_logs`, build a local `emitted: list[dict] = []`. For each emit site, assign the redacted dict to a local before logging, append it, and `return emitted` at the end. Example for the `tool_call` site:

```python
        fc = getattr(part, "function_call", None)
        if fc and getattr(fc, "name", None):
            args = getattr(fc, "args", None) or {}
            if tool_calls is not None:
                tool_calls.append(fc.name)
            payload = redact_event({
                "event": "tool_call",
                "trace_id": current_trace_id_or_new(),
                "workload": current_workload(),
                "tool_name": fc.name,
                "tool_args": redact_dict(args),
            })
            _log.info("tool_call", extra=payload)
            emitted.append(payload)
            continue
```

Apply the same pattern to the `llm_thought` and `tool_result` sites. Signature becomes `-> list[dict]`. For `_emit_llm_usage`: build `payload`, log it, `return payload`; keep the early `return None` when `usage is None`.

**Step 4: Run to verify pass + no regression**

Run: `python -m pytest tests/unit/test_adk_agent_event_logging.py tests/unit/test_adk_agent_tool_event_logging.py tests/unit/test_adk_agent_usage_logging.py tests/unit/test_adk_agent_thinking.py tests/unit/test_adk_agent_final_response_event.py -v`
Expected: ALL PASS.

**Step 5: Commit**

```bash
git add agent/adk_agent.py tests/unit/test_adk_agent_event_logging.py
git commit -m "refactor(adk): emit helpers return redacted dicts (stream prep)"
```

---

## Task 2: `run_chat_stream` async generator

New generator that yields tagged stream items: `{"type":"event","event":<redacted dict + seq/insert_id/timestamp>}` per timeline event and a terminal `{"type":"result","reply":...,"tool_calls":...,"session_id":...}`. Preserves the exact loop order and the empty-reply `RuntimeError`.

**Files:**
- Modify: `agent/adk_agent.py` (add `run_chat_stream` near `run_chat`; add `from datetime import datetime, timezone` import if absent)
- Test: Create `tests/unit/test_run_chat_stream.py`

**Step 1: Write the failing test**

```python
from __future__ import annotations
from types import SimpleNamespace
from unittest.mock import patch
import pytest

from agent import adk_agent
from agent.workload_context import reset_workload, set_workload
from tests.unit._adk_stubs import StubEvent as _Ev, StubPart as _P


async def _stub_run(*args, **kwargs):
    yield _Ev([_P(text="checking", thought=True)], partial=False)
    yield _Ev([_P(function_call=SimpleNamespace(
        name="read_drift", args={"PASSWORD": "s3cret"}))], partial=False)
    yield _Ev([_P(function_response=SimpleNamespace(
        name="read_drift", response={"ok": True}))], partial=False)
    yield _Ev(
        [_P(text='{"action":"no_op","env_diffs":[],"rationale":"ok","confidence":0.9}')],
        partial=False, final=True,
        usage=SimpleNamespace(prompt_token_count=1, candidates_token_count=1,
                              thoughts_token_count=0, total_token_count=2))


@pytest.mark.asyncio
async def test_run_chat_stream_order_terminal_and_redaction():
    token = set_workload("drift")
    items = []
    try:
        with patch.object(adk_agent, "Runner") as runner_cls:
            runner_cls.return_value.run_async = _stub_run
            async for it in adk_agent.run_chat_stream("hi", workload="drift"):
                items.append(it)
    finally:
        reset_workload(token)

    events = [it["event"] for it in items if it["type"] == "event"]
    kinds = [e["event"] for e in events]
    # order: thought, tool_call, tool_result, final_response, llm_usage
    assert kinds == ["llm_thought", "tool_call", "tool_result",
                     "final_response", "llm_usage"]
    # tool_call args redacted
    tc = next(e for e in events if e["event"] == "tool_call")
    assert tc["tool_args"]["PASSWORD"] != "s3cret"
    # streamed events carry synthetic ordering fields
    assert all("seq" in e and "insert_id" in e and "timestamp" in e
               for e in events)
    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)
    # terminal result is last
    assert items[-1]["type"] == "result"
    assert items[-1]["tool_calls"] == ["read_drift"]
    assert items[-1]["reply"]


@pytest.mark.asyncio
async def test_run_chat_stream_empty_reply_raises():
    async def _empty(*a, **k):
        yield _Ev([_P(function_call=SimpleNamespace(name="read_drift"))], partial=False)
        # final event with no text
        yield _Ev([_P(text="")], partial=False, final=True)

    token = set_workload("drift")
    try:
        with patch.object(adk_agent, "Runner") as runner_cls:
            runner_cls.return_value.run_async = _empty
            with pytest.raises(RuntimeError):
                async for _ in adk_agent.run_chat_stream("hi", workload="drift"):
                    pass
    finally:
        reset_workload(token)
```

**Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/test_run_chat_stream.py -v`
Expected: FAIL (`run_chat_stream` undefined).

**Step 3: Implement `run_chat_stream`**

Add an `AsyncIterator` generator that mirrors `run_chat`'s loop. Use a `seq` counter and a small helper to wrap each logged dict into a streamed copy with synthetic fields:

```python
async def run_chat_stream(
    prompt: str,
    session_id: str | None = None,
    *,
    workload: str = "drift",
):
    """Core streaming generator. Yields, in current-log order:

      {"type": "event",  "event": <redacted dict + seq/insert_id/timestamp>}
      ... and finally ...
      {"type": "result", "reply": str, "tool_calls": list, "session_id": str}

    Raises RuntimeError on empty reply (same as run_chat). Cloud Logging
    emission is unchanged — the same redacted dicts are logged by the
    emit helpers and a synthetic-field-augmented copy is yielded.
    """
    resolution = load_workload(workload)
    agent = build_chat_agent(resolution)
    session_service = InMemorySessionService()
    sid = session_id or str(uuid.uuid4())
    await session_service.create_session(
        app_name="driftscribe", user_id="driftscribe-runtime", session_id=sid)
    runner = Runner(agent=agent, app_name="driftscribe",
                    session_service=session_service)
    msg = types.Content(role="user", parts=[types.Part(text=prompt)])

    reply_chunks: list[str] = []
    tool_calls: list[str] = []
    final_response_logged = False
    seq = 0

    def _stream(payload: dict) -> dict:
        nonlocal seq
        seq += 1
        return {**payload, "seq": seq,
                "insert_id": f"stream-{seq}",
                "timestamp": datetime.now(timezone.utc).isoformat()}

    async for event in runner.run_async(
        user_id="driftscribe-runtime", session_id=sid, new_message=msg):
        if event.content and event.content.parts and getattr(event, "partial", None) is not True:
            for payload in _emit_event_logs(event, tool_calls=tool_calls):
                yield {"type": "event", "event": _stream(payload)}
        if event.is_final_response() and event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "thought", False):
                    continue
                if getattr(part, "text", None):
                    reply_chunks.append(part.text)
            accepted_text = "".join(reply_chunks)
            if accepted_text.strip() and not final_response_logged:
                response_preview, response_kind = _redact_final_response(accepted_text)
                fr_payload = redact_event({
                    "event": "final_response",
                    "trace_id": current_trace_id_or_new(),
                    "workload": current_workload(),
                    "response_preview": response_preview,
                    "response_kind": response_kind,
                })
                _log.info("final_response", extra=fr_payload)
                final_response_logged = True
                yield {"type": "event", "event": _stream(fr_payload)}
        usage_payload = _emit_llm_usage(event)
        if usage_payload is not None:
            yield {"type": "event", "event": _stream(usage_payload)}

    reply = "".join(reply_chunks).strip()
    if not reply:
        raise RuntimeError("ADK chat agent produced no final response")
    yield {"type": "result", "reply": reply,
           "tool_calls": tool_calls, "session_id": sid}
```

Note: the `final_response` emit moved here from `run_chat`; it is byte-identical (same redaction, same `final_response_logged` guard, same `.strip()` precondition). Add `from datetime import datetime, timezone` at the top of the module if not already imported.

**Step 4: Run to verify pass**

Run: `python -m pytest tests/unit/test_run_chat_stream.py -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add agent/adk_agent.py tests/unit/test_run_chat_stream.py
git commit -m "feat(adk): add run_chat_stream generator (timeline events + terminal)"
```

---

## Task 3: Reimplement `run_chat` as a drain of `run_chat_stream`

Single source of truth: the JSON path becomes a thin consumer. Existing `run_chat` tests are the regression guard.

**Files:**
- Modify: `agent/adk_agent.py:711-833` (`run_chat` body)
- Test: existing `tests/unit/test_adk_agent_*` (must still pass unchanged)

**Step 1: Confirm the regression tests currently pass**

Run: `python -m pytest tests/unit/test_adk_agent_event_logging.py tests/unit/test_adk_agent_final_response_event.py tests/unit/test_adk_agent_usage_logging.py -v`
Expected: PASS (baseline).

**Step 2: Replace `run_chat` body with the drain**

Keep the docstring. Replace the body (after the docstring) with:

```python
    async for item in run_chat_stream(
        prompt, session_id=session_id, workload=workload):
        if item["type"] == "result":
            return {
                "reply": item["reply"],
                "tool_calls": item["tool_calls"],
                "session_id": item["session_id"],
            }
    # run_chat_stream always ends in a result or raises; this guards a
    # malformed generator that exhausts without either.
    raise RuntimeError("ADK chat agent produced no final response")
```

**Step 3: Run the full adk_agent + chat-related suite**

Run: `python -m pytest tests/unit/test_adk_agent_event_logging.py tests/unit/test_adk_agent_final_response_event.py tests/unit/test_adk_agent_usage_logging.py tests/unit/test_adk_agent_thinking.py tests/unit/test_run_chat_stream.py -v`
Expected: ALL PASS (byte-identical logs + identical return dict).

**Step 4: Commit**

```bash
git add agent/adk_agent.py
git commit -m "refactor(adk): run_chat drains run_chat_stream (DRY, one source of truth)"
```

---

## Task 4: SSE on `/chat` (content negotiation + queue/heartbeat + ContextVar re-bind)

`/chat` returns a `StreamingResponse` when `Accept: text/event-stream`; else unchanged JSON. ContextVars re-bound inside the generator. Producer task + `asyncio.Queue` + 15s heartbeat. Errors in-loop become `event: error` frames. Exception→code mapping shared with the JSON path.

**Files:**
- Modify: `agent/main.py` (`/chat` handler `:1896-2046`; add imports: `StreamingResponse` from `fastapi.responses`, `Request` from `fastapi`, `asyncio`, `contextlib`, `json`, `set_trace_id`/`get_trace_id`/`reset_trace_id` from `driftscribe_lib.logging`)
- Test: Create `tests/unit/test_chat_sse.py`

**Step 1: Write the failing test**

```python
from __future__ import annotations
import json
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient

from agent import main as agent_main


def _client():
    return TestClient(agent_main.app)


def _parse_sse(text: str):
    """Return list of (event, data_dict) from an SSE body."""
    frames = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block or block.startswith(":"):
            continue
        ev = None
        data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                ev = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = json.loads(line[len("data:"):].strip())
        frames.append((ev, data))
    return frames


@pytest.fixture
def _stub_stream():
    async def _gen(prompt, session_id=None, *, workload="drift"):
        yield {"type": "event", "event": {
            "event": "tool_call", "tool_name": "read_drift",
            "tool_args": {}, "seq": 1, "insert_id": "stream-1",
            "timestamp": "t"}}
        yield {"type": "result", "reply": "done",
               "tool_calls": ["read_drift"], "session_id": "sid"}
    return _gen


def test_chat_streams_sse_when_accept_header(monkeypatch, _stub_stream):
    monkeypatch.setattr(agent_main.get_settings(), "use_adk", True, raising=False)
    with patch("agent.adk_agent.run_chat_stream", _stub_stream), \
         patch.object(agent_main, "verify_token", return_value=None), \
         patch.object(agent_main, "load_workload"), \
         patch.object(agent_main, "_eager_resolve_upgrade_contract"):
        c = _client()
        r = c.post("/chat", json={"prompt": "hi", "workload": "drift"},
                   headers={"Accept": "text/event-stream"})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    frames = _parse_sse(r.text)
    assert frames[0][0] == "meta" and "trace_id" in frames[0][1]
    assert any(ev is None and d.get("event") == "tool_call" for ev, d in frames)
    done = [d for ev, d in frames if ev == "done"]
    assert done and done[0]["reply"] == "done"


def test_chat_returns_json_without_accept_header(_stub_stream):
    async def _run_chat(prompt, session_id=None, *, workload="drift"):
        return {"reply": "done", "tool_calls": [], "session_id": "sid"}
    with patch("agent.adk_agent.run_chat", _run_chat), \
         patch.object(agent_main, "verify_token", return_value=None), \
         patch.object(agent_main, "load_workload"), \
         patch.object(agent_main, "_eager_resolve_upgrade_contract"):
        ... # settings.use_adk true as above
        c = _client()
        r = c.post("/chat", json={"prompt": "hi", "workload": "drift"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["reply"] == "done"


def test_chat_sse_emits_error_frame_on_inloop_failure():
    async def _boom(prompt, session_id=None, *, workload="drift"):
        raise RuntimeError("model misbehaved")
        yield  # pragma: no cover (make it a generator)
    with patch("agent.adk_agent.run_chat_stream", _boom), \
         patch.object(agent_main, "verify_token", return_value=None), \
         patch.object(agent_main, "load_workload"), \
         patch.object(agent_main, "_eager_resolve_upgrade_contract"):
        ... # settings.use_adk true
        c = _client()
        r = c.post("/chat", json={"prompt": "hi", "workload": "drift"},
                   headers={"Accept": "text/event-stream"})
    assert r.status_code == 200  # already committed to 200 before failure
    frames = _parse_sse(r.text)
    err = [d for ev, d in frames if ev == "error"]
    assert err and err[0]["status_hint"] == 502
```

> NOTE during implementation: the exact monkeypatching of `get_settings().use_adk` and the pre-flight stubs must match `agent/main.py`'s real names. Read the handler first and adjust the patch targets (e.g. `load_workload` may be imported into `agent.main`'s namespace). Fill the `...` placeholders with the same settings stub used in `test_chat_streams_sse_when_accept_header`. Keep the three assertions (SSE vs JSON, meta+done frames, error frame at 200) as the contract.

**Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/test_chat_sse.py -v`
Expected: FAIL (no SSE branch).

**Step 3: Implement**

Add a shared mapper near `/chat`:

```python
def _chat_error_payload(e: Exception) -> tuple[int, str]:
    """Map a run_chat(_stream) exception to (status, detail).

    Shared by the JSON path (raised as HTTPException) and the SSE path
    (surfaced as an `event: error` frame's status_hint). Mirrors the
    pre-streaming exception ladder exactly.
    """
    if isinstance(e, worker_client.WorkerClientError):
        return 502, f"chat worker call failed: {e}"
    if isinstance(e, MissingDeveloperKnowledgeApiKeyError):
        return 503, (
            f"workload cannot reach the Developer Knowledge MCP: {e}. "
            f"See Phase 17.B.1 for the Secret Manager binding that "
            f"provisions DEVELOPER_KNOWLEDGE_API_KEY.")
    if isinstance(e, RuntimeError):
        return 502, f"chat agent failed: {e}"
    return 500, f"chat agent failed unexpectedly: {e}"
```

Add the SSE helpers + generator (module-level):

```python
def _sse_frame(*, event: str | None = None, data: dict) -> str:
    head = f"event: {event}\n" if event else ""
    return f"{head}data: {json.dumps(data, default=str)}\n\n"


async def _chat_sse(prompt: str, session_id: str | None, workload: str,
                    trace_id: str):
    from agent.adk_agent import run_chat_stream
    # Codex fix #1: re-bind ContextVars INSIDE the generator. The trace-id
    # middleware + /chat's workload `finally` both reset before this body
    # iterator runs (call_next already returned). Set before create_task so
    # the producer task inherits the bound context.
    t_tok = set_trace_id(trace_id)
    w_tok = set_workload(workload)
    queue: asyncio.Queue = asyncio.Queue()

    async def _produce():
        try:
            async for item in run_chat_stream(
                prompt, session_id=session_id, workload=workload):
                await queue.put(("item", item))
        except Exception as e:  # noqa: BLE001 - mapped to a status hint
            await queue.put(("error", _chat_error_payload(e)))
        finally:
            await queue.put(("end", None))

    producer = asyncio.create_task(_produce())
    try:
        yield _sse_frame(event="meta", data={"trace_id": trace_id})
        while True:
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=15)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if kind == "item":
                item = payload
                if item["type"] == "event":
                    yield _sse_frame(data=item["event"])
                else:  # result
                    yield _sse_frame(event="done", data={
                        "reply": item["reply"],
                        "tool_calls": item["tool_calls"],
                        "session_id": item["session_id"]})
            elif kind == "error":
                status, detail = payload
                yield _sse_frame(event="error",
                                 data={"detail": detail, "status_hint": status})
            else:  # end
                break
    finally:
        producer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await producer
        reset_workload(w_tok)
        reset_trace_id(t_tok)
```

In the `/chat` handler: add `request: Request` to the signature. Keep all pre-flight checks. After `_eager_resolve_upgrade_contract(resolution)` and before the JSON path, branch:

```python
    wants_sse = "text/event-stream" in request.headers.get("accept", "")
    if wants_sse:
        trace_id = get_trace_id()
        return StreamingResponse(
            _chat_sse(req.prompt, req.session_id, req.workload, trace_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache",
                     "X-Accel-Buffering": "no",
                     "X-Trace-Id": trace_id})
```

Refactor the JSON path's `except` ladder to use `_chat_error_payload` (preserving the same statuses/messages), e.g.:

```python
    _workload_token = set_workload(req.workload)
    try:
        try:
            return await run_chat(req.prompt, session_id=req.session_id,
                                  workload=req.workload)
        finally:
            reset_workload(_workload_token)
    except (worker_client.WorkerClientError,
            MissingDeveloperKnowledgeApiKeyError, RuntimeError) as e:
        status, detail = _chat_error_payload(e)
        raise HTTPException(status_code=status, detail=detail) from e
```

> Keep the existing detailed comments. Verify the DK-error message wording matches closely enough; if a test pins the exact old string, keep that exact string in `_chat_error_payload`.

**Step 4: Run to verify pass + no regression**

Run: `python -m pytest tests/unit/test_chat_sse.py -v && python -m pytest tests/unit -q`
Expected: new tests PASS; full unit suite green.

**Step 5: Commit**

```bash
git add agent/main.py tests/unit/test_chat_sse.py
git commit -m "feat(chat): stream timeline over /chat via SSE (content-negotiated)"
```

---

## Task 5: UI — consume the SSE stream, render incrementally, backfill on done

`onSubmit` requests `Accept: text/event-stream`. If the response is event-stream, read `resp.body` with a `ReadableStream` reader, parse SSE frames, accumulate timeline events into an array fed to `renderTimeline`, and on `done` show the reply + `reloadDecisions()` + one `/trace` backfill. On stream error/disconnect, fall back to `pollTrace(trace_id)`. JSON responses keep the existing path.

**Files:**
- Modify: `agent/templates/transparency.html` (`onSubmit` `:1469-1576`; add a `consumeChatStream` helper near `pollTrace`)
- Test: Manual + e2e (`tests/e2e/ui/tests/transparency.spec.ts`). No JS unit harness exists for the template.

**Step 1: Add the SSE consumer helper**

Near `pollTrace`, add:

```javascript
// 22.x: consume the SSE stream from /chat. Accumulates timeline events
// into `events` (same shape renderTimeline consumes) and re-renders on
// each frame. Resolves with {reply, traceId} on `done`; rejects on a
// transport error so the caller can fall back to pollTrace.
async function consumeChatStream(resp, pollingId) {
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  const events = [];
  let traceId = "";
  let reply = null;
  let streamError = null;

  function handleFrame(block) {
    let ev = null, dataLine = null;
    for (const line of block.split("\n")) {
      if (line.startsWith(":")) return;            // heartbeat comment
      if (line.startsWith("event:")) ev = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLine = line.slice(5).trim();
    }
    if (dataLine == null) return;
    let data;
    try { data = JSON.parse(dataLine); } catch (_) { return; }
    if (ev === "meta") { traceId = data.trace_id || ""; if (traceId) setTracePill(traceId); }
    else if (ev === "done") { reply = data.reply; }
    else if (ev === "error") { streamError = data; }
    else { // default frame = timeline event
      events.push(data);
      if (pollingId === activePollingId) renderTimeline(events);
    }
  }

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const block = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      if (block.trim()) handleFrame(block);
    }
  }
  if (streamError) {
    const e = new Error(streamError.detail || "stream error");
    e.statusHint = streamError.status_hint;
    e.traceId = traceId;
    throw e;
  }
  return { reply, traceId, events };
}
```

**Step 2: Branch `onSubmit` on response content-type**

After the `if (!resp.ok)` block, before the existing "Success path", insert a streaming branch:

```javascript
        const ctype = resp.headers.get("Content-Type") || "";
        if (ctype.includes("text/event-stream")) {
          updateStatusPill("pending", "streaming…");
          try {
            const { reply, traceId } = await consumeChatStream(resp, myPollingId);
            if (myPollingId !== activePollingId) return;
            showFinalResponse(reply || "(empty reply)");
            updateStatusPill("complete", "complete");
            if (typeof window.driftscribe.reloadDecisions === "function") {
              window.driftscribe.reloadDecisions();
            }
            // One backfill /trace fetch: reconciles ordering + pulls
            // side-channel mcp_call events not carried on the stream.
            if (traceId) {
              try {
                const tr = await api("/trace/" + encodeURIComponent(traceId));
                if (tr.ok) {
                  const payload = await tr.json();
                  if (myPollingId === activePollingId &&
                      Array.isArray(payload.events)) renderTimeline(payload.events);
                }
              } catch (_) { /* backfill is best-effort */ }
            }
          } catch (err) {
            if (myPollingId !== activePollingId) return;
            // Transport failure mid-stream → fall back to Cloud Logging poll.
            if (err && err.statusHint) {
              showFinalResponse(String(err.message), { error: true });
            }
            const tid = (err && err.traceId) || resp.headers.get("X-Trace-Id") || "";
            if (tid) {
              setTracePill(tid);
              pollTrace(tid, myPollingId).catch(() =>
                updateStatusPill("error", "poll error"));
            } else {
              updateStatusPill("error", "stream error");
            }
          } finally {
            sendBtn.disabled = false;
          }
          return;
        }
```

The existing JSON success path stays below as the fallback for non-streaming responses.

**Step 3: Send the Accept header**

In the `api("/chat", {...})` call inside `onSubmit`, add `Accept: text/event-stream` to the headers:

```javascript
          resp = await api("/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json",
                       "Accept": "text/event-stream" },
            body: JSON.stringify({ prompt, workload }),
          });
```

**Step 4: Verify**

- Static: load the template render path test if present (`grep -rn transparency tests/unit`), run any HTML/template unit test.
- e2e (if the operator can run it): `cd tests/e2e/ui && npx playwright test transparency.spec.ts` — but this needs a live agent; treat as operator-run. Add/adjust a spec assertion that a `tool_call` row appears before the final reply card when streaming, if the harness supports a stubbed SSE.
- Manual smoke (operator): on `driftscribe.adp-app.com`, send a drift prompt and confirm tool rows appear progressively, not after a ~15s lag.

**Step 5: Commit**

```bash
git add agent/templates/transparency.html tests/e2e/ui/tests/transparency.spec.ts
git commit -m "feat(ui): consume /chat SSE stream, render timeline live + backfill"
```

---

## Task 6: Infra — Cloud Run timeout + concurrency

Raise the request timeout so long agent runs aren't cut, and bump concurrency so fallback/parallel GETs don't deadlock behind a live stream (Codex fix #4).

**Files:**
- Modify: `infra/cloudbuild.yaml` (the `gcloud run deploy driftscribe-agent` args)

**Step 1: Edit the deploy args**

Add/update under the `run deploy driftscribe-agent` step:

```yaml
    - --timeout=300
    - --concurrency=2
```

(Replace the existing `--concurrency=1` if present; add `--timeout` if absent.)

**Step 2: Verify the YAML is well-formed**

Run: `python -c "import yaml,sys; yaml.safe_load(open('infra/cloudbuild.yaml')); print('ok')"`
Expected: `ok`.

**Step 3: Commit**

```bash
git add infra/cloudbuild.yaml
git commit -m "chore(infra): bump Cloud Run timeout=300 + concurrency=2 for SSE"
```

---

## Final verification

1. `python -m pytest tests/unit -q` — full unit suite green.
2. `python -m pytest tests/integration -q` — integration green (if any touch `/chat`).
3. `git log --oneline feat/realtime-timeline-streaming` — six focused commits.
4. Operator smoke on `driftscribe.adp-app.com` after deploy: streaming timeline + JSON fallback (curl without `Accept`) both work.

## Out of scope / accepted

- Token-streaming the final reply text.
- Live `mcp_call` (backfilled after `done`).
- Cloud Run total-request timeout still caps very long runs.
