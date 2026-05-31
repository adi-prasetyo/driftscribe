# Runbook — coordinator `github-pat` rotation (classic → fine-grained) + merge-path hardening

The coordinator authenticates to GitHub with the `github-pat` Secret Manager
secret (mounted as `GITHUB_TOKEN`, `:latest`). C5g left it as an **over-scoped
classic PAT** — a security residual. This runbook rotates it to a least-privilege
**fine-grained** PAT, and covers the related (operator-decision) branch-protection
simplification (C5g carry-forward **2c**).

**Operator-only:** minting a GitHub PAT and editing branch protection need the
repo owner's GitHub account — they cannot be done from code/CI.

---

## §1. Rotate `github-pat` to a fine-grained PAT

### Target shape (matches `infra/scripts/setup_secrets.sh` header)
Fine-grained PAT, **Repository access = only `adi-prasetyo/driftscribe`**, permissions:

| Permission | Level | Why |
|---|---|---|
| Contents | **Read and write** | the C5e merge of an approved IaC PR creates a commit on `main` |
| Pull requests | Read | resolve head SHA / mergeability / PR metadata |
| Checks | Read | the merge gate reads check-runs (`_assert_required_checks_green`) |

Nothing else. (Commit statuses: Read too **only if** your required checks are
legacy *statuses* rather than check-runs — DriftScribe's are check-runs.)

### Steps

1. **Mint** the fine-grained PAT: GitHub → Settings → Developer settings →
   Fine-grained tokens → Generate new token. Resource owner = `adi-prasetyo`,
   repository access = *Only select repositories* → `driftscribe`, set the three
   permissions above, a short expiry. Copy the `github_pat_…` value.

2. **Add a new secret version** (operator, owner + ADC — does NOT delete old
   versions, so rollback is instant):
   ```bash
   printf '%s' "github_pat_XXXX" | \
     gcloud secrets versions add github-pat --project=driftscribe-hack-2026 --data-file=-
   ```

3. **Roll the coordinator** so a NEW revision starts and re-reads `:latest`. A
   unique `--revision-suffix` GUARANTEES a new revision — re-applying the same
   `:latest` ref alone can be a no-op template diff that creates no revision (a
   `:latest` secret is resolved at instance start, and the coordinator may be
   warm, so without a new revision the old value keeps serving). Env/SA/ingress
   are preserved:
   ```bash
   gcloud run services update driftscribe-agent \
     --region=asia-northeast1 --project=driftscribe-hack-2026 \
     --update-secrets=GITHUB_TOKEN=github-pat:latest \
     --revision-suffix=patrot-$(date +%Y%m%d%H%M%S)
   ```
   (Alternatively pin the exact new numeric version — `github-pat:<N>` from
   step 2 — for a fully deterministic roll; note a later cloudbuild deploy resets
   the ref to `:latest`.)

4. **Verify** the coordinator can still do its GitHub ops *before* disabling the
   old PAT. Either drive a real IaC approval through to the merge gate, or do a
   read-only parity check (the next `/iac-approvals` POST exercises
   PR-read + check-read + merge). Confirm no `401/403` from GitHub in the
   coordinator logs.

5. **Disable the classic PAT** (GitHub → Developer settings → Tokens (classic) →
   Delete/Revoke) — **but first** (see warning) confirm nothing else reuses it.

### ⚠️ Before disabling the classic PAT
The classic `github-pat` value may be **reused by other secrets**. Per
`infra/cloudbuild.upgrade-docs-update.yaml`, in prod **`upgrade-docs-github-pat`
reuses the broad classic value**. (Reason to keep them SEPARATE going forward is
permission/role isolation — least privilege + blast-radius separation — not repo
coverage: `UPGRADE_TARGET_REPO` currently also defaults to `adi-prasetyo/driftscribe`.)
Revoking the classic PAT breaks any secret still holding it, and
`gcloud secrets list` shows only NAMES — it cannot confirm two secrets share the
same VALUE. So before revoking, do ONE of:
- rotate `upgrade-docs-github-pat` (and any other reuser) to its own fine-grained
  PAT first — confirm by reading/replacing the value, not by listing names; **or**
- leave the classic PAT enabled until the upgrade workers are migrated.

### Rollback
The previous secret version is retained — re-point with
`gcloud secrets versions add github-pat …` using the prior value (or
`gcloud secrets versions list github-pat` then re-enable/redeploy), and the old
classic PAT keeps working until you revoke it.

---

## §2. (Operator decision) Drop the redundant CODEOWNER review on the IaC merge path — C5g 2c

**Context.** `main` branch protection requires `require_code_owner_reviews=true` +
`required_approving_review_count=1`. The sole account (`adi-prasetyo`) authors the
IaC PRs and **cannot approve its own PR**, so the coordinator's PAT merge is
*structurally* blocked (this is exactly what P2 now reports as a *permanent*
branch-protection block). The IaC merge path is **already gated** by the in-app
`/iac-approvals` CF-Access approval + the required status checks
(`static-gate`, `tofu`, `lint-test`) — so the CODEOWNER review is **redundant**
for IaC PRs.

**DONE — 2026-05-31 (executed as a C6e Path-A prerequisite; operator-approved).**
`required_pull_request_reviews` was **removed** on `main` so the coordinator's
merge-first can actually merge a create-class PR. This was necessary, not optional:
without it the coordinator-driven C6 flow cannot work in prod at all (the merge is
the irreversible first step of `_iac_create_merge_first`).

**Exact change.**
```
gh api -X DELETE repos/adi-prasetyo/driftscribe/branches/main/protection/required_pull_request_reviews
```
Clearing only `require_code_owner_reviews` is **insufficient** — `required_approving_
review_count=1` alone leaves a sole-owner self-authored PR at `mergeStateStatus=BLOCKED`,
and `merge_pr_at_sha` refuses any state ∉ `{clean, unstable}`. So the whole review
object had to go. (Confirmed live: PR #47 flipped `BLOCKED` → `CLEAN` immediately.)

**What was deliberately NOT changed (compensating controls — the controls are the
in-app gate + required checks, NOT a privileged escape hatch):**
- Required **status checks stay required**: `lint-test`, `GitGuardian Security Checks`
  (and `static-gate` / `tofu` on IaC PRs). CI + secret-scanning still gate every merge.
- `enforce_admins` stays **false** — no separate admin-bypass token is used; the
  coordinator merges through the normal API once the state is `clean`.
- The **authoritative human review for IaC** is the CF-Access `/iac-approvals` approval
  (operator identity bound to the signed plan), not a GitHub PR review.

**Caveat (repo-wide).** This is **repo-wide on `main`**, not path-scoped (GitHub can't
require a review for `iac/**` only), so GitHub-native review is no longer enforced for
**non-IaC** PRs either. Accepted for this single-operator portfolio: only `adi-prasetyo`
/ the coordinator PAT have push access, the owner already admin-bypassed `count:1` on
every merge, and the real gates (status checks + GitGuardian + the in-app IaC approval)
remain. Longer-term hardening (per Codex): a second reviewer, a GitHub App merge
identity, or a path-scoped ruleset would beat "sole owner + disabled reviews."

**Reachability note (Codex C6e review #3).** The tofu-apply worker is
`--ingress=internal` + `IAC_OPERATOR_AUTH_MODE=enforce`, so a direct operator-host curl
to the worker (`/baked-iac-hash`, `/propose`, `/apply`) is **not** a valid operational
check — it has no VPC path and no CF-Access operator JWT. The canonical path is
**coordinator → worker** (the operator's authenticated browser drives the coordinator,
which reaches the worker over the C5c VPC). This is intended, better posture.

**To restore** (if the policy is ever reversed):
```
gh api -X PUT repos/adi-prasetyo/driftscribe/branches/main/protection/required_pull_request_reviews \
  -f required_approving_review_count=1 -F require_code_owner_reviews=true \
  -F dismiss_stale_reviews=false -F require_last_push_approval=false
```
Note: restoring this re-blocks the coordinator's merge-first — create-class IaC PRs
would then need an out-of-band admin merge each time (undercuts the C6 automation).

---

## Notes
- `setup_secrets.sh` re-runs take `GITHUB_TOKEN` as arg #2 and `versions add` it —
  so a bootstrap re-run with the fine-grained value also rotates it (set
  `SETUP_EVENTARC=0` on re-runs per the eventarc runbook).
- The coordinator's other secrets (`coordinator-shared-token`,
  `developer-knowledge-api-key`) are unaffected by this rotation.
- **Scope is deliberately IaC-merge-only.** The fine-grained set has NO
  `Issues`/`Pull requests: write`, so the post-merge `create_issue_comment`
  (`driftscribe_lib/github.py`) is best-effort and may 403 (already tolerated),
  and any legacy coordinator issue / docs-PR creation path (`_perform_action`)
  would 403 under this token if exercised with `DRY_RUN=false`. That matches the
  IAM matrix's intended negative space — confirm those paths are not in active
  use before narrowing, or widen the PAT for them specifically.
