# Adopted into IaC management by DriftScribe (zero-change import).
# The import block is retained as a permanent audit record
# (adopt design 2026-06-11 §3).
resource "google_pubsub_topic" "adopt_shipping_events" {
  project = var.project_id
  name    = "shipping-events"
}

import {
  to = google_pubsub_topic.adopt_shipping_events
  id = "projects/driftscribe-hack-2026/topics/shipping-events"
}
