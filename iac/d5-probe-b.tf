resource "google_storage_bucket" "d5_probe_b_bucket" {
  name                        = "driftscribe-hack-2026-d5-probe-b"
  location                    = "US"
  uniform_bucket_level_access = true
}
