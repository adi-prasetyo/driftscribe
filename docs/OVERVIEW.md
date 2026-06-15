# DriftScribe — plain-English overview

> A 10-minute read. The "what is this and how does it hang together" doc.
> For the formal topology + IAM, see [`architecture/multi-agent-design.md`](architecture/multi-agent-design.md)
> and [`architecture/iam-matrix.md`](architecture/iam-matrix.md). For a picture,
> open [`architecture/architecture.html`](architecture/architecture.html) in a browser.

---

## 1. What it is, in one paragraph

DriftScribe is an **AI DevOps agent that watches your cloud infrastructure and proposes safe
fixes — but never applies a risky one on its own.** It runs entirely on Google Cloud Run. You
talk to it like a chatbot ("did anything drift?", "is this dependency safe to bump?"), it
reasons with an LLM (Gemini, via Google's Agent Development Kit), and when it wants to *change*
something it doesn't do it directly — it hands the job to a small, locked-down worker service,
and anything dangerous waits for a human to click **Approve**. It was built for the
DevOps × AI Agent Hackathon 2026 (Google Cloud Japan / Findy).

The whole project is one bet: **existing drift tools detect problems well but either stop at a
report or auto-apply changes with no human in the loop. DriftScribe puts the agent in the
middle — it proposes, the operator disposes — and makes "propose" safe to expose.**

---

## 2. The one idea to understand: coordinator + workers

There is **one public service** (the *coordinator*) and **several private workers**.

- **The coordinator** (`driftscribe-agent`) is the only thing humans can reach. It runs the LLM,
  decides what should happen, and talks to people. It can *think* and *propose*, but it owns
  almost no power to *act*.
- **The workers** are tiny, single-purpose services (read Cloud Run state, open a docs PR, do a
  rollback, bump a dependency, apply infrastructure changes…). Each one does exactly one job
  against one hardcoded target. **A worker ignores the request if you try to point it somewhere
  else.** Workers are private — a human can't call them directly, only the coordinator can.

Why split it this way? Because the LLM is the untrustworthy part. If a prompt-injection attack
talks the agent into "delete everything," it doesn't matter — the coordinator has no tool that
can, and the workers will refuse a request aimed at the wrong target. **The danger is contained
by architecture, not by hoping the LLM behaves.**

```
   Human / Eventarc / demo script
              │  (X-DriftScribe-Token)
              ▼
   ┌─────────────────────────────┐
   │   driftscribe-agent          │  ← the only public service; runs the LLM
   │   (coordinator)              │     decides, never executes risky actions
   └─────────────────────────────┘
              │  (Google-signed ID token, one per worker)
   ┌──────────┼───────────┬────────────┬─────────────┐
   ▼          ▼           ▼            ▼             ▼
 reader     docs       rollback    upgrade-*     tofu-editor  …private workers
(read-only)(opens PR) (needs       (deps)       (opens infra  each does ONE job
            approval)               infra)        PR only)     against ONE hardcoded target

   (tofu-apply — the only thing that runs `tofu apply` — is never an agent
    tool; the coordinator calls it only after a human approves the plan)
```

---

## 3. What it actually does (the "workloads")

A **workload** is one job the agent knows how to do: its own prompt, its own tool set, its own
workers. The coordinator routes each request to the right workload, and the LLM *only ever sees
the tools for that one workload* — it can't reach across.

Four workloads exist today, organised as a crew:

1. **Anchor** (the `drift` workload) — the only one that runs **autonomously**. A live Eventarc
   trigger fires whenever the `payment-demo` Cloud Run service config changes, waking Anchor
   automatically. Anchor watches the live env vars against a written "ops contract"
   (`demo/ops-contract.yaml`). If reality has drifted from the contract, it can: do nothing, open
   a docs PR, file a drift issue, or **roll back** the service — and a rollback always requires a
   human to approve via a signed, one-time, 15-minute link.

2. **Patch** (the `upgrade` workload) — watches a demo `package.json` against GitHub's security
   Advisory DB. If a dependency has a known vulnerability, it can open an **upgrade PR** bumping
   it. A strict non-LLM validator runs before any write: no downgrades, no new deps, only
   patch/minor jumps, the advisory URL must be real. Major version bumps are refused and routed to
   a human. Patch runs **on demand** from chat; its autonomous trigger is future work and is not
   wired up in this build.

3. **Explore** (the `explore` workload) — an on-demand, **chat-only** workload that is strictly
   read-only: it can read your *whole* GCP project's resources (via Cloud Asset Inventory) and the
   live resource map shown in the UI. Zero mutation tools — not even "notify."

4. **Provision** (the `provision` workload) — an on-demand, **chat-only** workload that can
   **author OpenTofu/Terraform changes**, but only as a *proposal*: its one mutation tool writes
   `iac/`-only HCL and opens a single PR through the `tofu-editor` worker. It **never touches live
   infrastructure.** The actual `tofu apply` happens **downstream, in a separate pipeline**, behind
   a plan-bound, HMAC-signed operator approval — run by the `tofu-apply` worker, which is the
   *only* thing allowed to mutate live infra and is *not* something the chat agent can invoke
   directly.

   So the shape is the same as everywhere else: **the agent proposes (a PR), a human approves, and
   one isolated worker applies — the agent itself can never apply.**

---

## 4. The safety model (layers, in plain words)

DriftScribe stacks several independent safety nets. Any one failing doesn't open the door.

- **Layer 0 — the agent's toolbox is a fixed, short list.** The LLM can only call tools on an
  explicit allowlist. There is no "run shell," no "make any HTTP request," no raw SDK access.
  Tests literally fail the build if a tool name or parameter looks dangerous (`shell`, `exec`,
  `delete`, `raw_url`, …). And each workload only gets its slice of the list.

- **Layer 1 — every service has its own identity and minimal permissions.** Each worker runs as
  its own service account with only the IAM it needs. The coordinator's permission to call the
  drift workers does *not* extend to the upgrade or infra workers. (The per-account grants are
  spelled out in `iam-matrix.md`.)

- **Layer 2 — each worker hardcodes its target and refuses to be redirected.** The reader only
  reads `payment-demo`. The docs worker only writes files matching `demo/docs/*.md`. The upgrade
  worker only touches one pinned lockfile. Send a request pointing elsewhere and the worker says
  no — the worker's *identity is the target*, the request body can't change it.

- **Layer 3 — humans in the loop for the dangerous things.** Rollbacks and infra applies don't
  happen because the agent decided so. The agent mints a signed approval link; a human opens it,
  reviews the plan, and clicks Approve; only then does the worker act. The approval is one-time,
  time-limited, and verified by the *worker* (not the coordinator) — so even a compromised
  coordinator can't fake or force an execution.

Two separate auth mechanisms keep the boundaries clean: humans reach the coordinator with a
shared **`X-DriftScribe-Token`**; the coordinator reaches workers with **Google-signed ID tokens**
minted per-worker. Neither one unlocks the other.

---

## 5. How a single request flows (concrete example: a drift rollback)

1. You ask the coordinator (chat, `/recheck`, or an automatic Eventarc trigger fires on a Cloud
   Run change).
2. The coordinator's LLM reads the live env via the **reader** worker, compares it to the ops
   contract, and decides a rollback is warranted.
3. It calls `propose_rollback_tool`. The **rollback** worker writes a pending approval to
   Firestore, mints a one-time signed token, and returns an approval URL.
4. The coordinator shows you the link (and/or pings the notifier). **Nothing has changed yet.**
5. You open the link, see the rollback plan rendered, and click **Approve**.
6. The coordinator forwards your click to the rollback worker — it does *not* itself hold the key
   to validate the approval.
7. The rollback worker checks the signature + 15-min expiry, flips the approval to "used" in a
   single Firestore transaction (so it can't be replayed), and *only then* tells Cloud Run to
   shift traffic to the target revision.

The Patch and Provision flows are the same shape: **agent proposes → human approves (or a strict
validator gates) → one locked worker executes.**

---

## 6. The pieces (where things live)

| Folder | What's in it |
| --- | --- |
| `agent/` | The coordinator: the LLM agent loop, intent classifier, approval pages, auth, the MCP attach, the IaC-authoring glue. |
| `workloads/` | Per-job config: system prompts, contracts, tool/worker/action lists. One folder per workload (`drift`, `upgrade`, `provision`, `explore`). Only *symbolic names* live here — no secrets, URLs, or repos. |
| `workers/` | The private execute-only services: `reader`, `docs`, `rollback`, `upgrade-reader`, `upgrade-docs`, `infra-reader`, `tofu-editor`, `tofu-apply`, plus the shared `notifier`. |
| `iac/` | The OpenTofu/Terraform the agent reads and writes (the demo's own infrastructure). |
| `frontend/` | The operator web UI — a Svelte single-page app served at the site root `/` (decisions timeline, infra resource graph, approval pages). |
| `demo/` | The things being watched: the `payment-demo` drift target + ops contract, and the pinned npm upgrade target. |
| `driftscribe_lib/` | Shared library code: structured logging + trace IDs, GitHub helpers, the HCL parser, the plan-approval schema, etc. |
| `docs/` | This file, the architecture docs, runbooks (how to deploy/operate), and the dated implementation plans. |
| `tests/` | Unit + integration suite (the bulk of the safety invariants are pinned here as tests). |
| `infra/` | Cloud Build pipelines + smoke tests + setup scripts. |

---

## 7. A few good-to-know facts

- **Grounded reasoning.** The coordinator attaches Google's *Developer Knowledge MCP* as a
  reasoning-time tool, so when it opens a docs/upgrade PR it can cite authoritative Cloud Run or
  package-migration guidance. Workers don't get MCP — only the coordinator reasons.
- **Everything is traceable.** Every request gets a trace ID that's stamped on every structured
  log line across all services. The UI's "trace" view (and Logs Explorer) can replay an entire
  decision — including the LLM's own thought summaries and token usage — for up to a year.
- **Costs almost nothing idle.** Workers scale to zero (`min-instances=0`), so when nobody's
  using it the bill is ~$0. A `/chat` call is on the order of a few hundredths of a cent.
- **It provisions its own demo.** The infra-graph / checkout demo was actually built *by
  DriftScribe itself* going through its own author → approve → apply loop — the agent dogfoods
  its own infrastructure pipeline.

---

## 8. Where to go deeper

- **The picture:** `docs/architecture/architecture.html` (open in a browser).
- **The formal topology + every worker's exact interface:** `docs/architecture/multi-agent-design.md`.
- **Who-can-call-what:** `docs/architecture/iam-matrix.md`.
- **Seeing it run, step by step:** `docs/demo-script.md`.
- **The top-level summary + tool comparison table:** `README.md`.
