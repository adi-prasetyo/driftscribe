# Adopted into IaC management by DriftScribe (zero-change import).
# The import block is retained as a permanent audit record
# (adopt design 2026-06-11 §3).
resource "google_storage_bucket" "adopt_driftscribe_hack_2026_cloudbuild" {
  name     = "driftscribe-hack-2026_cloudbuild"
  project  = var.project_id
  location = "us"
}

import {
  to = google_storage_bucket.adopt_driftscribe_hack_2026_cloudbuild
  id = "driftscribe-hack-2026_cloudbuild"
}
