# Coordinator describe timeout: stop misreading a legitimate ~30s CAI describe as a failure

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Close the backlog-3 residual — a single infra-reader `/describe` (~25–30s of CAI paging) sits at the coordinator's 30s `worker_client` timeout edge, so a legitimately-successful describe is intermittently misread as a transport failure (degraded graph, failed chat tool call). Give infra_reader calls a read budget comfortably above the observed wall clock, via a per-worker default timeout — and stop the SPA from stacking concurrent graph fetches now that one can legitimately run long.

**Architecture:** Two small changes. Backend: `agent/worker_client.py` gains a named `_DESCRIBE_HTTPX_TIMEOUT` constant plus a per-worker default-timeout map consulted by `call()` when the caller passes no explicit timeout — zero call-site changes, zero worker changes, zero deploy-flag changes. Frontend: `InfraDiagram.refresh()` gains an in-flight coalescing guard (skip *starting* a new fetch while one is pending) so the longer budget cannot let the poll/ladder stack slow graph requests against the concurrency-2 coordinator (Codex 019eba89 must-fix 1). Pins mirror the existing `_capture_timeout` apply-timeout test pattern. Coordinator rebake only (SPA + worker_client are both baked into the coordinator image).

**Tech Stack:** `agent/worker_client.py` (httpx), pytest (`respx` + the existing `_capture_timeout` fixture), Svelte/vitest (`InfraDiagram.test.ts`).

---

## Background (the residual)

Recorded during backlog item 3 (PR #113, infra-reader concurrency 1→8): with head-of-line blocking gone, a *single* warm `/infra/graph` fetch still came back `degraded: true` at **30.1s** — the worker's CAI describe legitimately takes ~25–30s and the coordinator's `worker_client` 30s timeout misclassifies the slow-but-successful case as a worker failure. This is the same bug class C5e fixed for `tofu apply` (a long-but-successful operation misread as transport failure), at much lower stakes: describe is read-only and recoverable, so the cost is a recurring degraded panel / failed tool call, not silent divergence.

## Grounding facts

1. `agent/worker_client.py:123` — `_HTTPX_TIMEOUT: Final[float] = 30.0` (one float ⇒ all four httpx phases at 30s). `call()` at `:375` builds the client with `timeout if timeout is not None else _HTTPX_TIMEOUT`.
2. Precedent: `_APPLY_HTTPX_TIMEOUT` (`:135`, `httpx.Timeout(connect=10.0, read=920.0, write=30.0, pool=10.0)`) passed only by `call_apply` (`:604`) — the C5e fix for the same misclassification class.
3. Exactly **two** infra_reader call sites, both via `worker_client.call("infra_reader", {})`: `agent/adk_tools.py:78` (`read_project_inventory_tool` — explore + provision chat, incl. adoption flows) and `agent/main.py:1925` (the `/infra/graph` proxy, which soft-fails `WorkerClientError` to a degraded 200). `/infra/graph/preview` does NOT call infra_reader (it resolves the C2 plan artifact ladder only) — verified by grep + read.
4. Measured wall clocks (item-3 live verify, 2026-06-12): warm solo describe ~25–30s; the incident fetch degraded at 30.1s; cold `/infra/graph` ≈25s; 4-parallel burst at the new concurrency 8 completed all-clean in 47.8s wall. Estate ~467 CAI resources and growing — solo latency scales with estate size.
5. Outer budget envelope — the **binding constraint is the Cloudflare edge, not Cloud Run** (Codex 019eba89 must-fix 2): the operator reaches the SPA and `/infra/graph` through the Cloudflare-proxied custom domain, whose proxied-response budget is ~100s (the repo already works around the related ~120s read-idle limit for SSE — `agent/main.py:4378` heartbeat comment). Behind the edge: the infra-reader deploy sets **no** `--timeout` ⇒ Cloud Run default 300s; the coordinator runs `--timeout=300` (`infra/cloudbuild.yaml:277`); the SPA's graph fetch has no AbortController. A 90s worker read keeps the whole coordinator response under ~95s — inside the edge budget — while 120s would not.
6. Test infrastructure exists: `tests/unit/test_worker_client.py:747` `_capture_timeout` fixture (records the `timeout` each constructed `httpx.Client` was built with); pins at `:764` (apply long), `:778`/`:789` (propose/deny default), `:797` (per-call override honored), `:807` (default fallback). **No pin covers infra_reader's timeout today.** The module defines `READER_URL`/`TOFU_APPLY_URL`-style constants; the implementer adds the infra-reader equivalent following the module's existing env-wiring pattern.
7. SPA fetch-stacking (verified for must-fix 1): `InfraDiagram.refresh()` (`frontend/src/components/InfraDiagram.svelte:197-245`) starts a fetch unconditionally — `fetchRun`/`lastAppliedFetch` guard response *application* (the PR #99 last-applied-wins livelock fix), never request *starts*. Triggers that call it fire-and-forget: the RefreshScheduler's 45s poll + 0/10/30/60s apply ladder + focus (`infra_refresh.ts`), and mount/expand. Only the manual Refresh button is already safe (`disabled={loading || mermaidLoading}`, `:478`). With a 90s-capable fetch, the 45s poll alone can hold TWO coordinator slots (concurrency=2) and starve `/chat`.
8. Comment audit for staleness: `call()`'s docstring (`:345-349`) claims "Only :func:`call_apply` passes a longer value" and calls the default "fine for every short worker call" — **both must be updated**. NOT stale: `agent/fanout.py:1393` (a **notifier** call — stays 30s, accurate), `workers/tofu_editor/main.py:126,130` (tofu_editor calls keep 30s — accurate), `frontend/src/lib/decision.ts:123-134` (past-tense narration of the 2026-06-11 boot-stampede incident — history, stays). `get_baked_iac_hash` (`:622`) is a tofu-apply GET — unaffected.

## Design decisions

1. **Per-worker default-timeout map, not a wrapper, not a global bump.**
   ```python
   # Backlog-3 residual (2026-06-12): the infra-reader's /describe pages the
   # whole CAI estate (~467 resources today) and legitimately takes ~25-30s
   # solo — one live fetch was misread as a transport failure at 30.1s
   # against the 30s default. Same misclassification class as C5e/apply, at
   # lower stakes: describe is read-only and RECOVERABLE, so we size the read
   # budget from observed wall clock (~3x worst, headroom for loaded slots +
   # estate growth), NOT from the worker's Cloud Run ceiling the way /apply
   # must. Upper bound: the operator-facing /infra/graph rides the
   # Cloudflare-proxied custom domain (~100s proxied-response budget — see
   # the SSE heartbeat comment in agent/main.py), so the coordinator's
   # response must finish under ~100s; 90s read + overhead fits, 120 would
   # not. connect stays tight so a down worker still fails fast.
   _DESCRIBE_HTTPX_TIMEOUT: Final = httpx.Timeout(
       connect=10.0, read=90.0, write=30.0, pool=10.0
   )

   # Per-worker DEFAULT timeouts, consulted by ``call`` only when the caller
   # passes no explicit ``timeout=``. Keyed like WORKER_ENDPOINTS; any worker
   # not listed gets _HTTPX_TIMEOUT. Endpoint-specific overrides (call_apply)
   # keep passing explicitly and win over this map by construction.
   _WORKER_DEFAULT_TIMEOUTS: Final[dict[str, "float | httpx.Timeout"]] = {
       "infra_reader": _DESCRIBE_HTTPX_TIMEOUT,
   }
   ```
   `call()`'s fallback becomes `_WORKER_DEFAULT_TIMEOUTS.get(worker, _HTTPX_TIMEOUT)`. By construction every infra_reader call — both current sites and any future one — gets the right budget with zero call-site edits. **Rejected:** (a) a named `call_describe()` wrapper — the named-wrapper doctrine exists for endpoint-path *security*, not timeouts; `/describe` is already the worker's only (default) endpoint, and a wrapper leaves bare `call("infra_reader", ...)` callable with the wrong default — the map fixes the default itself (Codex concurs: Layer-0 safe; transport policy, not endpoint authority); (b) raising the global 30s — would slow failure detection for every other worker to cover one worker's workload.
2. **Timeout bump, not coordinator-side caching.** Caching/dedup of describes is a larger change with staleness semantics (CAI is already eventually-consistent; `/infra/graph` deliberately sends `Cache-Control: no-store`; the RefreshScheduler exists precisely to re-fetch). The bug here is *misclassification* — the worker SUCCEEDS at ~30s and the client calls it a failure. Fix the classification; caching stays a possible future optimization if describe volume itself becomes a problem.
3. **read=90s** (was 120s in draft; lowered per Codex must-fix 2). ~3× the observed solo worst (fact 4) with headroom for concurrency-8 loaded slots and estate growth, and the largest value that keeps the operator-facing response inside the Cloudflare ~100s edge budget (fact 5) — a 110s-successful describe under a 120s budget would have the edge 524 the browser while the coordinator was still "succeeding." `connect=10.0` preserves fail-fast on a down/unreachable worker — the common real-failure mode. Deliberately NOT apply's above-worker-wall-clock rule (920>900): a premature describe timeout costs a degraded panel, not an unrecorded infra mutation.
4. **Frontend in-flight coalescing** (Codex must-fix 1). `InfraDiagram.refresh()` gains a module-state guard: if a refresh is already in flight, a new trigger returns immediately (coalesce — the pending response is at most seconds stale, and the panel is advisory).
   - Placement: the component, not the scheduler — it must guard EVERY trigger (poll, ladder, focus, expand, button) at the single choke point, and changing the scheduler's fire-and-forget `onFetch: () => void` contract to promise-tracking is a bigger interface change for the same effect.
   - **Must not regress PR #99 (last-applied-wins):** the guard suppresses request *starts*, upstream of the response-application logic, which stays byte-identical. No livelock is possible: the in-flight fetch always completes and (as the only outstanding run) always applies; the next trigger after completion fetches fresh. The skipped ladder rung is covered by the later rungs (10s rung skipped while the 0s fetch runs → 30s/60s rungs still ride out CAI lag).
   - Failure-mode math this buys: at most ONE `/infra/graph` request in flight per open panel — the 45s poll against a 90s-capable fetch can no longer hold both coordinator slots.
5. **Failure-mode honesty:** a genuinely hung describe now occupies a `/chat` slot (coordinator concurrency=2) for up to 90s instead of 30s before the tool call fails, and one slot for up to 90s on the graph path. Accepted: true hangs are rare (Cloud Run kills the worker request at 300s), the graph path is capped at one in-flight by decision 4, and the false-timeout case is recurring and worsens as the estate grows.
6. **Pins (mirroring the C5e-1 block):** (a) `call("infra_reader", {})` builds its client with `_DESCRIBE_HTTPX_TIMEOUT` (identity assert), `timeout.read >= 90.0` (semantic floor: ≥3× observed worst), `timeout.read <= 100.0` (the edge-budget ceiling — fact 5), `timeout.connect == 10.0` (fail-fast preserved), `timeout.write == 30.0` and `timeout.pool == 10.0` (all four phases named — Codex strengthening); (b) an explicit per-call `timeout=` override on infra_reader still wins over the map; (c) the existing `:807` default test gains `is not _DESCRIBE_HTTPX_TIMEOUT` (symmetric with its apply assert) so unlisted workers provably keep 30s.
7. **Docstring/comment updates:** rewrite `call()`'s `timeout:` arg doc (`:345-349`) to describe the two-level default (per-worker map, then `_HTTPX_TIMEOUT`) and name both long-budget cases — drop "fine for every short worker call" (fact 8); extend the `_HTTPX_TIMEOUT` comment (`:119-122`) with a pointer to the map.
8. **Ship surface:** `agent/worker_client.py` and the SPA are both coordinator-image content (`Dockerfile.agent`) ⇒ coordinator rebake + traffic pin. No worker rebakes (no gate/denylist/worker change), no deploy-flag changes.
9. **Live verify:** (a) new revision serving at the squash SHA; (b) 6 sequential warm authenticated `/infra/graph` fetches via run.app — ALL `degraded` falsy, timings logged; (c) re-run the item-3 4-parallel burst — ALL `degraded` falsy; (d) one authenticated `/infra/graph` GET through the Cloudflare custom domain (the operator's real path) — 200, `degraded` falsy; (e) coalescing: Playwright (or served-bundle inspection if a browser run is disproportionate) confirming an open panel issues no overlapping `/infra/graph` requests across a poll boundary. Honesty note: the incident was intermittent (30.1s vs a 30.0s edge), so clean probes alone can't prove the fix — the structural proof is the 90s headroom plus the pins; the probes prove no regression.

## Out of scope

- Coordinator-side describe caching/dedup (decision 2). Any other worker's timeout (notifier/editor/reader budgets are accurate today — fact 8). Infra-reader scaling flags (done, PR #113). The frontend's historical-incident comment (`decision.ts:123-134` — accurate past tense). Scheduler-contract changes (`onFetch` stays fire-and-forget — decision 4). Coordinator concurrency (its `--concurrency=2` rationale is documented and unchanged).

## Tasks

### Task 1: failing backend pins

**Files:** Modify `tests/unit/test_worker_client.py` (extend the C5e-1 per-call-timeout block).

1. Add the infra-reader URL constant + env wiring following the module's existing pattern for `READER_URL`/`TOFU_APPLY_URL` (check how those reach `_worker_url` — fixture/monkeypatch — and mirror it; do not invent a new mechanism).
2. Add:
   ```python
   @respx.mock
   def test_call_infra_reader_uses_describe_timeout(_capture_timeout) -> None:
       """Backlog-3 residual: a solo CAI describe takes ~25-30s, so infra_reader
       calls get _DESCRIBE_HTTPX_TIMEOUT by default (per-worker map), not the
       30s _HTTPX_TIMEOUT that misread a successful describe as a failure."""
       respx.post(f"{INFRA_READER_URL}/describe").respond(200, json={"resources": []})
       worker_client.call("infra_reader", {})
       assert len(_capture_timeout) == 1
       timeout = _capture_timeout[0]
       assert timeout is worker_client._DESCRIBE_HTTPX_TIMEOUT
       # Floor: >= 3x the observed solo worst (~25-30s warm describe).
       # Ceiling: the Cloudflare-proxied operator path allows ~100s total.
       assert 90.0 <= timeout.read <= 100.0
       assert timeout.connect == 10.0  # down worker still fails fast
       assert timeout.write == 30.0
       assert timeout.pool == 10.0

   @respx.mock
   def test_call_infra_reader_explicit_override_beats_the_map(_capture_timeout) -> None:
       """A caller-supplied timeout= wins over the per-worker default map."""
       respx.post(f"{INFRA_READER_URL}/describe").respond(200, json={"resources": []})
       custom = httpx.Timeout(connect=1.0, read=2.0, write=3.0, pool=4.0)
       worker_client.call("infra_reader", {}, timeout=custom)
       assert _capture_timeout == [custom]
   ```
3. Strengthen `test_call_default_timeout_when_no_override` (`:807`): add `assert _capture_timeout[0] is not worker_client._DESCRIBE_HTTPX_TIMEOUT`.
4. Run `.venv/bin/pytest tests/unit/test_worker_client.py -q` → expect the two new tests to FAIL (`AttributeError: _DESCRIBE_HTTPX_TIMEOUT`) and `:807` to fail on the missing attribute too; everything else PASS.
5. Commit: `test(worker-client): pin infra_reader describe timeout (failing)`

### Task 2: implement the per-worker default

**Files:** Modify `agent/worker_client.py`.

1. Add `_DESCRIBE_HTTPX_TIMEOUT` + `_WORKER_DEFAULT_TIMEOUTS` (decision-1 code + comments) below `_APPLY_HTTPX_TIMEOUT`.
2. Change `call()`'s client construction (`:375`) to `timeout=timeout if timeout is not None else _WORKER_DEFAULT_TIMEOUTS.get(worker, _HTTPX_TIMEOUT)`.
3. Update the `call()` docstring `timeout:` paragraph and the `_HTTPX_TIMEOUT` comment (decision 7).
4. Run: `.venv/bin/pytest tests/unit/test_worker_client.py -q` → PASS; then `.venv/bin/ruff check --no-cache .`
5. Commit: `fix(coordinator): infra_reader describe gets a 90s read budget — stop misreading a ~30s CAI describe as a failure`

### Task 3: frontend in-flight coalescing (TDD)

**Files:** Modify `frontend/src/components/InfraDiagram.svelte:197`, `frontend/tests/unit/InfraDiagram.test.ts`.

1. Write the failing test in `InfraDiagram.test.ts` following the file's existing stubbed-fetch component-test pattern: hold the first `/infra/graph` response unresolved (deferred promise), trigger a second refresh (whichever existing trigger the test file already exercises — e.g. the focus/poll path or a direct scheduler tick), assert the fetch stub was called exactly ONCE; then resolve, trigger again, assert a second call goes out. Run `cd frontend && npm run test:unit` → new test FAILS (two calls observed).
2. Implement: a `let refreshInFlight = false;` component flag; first lines of `refresh()` become `if (refreshInFlight) return; refreshInFlight = true;` with `refreshInFlight = false;` added in the existing `finally`. A short comment stating the constraint: one in-flight graph fetch per panel — the describe budget is now 90s and the coordinator runs concurrency=2, so stacked polls/ladder rungs must coalesce; response-application logic (PR #99 last-applied-wins) unchanged.
3. Run: `npm run test:unit` PASS (including the existing livelock/last-applied-wins tests untouched); `npm run check` clean. Full backend suite `.venv/bin/pytest -q` (unchanged, for the PR gate).
4. Commit: `fix(ui): coalesce in-flight /infra/graph fetches — one outstanding describe per open panel`

## Ship steps

1. Branch `fix/infra-reader-describe-timeout`, PR, CI watch, Codex completed-work review (same thread as plan review), squash-merge.
2. Coordinator rebake at the squash SHA → find revision by digest → `update-traffic` 100%. No worker rebakes (decision 8).
3. Live verify per decision 9 (revision + 6 sequential + 4-parallel burst + custom-domain GET + coalescing check).
4. Memory (close the residual; new coordinator revision pointer) + closing report.
