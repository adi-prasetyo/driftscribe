# DriftScribe — ProtoPedia submission (English)

> [日本語版はこちら](protopedia.ja.md)
>
> Submitted to: ProtoPedia (https://protopedia.net) — DevOps × AI Agent Hackathon 2026 (Google Cloud Japan / Findy). Each section is sized to drop straight into a ProtoPedia form field.

## Title

DriftScribe — a layered-safety AI DevOps agent for Cloud Run configuration drift

## Summary

DriftScribe is an AI agent that watches a live Cloud Run service (`payment-demo`), compares its running environment against a declared ops contract, and chooses between `no_op`, `docs_pr`, `rollback`, and `escalate`. The reasoning loop runs on Google ADK with Gemini 2.5 Flash on Vertex AI; destructive actions are gated behind an HMAC-signed one-shot human-in-the-loop (HITL) approval link. The result is layered safety on a single cloud (GCP): the agent proposes, the operator disposes, and the worker boundary keeps "propose" safe to expose.

## Highlights

- **Multi-agent layered safety**: One coordinator plus four execute-only workers (`reader` / `docs` / `rollback` / `notifier`) — five Cloud Run services total. Each worker enforces a hardcoded payload-intent policy and runs under its own service account, while the coordinator holds no mutation permissions on `payment-demo` (negative-space design).
- **HITL approval gate**: Rollbacks and other destructive actions only execute after the operator clicks through an HMAC-signed, single-use, 15-minute-TTL approval link. Both the coordinator and the rollback worker each flip Firestore state once inside a transaction, structurally defeating double-clicks and replay.
- **Google ADK + Vertex AI reasoning loop**: The Agent Development Kit drives the agent; Gemini 2.5 Flash on Vertex AI does the reasoning. A capability-bounded tool registry (Layer 0) restricts the LLM to six registered tools — prompt injection cannot reach `execute_shell`, arbitrary HTTP, or any SDK that isn't on the list, because the list is asserted by unit tests.
- **Two non-overlapping auth boundaries**: Operator → coordinator uses an `X-DriftScribe-Token` shared secret with constant-time comparison; coordinator → workers uses audience-bound Google ID tokens that workers verify by both audience and caller email. Compromising one boundary does not unlock the other.
- **Cost-conscious operations**: `min-instances=0` makes idle cost $0; per-call cost is ~$0.0003 (GCP + Gemini, estimated). An `X-Trace-Id` propagates from coordinator through every worker hop so a single request can be followed end-to-end in Cloud Logging.

## Stack

- Language / runtime: Python 3.12
- Web framework: FastAPI + uvicorn
- Agent framework: Google ADK (Agent Development Kit)
- LLM: Gemini 2.5 Flash (Vertex AI, asia-northeast1)
- Runtime: Cloud Run x 5 services (asia-northeast1)
- Data: Firestore (decisions, approvals)
- Events: Eventarc (Cloud Run audit-log trigger)
- Auth: Audience-bound Google ID tokens, Secret Manager, HMAC
- Notifications: External webhook (webhook.site for the demo)
- Build / quality: uv, ruff, pytest, Cloud Build
- CI: GitHub Actions (ruff + pytest on PRs and pushes to main)

## Demo

The 90-second walkthrough is structured as five beats (beat-a through beat-e), driven by `scripts/demo.sh`. beat-a establishes a clean baseline that resolves to `no_op`; beat-b introduces a deliberate drift and turns it into a `drift_issue`; beat-c is the ADK reasoning beat where the agent explains the cause; beat-d previews a docs PR from the docs worker; beat-e exercises the rollback worker through the HITL approval gate. Because the `X-Trace-Id` propagates from coordinator to every worker hop, you can follow a single request end-to-end in Cloud Logging.

- 90-second demo video: [TBD: 90-second demo video]
- Architecture diagram: [`docs/architecture/architecture.html`](../architecture/architecture.html) (self-contained HTML, open in a browser)
- Demo runbook: [`docs/demo-script.md`](../demo-script.md)

## Repository

https://github.com/adi-prasetyo/driftscribe

## Deployed URLs

- Coordinator (`driftscribe-agent`, public): [TBD after deploy: https://driftscribe-agent-xxxxx-an.a.run.app]
- Reader (`driftscribe-reader`, private): [TBD after deploy]
- Docs (`driftscribe-docs`, private): [TBD after deploy]
- Rollback (`driftscribe-rollback`, private): [TBD after deploy]
- Notifier (`driftscribe-notifier`, private): [TBD after deploy]
- Watched service (`payment-demo`, demo target): [TBD after deploy]

> The four private workers are deployed with `--no-allow-unauthenticated`; they are reachable only via audience-bound ID tokens minted by the coordinator's service account.
