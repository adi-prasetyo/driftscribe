# Eventarc Audit-Log Payload — DriftScribe `/eventarc` contract

The Phase 14 auto-trigger receives Cloud Run service mutations as Eventarc CloudEvents
wrapping a Cloud Audit Log entry. This document pins the payload shape the `/eventarc`
handler parses, so the integration tests in `tests/integration/test_eventarc.py` can
mock it confidently and the deploy step (`infra/scripts/setup_secrets.sh`'s trigger
creation in Phase 14.3) can use the right `methodName` filter.

The plan calls for runtime discovery (`gcloud logging read` on a real env update)
before writing the handler. We can't run gcloud from CI, so the handler is written to
accept BOTH known method-name variants; the operator confirms the actual `methodName`
on first deploy via:

```bash
gcloud run services update payment-demo --update-env-vars=DEMO=1 --project "$PROJECT"
gcloud logging read \
  'resource.type=cloud_run_revision AND protoPayload.methodName=~"Services\."' \
  --limit 1 --format=json --project "$PROJECT"
```

If `methodName` is something other than the two variants below, update the trigger
filter in `setup_secrets.sh`.

## CloudEvent envelope

Eventarc delivers an audit log entry to the destination Cloud Run service as a
CloudEvent. The HTTP request `driftscribe-agent` receives at `/eventarc` carries:

- `ce-id`: unique event id
- `ce-source`: e.g. `//cloudaudit.googleapis.com/projects/<P>/logs/cloudaudit.googleapis.com%2Factivity`
- `ce-specversion`: `1.0`
- `ce-type`: `google.cloud.audit.log.v1.written`
- `ce-subject`: resource path, e.g. `services/payment-demo`
- `content-type`: `application/json`
- `authorization`: `Bearer <id-token>` — minted by Eventarc against
  `eventarc-trigger-sa@$PROJECT.iam.gserviceaccount.com`; verified by the handler
  per `docs/architecture/multi-agent-design.md` Layer 1.

The JSON body is the audit `LogEntry` proto, JSON-serialized. The fields DriftScribe
cares about live inside `protoPayload`:

```json
{
  "protoPayload": {
    "@type": "type.googleapis.com/google.cloud.audit.AuditLog",
    "methodName": "google.cloud.run.v2.Services.UpdateService",
    "resourceName": "projects/<P>/locations/asia-northeast1/services/payment-demo",
    "serviceName": "run.googleapis.com",
    "authenticationInfo": { "principalEmail": "<operator-or-pipeline>" }
  },
  "resource": {
    "type": "cloud_run_revision",
    "labels": {
      "service_name": "payment-demo",
      "location": "asia-northeast1",
      "project_id": "<P>"
    }
  }
}
```

The handler reads:

- `resource.labels.service_name` → the Cloud Run service that mutated.
- `resource.labels.location` → the region.
- `protoPayload.methodName` → for logging/diagnostics (not branched on).

## `methodName` variants we accept

Audit logs emit different method names depending on which API path the caller used:

| methodName                                          | Triggered by                                              |
| --------------------------------------------------- | --------------------------------------------------------- |
| `google.cloud.run.v2.Services.UpdateService`        | `gcloud run services update`, Console UI, v2 Admin API.   |
| `google.cloud.run.v1.Services.ReplaceService`       | Older clients, some Terraform versions, raw v1 API calls. |

DriftScribe treats both as "service mutation; recheck drift". The Eventarc trigger
itself filters on **one** `methodName` value at a time (multi-value `methodName` in
`--event-filters` is not supported). The trigger created in `setup_secrets.sh`
filters on the v2 name (`UpdateService`); if `gcloud` ever switches to v1 we update
the trigger.

## Filtering at the handler

Even with the trigger filter, the handler must guard:

1. **Auth**: `Authorization: Bearer <id-token>` verified via
   `google.oauth2.id_token.verify_oauth2_token`; the `email` claim must equal
   `eventarc-trigger-sa@$PROJECT.iam.gserviceaccount.com`. Anything else → 401/403.
2. **Service whitelist**: only `payment-demo` is in scope for the demo. Other services
   that somehow reach this endpoint return 200 (so Eventarc doesn't retry forever)
   but record no decision; the response body carries `"ignored": "non-target-service"`.
3. **Region whitelist**: only `asia-northeast1`. Same 200-ignored pattern as above.
4. **Idempotency**: the event_key derivation mirrors `/recheck`'s (hash of the
   coordinator's view of live env). Eventarc may deliver the same audit log twice
   (at-least-once semantics), and the existing `record_event` claim refuses dupes.

## Why we don't read mutation details from the payload

The audit log doesn't carry the *new env values* — only that the service was
mutated. The handler is intentionally **payload-blind** beyond
`(service, region)` so the same idempotency / drift-detection logic as the
manual `/recheck` flow runs unchanged. The Reader Worker (Phase 11.3, with the
W3 traffic-serving fix from Phase 14) is what reads the post-mutation env.

## What changes vs. `/recheck`

The recorded decision document gets `trigger="eventarc"` instead of `trigger="chat"`
or `trigger="recheck"`. `infra/scripts/e2e_smoke.sh` polls Firestore for this label
to confirm the auto-trigger path fired.

## See also

- `docs/architecture/multi-agent-design.md` — overall multi-agent topology + Layer 1
  IAM model (Eventarc trigger SA is a peer of the worker SAs).
- `docs/architecture/iam-matrix.md` — the `eventarc-trigger-sa` row.
- `docs/plans/2026-05-19-driftscribe-v3-multi-agent.md` Phase 14 — the implementation
  plan this doc backs.
