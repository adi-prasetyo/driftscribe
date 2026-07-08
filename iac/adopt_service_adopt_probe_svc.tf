# Adopted into IaC management by DriftScribe (zero-change import).
# The import block is retained as a permanent audit record
# (adopt design 2026-06-11 §3).
resource "google_cloud_run_v2_service" "adopt_adopt_probe_svc" {
  name     = "adopt-probe-svc"
  location = "asia-northeast1"
  project  = var.project_id

  template {
    containers {
      image = "gcr.io/cloudrun/hello"
    }
  }

  lifecycle {
    ignore_changes = [client, client_version, scaling]
  }
}

import {
  to = google_cloud_run_v2_service.adopt_adopt_probe_svc
  id = "projects/driftscribe-hack-2026/locations/asia-northeast1/services/adopt-probe-svc"
}
