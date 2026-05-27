# Brownfield adoption of the existing payment-demo service into OpenTofu state.
# Declarative + reviewable; removable after the first successful apply (the import
# runs once, then state holds the resource). Foundation file — operator-only per
# the static gate (§5.1); an agent PR must not add or redirect import targets.
import {
  to = google_cloud_run_v2_service.payment_demo
  id = "projects/driftscribe-hack-2026/locations/asia-northeast1/services/payment-demo"
}
