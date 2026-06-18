# Tier 3 — Firestore-backed L2 cache + optional pre-warm for `GET /infra/graph`

**Date:** 2026-06-18
**Branch:** `perf/infra-graph-cache` (extends the already-implemented tier-1 in-process cache)
**Status:** spec — for Codex review, then TDD implementation

## Problem

`GET /infra/graph` proxies the `infra_reader` worker, which runs a live Cloud Asset
Inventory `SearchAllResources` over the whole project (~515 resources) — **25–35s per
call** (measured live). The Infrastructure panel re-fetches on every page load, so the
panel shows "loading" for ~half a minute whenever the cache is cold.

Tier 1 (already on this branch) added a **process-local in-process TTL cache**
(`_INFRA_INVENTORY_CACHE`, 60s default, lock-free, success-only). It makes repeated loads
instant **within one warm instance**.

### The gap tier 1 cannot close

The coordinator runs `--min-instances=0 --max-instances=1` (infra/cloudbuild.yaml:267-268).
Consequences:

- **There is never more than one instance** → the "cross-instance cache sharing" argument
  for a shared store is *moot* here. (Recorded honestly: a shared store would matter only
  if `--max-instances` were raised.)
- **The single instance scales to zero after ~15 min idle.** When it recycles, the next
  request hits a fresh instance with an empty in-process cache → a cold 25–35s fetch. This
  is the "first load after the demo's been idle is slow" first-impression problem. Tier 1
  fundamentally cannot survive instance recycling; an at-rest store can.

So tier 3's value proposition is **cold-start / instance-recycle survival**, not
multi-instance scaling.

## Design

A two-layer read-through cache plus an optional pre-warm trigger:

```
GET /infra/graph
  ├─ L1 (in-process, tier 1, monotonic clock, TTL = INFRA_GRAPH_CACHE_TTL_S, default 60s)
  │     hit → build_graph(cached) → 200   [X-Infra-Graph-Cache: hit]
  ├─ L2 (Firestore singleton doc config/infra_graph_cache, wall clock,
  │      TTL = INFRA_GRAPH_L2_CACHE_TTL_S, default 900s)
  │     hit → build_graph(cached) → 200   [X-Infra-Graph-Cache: hit-l2]
  └─ live → worker_client.call("infra_reader") (~30s)
            success → persist to BOTH L1 and L2 → build_graph → 200  [miss]
            error   → degraded DTO, NOT cached → 200                  [miss]
```

### Why this shape

- **L2 survives cold starts.** A freshly-recycled instance with an empty L1 reads the
  Firestore doc (~tens of ms) and serves a warm map instantly, as long as the doc is within
  `INFRA_GRAPH_L2_CACHE_TTL_S`. With a 15-min L2 TTL, reopening the page within 15 min of
  the last live fetch is instant even on a brand-new instance.
- **Steady-state is already mostly invisible without the scheduler.** The frontend
  `RefreshScheduler` polls every 45s while the page is open, keeping L1/L2 warm during
  active use. When both layers eventually expire, the next poll triggers ONE background
  live fetch — but the SPA keeps rendering the previously-loaded graph (its badge logic is
  `loading && !graph`), so a periodic 30s refresh behind an already-rendered panel is
  invisible. The only time a user sees "loading" is a **true cold open**: fresh instance,
  no L1, expired/absent L2, no previously-rendered graph.
- **The pre-warm closes that last gap** by keeping L2 always fresh so even a true cold open
  is instant. It is **optional** (see below).

### What we cache, and at-rest safety

Cache the **raw inventory with `declared_not_found` stripped**, and re-run `build_graph` on
every serve (mirrors tier 1's deliberate "store inventory, build on read" property — a
`build_graph` logic change reflects immediately, no stale labels/ranks).

- `build_graph` **never reads `declared_not_found`** (driftscribe_lib/infra_graph.py never
  references it), so stripping it is a provable no-op for the served DTO.
- `declared_not_found` is the only field carrying full CAI resource *paths*; stripping it
  before persisting removes the only meaningful topology-at-rest exposure. Secret resource
  *names* are already dropped by `build_inventory` (sensitive types get no `sample`), and
  `build_graph` defensively emits zero nodes for sensitive types regardless.
- Stripping is applied in one shared `_persist_infra_inventory()` helper used by both the
  live-fetch path and the refresh endpoint, so L1 and L2 store the identical stripped dict.
  (L1 is in-memory so the at-rest concern doesn't apply to it, but a single write path is
  simpler and `build_graph`-equivalent.)

A `format_version` integer is stamped into the L2 doc and checked on read; a mismatch is
treated as a miss. This invalidates stale-shaped docs across a deploy that changes the
cached-inventory contract (L1 is naturally cleared by instance recycle; L2 persists across
deploys, so it needs an explicit version gate).

### Clocks

- L1 keeps `time.monotonic()` (per-process, unchanged).
- L2 uses **wall clock** `time.time()` for `written_at` and the read-time age comparison.
  Monotonic values are meaningless across processes/instances. Both write and read use the
  coordinator's own `time.time()` (all GCP infra is NTP-synced; cache correctness is not a
  security boundary, so client-clock is acceptable and avoids mixing Firestore server time
  with coordinator time).

### Stampede

With `--max-instances=1 --concurrency=2`, a double live-fetch is possible but rare (single
user) and costs only one extra 30s call. The house L1 cache is deliberately lock-free
(Codex-approved); L2 follows suit — **no distributed lock / transaction**. Documented as an
accepted limitation, consistent with tier 1. (A `@firestore.transactional` CAS is a possible
future hardening but is over-engineering for a single-user demo.)

## Components

### 1. `agent/state_store.py` — new `InfraGraphCacheStore`

Mirror the `StateStore` / `InMemoryStateStore` / `FirestoreStateStore` house pattern:

```python
class InfraGraphCacheStore(Protocol):
    def get(self) -> dict | None: ...          # the stored record, or None on miss
    def set(self, record: dict) -> None: ...    # full-overwrite

class InMemoryInfraGraphCacheStore:             # test double, plain dict
class FirestoreInfraGraphCacheStore:            # client=None injectable; config/infra_graph_cache
```

- Record shape: `{"written_at": <float epoch>, "format_version": <int>, "payload": <stripped inventory dict>}`.
- The store is a **dumb singleton-doc persistence layer**: TTL / version logic lives in the
  caller (`get_infra_graph`), matching tier 1's "caller owns the freshness math" approach
  and keeping the store trivially testable.
- `FirestoreInfraGraphCacheStore` writes `config/infra_graph_cache` via full-overwrite
  `set()` (same singleton precedent as `config/pause`), and point-reads on `get()`.
  Fail-soft: a Firestore exception on read returns `None` (→ fall through to live); a
  Firestore exception on write is logged and swallowed (the response is already built — a
  cache write failure must never fail the request).

### 2. `agent/main.py`

- `_infra_graph_cache_store_singleton` + `get_infra_graph_cache_store()` selecting
  `InMemoryInfraGraphCacheStore` when `dry_run or not gcp_project`, else
  `FirestoreInfraGraphCacheStore(project=...)` — exact mirror of `get_state()`.
- `_reset_infra_graph_cache_store_for_tests()` nulls the singleton.
- `_persist_infra_inventory(inventory)` — strips `declared_not_found`, writes L1 (monotonic)
  and L2 (wall clock + `format_version`). Used by the live path and the refresh endpoint.
- Rewrite `get_infra_graph` to the L1 → L2 → live chain above. Header contract:
  - `Cache-Control: no-store` unconditional (unchanged).
  - `X-Infra-Graph-Cache`: `hit` (L1) | `hit-l2` (L2) | `miss` (live) | `disabled` (both TTLs ≤ 0).
  - `X-Infra-Graph-Cache-Age-S` on `hit` and `hit-l2`.
  - L1 and L2 gates are independent: L1 disabled still allows L2; L2 disabled falls to live.
- New `POST /internal/infra-graph/refresh` (pre-warm activation point):
  - 503 if `infra_prewarm_audience` or `gcp_project` unset (fail-closed, dormant until provisioned).
  - OIDC-verify via the new `verify_oidc_caller` helper: audience = `infra_prewarm_audience`
    (the full endpoint URL Cloud Scheduler stamps as `--oidc-token-audience`), email allowlist
    = `infra-prewarm-sa@{gcp_project}.iam.gserviceaccount.com`. 401 on token failure, 403 on
    email mismatch (mirrors `/eventarc`).
  - On success: force a live `infra_reader` fetch (bypassing L1/L2 read), `_persist_infra_inventory`,
    return `200 {"cached": true, "resource_count": N}`.
  - On worker error: **fail-soft 200** `{"cached": false, "reason": ...}` (log a warning). 200
    so Cloud Scheduler doesn't treat a transient worker blip as a failure and retry-storm the
    slow worker; the next tick will warm it.

### 3. `driftscribe_lib/auth.py` — new `verify_oidc_caller`

```python
def verify_oidc_caller(request, *, audience, allowed_emails, transport) -> dict:
    # Bearer header → verify_oauth2_token(token, transport, audience=audience) → 401 on any failure
    # email claim ∈ allowed_emails (hmac.compare_digest) → 403 on mismatch
```

Reusable OIDC entry-guard extracted alongside the existing `verify_caller`. Used by the new
endpoint now; `/eventarc` is **not** refactored onto it in this PR (avoid touching the live
drift path) — noted as a future consolidation.

### 4. `agent/config.py`

- `infra_graph_l2_cache_ttl_s: float = 900.0` + the same `math.isfinite` non-finite-fallback
  validator as `infra_graph_cache_ttl_s`. `<= 0` disables L2.
- `infra_prewarm_audience: str = ""` (empty → refresh endpoint 503s; dormant until set).

### 5. `tests/integration/conftest.py`

- Import + call `_reset_infra_graph_cache_store_for_tests()` in both setup and teardown of
  the autouse fixture.
- **Set `INFRA_GRAPH_L2_CACHE_TTL_S=0` by default** so the pre-existing tier-1 tests (which
  assert exact `miss`/`disabled`/worker-call-count behavior) are unaffected by the new L2
  layer. The dedicated L2 tests opt in by monkeypatching the TTL, mirroring tier 1's
  `_set_ttl` helper.

### 6. Tests (TDD)

- **Unit** `tests/unit/test_infra_graph_cache_store.py`: `InMemoryInfraGraphCacheStore`
  round-trip + miss; `FirestoreInfraGraphCacheStore` doc name / write payload / read mapping
  via an injected fake (mirror `test_state_store.py`'s `_build_firestore_mock`); read/write
  exceptions are swallowed → `None` / no-raise.
- **Unit** `tests/unit/test_oidc_caller.py`: `verify_oidc_caller` — happy path returns claims;
  missing/malformed Bearer → 401; bad audience/expired (verify raises) → 401; email not in
  allowlist → 403.
- **Integration** (extend `test_infra_graph_endpoint.py`):
  - L2 hit: L1 disabled, L2 enabled → 2nd request served from L2, worker called once,
    `X-Infra-Graph-Cache: hit-l2`, age header present.
  - L2 boundary / just-past-TTL expiry (monkeypatch `agent.main.time.time`).
  - L1 takes precedence over L2 when both warm (`hit`, not `hit-l2`).
  - degraded inventory + `WorkerClientError` never written to L2.
  - both TTLs ≤ 0 → `disabled`, worker called every time.
  - `declared_not_found` stripped from what L2 stores (planted path absent from the stored
    record) yet served DTO is byte-identical.
  - refresh endpoint: 503 when audience unset; 401 no/invalid token; 403 wrong SA; 200 +
    `cached:true` + L2 populated on success (worker stub); 200 + `cached:false` on worker error,
    L2 untouched.

### 7. Infra — codified, **NOT executed** (optional pre-warm)

The pre-warm has no in-repo precedent (`cloudscheduler.googleapis.com` is never enabled; no
tofu scheduler resource; IAM lives in `setup_secrets.sh`, not tofu). So:

- **Env wiring**: add `INFRA_PREWARM_AUDIENCE` to the `^@^`-delimited `ENV_VARS` in
  `infra/cloudbuild.coordinator-update.yaml` (guarded-append idiom) and to the `--set-env-vars`
  line in `infra/cloudbuild.yaml`. Default empty → endpoint stays dormant; setting it +
  provisioning the scheduler activates pre-warm.
- **`infra/scripts/setup_secrets.sh`**: a new `SETUP_INFRA_PREWARM=1`-gated block (mirroring
  the `SETUP_EVENTARC=1` gate) that: enables `cloudscheduler.googleapis.com`; creates
  `infra-prewarm-sa` via `create_service_account_idempotent`; grants it `run.invoker` on
  `driftscribe-agent`; creates the `gcloud scheduler jobs create http` job (POST the refresh
  URL, `--oidc-service-account-email=infra-prewarm-sa@…`, `--oidc-token-audience=<refresh URL>`,
  every ~10 min, attempt deadline ≥ 60s for the ~30s CAI fetch).
- **Runbook** appended to this doc (activation steps + how to roll back / disable).
- The coordinator's runtime SA (`driftscribe-agent@…`) already has Firestore access (it writes
  Decision docs), so **no new IAM is needed for the L2 cache itself** — only the optional
  scheduler adds IAM. Confirm `datastore.user` (or equivalent) at activation.

## Graceful degradation (invariants)

- Firestore unavailable (read or write) → never fails `/infra/graph`; degrades to L1 + live.
- `infra_reader` unavailable → degraded 200 DTO, nothing cached (unchanged from tier 1).
- Pre-warm not provisioned → endpoint 503s; L2 still works (warmed by user-driven live fetches),
  delivering cold-start survival within the L2 TTL window.
- Both caches disabled (TTLs ≤ 0) → behaves exactly like pre-cache (live every time).

## Out of scope / deferred

- Raising `--max-instances` (would make the shared-cache argument also about scaling).
- Stale-while-revalidate serving (Cloud Run throttles background tasks → unreliable; strict
  TTL chosen for predictability).
- Distributed stampede lock (`@firestore.transactional`).
- Refactoring `/eventarc` onto `verify_oidc_caller`.
- A `min-instances=1` warm floor (the cheaper non-code alternative; orthogonal, operator's call).

## Rollout

Code (components 1–6) ships in the PR and is safe-by-default: L2 defaults to a 900s TTL in
prod (helping cold starts immediately) and is disabled in tests; the refresh endpoint is
dormant until `INFRA_PREWARM_AUDIENCE` is set. The scheduler (component 7) is a separate,
optional operator step. Deploy bundles the pending merged-not-deployed frontend batch
(#134/#135/#136) into the coordinator image — deploy timing is the operator's call.

## Addendum — Codex review resolutions (thread 019eda8c)

Codex reviewed this spec read-only; resolutions folded in (all agreed on merit):

1. **[must-fix] L2 backend selection must NOT mirror `get_state()`.** Verified: prod runs
   `_DRY_RUN: 'true'` (cloudbuild.yaml:83, the "Option C" stance), and `get_state()` returns
   `InMemoryStateStore` when `dry_run` is true (main.py:360). Mirroring that would make the
   Firestore L2 cache *in-memory in prod* — defeating cold-start survival. **Resolution:**
   `get_infra_graph_cache_store()` selects `FirestoreInfraGraphCacheStore` whenever
   `gcp_project` is set, **independent of `dry_run`** (a read-only resource-map cache has no
   side effects, so DRY_RUN's no-mutations stance is irrelevant). Tests use an explicit
   module-level injection seam (`_set_infra_graph_cache_store_for_tests`) plus the default
   `INFRA_GRAPH_L2_CACHE_TTL_S=0`, so they never construct a real client.

2. **[must-fix] Do not bake an empty `INFRA_PREWARM_AUDIENCE=` into the full-deploy
   `--set-env-vars` baseline** — a full deploy would clobber an activated value. **Resolution:**
   treat it exactly like `EVENTARC_AUDIENCE`: set it via a post-deploy `--update-env-vars`
   step inside the `SETUP_INFRA_PREWARM=1` activation block, never in the baseline env list.

3. **[must-fix] Fail-soft must wrap store construction AND validate the record on read.**
   **Resolution:** the Firestore client is constructed lazily *inside* the try/except of
   `get`/`set` (a construction/auth error → `None`/no-raise, never a 500). On read, a record
   is served only if it is a dict, `format_version` matches exactly, `written_at` is a finite
   number, `payload` is a dict, and `payload` has no `error` key — otherwise it's a miss.

4. **[must-fix] Clamp weird wall-clock ages.** **Resolution:** a `written_at` more than 60s
   in the future (clock skew / hand-edited doc) is treated as a miss; the reported
   `X-Infra-Graph-Cache-Age-S` is clamped to `max(0, age)`.

5. **[should-fix] Honest at-rest wording.** The persisted inventory still carries counts,
   asset types, locations, and non-sensitive sample names; stripping `declared_not_found`
   removes the full declared-vs-live **path/identity** details, not "all topology."

6. **[should-fix] Scheduler retry limits.** Auth/config bugs still yield 401/403/503 that
   Cloud Scheduler *would* retry. **Resolution:** the codified job sets
   `--max-retry-attempts=1` (low) and `--attempt-deadline=120s` (covers the ~30s CAI fetch).

7. **[should-fix] Scheduler keeps the instance warm.** A ~10-min job sends enough traffic to
   keep the coordinator (and likely infra_reader) from scaling to zero — an effective
   warm-floor with a small always-on cost. Documented as an operational/cost consequence of
   activating pre-warm (orthogonal to, and cheaper-to-skip than, `min-instances=1`).

8. **[should-fix] Add a default-prod-ish L2 test.** A test exercises the real
   `FirestoreInfraGraphCacheStore` path at the default 900s TTL via an injected fake
   Firestore client (write-on-miss → `hit-l2` on the next request), so the production code
   path isn't left under-tested by the test-default `=0`.

**Design calls Codex validated:** raw-inventory-minus-`declared_not_found` + rebuild-on-serve;
L1 monotonic / L2 wall-clock + `format_version` (bump on payload-contract change); no
stampede lock at `max-instances=1`/`concurrency=2` (worst case during a rollout's brief
two-revision overlap is extra CAI calls, not corruption); reusing the `config` collection
(tiny write volume; runtime SA already holds `datastore.user`, setup_secrets.sh:253).

## Post-implementation review (folded in)

Two review rounds after implementation:

**Codex completed-work review (thread 019eda8c)** — 3 fixed: `cached:true` even when the
Firestore write was swallowed (→ `set()` now returns bool; `cached` tracks the persistent L2
write, with `reason: l2_write_failed`); `cached:true` from an L1-only write when L2 disabled
(→ `reason: l2_disabled`); InMemory store shallow-copy (→ deepcopy).

**4-lens adversarial workflow** — accepted + fixed: L2 hit now promotes into L1 (read-through,
so a cold instance doesn't re-read Firestore every poll; staleness bound documented as
L2_TTL+L1_TTL); `_read_l2_cache` wraps the store read so a misbehaving store can't 500 the
request; store logs `type(e).__name__` not `str(e)` (no resource-path leak); the
`SETUP_INFRA_PREWARM` block now **shifts traffic** to the new revision after the env update
(the coordinator's traffic is pinned, so the env-only update lands at 0% — without the shift
the scheduler would POST a 503-ing old revision and pre-warm would be silently dead) and is
idempotent (describe-then-act, skips the spurious revision on re-run); plus 6 added tests
(error-payload miss, L2→L1 promotion, store-read-error fail-soft through the endpoint,
refresh CAI-error soft-200, a positive "non-sensitive sample survives stripping" assertion,
and an end-to-end "refresh actually serves the next GET from L2").

**Declined (evaluated on merit):** (a) the pre-existing `verify_caller` echoing the caller's
own SA email in its 403 detail — real but out of tier-3 scope, near-zero risk (authenticated
caller's own identity on internal-ingress workers), and pinned by 8 worker test files; tracked
as a standalone hygiene follow-up. (b) the `compare_digest` "constant-time across the set"
concern — the SA email isn't secret and the docstring makes no constant-time claim. (c) the
pre-auth 503 config-state disclosure — deliberately mirrors the `/eventarc` + `verify_token`
fail-closed precedent (operator-debuggable, not attacker-useful).

## Runbook — activating the optional pre-warm

The L2 cache works out of the box (default 900s TTL) — no action needed for cold-start
survival within that window. The pre-warm is only for guaranteeing *always*-instant first
loads. To activate:

```bash
# Provisions: cloudscheduler API + infra-prewarm-sa + run.invoker on the
# coordinator + INFRA_PREWARM_AUDIENCE env (set post-deploy, never in the
# baseline) + the every-10-min Cloud Scheduler job. Idempotent; re-runnable.
SETUP_INFRA_PREWARM=1 infra/scripts/setup_secrets.sh driftscribe-hack-2026 <github-token>
```

Verify: `gcloud scheduler jobs describe infra-graph-prewarm --location=asia-northeast1`,
then watch a tick land — `GET /infra/graph` should report `X-Infra-Graph-Cache: hit-l2`
shortly after, and a brand-new coordinator revision should serve `hit-l2` on its first load.

Disable / roll back (no code change; the endpoint goes dormant again):

```bash
gcloud scheduler jobs delete infra-graph-prewarm --location=asia-northeast1 --quiet
gcloud run services update driftscribe-agent --region=asia-northeast1 \
  --remove-env-vars=INFRA_PREWARM_AUDIENCE   # → POST /internal/infra-graph/refresh 503s
```

Manual one-shot warm (e.g. right after a deploy, without the scheduler) — mint an OIDC token
for the prewarm SA and POST the endpoint:

```bash
TOKEN=$(gcloud auth print-identity-token \
  --impersonate-service-account=infra-prewarm-sa@driftscribe-hack-2026.iam.gserviceaccount.com \
  --audiences="$REFRESH_URL")
curl -fsS -X POST -H "Authorization: Bearer $TOKEN" "$REFRESH_URL"
```
