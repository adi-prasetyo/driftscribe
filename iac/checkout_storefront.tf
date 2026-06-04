resource "google_cloud_run_v2_service" "storefront" {
  name     = "storefront"
  location = var.region
  project  = var.project_id
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = "storefront-sa@${var.project_id}.iam.gserviceaccount.com"

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      image = "gcr.io/cloudrun/hello"

      env {
        name  = "PAYMENT_DEMO_URL"
        value = "https://payment-demo-u272wv52kq-an.a.run.app"
      }

      env {
        name = "PAYMENT_API_KEY"
        value_source {
          secret_key_ref {
            secret  = "payment-api-key"
            version = "latest"
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [client, client_version, scaling]
  }
}