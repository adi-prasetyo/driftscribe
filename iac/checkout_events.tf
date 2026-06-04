resource "google_pubsub_topic" "order_events" {
  name = "order-events"
}

resource "google_pubsub_subscription" "orders_sub" {
  name  = "orders-sub"
  topic = google_pubsub_topic.order_events.name
  ack_deadline_seconds = 10
  message_retention_duration = "604800s" # 7 days
}
