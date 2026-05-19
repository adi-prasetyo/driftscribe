# DriftScribe benchmarks

Operator-collected latencies from `infra/scripts/e2e_smoke.sh`.
Each row is appended automatically on a passing run of Test 6
(the Eventarc auto-trigger probe, gated by `RUN_EVENTARC_PROBE=1`).

## Eventarc auto-trigger latency

Wall-clock seconds from `gcloud run services update payment-demo`
to the first observed signal — either a Firestore `decisions` document
with `trigger="eventarc"` (DRY_RUN=false deploys) or a Cloud Run
access log of POST `/eventarc` returning 200 (DRY_RUN=true demo
deploys, where `InMemoryStateStore` bypasses Firestore).

Methodology: see Test 6 in `infra/scripts/e2e_smoke.sh`. Polled every
2 seconds with a 60-second wall-clock deadline. Firestore path checked
first (when `jq` is available), Cloud Run log path as fallback.

| timestamp | latency_s | path | commit |
| --- | --- | --- | --- |
