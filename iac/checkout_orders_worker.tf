resource "google_cloud_run_v2_service" "orders_worker" {
  name     = "orders-worker"
  location = var.region
  project  = var.project_id
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = "orders-worker-sa@${var.project_id}.iam.gserviceaccount.com"

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      image = "gcr.io/cloudrun/hello"

      env {
        name  = "ORDERS_SUBSCRIPTION"
        value = "orders-sub"
      }
    }
  }

  lifecycle {
    ignore_changes = [client, client_version, scaling]
  }
}