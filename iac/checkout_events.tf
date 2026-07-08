resource "google_pubsub_topic" "order_events" {
  project = var.project_id
  name    = "order-events"
  labels = {
    managed-by = "driftscribe-iac"
  }
}

resource "google_pubsub_subscription" "orders_sub" {
  project              = var.project_id
  name                 = "orders-sub"
  topic                = google_pubsub_topic.order_events.id
  ack_deadline_seconds = 20
  labels = {
    managed-by = "driftscribe-iac"
  }

  # Never expire. Nothing consumes this subscription between demo sessions,
  # so GCP's default 31-day idle expiry deleted it out-of-band (2026-07-08),
  # and the resulting +create in every tofu plan blocked all adoption PRs
  # (import-mixed-plan-forbidden-v1). PR #216 merged this config but its apply
  # was (correctly) drift-refused: the expiry deletion itself was still
  # unreconciled out-of-band drift. Recovery ran the runbook's state reconcile
  # (iac-apply-failure-recovery.md §2, refresh-only) and this PR re-expresses
  # the create per §7b.
  expiration_policy {
    ttl = ""
  }
}
