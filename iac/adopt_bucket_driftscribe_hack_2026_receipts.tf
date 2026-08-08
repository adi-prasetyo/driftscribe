# Adopted into IaC management by DriftScribe (zero-change import).
# The import block is retained as a permanent audit record
# (adopt design 2026-06-11 §3).
resource "google_storage_bucket" "adopt_driftscribe_hack_2026_receipts" {
  name     = "driftscribe-hack-2026-receipts"
  project  = var.project_id
  location = "asia-northeast1"
}

import {
  to = google_storage_bucket.adopt_driftscribe_hack_2026_receipts
  id = "driftscribe-hack-2026-receipts"
}
