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

import re
from dataclasses import dataclass
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


@dataclass(frozen=True)
class DeclaredIdentity:
    """One IaC-declared resource.

    identity: canonical GCP path (``projects/.../services/x``) or None when
        the parser could not resolve it (unsupported type / runtime-valued attrs).
    address: the HCL address (``<type>.<name>``) for display/debugging.
    source: "import_id" (high confidence) or "derived_resource" (derived).
    confidence: "high" | "derived".
    asset_type: the CAI asset type this identity should match, if known.
    """
    identity: str | None
    address: str
    source: str
    confidence: str
    asset_type: str | None = None


# v1 supports exactly Cloud Run v2 services. Map the HCL resource type to its
# CAI asset type + an identity template. Deliberately narrow (design §4.3 /
# Codex nit): static literal/var-default resolution only — unresolved is fine.
_CLOUD_RUN_V2 = "google_cloud_run_v2_service"
_SUPPORTED_RESOURCE_ASSET_TYPES = {_CLOUD_RUN_V2: "run.googleapis.com/Service"}

_REF_RE = re.compile(r"^\$\{(.+)\}$")
_VAR_RE = re.compile(r"^var\.([A-Za-z_][A-Za-z0-9_]*)$")


def _variable_defaults(parsed_files: dict[str, dict]) -> dict[str, str]:
    """Collect ``variable "x" { default = "lit" }`` literal defaults across files."""
    out: dict[str, str] = {}
    for parsed in parsed_files.values():
        for var_block in iter_blocks(parsed, "variable"):
            for name, body in var_block.items():
                if is_meta_key(name):
                    continue
                if isinstance(body, dict) and isinstance(body.get("default"), str):
                    out[block_label(name)] = unwrap(body["default"])
    return out


def _unwrap_ref(value: Any) -> str | None:
    """Unwrap an hcl2 reference scalar to its bare expression.

    hcl2 8.x renders a bare HCL reference as a wrapped interpolation, e.g.
    ``to = google_cloud_run_v2_service.payment_demo`` parses to the string
    ``"${google_cloud_run_v2_service.payment_demo}"``. Return the inner
    expression (``google_cloud_run_v2_service.payment_demo``), or None.
    """
    if not isinstance(value, str):
        return None
    s = unwrap(value)
    m = _REF_RE.match(s)
    return m.group(1) if m else s


def _resolve_scalar(value: Any, var_defaults: dict[str, str]) -> str | None:
    """Resolve an attribute to a literal string, or None if not statically known.

    Handles literal strings and ``var.x`` references (rendered ``${var.x}``)
    whose variable has a literal default. References to resources/data/locals or
    any other interpolation return None (correctly unresolved). v1 does NOT
    resolve ``local.*`` — locals are unresolved by design.
    """
    if not isinstance(value, str):
        return None
    s = unwrap(value)
    inner = _REF_RE.match(s)
    if inner:
        vm = _VAR_RE.match(inner.group(1))
        return var_defaults.get(vm.group(1)) if vm else None  # local.*/other → None
    if "${" in s:  # embedded interpolation → runtime-valued
        return None
    return s


def _asset_type_for_address(address: str | None) -> str | None:
    """Infer the CAI asset_type from an HCL address (``<type>.<name>``)."""
    if not address or "." not in address:
        return None
    rtype = address.split(".", 1)[0]
    return _SUPPORTED_RESOURCE_ASSET_TYPES.get(rtype)


def extract_declared_identities(
    files: dict[str, str],
) -> tuple[list[DeclaredIdentity], list[str]]:
    """Extract the IaC-declared identity set from filename -> HCL text.

    Returns ``(identities, parse_errors)`` where ``parse_errors`` lists the
    filenames that failed to parse (the worker surfaces this as a degraded
    declared-set status). Identities are de-duplicated by (asset_type, identity),
    keeping the highest-confidence source so a high-confidence import never loses
    a known asset_type.
    """
    parsed_files: dict[str, dict] = {}
    parse_errors: list[str] = []
    for fn, content in files.items():
        p = parse_hcl(content)
        if p is None:
            parse_errors.append(fn)
        else:
            parsed_files[fn] = p

    var_defaults = _variable_defaults(parsed_files)
    found: list[DeclaredIdentity] = []

    # (a) import blocks — high confidence. `to` and `id` both arrive wrapped.
    for parsed in parsed_files.values():
        for imp in iter_blocks(parsed, "import"):
            raw_id = imp.get("id")
            if raw_id is None:
                continue
            ident = unwrap(raw_id)
            address = _unwrap_ref(imp.get("to")) or ""
            asset_type = _asset_type_for_address(address)  # None if unsupported type
            found.append(DeclaredIdentity(ident, address, "import_id", "high", asset_type))

    # (b) supported resource blocks — derived confidence.
    for parsed in parsed_files.values():
        for rtype, name, body in iter_typed_blocks(parsed, "resource"):
            address = f"{rtype}.{name}"
            asset_type = _SUPPORTED_RESOURCE_ASSET_TYPES.get(rtype)
            if asset_type is None:
                found.append(DeclaredIdentity(None, address, "derived_resource", "derived"))
                continue
            if rtype == _CLOUD_RUN_V2:
                proj = _resolve_scalar(body.get("project"), var_defaults)
                loc = _resolve_scalar(body.get("location"), var_defaults)
                svc = _resolve_scalar(body.get("name"), var_defaults)
                ident = (
                    f"projects/{proj}/locations/{loc}/services/{svc}"
                    if (proj and loc and svc) else None
                )
                found.append(
                    DeclaredIdentity(ident, address, "derived_resource", "derived", asset_type)
                )

    # De-dup by (asset_type, identity) — NOT identity alone. Keying by the pair
    # is what keeps an unsupported import (asset_type=None) distinct from a
    # supported resource that happens to share the identity string: they get
    # different keys, so the None-typed import can never inherit the supported
    # type and become matchable (Codex review). Within one (asset_type,
    # identity) key the asset_type is identical, so on collision we just keep
    # the higher-confidence source. Unresolved (identity None) entries are all
    # kept (they're reported, never matched).
    best: dict[tuple[str | None, str], DeclaredIdentity] = {}
    unresolved: list[DeclaredIdentity] = []
    for d in found:
        if d.identity is None:
            unresolved.append(d)
            continue
        key = (d.asset_type, d.identity)
        prev = best.get(key)
        if prev is None or (prev.confidence == "derived" and d.confidence == "high"):
            best[key] = d
    return list(best.values()) + unresolved, parse_errors
