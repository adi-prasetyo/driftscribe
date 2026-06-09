#!/usr/bin/env python3
"""One-time backfill: stamp `pr_title` onto the existing `iac_apply` decision docs.

The PR title is captured at apply time going forward (agent/main.py `_fetch_pr_title`),
so only NEW decisions carry it. This backfills the rows that pre-date that change so
the operator rail shows a subtitle for them too.

Idempotent + narrow: sets ONLY `pr_title`, only on `action == "iac_apply"` docs whose
pr_number is in the known map, and only when the stored value differs. No other field
is touched. Dry-run by default — pass `--apply` to write.

    python3 scripts/backfill_iac_pr_title.py            # dry run (read-only)
    python3 scripts/backfill_iac_pr_title.py --apply     # perform the write
"""

import sys

from google.cloud import firestore

PROJECT = "driftscribe-hack-2026"

# pr_number -> as-applied GitHub PR title (fetched live from adi-prasetyo/driftscribe).
TITLES = {
    32: "feat(iac): C5g — repoint payment-demo to dedicated runtime SA",
    47: "test(iac): C6e — create-class e2e probe (throwaway denylist-clean bucket)",
    66: "infra(checkout): assets bucket + order-events topic & subscription",
    68: "infra(checkout): storefront + orders-worker Cloud Run services",
}


def main(apply: bool) -> int:
    db = firestore.Client(project=PROJECT)
    to_write = []  # (doc_ref, pr_number, title)
    skipped_present = 0
    skipped_unknown = 0
    for snap in db.collection("decisions").stream():
        d = snap.to_dict()
        if d.get("action") != "iac_apply":
            continue
        pr = d.get("pr_number")
        title = TITLES.get(pr)
        if title is None:
            skipped_unknown += 1
            print(f"  SKIP  {snap.id}  PR #{pr}  (no title in map)")
            continue
        if d.get("pr_title") == title:
            skipped_present += 1
            continue
        to_write.append((snap.reference, pr, title))

    print(f"\n{len(to_write)} doc(s) to update; "
          f"{skipped_present} already correct; {skipped_unknown} unknown-PR.")
    for _ref, pr, title in to_write:
        print(f"  SET   PR #{pr:<3} pr_title = {title!r}")

    if not apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to write.")
        return 0

    if not to_write:
        print("\nNothing to write.")
        return 0

    batch = db.batch()
    for ref, _pr, title in to_write:
        batch.update(ref, {"pr_title": title})  # update() sets ONLY this field
    batch.commit()
    print(f"\nWROTE pr_title on {len(to_write)} doc(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main(apply="--apply" in sys.argv[1:]))
