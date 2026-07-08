# Adopted into IaC management by DriftScribe (zero-change import).
# The import block is retained as a permanent audit record
# (adopt design 2026-06-11 §3).
resource "google_pubsub_subscription" "adopt_adopt_probe_sub" {
  project = var.project_id
  name    = "adopt-probe-sub"
  topic   = "projects/driftscribe-hack-2026/topics/adopt-probe-topic"
}

import {
  to = google_pubsub_subscription.adopt_adopt_probe_sub
  id = "projects/driftscribe-hack-2026/subscriptions/adopt-probe-sub"
}
