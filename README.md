# DriftScribe
> [日本語版はこちら](README.ja.md)

[![CI](https://github.com/adi-prasetyo/driftscribe/actions/workflows/ci.yml/badge.svg)](https://github.com/adi-prasetyo/driftscribe/actions/workflows/ci.yml)

AI DevOps agent for live Cloud Run drift detection. Submission for DevOps × AI Agent Hackathon 2026 (Google Cloud Japan / Findy).

**Architecture diagram:** [`docs/architecture/architecture.html`](docs/architecture/architecture.html) — self-contained, open in a browser.

## What is this?

DriftScribe watches a live Cloud Run service (`payment-demo`) and compares its
running environment against a declared ops contract. When drift appears, an
ADK-driven agent decides between four outcomes — `no_op`, `docs_pr`,
`rollback`, or `escalate` — and dispatches the work to execute-only workers.
Destructive actions (rollback) require human approval via an HMAC-signed
one-shot link. Five Cloud Run services total: one coordinator + four workers.

## Demo

```bash
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh beat-a   # baseline → no_op
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh beat-b   # drift → drift_issue
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh beat-c   # ADK reasoning beat
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh beat-d   # docs PR preview
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh beat-e   # rollback w/ HITL gate
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh cleanup  # restore baseline
```

Full operator runbook (screen layout, timing, expected outputs):
[`docs/demo-script.md`](docs/demo-script.md).

## How it works

The coordinator (`driftscribe-agent`) is the only public-facing service. It
hosts the ADK agent loop, intent classifier, and approval HMAC pages. Four
execute-only workers (`reader`, `docs`, `rollback`, `notifier`) sit behind
`--no-allow-unauthenticated` and refuse any direct human traffic — only the
coordinator's service account, with an audience-bound Google ID token, can
reach them. Each worker enforces a hardcoded payload-intent policy: the
request body cannot redirect it to a different target service, repo path, or
webhook URL.

Triggers fan in from three directions: Eventarc audit-log events,
operator-driven `/chat` natural-language requests, and manual `/recheck`
calls. The full topology and the two non-overlapping auth layers are
documented in [`docs/architecture/multi-agent-design.md`](docs/architecture/multi-agent-design.md).

## Cost & latency

Per `/chat` call: ~$0.0002 GCP + ~$0.0001 Gemini = ~$0.0003 (estimated;
verify with the 20-call benchmark below). p50 latency: TBD ms
classifier-path, TBD ms ADK-path. p95: TBD ms. Idle cost at
`min-instances=0`: $0. Demo total spend over hackathon: TBD (pull from GCP
billing breakdown before submission).

To collect real numbers, run 20 back-to-back `/chat` calls against the
deployed coordinator and record the `X-Trace-Id` + wall-clock for each;
compute p50/p95 from the resulting series. Procedure lives alongside the
demo runner — see `scripts/demo.sh` for the request shape and operator-token
resolution.

## How DriftScribe differs from existing tools

| | DriftScribe | Drift (CloudPosse) | Steampipe | Cloud Custodian | AWS Config Rules |
| --- | --- | --- | --- | --- | --- |
| AI-driven decisions | ✓ | ✗ | ✗ | ✗ | ✗ |
| HITL approval gates | ✓ | ✗ | ✗ | ✗ | ✗ |
| Layered safety (OS + policy) | ✓ | ✗ | ✗ | partial | partial |
| Multi-cloud | ✗ (GCP only) | ✓ (Terraform-aware, multi) | ✓ | ✓ (AWS-primary) | ✗ (AWS) |
| Open source | ✓ | ✓ | ✓ | ✓ | ✗ |
| Deployment surface | Cloud Run (5 svcs) | Terraform | Plugin host | Lambda | Managed service |
| Target user | DevOps + SRE on GCP | IaC platform teams | SQL-fluent ops | AWS ops | AWS compliance teams |

DriftScribe trades multi-cloud breadth for layered safety on a single
platform; it's hackathon-stage, the others are production-mature. The wager
is that AI + HITL is the missing axis — existing tools detect drift well but
either stop at the report (Drift, Steampipe) or auto-remediate without a
human in the loop (Custodian, Config Rules). DriftScribe sits in the middle:
the agent proposes, the operator disposes, and the worker boundary makes
"propose" safe to expose.

## Repository layout

- [`agent/`](agent/) — coordinator service (ADK agent, classifier, approvals, auth)
- [`workers/`](workers/) — four execute-only worker services
- [`demo/`](demo/) — `payment-demo` target service + ops contract
- [`docs/architecture/`](docs/architecture/) — diagram, multi-agent design, IAM matrix
- [`docs/runbooks/`](docs/runbooks/) — deploy + operate
- [`docs/plans/`](docs/plans/) — phased implementation plan
- [`scripts/`](scripts/) — demo runner
- [`infra/`](infra/) — Cloud Build + smoke tests
- [`tests/`](tests/) — unit + integration suite

## Status

Phase 16 (submission polish) in progress. Hackathon submission deadline
2026-07-10. Current implementation plan: [`docs/plans/2026-05-19-driftscribe-v3-multi-agent.md`](docs/plans/2026-05-19-driftscribe-v3-multi-agent.md).
