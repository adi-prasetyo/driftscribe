# `iac-operator/` — operator-applied infrastructure

A **second OpenTofu root**, deliberately separate from [`iac/`](../iac/).

`iac/` is the **agent's** authoring surface: DriftScribe's Provision crew opens
PRs against it, CI plans them as `tofu-plan-builder`, and an approved plan is
applied by `tofu-apply-sa`. This root is the opposite — nothing in the product
writes here, and no automation applies it. An operator runs it by hand with
their own credentials.

## Why this is not in `iac/`

The first thing to land here is the IAM grant the rollback worker depends on
(`ds-10m`). Putting it in `iac/` was the obvious move and does not work, for two
independent reasons — both measured against the live project, not assumed:

1. **It would break every plan immediately.** `tofu-plan-builder` holds exactly
   `roles/pubsub.viewer`, `roles/run.viewer`, `roles/storage.bucketViewer`. It
   cannot `iam.roles.get` or `resourcemanager.projects.getIamPolicy`, so
   refreshing an IAM resource fails — and a root module's refresh failure takes
   the whole plan with it, including the adopt/Provision flow the live demo
   runs on.

2. **Fixing that means handing the apply SA project IAM.** `tofu-apply-sa`
   applies plans that originate in **agent-authored PRs**. Granting it
   `roles/iam.roleAdmin` + `roles/resourcemanager.projectIamAdmin` so it could
   write IAM would mean the identity that executes agent-proposed changes can
   rewrite who can do what in the project. That is a strictly larger blast
   radius than everything DriftScribe is otherwise allowed to touch, and it
   contradicts the product's own claim that it never applies a risky change on
   its own.

Splitting the root keeps the declaration — which is the whole point of the bead
— while leaving both automation identities exactly as least-privileged as they
are today. **No new IAM grants are required to adopt this directory.**

## What you give up

`iac/`'s CI plans on every PR; this root does not. Drift here is caught when an
operator runs `tofu plan`, not automatically. That is a real reduction versus
the in-`iac/` version, and it is the deliberate trade: continuous detection of
one grant is not worth giving the agent's apply path authority over project IAM.

`workers/rollback/main.py` already fails loudly enough to notice in the
meantime — see the failure signature in `ds-10m` and in that module's comment.

## Running it

Requires an operator credential that can administer IAM (project owner does).

```bash
cd iac-operator
tofu init -backend-config=... \
  -var="tofu_state_kms_key=$GCP_TOFU_STATE_KMS_KEY"
tofu plan  -var="tofu_state_kms_key=$GCP_TOFU_STATE_KMS_KEY"
tofu apply -var="tofu_state_kms_key=$GCP_TOFU_STATE_KMS_KEY"
```

State lives in the same bucket as `iac/` under a **different prefix**
(`operator`), so the two roots can never read or clobber each other's state.

The resources here already exist in the project — they were created out of band
on 2026-07-28. `imports.tf` adopts them rather than recreating them; the first
`plan` should report **no changes**. If it proposes to *create* the role, stop:
that means the import target is wrong, and applying would fail on an
already-exists conflict rather than adopting anything.
