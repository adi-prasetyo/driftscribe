# Phase C5c — Coordinator→internal-worker VPC reachability (the live BLOCKER)

**Status:** DRAFT rev-2 (Codex round-1 folded: 2 BLOCKER + 7 IMPORTANT + 1 NIT;
core design validated — OIDC/Host/SNI fine, `private-ranges-only`+VIP correct, `/24` fine,
no Serverless-VPC-Access API needed, `run.app` zone does NOT touch `*.googleapis.com`,
eventarc inbound unaffected). Author: agent. Date: 2026-05-30.
**Parent:** `docs/plans/2026-05-30-infra-iac-phase-c5-coordinator-integration.md` §3.2 + §7.
**Predecessors (merged):** C5a–C5e all merged (`bf3b6a7` is HEAD). C4 worker live-proven.
**Nature:** ONE code+test PR (probe endpoint + VPC script + deploy path) **then** a
staged, `--no-traffic`-gated live execution against the **production** coordinator
(`driftscribe-agent`, serves live chat at `driftscribe.adp-app.com`).

---

## 1. Why C5c exists

The merged C5e POST flow (`/iac-approvals`) calls `worker_client.call_apply` →
`TOFU_APPLY_URL` (`/propose`, `/apply`). But **the network path does not exist yet**:

| Live fact (verified 2026-05-30) | Value |
|---|---|
| Worker `driftscribe-tofu-apply` ingress | **internal** (`*.run.app` = `…tofu-apply-u272wv52kq-an.a.run.app`) |
| Coordinator `driftscribe-agent` | ingress=all, **no VPC** (`vpcAccess=None`), `DRY_RUN=false` |
| `TOFU_APPLY_URL` on coordinator | **UNSET** |
| `compute` / `dns` / `vpcaccess` APIs | **DISABLED** (only `run` enabled) |
| Coordinator SA `run.invoker` on worker | **present** (`driftscribe-agent@…`) |
| Coordinator Cloud Run invoker | `allUsers` (+ eventarc-sa) → revision URL directly curl-able |
| Deploy principal | me = `theghostsquad00@gmail.com` = `roles/owner` (has `compute.subnetworks.use`) |
| Worker `/healthz` | exists (GET, returns `{ok:true}`) — needs an OIDC token (service requires auth) |

A valid OIDC token does **not** help — ingress is a network control. So the **only**
missing layer is networking.

## 2. Design (locked by parent §3.2 — Direct VPC egress + run.app private DNS → VIP)

- **Coordinator gets Direct VPC egress; worker stays `ingress=internal`.** No
  always-on connector VM, no Cloud NAT.
- `*.run.app` resolves publicly, so `private-ranges-only` alone routes it *around* the
  VPC and internal-ingress refuses it. Fix = a **Cloud DNS private zone** for `run.app.`
  attached to the VPC, redirecting `run.app`/`*.run.app` → the **private.googleapis.com
  VIP `199.36.153.8/30`**, which `private-ranges-only` *does* route through the VPC
  (authoritatively confirmed: under `private-ranges-only` the PGA VIPs 199.36.153.8/30
  and .4/30 route through the VPC in addition to RFC1918/6598; public dests egress
  direct — preserving Vertex/GitHub/Cloudflare/Notifier egress with **no NAT**).
- **`private` (`.8/30`)**, not `restricted` (`.4/30`) — we are not in a VPC-SC perimeter.

### 2.1 The reachability-probe gap (NEW finding → adds a small coordinator endpoint)

Parent §7 step 3 requires "`curl` coordinator→worker `/healthz`" as the GO gate. But
**only a VPC-attached caller resolves the `run.app` private zone**, so the probe must
originate *inside* the coordinator service. The coordinator's existing `/health` just
returns `{ok:true}` (no downstream probe). Therefore C5c adds a minimal, permanent,
**read-only** diagnostic endpoint. This is the unambiguous signal the parent wants
(`/healthz` 200 = reachable; a broken network gate returns 403/404 *pre-app*, which the
plan warns is "trivially mistaken for auth failure").

## 3. Deliverable A — code+test PR (`c5c-vpc-reachability`)

1. **`agent/worker_client.py`** — `probe_worker_health(worker) -> dict` (probes ONE
   worker; the endpoint loops over **all** of them — Codex BLOCKER #2: Gate 2 must be
   concrete, probe every configured worker URL via the real `mint_id_token`+`GET /healthz`
   path, not a vague "least-side-effect call TBD"):
   - `base = _worker_url(worker)` (skip/`unset` if its URL env is empty).
   - `token = mint_id_token(base)` (aud = worker **root**, same rule as `call`).
   - `httpx.get(f"{base}/healthz", headers={"Authorization": f"Bearer {token}"}, timeout=10s)`.
   - Returns `{"worker", "target": base, "reachable": <got ANY HTTP response>,
     "status_code", "latency_ms"}`. `reachable=True` means the route+TLS worked (even a
     404/403 from the *app* proves the network path — the whole point vs. a pre-app
     DNS/route blackhole). Transport error → `{"reachable": False, "error": <class+msg>}`
     (never raises through). Hardcoded `/healthz`; **never** ADK-exposed.
2. **`agent/main.py`** — `GET /iac-apply/reachability`:
   - Gated by the **same shared-token dependency** the existing ops endpoints use
     (`X-DriftScribe-Token`; confirm against `/recheck`'s dep at implementation) — so it
     is curl-able on the **tagged no-traffic revision URL** (bypasses Cloudflare) during
     the staged smoke, and behind CF later. NOT CF-mandatory (diagnostic, no mutation).
     **Token via header only, never query param; `Cache-Control: no-store`; minimal
     fields, no raw socket internals** (Codex NIT #10).
   - Probes **every** configured worker: the new `tofu_apply` **plus the 7 siblings**
     (`reader, docs, rollback, notifier, upgrade-reader, upgrade-docs, infra-reader`) —
     this single call drives BOTH gates. Returns
     `{"go": <bool>, "worker_healthy": <tofu_apply status==200>, "results": [<per-worker>]}`.
     - `go = worker_healthy AND every sibling reachable` (sibling = "received any HTTP
       status"; the tofu_apply worker specifically must be **200**, since the coordinator
       holds `run.invoker` + `/healthz` exists).
   - HTTP 200 when `go`, 502 when not, 503 when `TOFU_APPLY_URL` unset. Read-only.
3. **Tests:** unit `test_worker_client.py` (probe: 200→reachable+healthy, 403/404→reachable
   not-healthy, transport-error→reachable:false+error, URL-unset→skip/unset); integration
   `test_iac_reachability.py` (wrong/absent token→401/403; `go=true` happy path; one
   sibling unreachable→`go=false`+502; tofu_apply 403→`go=false`; `TOFU_APPLY_URL`
   unset→503; `Cache-Control: no-store` present; token-in-query rejected). Suite stays green.
4. **`infra/scripts/setup_coordinator_vpc.sh`** (NEW; sources `_setup_lib.sh`;
   `set -euo pipefail`; idempotent **and fail-closed on collision**; `DRY_RUN=1` prints
   only; **out-of-band, NOT in `iac/`** — a VPC create would trip C4's fidelity guard +
   break the zero-diff import):
   1. `gcloud services enable compute.googleapis.com dns.googleapis.com` (idempotent).
   2. VPC `driftscribe-vpc` (`--subnet-mode=custom`) if absent. **If it already exists,
      verify mode==custom and do NOT mutate it** (Codex IMPORTANT #6).
   3. Subnet `driftscribe-coord-an1` in `asia-northeast1`, range `10.8.0.0/24`,
      `--enable-private-ip-google-access` (Direct VPC egress consumes 1 IP/instance; /24
      generous). If it exists, **verify region+range+PGA match expected**, else abort.
   4. Cloud DNS **private** zone `run-app` (`--dns-name=run.app.`,
      `--visibility=private --networks=driftscribe-vpc`). Records (Codex IMPORTANT #7 —
      Google's documented pattern): **`run.app.` = `A` → `199.36.153.8 199.36.153.9
      199.36.153.10 199.36.153.11`** (TTL 300); **`*.run.app.` = `CNAME` → `run.app.`**
      (wildcard CNAME, NOT wildcard A). **If a `run.app.` private zone already exists,
      verify its visibility/attached-network/records match expected — do NOT silently add
      or overwrite records in a pre-existing zone** (could break unrelated VPC resources).
   5. Confirm the `0.0.0.0/0 → default-internet-gateway` route exists (custom-mode VPC
      auto-creates it; PGA reaches the VIP via it). Add an explicit
      `199.36.153.8/30 → default-internet-gateway` route only if missing.
   6. **Grant `roles/compute.networkUser` to the Cloud Run service agent**
      `service-1079423440495@serverless-robot-prod.iam.gserviceaccount.com` on the
      subnet/project (Codex IMPORTANT #4 — Direct VPC egress needs the *Cloud Run* service
      agent to use the subnet; normally covered by `roles/run.serviceAgent`, but grant
      explicitly to be safe). Optionally `--network-user=<member>` for a non-owner deploy
      principal (the live step runs as **owner via local gcloud**, which already has it —
      Codex IMPORTANT #5; the `cloudbuild` VPC path is future-CI and would need the Cloud
      Build SA preflighted).
   7. Print the **DNS resolution proof** + the exact staged redeploy + smoke commands.
5. **`infra/cloudbuild.coordinator-update.yaml`** — add an OPTIONAL VPC path: new
   substitutions `_VPC_NETWORK`, `_VPC_SUBNET`, `_VPC_EGRESS`, `_TOFU_APPLY_URL` that,
   when set, append `--network/--subnet/--vpc-egress` + `--update-env-vars TOFU_APPLY_URL`
   to the `gcloud run services update`. Empty defaults = today's behaviour unchanged.
   (The staged live deploy below uses a direct `gcloud run services update` for tighter
   control of `--no-traffic`; this yaml change keeps the documented path coherent.)

## 4. Deliverable B — staged live execution (operator; I drive via ADC; traffic-shift GATED)

Order is chosen so **live chat traffic never moves until the gate is green**, and a
broken guess is zero-blast-radius (abandon the no-traffic revision).

| # | Step | Touches live traffic? | Reversible? |
|---|---|---|---|
| 0 | **Capture `OLD_REV`** = current serving revision; (optional) tag it `stable-prevpc` (Codex IMPORTANT #8 — record the known-good rollback target before anything) | no | n/a |
| 1 | Build+push coordinator image w/ probe (Cloud Build) | no | n/a |
| 2 | `setup_coordinator_vpc.sh` (enable APIs; VPC/subnet/DNS/route/service-agent IAM) | **no** — running coordinator isn't in the VPC | additive; deletable |
| 3 | `gcloud run services update driftscribe-agent --image=<new> --network=driftscribe-vpc --subnet=driftscribe-coord-an1 --vpc-egress=private-ranges-only --update-env-vars TOFU_APPLY_URL=https://driftscribe-tofu-apply-u272wv52kq-an.a.run.app` **`--no-traffic --tag c5c`** (Codex BLOCKER #1 — a tag gives a stable callable URL `https://c5c---driftscribe-agent-…-an.a.run.app`; a 0%-traffic revision has no plain reachable URL otherwise) | **no** (`--no-traffic`) | abandon revision |
| 4 | **Smoke the tagged revision** `https://c5c---driftscribe-agent-…run.app` + `X-DriftScribe-Token` (it runs WITH the VPC config, so the smoke truly exercises the egress path) | no | — |
| 5 | **GO/NO-GO gate** (below) | no | — |
| 6 | **[GATED on explicit user OK]** `gcloud run services update-traffic driftscribe-agent --to-tags c5c=100` (then untag) | **YES** | revert traffic to `OLD_REV` |
| 7 | **Post-shift live verify (REQUIRED, not optional — Codex IMPORTANT #9):** `/health` + a **real `/chat` through Cloudflare** (the tagged smoke proved outbound egress, NOT the production-host inbound path / CF / form absolute-URLs) | — | step-8 revert |
| 8 | Rollback if needed: `update-traffic --to-revisions=<OLD_REV>=100` (old rev = no VPC, known-good; traffic shift is fast but not instantaneous — in-flight requests drain) | — | — |

**Cold-start mitigation (Codex IMPORTANT #3):** Direct VPC egress can add a first-connection
delay (sometimes >1 min) on a fresh instance, so a later cold instance serving `/chat`
could see the worker call / 10s probe time out on its very first egress. Mitigations,
decided at execution: (a) a brief **retry/backoff on first-connection failures** in the
probe + `call_apply` path; and/or (b) keep one warm instance with **`--min-instances=1`**
on the coordinator during/after rollout (reasonable for a live chat UX anyway; small
always-on cost). The single tagged-revision smoke warms ONE instance — it does not by
itself prove cold instances are fine.

### 4.1 GO/NO-GO (the run.app zone rewrites EVERY coordinator→Cloud-Run call)

The private zone redirects **all 8** coordinator→`*.run.app` targets through the VIP:
the new worker **plus** the 7 siblings (`reader, docs, rollback, notifier,
upgrade-reader, upgrade-docs, infra-reader`, all `…u272wv52kq-an.a.run.app`). The single
`GET /iac-apply/reachability` call on the **tagged revision URL** + `X-DriftScribe-Token`
drives **both** gates at once (Codex BLOCKER #2 — concrete, no "TBD"):

- **Gate 1 — new path:** `worker_healthy == true` (tofu_apply `/healthz` == **200** over
  the VPC; an unrouted call would 403/404 *pre-app*, so 200 unambiguously proves VPC
  routing to an internal-ingress service).
- **Gate 2 — no regression:** **every** sibling `reachable == true` (received any HTTP
  status via the same `mint_id_token`+`GET /healthz` path the real calls use → proves the
  rewritten zone didn't blackhole them; a 404/403 from the app still proves the route).
- **GO = `go:true`** (`worker_healthy AND all siblings reachable`). Plus print the **DNS
  resolution proof** (the zone resolves `*.run.app` → a VIP IP).
- **NO-GO** → abandon the tagged revision (no `update-traffic`; untag), keep live on
  `OLD_REV`, diagnose. Common fixes: missing/wrong A record, wildcard CNAME absent, wrong
  VIP, PGA off, route absent, service-agent networkUser missing, or egress=all-traffic
  needed (→ then Cloud NAT, out of C5c).

## 5. Risks & residuals

- **Live-only correctness** (parent §9): network-gate failures masquerade as auth
  failures → the `/healthz` 200 gate + staged no-traffic smoke is the mitigation. The
  traffic shift is the only outward-facing step and is gated + instantly revertible.
- Sibling-call regression after the zone rewrite is the real GO/NO-GO risk → Gate 2.
- `private-ranges-only` keeps public egress direct (no NAT) — confirmed; if a future need
  forces `all-traffic`, that requires Cloud NAT (out of C5c).
- Subnet sizing: `/24` ample for Direct VPC egress at this scale.
- Pre-existing, NOT C5c: an unbound `eventarc-trigger-sa` invoker binding on the
  coordinator + the absent drift trigger (tracked elsewhere) — left untouched.

## 6. Process

Per CLAUDE.md: Codex-review THIS plan before presenting (new C5c thread); after the code
PR, `codex-reply` to review the implementation against the plan; the live execution is
operator-gated (user OKs the traffic shift). Optionally an adversarial Workflow review of
the script + staged-deploy sequence for prod-safety before step 6.
