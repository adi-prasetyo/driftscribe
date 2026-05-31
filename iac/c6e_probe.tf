# C6e create-class end-to-end probe — THROWAWAY.
#
# This declares a single, free, empty Cloud Storage bucket whose only purpose is to
# prove Phase C6 (head-config delivery): a plan that CREATES a brand-new top-level
# resource flows through the gated pipeline once the worker's iac/ is re-baked from
# the merged head that declares it. The C1–C5 fidelity gate refused ALL creates; the
# C6 tree-hash gate (workers/tofu_apply/main.py::_verify_iac_tree_or_raise) admits a
# create only when the baked iac/ subtree hash matches the approved c6.v1 sidecar.
#
# Denylist-clean by construction (driftscribe_lib/iac_plan_denylist.py):
#   - name does NOT end in "-tofu-state"/"-tofu-artifacts" (CONTROL_PLANE_BUCKET_SUFFIXES),
#     so it is not a protected control-plane bucket.
#   - no IAM bindings (iam-change-forbidden-v1 only trips on google_*_iam_* / policy data).
#   - create-only action (delete/forget/replace are forbidden; create-of-declared is the
#     whole point of C6).
#
# Removal is OUT-OF-BAND (the pipeline forbids delete by design): after the e2e,
# `gcloud storage buckets delete` + `tofu state rm` + revert this file + re-bake clean.
resource "google_storage_bucket" "c6e_probe" {
  name     = "driftscribe-hack-2026-c6e-probe"
  project  = var.project_id
  location = var.region

  # Free to keep empty; force_destroy keeps out-of-band cleanup trivial.
  force_destroy = true

  # Safe-by-default posture (no public exposure, no ACLs).
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  labels = {
    purpose    = "c6e-create-class-e2e"
    throwaway  = "true"
    managed-by = "driftscribe-iac"
  }
}
