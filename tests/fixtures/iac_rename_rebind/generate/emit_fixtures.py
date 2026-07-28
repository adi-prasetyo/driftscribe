#!/usr/bin/env python3
"""Build the repo fixtures for the rename/rebind plan shapes.

Split of concerns, deliberately explicit:

* The **structure** of each fixture -- which addresses appear under
  ``resource_changes`` vs ``resource_drift``, their ``actions`` tuples, and the
  presence/content of ``importing`` -- is taken VERBATIM from real OpenTofu
  1.12.0 runs. This emitter re-reads those runs and ASSERTS the structure still
  matches, so it fails loudly if the observation ever changes.

* The **attribute bodies** are authored google_storage_bucket values. The
  observed runs used docker_volume (chosen because `name` is its physical
  identity and forces replacement, exactly like a bucket's), so its bodies carry
  `driver`/`mountpoint`/`driver_opts`, which would be nonsense under a GCS type.

The raw observed plans are copied to `raw/` so the transcription is auditable.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sys

SRC = pathlib.Path(__file__).parent / "out"
DST = pathlib.Path("/home/adi/driftscribe/tests/fixtures/iac_rename_rebind")

OLD = "driftscribe-demo-assets-old"
NEW = "driftscribe-demo-assets-new"
PROVIDER = "registry.opentofu.org/hashicorp/google"


def bucket_body(name: str) -> dict:
    return {
        "id": name,
        "name": name,
        "project": "driftscribe-hack-2026",
        "location": "US",
        "storage_class": "STANDARD",
        "force_destroy": False,
        "uniform_bucket_level_access": True,
        "url": f"gs://{name}",
        "labels": {},
    }


def rc(addr_name: str, actions: list[str], *, body_name: str,
       importing: str | None = None, deleting: bool = False) -> dict:
    change: dict = {"actions": actions}
    if actions == ["create"]:
        change["before"] = None
        change["after"] = bucket_body(body_name)
    elif deleting:
        change["before"] = bucket_body(body_name)
        change["after"] = None
    else:
        change["before"] = bucket_body(body_name)
        change["after"] = bucket_body(body_name)
    change["after_unknown"] = {}
    change["before_sensitive"] = {}
    change["after_sensitive"] = {}
    if importing is not None:
        change["importing"] = {"id": importing}
    return {
        "address": f"google_storage_bucket.{addr_name}",
        "mode": "managed",
        "type": "google_storage_bucket",
        "name": addr_name,
        "provider_name": PROVIDER,
        "change": change,
    }


def plan(resource_changes: list[dict], resource_drift: list[dict]) -> dict:
    return {
        "format_version": "1.2",
        "terraform_version": "1.12.0",
        "resource_changes": resource_changes,
        "resource_drift": resource_drift,
    }


VANISHED_X = rc("x", ["delete"], body_name=OLD, deleting=True)

FIXTURES: dict[str, tuple[str, dict]] = {
    # One-PR rebind: drop old address from config, import renamed object at a new
    # address. C1 passes (resource_changes is a lone zero-change import); the
    # tofu-apply freshness gate refuses (resource_drift carries a delete).
    "rename_old_gone_new_address_import.json": (
        "case5_newaddr_import",
        plan([rc("y", ["no-op"], body_name=NEW, importing=NEW)], [VANISHED_X]),
    ),
    # Same-address rebind: the import block is INERT because its target is
    # already in prior state, so the plan degrades to a bare create.
    "rename_old_gone_same_address_import.json": (
        "case1_rebind_import",
        plan([rc("x", ["create"], body_name=NEW)], [VANISHED_X]),
    ),
    # Rebind attempted while the old object still exists live: the plan carries a
    # real destroy, which C1 hard-denies.
    "rename_old_live_new_address_import.json": (
        "case8_old_still_live",
        plan(
            [rc("x", ["delete"], body_name=OLD, deleting=True),
             rc("y", ["no-op"], body_name=NEW, importing=NEW)],
            [],
        ),
    ),
    # Positive control: the ordinary adoption shape that already ships.
    "adopt_clean_import_no_drift.json": (
        "case7_import_positive_control",
        plan([rc("y", ["no-op"], body_name=NEW, importing=NEW)], []),
    ),
}


def observed_structure(p: dict) -> tuple:
    def key(entries):
        return tuple(
            (
                e["address"].split(".", 1)[1],
                tuple(e["change"]["actions"]),
                bool((e["change"] or {}).get("importing")),
            )
            for e in entries or []
        )
    return key(p.get("resource_changes")), key(p.get("resource_drift"))


def main() -> int:
    DST.mkdir(parents=True, exist_ok=True)
    (DST / "raw").mkdir(exist_ok=True)
    failures = 0
    for name, (case, built) in FIXTURES.items():
        raw = json.loads((SRC / f"{case}.json").read_text())
        want = observed_structure(raw)
        got = observed_structure(built)
        # case8's observed run carried an incidental resource_drift `update` on x
        # from a docker readback artifact (driver_opts null<->empty) with no GCS
        # analogue; it is dropped, and C1 blocks on resource_changes regardless.
        if case == "case8_old_still_live":
            want = (want[0], ())
        if want != got:
            print(f"MISMATCH {name}\n  observed={want}\n  built   ={got}", file=sys.stderr)
            failures += 1
            continue
        (DST / name).write_text(json.dumps(built, indent=2) + "\n", encoding="utf-8")
        shutil.copyfile(SRC / f"{case}.json", DST / "raw" / f"{case}.json")
        print(f"ok  {name}  <- {case}  {want}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
