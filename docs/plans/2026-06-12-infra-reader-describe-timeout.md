# Coordinator describe timeout: stop misreading a legitimate ~30s CAI describe as a failure

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Close the backlog-3 residual — a single infra-reader `/describe` (~25–30s of CAI paging) sits at the coordinator's 30s `worker_client` timeout edge, so a legitimately-successful describe is intermittently misread as a transport failure (degraded graph, failed chat tool call). Give infra_reader calls a read budget comfortably above the observed wall clock, via a per-worker default timeout.

**Architecture:** Coordinator-only change in `agent/worker_client.py`: a named `_DESCRIBE_HTTPX_TIMEOUT` constant plus a per-worker default-timeout map consulted by `call()` when the caller passes no explicit timeout. Zero call-site changes, zero worker changes, zero deploy-flag changes. Pins mirror the existing `_capture_timeout` apply-timeout test pattern. Coordinator rebake only.

**Tech Stack:** `agent/worker_client.py` (httpx), pytest (`respx` + the existing `_capture_timeout` fixture).

---

## Background (the residual)

Recorded during backlog item 3 (PR #113, infra-reader concurrency 1→8): with head-of-line blocking gone, a *single* warm `/infra/graph` fetch still came back `degraded: true` at **30.1s** — the worker's CAI describe legitimately takes ~25–30s and the coordinator's `worker_client` 30s timeout misclassifies the slow-but-successful case as a worker failure. This is the same bug class C5e fixed for `tofu apply` (a long-but-successful operation misread as transport failure), at much lower stakes: describe is read-only and recoverable, so the cost is a recurring degraded panel / failed tool call, not silent divergence.

## Grounding facts

1. `agent/worker_client.py:123` — `_HTTPX_TIMEOUT: Final[float] = 30.0` (one float ⇒ all four httpx phases at 30s). `call()` at `:375` builds the client with `timeout if timeout is not None else _HTTPX_TIMEOUT`.
2. Precedent: `_APPLY_HTTPX_TIMEOUT` (`:135`, `httpx.Timeout(connect=10.0, read=920.0, write=30.0, pool=10.0)`) passed only by `call_apply` (`:604`) — the C5e fix for the same misclassification class.
3. Exactly **two** infra_reader call sites, both via `worker_client.call("infra_reader", {})`: `agent/adk_tools.py:78` (`read_project_inventory_tool` — explore + provision chat, incl. adoption flows) and `agent/main.py:1925` (the `/infra/graph` proxy, which soft-fails `WorkerClientError` to a degraded 200). `/infra/graph/preview` does NOT call infra_reader (it resolves the C2 plan artifact ladder only) — verified by grep + read.
4. Measured wall clocks (item-3 live verify, 2026-06-12): warm solo describe ~25–30s; the incident fetch degraded at 30.1s; cold `/infra/graph` ≈25s; 4-parallel burst at the new concurrency 8 completed all-clean in 47.8s wall. Estate ~467 CAI resources and growing — solo latency scales with estate size.
5. Outer budget envelope: the infra-reader deploy sets **no** `--timeout` ⇒ Cloud Run default 300s request timeout; the coordinator runs `--timeout=300` (`infra/cloudbuild.yaml:277`); the SPA's graph fetch has no AbortController (and the Playwright recipe already waits 90s for a cold graph). A 120s client read fits inside every outer budget.
6. Test infrastructure exists: `tests/unit/test_worker_client.py:747` `_capture_timeout` fixture (records the `timeout` each constructed `httpx.Client` was built with); pins at `:764` (apply long), `:778`/`:789` (propose/deny default), `:797` (per-call override honored), `:807` (default fallback). **No pin covers infra_reader's timeout today.** The module defines `READER_URL`/`TOFU_APPLY_URL`-style constants; the implementer adds the infra-reader equivalent following the module's existing env-wiring pattern.
7. Comment audit for staleness: `call()`'s docstring (`:345-349`) claims "Only :func:`call_apply` passes a longer value" — **must be updated**. NOT stale: `agent/fanout.py:1393` (a **notifier** call — stays 30s, accurate), `workers/tofu_editor/main.py:126,130` (tofu_editor calls keep 30s — accurate), `frontend/src/lib/decision.ts:123-134` (past-tense narration of the 2026-06-11 boot-stampede incident — history, stays). `get_baked_iac_hash` (`:622`) is a tofu-apply GET — unaffected.

## Design decisions

1. **Per-worker default-timeout map, not a wrapper, not a global bump.**
   ```python
   # Backlog-3 residual (2026-06-12): the infra-reader's /describe pages the
   # whole CAI estate (~467 resources today) and legitimately takes ~25-30s
   # solo — one live fetch was misread as a transport failure at 30.1s
   # against the 30s default. Same misclassification class as C5e/apply, at
   # lower stakes: describe is read-only and RECOVERABLE, so we size the read
   # budget from observed wall clock (~4x worst, headroom for loaded slots +
   # estate growth), NOT from the worker's Cloud Run ceiling the way /apply
   # must. connect stays tight so a down worker still fails fast.
   _DESCRIBE_HTTPX_TIMEOUT: Final = httpx.Timeout(
       connect=10.0, read=120.0, write=30.0, pool=10.0
   )

   # Per-worker DEFAULT timeouts, consulted by ``call`` only when the caller
   # passes no explicit ``timeout=``. Keyed like WORKER_ENDPOINTS; any worker
   # not listed gets _HTTPX_TIMEOUT. Endpoint-specific overrides (call_apply)
   # keep passing explicitly and win over this map by construction.
   _WORKER_DEFAULT_TIMEOUTS: Final[dict[str, "float | httpx.Timeout"]] = {
       "infra_reader": _DESCRIBE_HTTPX_TIMEOUT,
   }
   ```
   `call()`'s fallback becomes `_WORKER_DEFAULT_TIMEOUTS.get(worker, _HTTPX_TIMEOUT)`. By construction every infra_reader call — both current sites and any future one — gets the right budget with zero call-site edits. **Rejected:** (a) a named `call_describe()` wrapper — the named-wrapper doctrine exists for endpoint-path *security*, not timeouts; `/describe` is already the worker's only (default) endpoint, and a wrapper leaves bare `call("infra_reader", ...)` callable with the wrong default — the map fixes the default itself; (b) raising the global 30s — would slow failure detection for every other worker to cover one worker's workload.
2. **Timeout bump, not coordinator-side caching.** Caching/dedup of describes is a larger change with staleness semantics (CAI is already eventually-consistent; `/infra/graph` deliberately sends `Cache-Control: no-store`; the RefreshScheduler exists precisely to re-fetch). The bug here is *misclassification* — the worker SUCCEEDS at ~30s and the client calls it a failure. Fix the classification; caching stays a possible future optimization if describe volume itself becomes a problem.
3. **read=120s.** ~4× the observed solo worst (fact 4), with headroom for concurrency-8 loaded slots and estate growth; still well inside the coordinator's own 300s request budget and the worker's 300s Cloud Run timeout, so a true hang still resolves within the request's life. `connect=10.0` preserves fail-fast on a down/unreachable worker — the common real-failure mode. Deliberately NOT apply's above-worker-wall-clock rule (920>900): a premature describe timeout costs a degraded panel, not an unrecorded infra mutation.
4. **Failure-mode honesty:** a genuinely hung describe now occupies a `/chat` slot (coordinator concurrency=2) for up to 120s instead of 30s before the tool call fails. Accepted: true hangs are rare (and Cloud Run kills the worker request at 300s), while the false-timeout case is recurring and worsens as the estate grows.
5. **Pins (mirroring the C5e-1 block):** (a) `call("infra_reader", {})` builds its client with `_DESCRIBE_HTTPX_TIMEOUT` (identity assert), `timeout.read >= 120.0` (semantic floor: ≥4× observed worst), `timeout.connect == 10.0` (fail-fast preserved); (b) an explicit per-call `timeout=` override on infra_reader still wins over the map; (c) the existing `:807` default test gains `is not _DESCRIBE_HTTPX_TIMEOUT` (symmetric with its apply assert) so unlisted workers provably keep 30s.
6. **Docstring/comment updates:** rewrite `call()`'s `timeout:` arg doc (`:345-349`) to describe the two-level default (per-worker map, then `_HTTPX_TIMEOUT`) and name both long-budget cases; extend the `_HTTPX_TIMEOUT` comment (`:119-122`) with a pointer to the map.
7. **Ship surface:** `agent/worker_client.py` is coordinator code (baked via `Dockerfile.agent`) ⇒ coordinator rebake + traffic pin. No worker rebakes (no gate/denylist/worker change), no deploy-flag changes.
8. **Live verify:** (a) new revision serving at the squash SHA; (b) 6 sequential warm authenticated `/infra/graph` fetches — ALL `degraded` falsy, timings logged; (c) re-run the item-3 4-parallel burst — ALL `degraded` falsy. Honesty note: the incident was intermittent (30.1s vs a 30.0s edge), so clean probes alone can't prove the fix — the structural proof is the 120s headroom plus the pins; the probes prove no regression.

## Out of scope

- Coordinator-side describe caching/dedup (decision 2). Any other worker's timeout (notifier/editor/reader budgets are accurate today — fact 7). Infra-reader scaling flags (done, PR #113). The frontend's historical-incident comment (`decision.ts:123-134` — accurate past tense).

## Tasks

### Task 1: failing pins

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
       # >= 4x the observed solo worst (~25-30s warm describe, 2026-06-12).
       assert timeout.read >= 120.0
       assert timeout.connect == 10.0  # down worker still fails fast

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
3. Update the `call()` docstring `timeout:` paragraph and the `_HTTPX_TIMEOUT` comment (decision 6).
4. Run: `.venv/bin/pytest tests/unit/test_worker_client.py -q` → PASS; full `.venv/bin/pytest -q`; `.venv/bin/ruff check --no-cache .`
5. Commit: `fix(coordinator): infra_reader describe gets a 120s read budget — stop misreading a ~30s CAI describe as a failure`

## Ship steps

1. Branch `fix/infra-reader-describe-timeout`, PR, CI watch, Codex completed-work review (same thread as plan review), squash-merge.
2. Coordinator rebake at the squash SHA → find revision by digest → `update-traffic` 100%. No worker rebakes (decision 7).
3. Live verify per decision 8 (revision + 6 sequential + 4-parallel burst, all `degraded` falsy).
4. Memory (close the residual; new coordinator revision pointer) + closing report.
