# ds-10m — the one IAM grant the rollback worker cannot run without.
#
# `rollback-agent-sa` polls its Cloud Run traffic-shift LRO (`op.result()` in
# workers/rollback/main.py, the only LRO poll in the codebase). That needs
# `run.operations.get` bound at the PROJECT level.
#
# The trap this replaces: the SA already held `roles/run.developer`, which
# CONTAINS run.operations.get — but bound to the `payment-demo` SERVICE. A
# Cloud Run operation is `projects/{p}/locations/{l}/operations/{id}`, not a
# child of the service, so that binding granted the permission over nothing.
# Resource-scoped IAM that does not reach where you assume.
#
# Failure signature if this is ever lost: every rollback returns
# `outcome_unknown` + HTTP 502 while the traffic shift STILL LANDS, and
# `/reconcile` 403s so it never settles. Silent from the operator's seat — the
# only tell is `PermissionDenied` in the worker's `execute: poll raised` /
# `reconcile: could not read operation` logs. That silence is precisely why
# this belongs in reviewable code instead of one operator's shell history.

resource "google_project_iam_custom_role" "run_operations_reader" {
  role_id = "driftscribeRunOperationsReader"
  project = var.project_id
  title   = "DriftScribe Run Operations Reader"
  description = join("", [
    "Read Cloud Run long-running operations at the project level. Exists ",
    "because roles/run.developer bound to a single service does not reach an ",
    "operation resource. One permission, deliberately.",
  ])

  # EXACTLY one permission. This role is not a convenience bucket — the whole
  # reason it is custom rather than roles/run.viewer is to grant the single
  # thing the poll needs and nothing adjacent to it.
  permissions = ["run.operations.get"]
}

resource "google_project_iam_member" "rollback_sa_run_operations" {
  project = var.project_id
  role    = google_project_iam_custom_role.run_operations_reader.id
  member  = "serviceAccount:rollback-agent-sa@${var.project_id}.iam.gserviceaccount.com"
}
