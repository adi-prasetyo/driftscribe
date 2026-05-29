# payment-demo — the live Cloud Run service DriftScribe monitors/edits (brownfield).
#
# This resource is authored from the *documented* live shape (infra/cloudbuild.yaml
# deploy step + demo/ops-contract.yaml), NOT from a live read. It is adopted into
# state via the import block in imports.tf. Reaching an exactly-empty `tofu plan`
# is an OPERATOR step (see docs/runbooks/iac-bootstrap.md): after `tofu import`,
# the operator iterates the fields marked "RECONCILE" below until the plan is empty.
resource "google_cloud_run_v2_service" "payment_demo" {
  name     = "payment-demo"
  location = var.region
  project  = var.project_id

  # RECONCILE: `--allow-unauthenticated` in cloudbuild.yaml implies public ingress.
  # The exact stored value may be INGRESS_TRAFFIC_ALL; operator confirms post-import.
  ingress = "INGRESS_TRAFFIC_ALL"

  template {
    # Concurrency / instance bounds from the cloudbuild deploy step
    # (--concurrency=1, --min-instances=0, --max-instances=1).
    max_instance_request_concurrency = 1

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      # Pinned to the tag actually serving at import time (2026-05-27 adoption).
      # CI mutates the live tag on deploy, so expect to re-pin this (or adopt a
      # Phase C ignore_changes policy) whenever payment-demo is redeployed.
      image = "asia-northeast1-docker.pkg.dev/driftscribe-hack-2026/driftscribe/payment-demo:158a072"

      # Documented contract env (demo/ops-contract.yaml): PAYMENT_MODE is locked to
      # "mock"; FEATURE_NEW_CHECKOUT is the operator-toggleable feature flag.
      env {
        name  = "PAYMENT_MODE"
        value = "mock"
      }
      env {
        name  = "FEATURE_NEW_CHECKOUT"
        value = "false"
      }

      # RECONCILE: Cloud Run applies default resource limits (CPU/memory) and a
      # default container port (8080) even when not set on deploy. The server
      # returns these in state, so the operator may need to add a `resources {}`
      # block and/or `ports {}` here to reach a zero-diff plan.
    }
  }

  # The v2 API returns volatile gcloud-deploy metadata (`client`/`client_version`
  # — the gcloud CLI version that last deployed) and a top-level service `scaling`
  # block of defaults (manual/min = 0) on read. Neither is part of the demo's
  # desired state, and pinning the gcloud version would force a perpetual diff, so
  # they are ignored rather than declared. (template.scaling — the per-revision
  # min/max above — IS still managed.) Other server-populated defaults the plan
  # surfaced (resources, ports, startup_probe, launch_stage, traffic→LATEST) match
  # as computed reads and need no config.
  lifecycle {
    ignore_changes = [client, client_version, scaling]
  }
}

# C4 live no-op smoke (2026-05-29): this comment is the only change in the
# smoke PR. HCL comments are ignored by tofu, so the plan-builder produces a
# zero-action plan — exercising the full /propose → /apply gate (HMAC, integrity,
# denylist, fidelity, freshness, saved-plan apply) against the sole-mutator worker
# with no real infra change. Safe to remove after the smoke is recorded.
