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

**Decision (yours).** Drop the required review *for the IaC merge to succeed via
the coordinator*, relying on the in-app gate + required checks. Per Codex this is
sound **but**: (a) do it **after** the PAT rotation (§1) so the merge identity is
already least-privilege, and (b) do **not** give the coordinator a broad
branch-protection *bypass* (`enforce_admins` / admin-merge token) — the controls
should be the in-app approval + the required checks, not a privileged escape hatch.

**Which knob.** Unblocking the coordinator needs the *review requirement* gone,
which on `main` is `required_pull_request_reviews` — note that clearing only
`require_code_owner_reviews` does NOT unblock if `required_approving_review_count >= 1`
remains (the author still can't self-approve), so you must address the count too
(or remove `required_pull_request_reviews` entirely). The required status checks
(`static-gate`, `tofu`, `lint-test`) stay required.

**Tradeoff.** Dropping the review requirement shifts trust from GitHub-native
review onto the in-app gate + required checks. It is **repo-wide on `main`**, not
path-scoped — GitHub branch protection can't require a review for `iac/**` only —
so it weakens review enforcement for **non-IaC** PRs to `main` too. If you want to
keep that for non-IaC changes, the alternative is to **keep admin-merge** as the
IaC path (today's behavior) and treat the permanent-block banner (P2) as the
expected UX.

**If you decide to drop it**, I can make the change (I have `gh` as
`adi-prasetyo`) — it's a `gh api` PATCH of the branch-protection
`required_pull_request_reviews` — but I will confirm with you first since it's a
live, security-relevant branch-protection edit.

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
