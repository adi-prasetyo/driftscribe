# reconcile-probe: Phase 3 PR1 post-apply zero-diff checkpoint (do not merge)
resource "google_storage_bucket" "checkout_assets" {
  name     = "driftscribe-hack-2026-assets"
  project  = var.project_id
  location = var.region

  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  labels = {
    purpose    = "checkout-demo-assets"
    managed-by = "driftscribe-iac"
  }
}