resource "google_storage_bucket" "checkout_assets" {
  name                        = "driftscribe-hack-2026-checkout-assets"
  location                    = "asia-northeast1"
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  force_destroy               = true

  labels = {
    environment = "demo"
    purpose     = "checkout-assets"
  }
}
