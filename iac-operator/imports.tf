# Brownfield adoption: both resources below ALREADY EXIST. They were created out
# of band on 2026-07-28 to unblock the rollback LRO poll, which is exactly the
# situation ds-10m exists to end. These import blocks adopt them into state
# rather than recreating them.
#
# A first `tofu plan` here must report NO CHANGES. If it proposes to CREATE the
# custom role, the import target below is wrong — stop rather than applying,
# because the apply would fail on an already-exists conflict instead of
# adopting anything. (A deleted-and-recreated custom role also keeps its
# `deleted` tombstone for 7 days, so a botched recreate is not instantly
# recoverable.)
#
# Same convention as iac/imports.tf: declarative, reviewable, removable after
# the first successful apply.

import {
  to = google_project_iam_custom_role.run_operations_reader
  id = "projects/driftscribe-hack-2026/roles/driftscribeRunOperationsReader"
}

# google_project_iam_member's import id is "{project} {role} {member}" —
# SPACE-separated, not slash-separated. A slash-separated id parses as a
# malformed role and fails at import time rather than silently binding the
# wrong thing.
import {
  to = google_project_iam_member.rollback_sa_run_operations
  id = "driftscribe-hack-2026 projects/driftscribe-hack-2026/roles/driftscribeRunOperationsReader serviceAccount:rollback-agent-sa@driftscribe-hack-2026.iam.gserviceaccount.com"
}
