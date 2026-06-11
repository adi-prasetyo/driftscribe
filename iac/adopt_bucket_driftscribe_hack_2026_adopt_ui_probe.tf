# Adopted into IaC management by DriftScribe (zero-change import).
# The import block is retained as a permanent audit record
# (adopt design 2026-06-11 §3).
resource "google_storage_bucket" "adopt_driftscribe_hack_2026_adopt_ui_probe" {
  name     = "driftscribe-hack-2026-adopt-ui-probe"
  project  = var.project_id
  location = "asia-northeast1"
}

import {
  to = google_storage_bucket.adopt_driftscribe_hack_2026_adopt_ui_probe
  id = "driftscribe-hack-2026-adopt-ui-probe"
}
