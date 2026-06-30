# Plan: advisory live PR-state check on the IaC approval page

- **Date:** 2026-06-30
- **Status:** DEFERRED / recorded only (post-presentation polish). Not started.
- **Owner context:** dogfooding `/iac-approvals/168`; raised after the #186 reject-clarity work.
- **Reviewed by:** Codex MCP (`019f189c-...`), findings folded in below (see "Codex review").
- **Scope:** the approval-page GET only. The homepage "Open infra changes (N)" card is a separate,
  already-correct surface (see "Not in scope").

---

## Problem

`GET /iac-approvals/{pr_number}` (operator-only, behind Cloudflare Access) renders the Approve/Reject
form resolved **entirely from the stored, signed C2 plan artifact** (propose-on-approve). It deliberately
does **not** read live GitHub PR open/closed state on the approvable path — the GET docstring brags that the
approvable path adds **zero** GitHub round-trips.

Consequence (the papercut): if the operator **closes** the GitHub PR out-of-band (GitHub has no
"delete PR", only close), the page still shows a fully-actionable **Approve** button. Clicking Approve then
fails downstream at the merge step (`github.merge_pr_at_sha` can't merge a closed PR). So the page is
**stale but harmless** — it offers an action that can't complete, and only a *click* (not a page load)
surfaces the problem.

Severity is low: the page is operator-only behind CF Access (no judge/viewer can reach it), and the
failure is safe (merge just fails). This is polish, not a bug fix. Hence DEFERRED.

### Not in scope (already correct)

The homepage **"Open infra changes (N)"** band + per-row "Review pending adoption (PR #N) →" link is driven
by `GET /infra/pending-approvals` → `_list_pending_approvals()` →
`repo.get_issues(state="open", labels=["driftscribe-infra"])` (`agent/main.py`). A closed PR drops out of
that `state="open"` query (after the 60s cache TTL + reload). That surface needs **no** change.

---

## Decision

Add an **advisory, fail-open, live `repo.get_pull(pr_number)` PR-state check** to the approval-page GET,
scoped to the **fresh approvable render only** (no existing DriftScribe decision). When the PR is
definitively **closed** (not merged) → suppress Approve + advisory banner. When **merged with no DriftScribe
decision record** → suppress + a "can't safely continue, recover manually" banner. When **open** or
**undeterminable** (any GitHub error / unconfigured) → render exactly as today (fail-open; always-200 preserved).

### Why live `get_pull`, not the cache

The `/infra/pending-approvals` 60s cache is a **filtered list** (`state=open` AND `labels=[driftscribe-infra]`,
sorted, capped). "Not in the list" ≠ "closed" — it could mean the label was removed, the PR fell past the
list cap, or the cache is up to 60s stale. Using membership as a state oracle would produce **false "this is
closed"** on a genuinely-open, approvable PR — worse than the current papercut. A direct `get_pull` on this
single PR is the only authoritative answer, and this page is operator-only / low-traffic / behind CF Access,
so rate limits are a non-issue. **Do not use the cache as a state oracle.**

### Rejected alternatives

1. **Cache as PR-state oracle** — rejected (false-negative risk, see above).
2. **Blocking "query GitHub first, then render" gate** — rejected: regresses the deliberate zero-GitHub-call
   fast path and couples page availability to GitHub uptime. Any state check must be fail-open anyway
   (GitHub error → still render the form), so it can never be a hard gate.
3. **Do nothing on the GET; only improve the POST merge-failure message** — recorded as the **cheapest
   fallback**. Only helps *after* a click, but it's defensible for deferred polish (Codex agreed). If we
   never get to the GET advisory, at minimum make the POST's closed-PR merge failure say plainly: "PR #N is
   closed on GitHub; nothing to apply."

---

## Implementation steps

Reference points in `iac_approval_get` (read from `origin/main:agent/main.py` — local `main` is stale):
the gate ladder computes `can_approve` (view None / unverifiable / integrity / denylist / artifact-consistency
/ cf-anonymous / token / dry-run / pause / autonomy-dial → else `True`). Then a best-effort **decision-state
block** `if (can_approve or _anonymous_only) and view is not None and s.github_repo:` reads the StateStore
decision pointer (`find_decision_for_event`), runs `reconcile_merge_state(...)`, and sets
`resolved_decision="approve"` + a banner for applied+merged / terminal-failed states (suppressing the form).
The render builds `ctx`; if `resolved_decision` is set it adds `ctx["decision"]/["outcome"]/["outcome_severity"]`.
`show_summary` is gated on `not resolved_decision`.

Template (`origin/main:agent/templates/iac_approval.html`): the **top** banner `{% if decision %}` only has
inner branches for `decision == "approve"` (~L30) and `decision == "reject"` (~L36) — any other value renders
an empty banner. The **bottom** Approve form `{% if can_approve %}` ... `{% elif decision %}` (~L308)
suppresses the form for **any** truthy decision.

### 1. New helper `_iac_pr_state(s, pr_number) -> Literal["open","closed","merged"] | None`

- One `repo.get_pull(pr_number)`; return `"merged"` if `.merged`, else `.state` (`"open"`/`"closed"`).
- Return `None` on unconfigured (`not (s.github_token and s.github_repo)`) or **any** exception (fail-open),
  mirroring `_iac_pr_existence`'s fail-soft style.
- Keep it **separate** from `_iac_pr_existence` (which runs only on the mutually-exclusive *no-plan* path),
  so #175's nonexistent-PR tests are untouched and there's no double `get_pull`.
- **Timeout (Codex):** confirm the PyGithub client has a sane timeout (default 15s) so a slow GitHub doesn't
  hang the approvable render. Consider a shorter timeout for this advisory call, or accept the existing one
  and document it. Fail-open does **not** cover a *slow* call without a timeout.

### 2. Wire into the GET — fresh-approvable render only (Codex-corrected scoping)

Thread a flag out of the decision-state block:
- `_decision_lookup_ok` = the `find_decision_for_event` call **succeeded** (did not hit the `except`).
- `existing is None` after a successful lookup ⇒ genuinely **no** DriftScribe decision for this event key.

Run the new check **only** when **all** hold: `can_approve` (still True after the ladder + decision block)
**AND** `_decision_lookup_ok` **AND** `existing is None`. This:
- excludes anonymous/operator-only renders (gate on `can_approve`, not `_anonymous_only`) → preserves the
  zero-extra-call-on-non-approvable-path invariant and avoids a `get_pull` per anonymous demo viewer;
- **excludes the resume/terminal states** (`waiting_for_rebake`, `applied+failed`, applied+merged,
  terminal-failed) — those have `existing is not None`, so the simple `.merged`/`.state` check never fires
  on them. **This is the critical fix:** `waiting_for_rebake + merged` legitimately keeps the form, and
  `applied+failed` must keep relying on `reconcile_merge_state` (which checks merged-*at-the-approved-head*,
  not just "PR is merged");
- on a decision-lookup **error** (`_decision_lookup_ok == False`), falls back to today's behavior (show the
  form) — never suppress a possible resume on a transient read error.

Then:
- `state == "closed"` → `can_approve = False`; banner: *"PR #N is closed on GitHub (closed outside
  DriftScribe). There is nothing to approve here; reopen the PR if you still want to apply this plan."*
- `state == "merged"` (and we got here, so no decision record) → `can_approve = False`; banner (Codex
  wording): *"PR #N is already merged on GitHub, but DriftScribe has no approval/apply record for this
  artifact — this page can't safely continue. Re-plan or recover manually."*
- `state in ("open", None)` → unchanged.

### 3. Template

Add one branch in the top banner block:
```jinja
{% elif decision == "external_close" %}
  <div class="ds-blocked">{{ outcome }}</div>
```
The bottom form is already suppressed by the generic `{% elif decision %}` at ~L308 (since `can_approve` is
also False). Carry the banner via `resolved_decision = "external_close"` + `resolved_outcome`.

**Open decision (Codex):** reusing `resolved_decision` also hides the "What this change does" summary
(`show_summary = ... and not resolved_decision`). That's **consistent** with the existing terminal banners
(applied+merged, terminal-failed all suppress the summary), and for a closed/externally-merged PR the plan is
moot — so suppression is acceptable. If we'd rather keep the page **inspectable** (summary visible), use a
**separate context key** (`approval_advisory` / `approval_blocked_message`) instead of overloading `decision`,
plus its own top-banner branch; the bottom form is already gated by `can_approve=False`. Pick one deliberately
and pin the choice with a test. *Leaning: reuse `decision="external_close"` for minimal surface; revisit if
we want the summary shown.*

### 4. Tests

- **Unit `_iac_pr_state`:** open→`"open"`, closed-unmerged→`"closed"`, merged→`"merged"`, error→`None`,
  unconfigured→`None`.
- **Integration (GET), fresh-approvable, no decision:**
  - `get_pull` reports `"closed"` → Approve suppressed + advisory copy present.
  - reports `"open"` → form present (regression guard).
  - `get_pull` raises → form present (fail-open).
  - reports `"merged"` → suppressed + "no approval/apply record" copy.
  - assert at most **one** extra `get_pull` on the approvable path, and **zero** on a non-approvable path
    (e.g. pause/dry-run/anonymous).
- **Codex-requested regression guards:**
  - `waiting_for_rebake` + PR merged → **form still shows** (resume path not broken).
  - `applied+failed` + PR merged → **not** converted by the new simple check (left to
    `reconcile_merge_state`).

---

## Invariants to preserve

- **Always-200 / probe-safe.**
- **Fail-open** on any GitHub error (catch around `get_repo`/`get_pull`), call placed **before** any token mint.
- **Zero new GitHub round-trips on every non-approvable path** — the new call runs only on the fresh
  approvable render (`can_approve AND _decision_lookup_ok AND existing is None`).
- **No new plan-approval token mint.**
- **Resume/terminal states untouched** — the new check is gated out of every `existing is not None` path.

---

## Effort

Small: one helper, one GET wiring block (with the flag threaded out of the decision block), one template
branch, ~7-8 tests. No new infra, no new endpoint, no migration. In-character with the #175 nonexistent-PR
fail-soft pattern.

## Codex review (folded in)

Thread `019f189c-6446-7f82-8ffd-bc8f15b383bd`. Key findings:
1. **(Material, accepted)** "can_approve still True" was too broad → would break `waiting_for_rebake + merged`
   resume. Re-scoped to fresh/no-decision only; resume + applied+failed explicitly excluded; added the two
   regression guards above.
2. **(Accepted)** Stronger merged-without-decision wording ("no approval/apply record… recover manually").
3. **(Accepted)** Timeout caveat — fail-open ≠ fast; confirm client timeout.
4. **(Recorded as open tradeoff)** Reusing `resolved_decision` hides `show_summary`; offered the
   separate-key alternative. Leaning reuse for minimal surface (consistent with existing terminal banners).
5. **(Agreed)** For deferred polish, the POST-message-only fallback is defensible; if doing the GET advisory,
   keep it narrow.
