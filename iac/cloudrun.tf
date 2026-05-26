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
      # RECONCILE: the live image tag is mutated by CI (:manual or a commit SHA).
      # The operator pins this to whatever tag is actually serving at import time;
      # a stale tag here is the most likely source of a non-empty plan.
      image = "asia-northeast1-docker.pkg.dev/driftscribe-hack-2026/driftscribe/payment-demo:manual"

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

  # RECONCILE: the v2 API populates several server-managed/default fields on read
  # (e.g. `launch_stage`, `traffic` weights to LATEST, annotations/labels, the
  # default execution_environment). Add/adjust as the post-import plan reveals
  # them; do not guess values that aren't documented.
}
