"""Pure helpers for the operator-facing "open infra changes" surface.

No I/O: given a PR's number/title/url/body, derive the adopted resource (when the
PR is an adoption, whose body carries the deterministic ``**Import id:** `...` ``
line rendered by :mod:`driftscribe_lib.adopt_recipe`). The GitHub listing lives in
the agent layer; this module stays import-light and unit-testable.
"""
from __future__ import annotations

import re

# Depend ONLY on adopt_recipe (not infra_graph): _ID_SHAPES + _RTYPE_TO_ASSET_TYPE
# are co-located there for exactly the 4 adoptable types and are drift-pinned by
# adopt_recipe's own tests. Using infra_graph.PLAN_RTYPE_TO_ASSET_TYPE would couple
# this parser to the display-graph module and risk future non-adoptable mappings
# leaking in (Codex review finding 1).
from driftscribe_lib.adopt_recipe import _ID_SHAPES, _RTYPE_TO_ASSET_TYPE

# Matches the body line adopt_recipe renders: ``**Import id:** `<id>` `` (the id is
# inside single backticks). Tolerant of surrounding whitespace; first match wins.
_IMPORT_ID_RE = re.compile(r"\*\*Import id:\*\*\s*`([^`]+)`")


def extract_import_id(pr_body: str | None) -> str | None:
    """The import id from an adoption PR body, or None if absent/empty."""
    if not pr_body:
        return None
    m = _IMPORT_ID_RE.search(pr_body)
    return m.group(1) if m else None


def import_id_to_resource(import_id: str) -> tuple[str, str] | None:
    """Reverse an adoption import id to ``(asset_type, resource_name)``.

    Uses the SAME shape regexes the renderer/static-gate enforce
    (:data:`adopt_recipe._ID_SHAPES`), so an id this accepts is exactly one the
    pipeline could have produced. ``resource_name`` is the bare short name (the
    last path segment), matching the infra-graph node ``name``. Returns None for
    an id matching no adoptable shape.
    """
    if not import_id:
        return None
    for rtype, shape in _ID_SHAPES.items():
        if shape.fullmatch(import_id):
            asset_type = _RTYPE_TO_ASSET_TYPE.get(rtype)
            if not asset_type:
                return None
            name = import_id.rsplit("/", 1)[-1]
            return (asset_type, name)
    return None


def build_pending_approval(
    pr_number: int, title: str, url: str, pr_body: str | None
) -> dict:
    """A single pending-approval DTO. ``asset_type``/``resource_name`` are blank
    when the PR is not a parseable adoption (freehand/new-resource infra PR)."""
    asset_type = ""
    resource_name = ""
    import_id = extract_import_id(pr_body)
    if import_id:
        resolved = import_id_to_resource(import_id)
        if resolved is not None:
            asset_type, resource_name = resolved
    return {
        "pr_number": pr_number,
        "title": title,
        "url": url,
        "asset_type": asset_type,
        "resource_name": resource_name,
    }
