"""Policy-free native-syntax HCL parsing primitives (shared).

Lifted out of tools/iac_static_gate.py in Phase B so both the static gate
(policy enforcement) and the infra-reader worker (declared-identity
extraction) parse HCL the same way. This module contains NO policy — no
allow/deny lists, no rule logic. hcl2 8.x is a lossy round-trip: block
labels and string scalars arrive wrapped in literal double-quotes, and
synthetic dunder-metadata keys (__is_block__, __inline_comments__, …) are
injected whenever the source has comments. The helpers normalize labels and
filter every __dunder__ meta key.
"""
from __future__ import annotations

from typing import Any

import hcl2


def is_meta_key(key: str) -> bool:
    """True for an hcl2-injected dunder-metadata key (``__is_block__`` …)."""
    return isinstance(key, str) and key.startswith("__") and key.endswith("__")


def unwrap(value: Any) -> Any:
    """Strip the literal surrounding double-quotes hcl2 8.x leaves on scalars/labels."""
    if isinstance(value, str) and len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def block_label(key: str) -> str:
    """Normalize a quote-wrapped block-label dict key to its bare identifier."""
    return unwrap(key)


def parse_hcl(content: str) -> dict | None:
    """Parse native-syntax HCL via hcl2; return the dict or ``None`` on any failure.

    Fail-closed: callers treat ``None`` as a parse error (the gate raises a
    violation; the reader marks the declared set unknown).
    """
    try:
        return hcl2.loads(content)
    except Exception:  # noqa: BLE001 - fail-closed: any parse failure is None
        return None


def iter_blocks(parsed: dict, kind: str) -> list[dict]:
    """Return the list of top-level blocks of a given kind (resource/data/...)."""
    blocks = parsed.get(kind)
    if blocks is None:
        return []
    if isinstance(blocks, list):
        return [b for b in blocks if isinstance(b, dict)]
    if isinstance(blocks, dict):
        return [blocks]
    return []


def iter_typed_blocks(parsed: dict, kind: str):
    """Yield ``(type, name, body)`` for each resource/data block.

    Phase B note: unlike the gate's original 2-tuple yield, this yields the
    NAME too — the declared-identity resolver needs the resource's local name
    to build addresses. The gate's callsites ignore the name.
    """
    for block in iter_blocks(parsed, kind):
        for type_label, by_name in block.items():
            if is_meta_key(type_label):
                continue
            rtype = block_label(type_label)
            if not isinstance(by_name, dict):
                continue
            for name_label, body in by_name.items():
                if is_meta_key(name_label):
                    continue
                yield rtype, block_label(name_label), (body if isinstance(body, dict) else {})
