resource "google_pubsub_topic" "order_events" {
  project = var.project_id
  name    = "order-events"

  labels = {
    managed-by = "driftscribe-iac"
  }
}

resource "google_pubsub_subscription" "orders_sub" {
  project = var.project_id
  name    = "orders-sub"
  topic   = google_pubsub_topic.order_events.id

  ack_deadline_seconds = 20

  labels = {
    managed-by = "driftscribe-iac"
  }
}
