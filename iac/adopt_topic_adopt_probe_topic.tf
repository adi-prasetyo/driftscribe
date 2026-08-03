# Adopted into IaC management by DriftScribe (zero-change import).
# The import block is retained as a permanent audit record
# (adopt design 2026-06-11 §3).
#
# Re-planned per runbook §7b (iac-apply-failure-recovery.md). PR #168 merged
# this declaration on 2026-07-31 and parked at `waiting_for_rebake`, but its
# saved C2 plan is permanently stale: the #168 branch was cut BEFORE #221 added
# the `expiration_policy` block to checkout_events.tf and merged after it, so
# the approved head's `iac/` tree (iac_tree_hash 769eaed6) never equalled
# merged main's (38310334). A worker re-baked from main would therefore refuse
# the resume with `tree_mismatch_refused`, exactly as the C6 gate is designed
# to. The gate is not being worked around here: this PR re-expresses the same
# import off current main so head and merged main share one tree, and the
# import is planned and approved afresh. #168's parked decision is retired per
# §7e.
resource "google_pubsub_topic" "adopt_adopt_probe_topic" {
  project = var.project_id
  name    = "adopt-probe-topic"
}

import {
  to = google_pubsub_topic.adopt_adopt_probe_topic
  id = "projects/driftscribe-hack-2026/topics/adopt-probe-topic"
}
