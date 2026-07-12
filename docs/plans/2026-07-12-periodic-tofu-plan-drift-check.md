# Periodic tofu-plan drift check - Roadmap plan

> **Status:** post-submission roadmap sketch (written 2026-07-12, the submission
> deadline day, deliberately NOT scheduled before the judging window closes).
> This document fixes the design decisions and phase boundaries. When the work
> is picked up, expand each phase into a full task-by-task implementation plan
> (per-file steps, failing tests first) in the style of the repo's existing
> implementation plans (e.g. the unmatched-IaC-declarations plan on the PR #244
> branch, whose band + Investigate-prefill pattern Phase 3 reuses).

**Goal:** Detect attribute-level drift on IaC-managed resources (a console edit
to a bucket lifecycle rule, a manually changed env var on a managed service) by
periodically running the existing non-mutating `tofu plan -refresh-only
-detailed-exitcode` primitive against live infrastructure, and surface the
findings as investigable evidence in the Infrastructure panel, without adding
any new mutation path or any new autonomous crew trigger.

**Architecture:** A new read-only endpoint on the **tofu-apply worker** (the
only service that already has the baked `tofu` binary, baked providers, baked
`iac/` from `main`, GCS state access, and the KMS decrypt permission) runs the
refresh-only plan in advisory mode (`-lock=false`) and returns a bounded,
redaction-safe summary. Cloud Scheduler triggers the check through the
coordinator, which persists the latest bounded result to Firestore together
with a separate config-freshness verdict (baked `iac/` hash vs current `main`).
The SPA renders the result as a band in the Infrastructure panel with the same
Investigate-into-Provision prefill pattern as the unmatched-declarations band.

**Scope:** Detection and surfacing only. No auto-remediation, no auto-apply, no
change to `AUTONOMOUS_TRIGGER_WORKLOADS`, no new mutation tool, no relaxation of
any C4 apply gate. Anchor is unchanged.

**Tech stack:** Python 3.12 / FastAPI / OpenTofu 1.12 (GCS backend, gcp_kms
state encryption) / Cloud Scheduler / Firestore / Svelte 5 / TypeScript.

---

## Why this fills a real gap

Today's drift detection is **existence-level**, built on Cloud Asset Inventory
(CAI) vs parsed HCL declarations:

- a live resource with no matching declaration becomes `not_in_iac` /
  adoptable drift (the demo's centerpiece);
- a declaration with no live match becomes the `unmatched_iac` band
  (PR #244 lineage).

What nothing catches is **attribute drift on a managed resource**: the resource
exists, matches its declaration by identity, and renders green while its live
configuration has silently diverged. A refresh-only plan reads provider APIs
directly, so it also sidesteps CAI's eventual-consistency lag (the reason
PR #236 needed the ListAssets bucket backfill).

### What a refresh-only plan actually proves

Be precise in code, copy, and prompts: `tofu plan -refresh-only` reports
differences between the **prior state snapshot and refreshed live objects**
(state-vs-live), not literally config-vs-live. Under DriftScribe's invariants
(the tofu-apply worker is the sole state mutator and every apply passes the
gate), state tracks config after each apply, so state-vs-live is an excellent
proxy for config-vs-live and a normal ClickOps edit is exactly what it
catches. But divergent values that have already been absorbed into state (a
manual refresh, state surgery, partial-failure recovery) would pass a
refresh-only check while a normal plan would still propose changes. A normal
`tofu plan` is the authoritative convergence check; it is deliberately not
used here because it also reports merged-but-not-yet-applied config changes as
pending work, which is a different signal. Keep the distinction alive in the
eventual UI language ("live infrastructure changed out-of-band", not "live
differs from main"), and revisit a normal-plan mode as a possible Phase 4
extension.

### Why it cannot replace the CAI reader

`tofu` is blind to anything not in its state or config. Unmanaged ClickOps
resources, the whole adoption story, are invisible to `plan`. The end state is
two complementary oracles:

| Oracle | Sees | Misses |
| --- | --- | --- |
| CAI vs HCL (infra-reader) | unmanaged/adoptable resources; declared-not-live | attribute drift on managed resources |
| refresh-only plan (this plan) | attribute drift + managed-but-deleted-live | anything not in state/config |

---

## What already exists (foundation, do not rebuild)

- `workers/tofu_apply/tofu_runner.py` already implements the exact primitive as
  the pre-apply **freshness gate**: `tofu plan -refresh-only -detailed-exitcode
  -out=refresh.tfplan` followed by `tofu show -json refresh.tfplan` (the saved
  plan, not the `plan -json` event stream, is where `resource_drift` lives; the
  saved plan is encrypted under the `iac/` plan-encryption policy, so the KMS
  env var must be present for `show` too). Exit 0 = fresh, 2 = drift
  (`FreshnessDrift`), 1 = error (`TofuStepError`), plus `LockRefused`
  classification and `classify_refresh_drift`, a semantic gate over
  `resource_drift` with per-type benign-attribute allowlists. All of it is
  testable through the injectable `run_tofu` seam (no live tofu/GCP in tests).
- The runtime apply sequence already performs the backend-connected
  `tofu init -input=false -lockfile=readonly` (the Docker build's init is
  `-backend=false`, providers only) and already copies `IAC_DIR` into a
  per-request temporary work directory before running tofu.
- The worker image hermetically bakes `tofu`, the provider tree, and `iac/`
  from `main` at build time, and exposes `GET /baked-iac-hash` plus the C6
  `iac_tree_hash` machinery to detect a stale bake.
- State lives in the GCS backend (`driftscribe-hack-2026-tofu-state`) with
  gcp_kms state encryption; `TF_VAR_tofu_state_kms_key` is already injected on
  every tofu subprocess call.
- Worker callers are authenticated by service-account identity
  (`_verify_caller_dep`); the coordinator already holds `run.invoker` on the
  worker. For scheduled ingress, `driftscribe_lib/auth.py::verify_oidc_caller`
  plus the Cloud Scheduler service-account pattern in
  `infra/scripts/setup_secrets.sh` (dedicated SA, `roles/run.invoker`, exact
  endpoint URL as OIDC audience, in-app email allowlist) is the template.
- The SPA already has the band + Investigate-prefill patterns: pending
  approvals (PR #184) and unmatched IaC declarations (PR #244 lineage).

## Design decisions (settled, with rationale)

1. **Detection runs in the tofu-apply worker, not GitHub Actions.**
   `iac.yml`'s WIF OIDC condition deliberately admits only push-to-`main` and
   `workflow_dispatch`; widening it to `schedule` would hand recurring cloud
   credentials to CI and duplicate tofu/provider/KMS plumbing the worker
   already bakes. A scheduled GH workflow may still be added later as a loud
   *alerting* probe (demo-health.yml pattern), but the plan run itself happens
   in the worker. The flip side of running inside the sole-mutator service
   account: the endpoint must accept **no caller-controlled tofu arguments or
   paths whatsoever** (a trigger, not a parameterized runner).
2. **Advisory runs use `-lock=false`, and honesty about what that buys.** The
   GCS backend acquires its lock by writing an object, and a locked periodic
   plan could contend with a real apply or a C2 plan-builder run. `-lock=false`
   guarantees the checker can never contend for the state lock with an apply;
   it does NOT guarantee the advisory result is coherent if it overlaps one,
   and it does not remove request-level queueing: the worker runs at
   max-instances=1 / concurrency=1, so an apply arriving mid-check queues
   behind it. That serialization is also what keeps the two from overlapping;
   accept it, and bound it with the drift-check subprocess timeout so the
   queueing delay an apply can experience is short and known. Add a
   best-effort in-progress-apply precheck that yields an `apply_in_progress`
   (inconclusive) result, and never persist an advisory result known to
   overlap an apply. The existing *locked* freshness
   gate inside `run_apply_sequence` stays untouched as the only pre-apply
   authority.
3. **Drift is reported against the *baked* config, with staleness as a
   separate verdict, not an outcome override.** Fetching and executing current
   `main` HCL at runtime inside the high-privilege worker is off the table
   (trust + reproducibility). Instead the result carries the baked tree hash;
   the coordinator compares it against current `main` (existing GitHub read
   path) and stores a **config-freshness verdict alongside, never instead of,
   the raw check outcome**: `current | stale | unknown`, plus the observed
   `main` commit SHA and validation time. `main` can advance right after a
   check, so the UI presents results as "against main as of <SHA>/<time>". A
   failed GitHub read yields `unknown`, which is not trusted as current.
4. **Findings route to Provision, never Anchor, never autonomously.**
   PR #194 deliberately taught Anchor to route the infra-map sense of "drift"
   away from itself; Anchor's autonomy (the only live Eventarc trigger) stays
   scoped to Cloud Run config drift. The band's Investigate action prefills an
   unsent Provision draft, exactly like the unmatched-declarations band. No
   addition to `AUTONOMOUS_TRIGGER_WORKLOADS`.
5. **Perma-diff noise control is a first-class requirement, not a follow-up.**
   Real plans show perpetual diffs from server-set fields and provider
   defaults; without filtering, the band cries wolf every cycle and the signal
   dies. `classify_refresh_drift` supplies the diff-walking and allowlist
   primitives, but its verdict is shaped for a binary apply decision; the
   checker needs a **sibling pure projection** over the same primitives that
   returns reported findings, suppressed-benign count, sensitive/unreportable
   changes (reported at resource level, values never), and
   deletion/recreation events. Always report how many entries were suppressed
   (no silent filtering).
6. **Cadence is coarse and runs are fenced.** Cloud Scheduler every 6 hours
   plus operator-initiated on-demand. A refresh touches every managed
   resource's API on each run; this is drift detection, not alerting, and the
   GCP budget-cap follow-up (H2) is still open. Overlap control is a
   Firestore-backed lease/idempotency record (scheduler retries and on-demand
   clicks must not stack runs, and an older completion must not overwrite a
   newer result), not just an in-memory TTL.

## Result contract (sketch)

Bounded, redaction-safe document persisted by the coordinator (shape to be
finalized in the implementation plan):

```json
{
  "check": "clean | drift | apply_in_progress | error",
  "checked_at": "2026-07-12T09:00:00Z",
  "exit_code": 2,
  "baked_iac_hash": "…",
  "config_freshness": {
    "verdict": "current | stale | unknown",
    "main_sha": "…",
    "validated_at": "2026-07-12T09:00:05Z"
  },
  "drifted": {
    "count": 2,
    "entries": [
      {
        "address": "google_storage_bucket.receipts",
        "change_kind": "update | delete | recreate | unknown",
        "changed_attrs": ["lifecycle_rule"]
      }
    ],
    "suppressed_benign": 1,
    "sensitive_resource_level": 0,
    "truncated": 0
  }
}
```

Rules carried over from the #244 projection, tightened per review:

- `check` and `exit_code` must agree (`drift` pairs with exit 2, `clean` with
  exit 0); `exit_code` is omitted for `apply_in_progress` and pre-plan errors.
- Cap **every** dimension with module constants: max resources, max attribute
  names per resource, max attribute-name length, max total document size, and
  subprocess/parse time. Sort before truncating; honest `count`/`truncated`;
  omit empty sections.
- Project **top-level attribute names only**, not deep leaf paths (map keys
  inside leaf paths are effectively untrusted, unbounded names).
- Never return or persist raw plan JSON, plan text, `stdout`/`stderr`,
  provider diagnostics, or exception strings (provider errors can quote
  config/API values). Errors surface as stable error codes plus a run ID;
  bounded diagnostics go to service logs only.
- Every string is untrusted text in the browser (`normalizeForPrompt` at the
  prefill boundary). tofu masks values marked sensitive, but the
  cap-and-allowlist stance does not rely on that.

## Phases

### Phase 1 - worker: read-only `/drift-check` endpoint

`workers/tofu_apply/` gains a POST endpoint (SA-authenticated via the existing
`_verify_caller_dep`, zero request parameters that reach tofu) that: copies
the baked `iac/` to a per-request temp dir (existing apply-handler pattern,
guaranteed cleanup including the encrypted saved plan); runs the existing
runtime backend init; runs `tofu plan -refresh-only -detailed-exitcode
-out=refresh.tfplan -lock=false` then `tofu show -json refresh.tfplan` (reuse
the freshness-gate sequence and its error classification); classifies the exit
code; and applies the sibling noise projection over `resource_drift` to
produce the bounded result contract plus `baked_iac_hash`. Add a subprocess
timeout to the runner seam for this path (`_default_run_tofu` currently has
none; a wedged provider must die before Cloud Run's 900 s request limit). No
Firestore writes from the worker, no lock acquisition, no change to
`/propose`/`/apply`. Full decision-matrix coverage through the `run_tofu`
seam, including the conservative projection (Phase 3 cannot safely consume the
contract otherwise).

### Phase 2 - schedule + persist

Coordinator endpoint invoked by Cloud Scheduler using the existing pattern
(dedicated scheduler SA, `roles/run.invoker` on the coordinator, exact
endpoint URL as OIDC audience, `verify_oidc_caller` email allowlist; include
the IAM/setup-script work, scheduler attempt deadline, and retry policy in the
implementation plan). It acquires the Firestore lease, invokes the worker,
computes the config-freshness verdict (current `main` `iac/` tree hash via the
existing GitHub read path; failure = `unknown`), and persists the latest
bounded result plus a small capped history, newest-wins fencing. On-demand
refresh is a **separate operator-authenticated route** sharing the same lease.

### Phase 3 - surface in the SPA

The latest persisted result rides an existing token-gated read
(`/infra/graph` or a sibling endpoint; if a new read endpoint, it must join
the demo proxy `DEMO_ALLOWLIST`, the PR #208 lesson; but the on-demand
*trigger* route must NOT, since it causes provider-API work). The
Infrastructure panel renders a band ("Live infrastructure changed out-of-band",
checked-at timestamp, "against main as of <short SHA>", bounded rows, honest
truncation/suppression counts) with an Investigate action that prefills an
unsent Provision draft: report the evidence, do not change files, do not open
a PR, ask the operator to confirm intent. A `stale` freshness verdict renders
as "check ran against an outdated IaC snapshot" next to whatever the raw check
found; `apply_in_progress` renders as inconclusive, not clean.

### Phase 4 - noise control hardening + optional alerting (follow-up)

Tune the benign-attribute allowlist against a few weeks of real results;
optionally add a zero-credential scheduled GH workflow that probes the result
endpoint and fails loudly on `error`/persistent `drift` (demo-health.yml
notification contract); evaluate a normal-plan mode (config-convergence:
config vs refreshed live) as a distinct "pending config changes" signal.

## Explicit non-goals

- No auto-remediation of attribute drift (no PR authoring from the checker, no
  state migration, no `tofu apply` of any kind from this path).
- No crew auto-dispatch on findings; widening autonomy beyond Anchor's single
  Eventarc trigger is a separate, deliberate decision.
- The advisory plan output never feeds `/apply`; the locked freshness gate
  inside the apply sequence remains the only pre-apply drift authority.
- No new state-bucket or KMS permissions anywhere else; keeping detection in
  the tofu-apply worker is precisely what avoids credential spread. (The
  worker does gain one new callable route; "zero new mutation surface" is the
  outcome claim, and the route takes no tofu-reaching parameters.)

## Acceptance criteria (for the eventual implementation)

- A manual out-of-band edit to a managed demo resource appears in the band
  within one scheduled cycle, names the resource address, change kind, and
  changed attribute, and disappears within one cycle after revert.
- The periodic check never contends for the state lock with a real apply and
  never fails one; an apply arriving mid-check experiences at most the
  bounded, timeout-capped queueing delay of the worker's serialized request
  handling. A check that would overlap an apply yields `apply_in_progress`,
  and no advisory result known to overlap an apply is persisted.
- A stale worker bake still reports the raw check outcome, paired with a
  `stale` freshness verdict; a GitHub read failure yields `unknown`, never
  silently `current`.
- Suppressed-benign, sensitive-resource-level, and truncation counts are
  always visible when non-zero.
- Zero new mutation surface: `/propose`/`/apply` behavior is unchanged and the
  new route accepts no caller-controlled tofu arguments or paths.
- README "Scope & roadmap" links here (done in the same PR as this document).
