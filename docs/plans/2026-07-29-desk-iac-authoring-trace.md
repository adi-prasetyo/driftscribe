# ds-qua — the desk's iac pending card needs the AUTHORING trace

**Status:** plan, pre-implementation. Branch `fix/desk-iac-authoring-trace` off `0020895`.

## The defect

`ds-wd2.15` shipped the pending card's "view the reasoning behind this →" link for the
**rollback** arm only. Both iac arms render `traceId: null` deliberately.

The gap that matters is rule **2a** (listing provenance, `desk.ts::selectPendingIac`).
When `deskModel` picks it there is no decision doc at all, so `activeDecision()` returns
null and `DriftDiffCard` self-suppresses. The card is then a title, a PR number and two
buttons: **the highest-stakes CTA on the front door carrying the least evidence on the
page.** That is the judges' exact complaint (フロントエンドが後付け) sitting on the view
the redesign exists to fix.

## Why the two obvious sources are both wrong

1. **`iac_apply.trace_id`.** `_record_iac_decision` stamps `current_trace_id_or_new()`
   (`agent/main.py` ~5352) and runs inside the **approve/apply POST** — a different HTTP
   request from the crew run that authored the PR. Linking there points the operator at
   the trace of their own approval click. Two tests in `desk.test.ts` pin this absence on
   purpose; they must stay green.
2. **Parsing a trace id out of the PR body.** Tempting (pure, no I/O, mirrors
   `extract_import_id`) and **rejected**: `open_infra_pr_tool` bodies are *model-authored*.
   A model could emit `**Reasoning trace:** <any hex32>` and the desk would hand the
   operator forged evidence. That is precisely the ds-y5i failure class — a guard trusting
   a value it never independently established. `extract_import_id` has the same
   forgeability, but its worst case is a wrong type/name label, not a wrong evidence link.

Also considered and rejected: **a GitHub label** (`ds-trace-<hex32>`) riding the existing
issues listing at zero extra I/O. Rejected because a collaborator with Triage+ can add
labels — the listing's own docstring already documents that trust caveat, and it accepts
it only because "the worst case of a mislabeled PR is a spurious row, never a bad action."
Forged *evidence* breaks that bound.

## Design — a per-PR authoring-trace record

The trustworthy association already exists: `iac_pr_pointer(result)` derives
`{pr_number, pr_url}` from the **editor worker's own result**, never from model prose, and
both authoring paths gate on it. Record the authoring trace at that exact gate.

### 1. New store — `agent/iac_pr_trace_store.py`

Mirrors `agent/iac_pr_source_cache.py` (same lazy-client, Protocol + in-memory twin,
fail-soft shape; that module is the template).

- Collection `iac_pr_trace`, **one doc per (repo, PR)**.
- ⚠️ **The key must be repo-scoped, not `str(pr_number)`.** PR numbers are
  repository-local, and the two ends genuinely disagree about which repo they mean:
  authoring targets `resolve_iac_editor_target()`, which honors
  `IAC_EDITOR_TARGET_REPO_OVERRIDE` (`agent/workloads/registry.py:710`), while the
  listing reads `Settings.github_repo` (`agent/main.py:3492`). The codebase already
  treats that divergence as live — `adk_tools.py:1457` insists the dupe-guard query
  "the SAME repo the PR will open against … not settings.github_repo", and
  `test_dupe_guard_checks_the_editor_target_repo_not_github_repo` pins it. Keying on
  the number alone would let target-repo B's trace render on listing-repo A's PR #7:
  an over-claim, not a graceful absence — the ds-y5i "record A, release B" defect
  again.
  Doc id = `sha256(f"{repo}\x00{pr_number}")` (a repo slug contains `/`, which is
  illegal in a Firestore document id). The body carries `repo` and `pr_number`
  **verbatim**, and `get` re-checks both against what was asked. That check is **schema
  integrity, not authentication**: it rules out a digest collision, a corrupt record, or
  a document keyed by a future `_doc_id` that disagrees with this one. It does *not*
  stop an authorized Firestore writer, who can compute the id from public inputs and
  store a matching body — Firestore IAM is the boundary there, as for every other
  collection the coordinator reads.
- `get(repo, pr_number) -> str | None`
- `set_if_absent(repo, pr_number, trace_id) -> bool` — **first writer wins**, via
  Firestore `create()` with `AlreadyExists` narrowly swallowed (the same idiom as
  `state_store.py:782`). This guards races and any future call path; it is **not**
  justified by the adoption dupe-guard, which returns `status: "rejected"` and never
  reaches the authoring tail at all (`agent/adk_tools.py:1459`) — an earlier draft of
  this plan claimed otherwise and was wrong.
- Fail-soft **both** directions: a read error degrades to `None` (no link), a write error
  is logged and swallowed. This is evidence, not a gate — it must never 5xx the desk, and
  unlike ds-y5i's approval record **nothing is released on the strength of it**, so
  fail-soft is correct here rather than fail-closed. Note the distinction that makes
  that true: *absence* is silent and honest, but a syntactically valid record for the
  wrong PR is an over-claim — which is why the two fixes above are about identity, not
  availability.
- Log the exception **type**, never `str(e)` (a PermissionDenied embeds the full document
  resource path).
- The lazy singleton + test-override accessor lives **in this module**, not in
  `agent/main.py` where `iac_pr_source_cache`'s lives. Both `adk_tools.py` and
  `fanout.py` write while `main.py` reads; hanging the accessor off `main` would point
  the dependency the wrong way and let writers and readers bind different instances
  under DRY_RUN.

### 2. Write sites — the two `iac_pr_pointer` gates

Exactly parallel to the existing `notify_iac_pr_pending` call, at both places:

- `agent/adk_tools.py::_open_iac_pr_and_notify` (serves `open_infra_pr_tool` **and**
  `propose_adoption_tool`)
- `agent/fanout.py` ~1445 (the D5 fan-out orchestrator opens PRs on its own path)

These are the only two callers of `call_open_infra_pr`; the tofu-editor is the only
caller of `open_iac_pr`. There is no third authoring, sweep or reconcile path.

⚠️ **Use `get_trace_id()`, never `current_trace_id_or_new()`.** The latter *mints* a fresh
id when the ContextVar is unset — recording that would store an id with no logged
reasoning behind it, i.e. a link that opens an empty timeline. No trace in context → no
record → no link. Validate hex32 before storing.

The ContextVar is genuinely bound at both sites: the HTTP middleware binds per request
(`driftscribe_lib/logging.py:276`), the SSE generator re-binds before `create_task`
(`agent/main.py:6863`), and `create_task` / `to_thread` / ADK's synchronous tool
execution all use `contextvars.copy_context()`. Tests still pin the empty-context case,
because that is an assumption about someone else's executor.

⚠️ **In `fanout.py` the write must go through `await asyncio.to_thread(...)`** — Firestore
`create()` is blocking and that gate runs on the event loop — and it must happen
**before** the notify call, which is bounded only by worker_client's 30s timeout.

### 2b. Record only a NEWLY OPENED PR (needs a one-line tofu-editor change)

`driftscribe_lib.github.open_iac_pr` returns an **existing** PR when the branch already
has one (`driftscribe_lib/github.py:301,343`, `reused = True`), and the tofu-editor
computes `result.get("reused")` but **drops it from the HTTP response**
(`workers/tofu_editor/main.py:358,372`). So both coordinator gates today see an ordinary
valid pointer and cannot tell "opened now" from "rediscovered". With no prior record —
a pre-deployment PR, a swallowed write, a crash window — a replay would become the first
writer and attach its own trace to a PR it did not author.

Fix: return `reused` from the worker, and record **only on `result.get("reused") is
False`** — strict identity, not truthiness.

⚠️⚠️ **The strict gate has to hold at BOTH ends, and my first implementation broke it
at the writer.** I shipped `"reused": bool(result.get("reused"))` in the worker, which
maps missing / `None` / `0` / `""` to a literal `False` — so the coordinator's strict
`is False` saw a laundered value and recorded anyway. *And I wrote a test that pinned
that behavior* (`test_a_missing_reused_key_is_reported_as_not_reused`). Both halves are
the ds-y5i pattern verbatim: a guard checking the thing named rather than the value that
ships, protected by a fixture shaped like the missing check.

The worker now derives **one** conservative value and uses it for both decisions:

```python
newly_opened = result.get("reused") is False
if req.dispatch_plan_builder and newly_opened: ...
"reused": not newly_opened
```

Unknown therefore reports `reused=True` — suppressing both the plan-builder dispatch and
the provenance record. That also makes the dispatch gate conservative on an unknown,
which it was not before (`not result.get("reused")` treated missing as new).

⚠️ **Strict `is False`, and an earlier draft of this plan had it wrong.** I first wrote
`reused is not True` and called it "skew-safe: an old worker that omits the field still
records, degrading to today's behavior." That is backwards. Today there is no record and
therefore no link; under `is not True` an old worker's *reused* PR would record the
current trace and render a link to reasoning that did not author it — reintroducing the
exact false-evidence path this section exists to close. It also reads `"true"`, `1` and
`None` as permission to record. **The safety direction here is unambiguous: a missing
link is fine, a wrong link is not**, so every ambiguous value must degrade to no record.

Skew, with the strict predicate:

| coordinator | worker | outcome |
|---|---|---|
| new | new | fresh PR records; reused PR does not |
| new | old (no field) | nothing records — honest absence |
| old | new | extra response field ignored |
| any | malformed value | nothing records |

No ordering is *unsafe*, but **deploy the worker first**: PRs opened during a
coordinator-first window would be permanently link-less.

### 3. Read site — `_list_pending_approvals`

`build_pending_approval` stays **pure**: it gains an optional `authoring_trace_id: str |
None = None` argument, validates hex32, and emits `authoring_trace_id: ""` when absent or
malformed (same blank-string convention as `asset_type`/`resource_name`). The I/O stays in
the agent layer, where `_list_pending_approvals` does one store `get(s.github_repo, n)`
per listed PR — the same `s.github_repo` it already built the listing from, so the repo
the key is scoped to is the repo the row came from.

Cost: one Firestore doc read per open infra PR, per 60s cache miss
(`_PENDING_APPROVALS_TTL_S`). Open infra PRs are a handful; the endpoint is already
fail-soft on any exception.

### 4. Frontend

- `PendingApproval` (`infra_graph.ts`) gains `authoring_trace_id?: string`.
- `DeskPendingIac.traceId` widens `null` → `string | null`, populated **on the listing arm
  only**, gated by `isReplayableTraceId` (the same predicate the rollback arm uses, so the
  desk can never offer a link that opens once but fails to restore when shared).
- **Rule 2b keeps `traceId: null`.** Its row is an `iac_apply` decision whose trace is the
  approval click, and `/decisions` carries no authoring trace. 2b also renders a real diff
  table, so it is not the arm that reads as broken. Out of scope; the existing pins stay.
- **The render site needs no change** — `ApprovalDesk.svelte` is already
  `{#if model.traceId && onOpenTrace}`.

## Honest degradation

A PR opened **before** this ships has no record, so its card renders no link — same as
today. Correct: we genuinely do not know its authoring trace. Never fabricate one.

## Tests

- `tests/unit/test_iac_pr_trace_store.py` — in-memory + Firestore-double: first-writer-
  wins, read/write fail-soft, no `str(e)` in logs, and **the repo-scoping matrix**: the
  same PR number under two repos never cross-serves, and a document whose body names a
  different repo is refused on read.
- Write-site tests at **both** gates: records on a confirmed PR; records nothing when
  `iac_pr_pointer` returns None; records nothing when the ContextVar is empty (**and
  asserts no id was minted** — the mutation test for the `current_trace_id_or_new` trap);
  records nothing when the worker reports `reused: True`; **records nothing when the
  worker omits `reused` entirely** (the old-worker skew case — absence is not consent);
  records nothing for a malformed `reused` (`"true"`, `1`, `None`); a store raise does
  not break PR opening; the write is repo-scoped to `authority.target_repo`, not
  `settings.github_repo`.
- Ephemerality needs no test of its own, and claiming one would have been false: both
  write sites sit in the tool / orchestrator layer, strictly **below**
  `_persist_chat_turn`, and neither reads `conv["ephemeral"]`. The fan-out tests already
  drive `run_provision_fanout_stream` with no conversation at all and still assert the
  record, which is the same property from the other side.
- `tests/unit/test_pending_approvals.py` — `build_pending_approval` hex32 validation.
- `tests/unit/test_pending_approvals_endpoint.py` — the DTO carries the id end to end.
- `frontend/tests/unit/desk.test.ts` — listing arm surfaces the trace; a malformed one is
  refused; **the two existing iac-decision pins stay green**.

## Deploy

Two services. No order is *unsafe*, but **worker first** — see §2b:

1. tofu-editor worker — `infra/cloudbuild.tofu-editor.yaml` (one added response field)
2. coordinator — `infra/cloudbuild.coordinator-update.yaml` + `update-traffic`

Runtime SA already holds `roles/datastore.user`.

## Residual, stated honestly

- **The record binds a trace to a PR NUMBER, not to the PR's current contents.** After
  the PR opens, anyone with GitHub Write can force-push to the branch or edit the
  title/body; the listing then renders the current row beside the original authoring
  trace. That trace really did open PR #N, but it did not author what the operator is
  now looking at. **Accepted deliberately, and filed as its own bead.** Closing it means
  binding the creation `head_sha` and comparing against the current head — which costs a
  `get_pull` per open PR on an endpoint whose docstring specifically celebrates needing
  *no* per-PR round-trip, plus another worker field. A title/body digest would be free
  (the issue object already carries both) but covers only the cosmetic half, and an
  equality check against a body GitHub may normalize is exactly the kind of brittle
  guard that turns routine drift into a silent total outage — the same reason `ds-x5l`
  was declined. The honest scope of the link today is *"the run that opened this PR"*.
- Repo **slug** is not an immutable repository identity: deleting and recreating a repo
  under the same slug could reuse PR numbers against old records. Filed with the above;
  the fix is the numeric GitHub repository id.
- An authorized Firestore writer can forge a correctly-keyed record. The body check is
  integrity, not authentication; Firestore IAM is the boundary.
- Cloud Logging lag can make a correct link open a briefly-empty timeline. The
  association is still right; only the render is late.
- Missing writes, pre-deployment PRs, rollout skew and malformed records all produce **no
  link**, never a wrong one.

## Live acceptance (ds-qua criterion)

Open a fresh adoption PR on prod via chat, confirm the desk's listing-arm card renders the
link, and confirm it opens **the authoring run's** timeline — not the approval click's.

## Resolved in review

1. **Ephemeral — write anyway.** `ephemeral` suppresses *conversation* persistence
   (`agent/main.py:618`); it does not make a real GitHub PR or its Cloud Logging trace
   ephemeral. A PR that will appear on the desk should carry its evidence. Pinned by a
   test.
2. **New collection, not the turn store.** The bead's original DESIGN said "the
   conversation turn whose `iac_pr` matches", but turns live in per-conversation `turns`
   subcollections, so resolving by PR needs a collection-group query **and** a
   collection-group-scoped index — and this repo carries no Firestore index config at
   all, making it an out-of-band infra step. A point lookup avoids it, is cheaper, and
   works for ephemeral runs that have no turn document at all.
3. **Both write sites are the complete set**, and the trace ContextVar really is bound at
   both — verified through ADK's `contextvars.copy_context()` around synchronous tool
   execution (google-adk 1.33.0).

## Deliberately accepted

Rule **2b** (decision provenance) keeps `traceId: null`. Giving it one would mean
enriching `/decisions`, and that arm already renders a real diff table — it is not the
one that reads as broken. Out of scope for this bead.
