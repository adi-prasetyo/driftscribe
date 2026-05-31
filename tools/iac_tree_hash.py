"""Print the canonical ``iac/``-tree content hash (CLI + re-export).

The hash function lives in ``driftscribe_lib.iac_tree`` (Phase C6a-1) so the worker
container — which ships ``driftscribe_lib/`` but NOT ``tools/`` — shares the identical
definition. This module is the ``python -m tools.iac_tree_hash <dir>`` CLI the C2
plan-builder workflow (.github/workflows/iac.yml) uses to compute the hash over the
checked-out ``iac/``.

Usage:
    python -m tools.iac_tree_hash iac        # prints the 64-hex digest, no newline
"""
from __future__ import annotations

from driftscribe_lib.iac_tree import IacTreeHashError, iac_tree_hash  # noqa: F401  (re-export)


def _main(argv: list[str]) -> int:
    import argparse
    import sys as _sys

    parser = argparse.ArgumentParser(prog="iac_tree_hash")
    parser.add_argument("iac_dir", help="path to the iac/ directory to hash")
    ns = parser.parse_args(argv)
    try:
        digest = iac_tree_hash(ns.iac_dir)
    except IacTreeHashError as e:
        # Fail-closed: a missing/symlinked iac/ or a symlink within must be a hard
        # error, not a silent empty-tree hash (the workflow then fails before upload).
        print(str(e), file=_sys.stderr)
        return 1
    print(digest, end="")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    import sys as _sys

    _sys.exit(_main(_sys.argv[1:]))
