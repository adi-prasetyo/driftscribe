# Phase B — Whole-Project Infra Reader: Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Add a read-only `infra-reader` worker that enumerates the project's Cloud Asset Inventory–searchable resources and labels each "declared in IaC" vs "not in IaC" (by parsing the committed `iac/*.tf`), surfaced to the `explore` chat workload via a new read-only `read_project_inventory` tool.

**Architecture:** A new Cloud Run worker (`workers/infra_reader/`) mirrors `workers/reader/`: FastAPI, `verify_caller` auth, a zero-arg `extra="forbid"` request. It calls CAI `search_all_resources` with a minimal `read_mask`, parses the baked-in `iac/` dir for declared identities (no tofu state, no KMS), and returns a bounded summary. Coordinator wiring mirrors the existing `drift_reader` tool/worker registration. A policy-free HCL parser is lifted out of `tools/iac_static_gate.py` into `driftscribe_lib/iac_hcl.py` and shared.

**Tech Stack:** Python 3.12+, FastAPI, pydantic v2, `google-cloud-asset`, `python-hcl2` (dev→runtime), pytest, uv. Design: `docs/plans/2026-05-27-infra-iac-phase-b-design.md`.

**Conventions for the executor:**
- Work in this worktree: `/home/adi/driftscribe/.worktrees/infra-iac-phase-b` (branch `feat/infra-iac-phase-b`).
- Run commands with `uv run` (e.g. `uv run pytest …`, `uv run ruff check .`).
- TDD: write the failing test, see it fail, implement minimally, see it pass, commit. Frequent small commits.
- `[AGENT]` tasks are code/tests/docs the executor does fully. `[OPERATOR]` tasks touch live GCP/deploy — the executor **authors** the file but **must NOT execute** the deploy/IAM/API steps. Mirror Phase A's discipline.
- Secret hygiene: never echo secret values in code/tests/logs.

---

## Task 0: Dependencies [AGENT]

**Files:**
- Modify: `pyproject.toml` (move `python-hcl2` to runtime; add `google-cloud-asset`)

**Context:** Phase A added `python-hcl2>=4.3` under `[project.optional-dependencies].dev`. The shared parser becomes runtime code imported by the worker, so `python-hcl2` must be a **runtime** dependency. The worker also needs `google-cloud-asset`.

**Step 1: Inspect current deps**

Run: `grep -n "python-hcl2\|google-cloud-\|dependencies" pyproject.toml`
Note the runtime `dependencies = [...]` array and the `[project.optional-dependencies].dev` array.

**Step 2: Edit pyproject.toml**

- Add to the runtime `dependencies` array: `"google-cloud-asset>=3.25"` and `"python-hcl2>=4.3"`.
- Remove `python-hcl2>=4.3` from the `dev` optional-dependencies array (it's now runtime; keep it resolvable for dev via the base deps).

**Step 3: Sync and verify import**

Run: `uv sync --extra dev && uv run python -c "import hcl2, google.cloud.asset_v1; print('ok')"`
Expected: `ok`

**Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build(iac): add google-cloud-asset; promote python-hcl2 to runtime dep"
```

---

## Task 1: Lift policy-free HCL parsing into a shared module (golden-parity first) [AGENT]

**Files:**
- Create: `driftscribe_lib/iac_hcl.py`
- Create: `tests/unit/test_iac_hcl.py`
- Create: `tests/unit/test_iac_static_gate_parity.py` (golden parity, written BEFORE the refactor)
- Modify: `tools/iac_static_gate.py` (import primitives from the shared module)

**Context:** `tools/iac_static_gate.py` (merged Phase A) holds the only HCL parser. We must move the **policy-free** primitives only — `_is_meta_key`, `_unwrap`, `_block_label`, `_parse`, `_iter_blocks`, `_iter_typed_blocks` — into `driftscribe_lib/iac_hcl.py`, keep all **gate policy** (`ALLOWED_PROVIDERS`, `evaluate`, the rule constants) in the gate, and prove byte-for-byte parity. The gate's existing suite (`tests/unit/test_iac_static_gate.py`, `…_cli.py`) must stay green **unchanged**.

**Step 1: Write the golden-parity test FIRST (guards the refactor)**

Create `tests/unit/test_iac_static_gate_parity.py`. It captures the gate's current parse/iteration behavior on the real committed `iac/*.tf` and pins it, so the refactor can't silently change parsing:

```python
"""Golden-parity guard for the Task-1 parser refactor.

Captures tools.iac_static_gate's parse + block-iteration output on the real
committed iac/*.tf BEFORE primitives move to driftscribe_lib.iac_hcl, then
asserts the shared module reproduces it byte-for-byte. Protects the merged
Phase A gate from a refactor regression.
"""
from pathlib import Path

import pytest

IAC = Path(__file__).resolve().parents[2] / "iac"
TF_FILES = sorted(p.name for p in IAC.glob("*.tf"))


@pytest.mark.parametrize("fname", TF_FILES)
def test_shared_parse_matches_gate_parse(fname):
    from tools import iac_static_gate as gate
    from driftscribe_lib import iac_hcl

    content = (IAC / fname).read_text(encoding="utf-8")
    # Same parser, same result (the gate delegates to the shared parser).
    assert iac_hcl.parse_hcl(content) == gate._parse(fname, content)


def test_meta_key_and_unwrap_parity():
    from tools import iac_static_gate as gate
    from driftscribe_lib import iac_hcl

    for k in ("__is_block__", "__start_line__", "__inline_comments__"):
        assert iac_hcl.is_meta_key(k) is True
        assert gate._is_meta_key(k) is True
    assert iac_hcl.is_meta_key("google") is False
    assert iac_hcl.unwrap('"hashicorp/google"') == "hashicorp/google"


def test_gate_iter_typed_blocks_still_yields_2_tuples():
    """The gate's internal contract is (type, body); the shared module yields
    (type, name, body). Pin BOTH so the adapter can't silently change either."""
    from tools import iac_static_gate as gate
    from driftscribe_lib import iac_hcl

    src = 'resource "null_resource" "x" { triggers = {} }'
    parsed = iac_hcl.parse_hcl(src)
    gate_yield = list(gate._iter_typed_blocks(parsed, "resource"))
    shared_yield = list(iac_hcl.iter_typed_blocks(parsed, "resource"))
    assert all(len(t) == 2 for t in gate_yield)          # (type, body)
    assert all(len(t) == 3 for t in shared_yield)        # (type, name, body)
    assert gate_yield[0][0] == "null_resource"
    assert shared_yield[0][:2] == ("null_resource", "x")


def test_gate_policy_intact_after_refactor():
    """The refactor must not loosen the gate. Spot-check the high-value rules
    still fire (the policy stays in tools/iac_static_gate.py, not the shared
    module)."""
    from tools.iac_static_gate import GateInput, GateMode, evaluate

    bad = {
        "iac/x.tf": 'resource "null_resource" "x" {}\n'
                    'data "external" "y" { program = ["sh"] }',
    }
    rules = {v.rule for v in evaluate(
        GateInput(mode=GateMode.OPERATOR, changed_paths=("iac/x.tf",), hcl_files=bad)
    )}
    assert "arbitrary-execution" in rules
    assert "forbidden-data-source" in rules
```

> **Executor note:** the parse-equality test alone is partly tautological once the gate delegates to the shared parser — the three tests above are the real guard (the gate's 2-tuple contract, the shared 3-tuple, and that `evaluate()` policy still fires). Include all of them.

**Step 2: Run it — expect failure (shared module doesn't exist yet)**

Run: `uv run pytest tests/unit/test_iac_static_gate_parity.py -q`
Expected: FAIL (`ModuleNotFoundError: driftscribe_lib.iac_hcl`)

**Step 3: Create `driftscribe_lib/iac_hcl.py`**

Move the policy-free primitives verbatim (rename leading underscores to public where the gate/worker import them). Preserve the docstrings — they encode the hcl2 8.x lossy-round-trip rationale.

```python
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
```

**Step 4: Refactor `tools/iac_static_gate.py` to import the shared primitives**

Replace the local `_is_meta_key`, `_unwrap`, `_block_label`, `_parse`, `_iter_blocks` bodies with thin re-exports/aliases so the gate's internal callsites are unchanged:

```python
from driftscribe_lib.iac_hcl import (
    block_label as _block_label,
    is_meta_key as _is_meta_key,
    iter_blocks as _iter_blocks,
    parse_hcl,
    unwrap as _unwrap,
)


def _parse(path: str, content: str) -> dict | None:
    return parse_hcl(content)
```

For `_iter_typed_blocks`, the gate uses `(type, body)`. Adapt it to consume the shared 3-tuple while preserving the gate's 2-tuple internal contract:

```python
def _iter_typed_blocks(parsed: dict, kind: str):
    from driftscribe_lib.iac_hcl import iter_typed_blocks
    for rtype, _name, body in iter_typed_blocks(parsed, kind):
        yield rtype, body
```

Keep ALL policy in the gate (`BUILTIN_PROVIDERS`, `ALLOWED_PROVIDERS`, `evaluate`, `_collect_providers`, `_body_has_block`, rule constants). Do NOT move them.

**Step 5: Run parity + the full existing gate suite — all green**

Run: `uv run pytest tests/unit/test_iac_static_gate_parity.py tests/unit/test_iac_static_gate.py tests/unit/test_iac_static_gate_cli.py -q`
Expected: PASS, with the two pre-existing gate test files **unchanged**.

**Step 6: Add focused shared-module unit tests**

Create `tests/unit/test_iac_hcl.py` covering: `is_meta_key` (dunder vs real ident), `unwrap` (quoted/unquoted/non-str), `parse_hcl` (valid → dict, invalid → None), `iter_blocks` (list/dict/missing), `iter_typed_blocks` yields `(type, name, body)` incl. a commented file (dunder keys skipped).

Run: `uv run pytest tests/unit/test_iac_hcl.py -q` → PASS.

**Step 7: Commit**

```bash
git add driftscribe_lib/iac_hcl.py tools/iac_static_gate.py tests/unit/test_iac_hcl.py tests/unit/test_iac_static_gate_parity.py
git commit -m "refactor(iac): lift policy-free HCL parsing into driftscribe_lib.iac_hcl (golden parity)"
```

---

## Task 2: Declared-identity extraction (import IDs + Cloud Run v2 resolver) [AGENT]

**Files:**
- Modify: `driftscribe_lib/iac_hcl.py` (add identity extraction)
- Create: `tests/unit/test_iac_declared_identities.py`
- Create test fixtures inline (use `tmp_path` or string literals mirroring real `iac/`).

**Context:** Build the "declared in IaC" identity set from parsed HCL with confidence tiers (design §4.3). Two contributors: (a) `import { id = "…" }` blocks → **high** confidence; (b) `resource` blocks of supported types → **derived** confidence, resolving `var.X` against `variable` defaults. v1 supports exactly `google_cloud_run_v2_service`. Real data (from `iac/imports.tf` + `iac/cloudrun.tf` + `iac/variables.tf`): import id `projects/driftscribe-hack-2026/locations/asia-northeast1/services/payment-demo`; the resource resolves `name="payment-demo"`, `location=var.region` (default `asia-northeast1`), `project=var.project_id` (default `driftscribe-hack-2026`) → the SAME identity. The two contributors must agree for payment-demo.

**Step 1: Write failing tests**

Create `tests/unit/test_iac_declared_identities.py`:

```python
from driftscribe_lib import iac_hcl

IMPORTS_TF = '''
import {
  to = google_cloud_run_v2_service.payment_demo
  id = "projects/driftscribe-hack-2026/locations/asia-northeast1/services/payment-demo"
}
'''
VARIABLES_TF = '''
variable "project_id" { type = string\n default = "driftscribe-hack-2026" }
variable "region" { type = string\n default = "asia-northeast1" }
'''
CLOUDRUN_TF = '''
resource "google_cloud_run_v2_service" "payment_demo" {
  name     = "payment-demo"
  location = var.region
  project  = var.project_id
}
'''


PD_IDENTITY = "projects/driftscribe-hack-2026/locations/asia-northeast1/services/payment-demo"


def test_import_block_identity_is_high_confidence_with_address_and_asset_type():
    decls, parse_errors = iac_hcl.extract_declared_identities({"imports.tf": IMPORTS_TF})
    assert parse_errors == []
    ids = {d.identity: d for d in decls}
    assert PD_IDENTITY in ids
    d = ids[PD_IDENTITY]
    assert d.source == "import_id"
    assert d.confidence == "high"
    # `to` parses as "${google_cloud_run_v2_service.payment_demo}"; must be
    # unwrapped to the bare address and the asset_type inferred from the type.
    assert d.address == "google_cloud_run_v2_service.payment_demo"
    assert d.asset_type == "run.googleapis.com/Service"


def test_cloud_run_resource_resolves_var_defaults_to_derived_identity():
    files = {"variables.tf": VARIABLES_TF, "cloudrun.tf": CLOUDRUN_TF}
    decls, _ = iac_hcl.extract_declared_identities(files)
    derived = [d for d in decls if d.source == "derived_resource"]
    assert any(
        d.identity == PD_IDENTITY and d.confidence == "derived"
        and d.asset_type == "run.googleapis.com/Service"
        for d in derived
    )


def test_import_and_resource_agree_high_confidence_wins_keeps_asset_type():
    files = {"imports.tf": IMPORTS_TF, "variables.tf": VARIABLES_TF, "cloudrun.tf": CLOUDRUN_TF}
    decls, _ = iac_hcl.extract_declared_identities(files)
    matches = [d for d in decls if d.identity == PD_IDENTITY]
    assert len(matches) == 1                      # de-duped
    assert matches[0].confidence == "high"        # import wins
    assert matches[0].asset_type == "run.googleapis.com/Service"  # not lost in dedup


def test_unsupported_import_target_has_no_asset_type():
    # An import to a type with no v1 resolver: address unwrapped, asset_type None.
    src = ('import {\n to = google_storage_bucket.b\n'
           ' id = "my-proj/my-bucket"\n}')
    decls, _ = iac_hcl.extract_declared_identities({"i.tf": src})
    d = next(d for d in decls if d.source == "import_id")
    assert d.address == "google_storage_bucket.b"
    assert d.asset_type is None                   # unsupported type → not matchable


def test_unsupported_resource_type_is_identity_unresolved():
    files = {"x.tf": 'resource "google_storage_bucket" "b" { name = "x" }'}
    decls, _ = iac_hcl.extract_declared_identities(files)
    assert any(d.identity is None and d.address == "google_storage_bucket.b" for d in decls)


def test_runtime_valued_attribute_is_unresolved():
    files = {
        "x.tf": 'resource "google_cloud_run_v2_service" "s" { name = "n" location = google_x.y.loc project = "p" }'
    }
    decls, _ = iac_hcl.extract_declared_identities(files)
    assert any(d.identity is None for d in decls)


def test_parse_error_is_reported():
    decls, parse_errors = iac_hcl.extract_declared_identities({"bad.tf": 'resource "x" {{{ '})
    assert "bad.tf" in parse_errors
```

> **Note (locals):** the design allowed resolving `var`/`local` defaults; v1 resolves **`var` defaults only**. `local`-valued attributes (`location = local.region`) are treated as **unresolved** (identity None). Document this in the resolver docstring; add `locals` support only if a real `iac/` file needs it (YAGNI).

**Step 2: Run — expect failure**

Run: `uv run pytest tests/unit/test_iac_declared_identities.py -q`
Expected: FAIL (`extract_declared_identities` undefined).

**Step 3: Implement in `driftscribe_lib/iac_hcl.py`**

```python
from dataclasses import dataclass


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


import re

_REF_RE = re.compile(r"^\$\{(.+)\}$")
_VAR_RE = re.compile(r"^var\.([A-Za-z_][A-Za-z0-9_]*)$")


def _unwrap_ref(value) -> str | None:
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


def _resolve_scalar(value, var_defaults: dict[str, str]) -> str | None:
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
    declared-set status). Identities are de-duplicated by identity, keeping the
    highest-confidence source and merging asset_type so a high-confidence import
    never loses a known asset_type.
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
                found.append(DeclaredIdentity(ident, address, "derived_resource", "derived", asset_type))

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
```

> **Executor:** add a test for this exact edge — an unsupported high-confidence import (`asset_type=None`) and a supported derived resource that share the same identity string must remain TWO distinct `DeclaredIdentity` entries (one with `asset_type=None`, one with the supported type), so the unsupported one is never matchable in Task 3.

**Step 4: Run — expect pass**

Run: `uv run pytest tests/unit/test_iac_declared_identities.py -q` → PASS.

**Step 5: Add a real-files regression test**

Add a test that reads the actual `iac/imports.tf`, `iac/variables.tf`, `iac/cloudrun.tf` and asserts the payment-demo identity is present at `high` confidence (import wins over derived). This catches a drift between the resolver and the real committed HCL (the same class of bug CI caught in Phase A).

Run: `uv run pytest tests/unit/test_iac_declared_identities.py -q` → PASS.

**Step 6: Commit**

```bash
git add driftscribe_lib/iac_hcl.py tests/unit/test_iac_declared_identities.py
git commit -m "feat(iac): declared-identity extraction (import IDs + Cloud Run v2 resolver, confidence tiers)"
```

---

## Task 3: Inventory builder (pure CAI-result → summary logic) [AGENT]

**Files:**
- Create: `driftscribe_lib/infra_inventory.py`
- Create: `tests/unit/test_infra_inventory.py`

**Context:** Pure, mockable logic that takes already-fetched CAI results + the declared set and produces the bounded summary (design §4.5). Keeping it pure (no network) makes it trivially testable; the worker (Task 4) supplies real CAI results. Implements: name normalization, type-aware matching, confidence carry-through, sensitive-type counts-only, sample capping, `declared_not_found` with reason codes + identity redaction.

**Step 1: Write failing tests** (`tests/unit/test_infra_inventory.py`)

Cover, each as its own test:
- `normalize_cai_name("//run.googleapis.com/projects/p/locations/l/services/s")` == `"projects/p/locations/l/services/s"`.
- a CAI result whose `(asset_type, normalized name)` matches a `high` DeclaredIdentity → sample entry `iac=True, match_confidence="high"`; counts roll up to `declared_in_iac`.
- a non-matching CAI result → `iac=False, match_confidence=None`, rolls into `not_in_iac`.
- **type-aware non-force-match:** a declared identity with `asset_type=None` (unsupported import) whose identity string equals a live resource's normalized name must NOT match (different/absent asset_type) — the live resource stays `iac=False`, and the declaration lands in `declared_not_found` with `possible_causes=["asset_type_not_supported"]`.
- **conditioned causes:** an unresolved declaration (`identity=None`) → `declared_not_found` entry with `possible_causes=["identity_unresolved"]`, no `identity` field (address present); a resolved+supported+unmatched declaration → `["cai_lag","not_yet_applied","format_mismatch"]`.
- sample capping: 25 results of one type → `count==25`, `len(sample)<=10`.
- sensitive type (`secretmanager.googleapis.com/Secret`) → entry has `sensitive=True` and NO `sample` key.
- a declared identity with no live match → appears in `declared_not_found` with `source`, `confidence`, non-empty `possible_causes`.
- a declared-not-found whose declared `asset_type` is sensitive → `identity` omitted, `identity_redacted=True`.
- output carries `inventory_source="cloud_asset_inventory"`, a non-empty `freshness_caveat`, and the passed-through `iac_snapshot_sha`.
- `total_resources`, `declared_in_iac`, `not_in_iac` are internally consistent (declared+not == total).

**Step 2: Run — expect failure.** `uv run pytest tests/unit/test_infra_inventory.py -q` → FAIL.

**Step 3: Implement `driftscribe_lib/infra_inventory.py`**

```python
"""Pure inventory-summary builder for the infra-reader worker.

No network. Takes normalized CAI resource records + the IaC declared-identity
set and produces the bounded, redaction-safe summary the worker returns
(design §4.5). Kept pure so it is fully unit-testable; the worker supplies the
real CAI page iterator and the declared set.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

from driftscribe_lib.iac_hcl import DeclaredIdentity

# Asset types whose resource NAMES tend to carry sensitive data — surfaced as
# counts only (no sample, identity redacted in declared_not_found). Small,
# explicit, prefix-exact (Codex nit: no fuzzy matching).
SENSITIVE_ASSET_TYPES = frozenset({
    "secretmanager.googleapis.com/Secret",
    "secretmanager.googleapis.com/SecretVersion",
})

_FRESHNESS = (
    "CAI is eventually consistent and does not cover all resource types; "
    "this is a best-available index, not ground truth."
)
_SAMPLE_CAP = 10


@dataclass(frozen=True)
class CaiResource:
    """The masked CAI fields we use (read_mask = name,assetType,location)."""
    name: str            # full //service/projects/.../X
    asset_type: str
    location: str


def normalize_cai_name(name: str) -> str:
    """Strip the ``//<service>/`` scheme prefix → comparable ``projects/.../X`` path."""
    if name.startswith("//"):
        # //run.googleapis.com/projects/... -> projects/...
        rest = name[2:]
        slash = rest.find("/")
        return rest[slash + 1:] if slash != -1 else rest
    return name


def _is_sensitive(asset_type: str) -> bool:
    return asset_type in SENSITIVE_ASSET_TYPES


def build_inventory(
    resources: list[CaiResource],
    declared: list[DeclaredIdentity],
    *,
    project: str,
    iac_snapshot_sha: str,
    declared_parse_ok: bool = True,
) -> dict:
    """Build the bounded summary dict. See design §4.5 for the shape."""
    # Type-aware match index: only declarations with BOTH a resolved identity
    # AND a known (supported) asset_type are matchable. Keying by
    # (asset_type, identity) prevents force-matching an unsupported import ID
    # against an unrelated live resource that happens to share a path suffix.
    matchable: dict[tuple[str, str], DeclaredIdentity] = {
        (d.asset_type, d.identity): d
        for d in declared
        if d.asset_type is not None and d.identity is not None
    }
    matched_keys: set[tuple[str, str]] = set()

    type_buckets: dict[str, dict] = defaultdict(
        lambda: {"count": 0, "declared_in_iac": 0, "not_in_iac": 0, "_samples": []}
    )
    declared_total = 0
    for r in resources:
        norm = normalize_cai_name(r.name)
        key = (r.asset_type, norm)
        decl = matchable.get(key)
        bucket = type_buckets[r.asset_type]
        bucket["count"] += 1
        if decl is not None:
            matched_keys.add(key)
            bucket["declared_in_iac"] += 1
            declared_total += 1
            conf = decl.confidence
            iac = True
        else:
            bucket["not_in_iac"] += 1
            conf = None
            iac = False
        if len(bucket["_samples"]) < _SAMPLE_CAP:
            display = norm.rsplit("/", 1)[-1] if norm else r.name
            bucket["_samples"].append(
                {"name": display, "location": r.location, "iac": iac, "match_confidence": conf}
            )

    by_type: dict[str, dict] = {}
    for atype, b in sorted(type_buckets.items()):
        sensitive = _is_sensitive(atype)
        entry = {
            "count": b["count"],
            "declared_in_iac": b["declared_in_iac"],
            "not_in_iac": b["not_in_iac"],
            "sensitive": sensitive,
        }
        if not sensitive:
            entry["sample"] = b["_samples"]
        by_type[atype] = entry

    # declared_not_found: every declared item with no live match, categorized by
    # WHY it didn't match. possible_causes is conditioned, not a blanket list.
    declared_not_found = []
    for decl in declared:
        if decl.asset_type is not None and decl.identity is not None:
            if (decl.asset_type, decl.identity) in matched_keys:
                continue
            causes = ["cai_lag", "not_yet_applied", "format_mismatch"]
        elif decl.identity is None:
            causes = ["identity_unresolved"]      # runtime-valued attrs
        else:  # has identity but unsupported asset_type → not matchable
            causes = ["asset_type_not_supported"]
        sensitive = decl.asset_type is not None and _is_sensitive(decl.asset_type)
        entry = {
            "address": decl.address or None,
            "asset_type": decl.asset_type,
            "source": decl.source,
            "confidence": decl.confidence,
            "possible_causes": causes,
        }
        if sensitive:
            entry["identity_redacted"] = True       # have identity, withhold it
        elif decl.identity is not None:
            entry["identity"] = decl.identity
        # else: identity is None (unresolved) — neither field; `address` carries it
        declared_not_found.append(entry)

    total = len(resources)
    out = {
        "project": project,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inventory_source": "cloud_asset_inventory",
        "freshness_caveat": _FRESHNESS,
        "iac_snapshot_sha": iac_snapshot_sha,
        "total_resources": total,
        "declared_in_iac": declared_total,
        "not_in_iac": total - declared_total,
        "by_type": by_type,
        "declared_not_found": declared_not_found,
        "truncated": {"per_type_sample": _SAMPLE_CAP},
    }
    if not declared_parse_ok:
        out["declared_set_status"] = "parse_error"
    return out
```

**Step 4: Run — expect pass.** `uv run pytest tests/unit/test_infra_inventory.py -q` → PASS.

**Step 5: Commit**

```bash
git add driftscribe_lib/infra_inventory.py tests/unit/test_infra_inventory.py
git commit -m "feat(iac): pure inventory-summary builder (matching, confidence, sensitive redaction, capping)"
```

---

## Task 4: The infra-reader worker [AGENT]

**Files:**
- Create: `workers/infra_reader/__init__.py`, `workers/infra_reader/main.py`
- Create: `workers/infra_reader/tests/__init__.py`, `workers/infra_reader/tests/test_describe.py`

**Context:** Mirror `workers/reader/main.py`. Add CAI enumeration with the minimal `read_mask`, read the baked-in `iac/` dir, build the declared set + summary, degrade gracefully on permission errors. Env: `GCP_PROJECT`, `OWN_URL`, `ALLOWED_CALLERS` (required); `IAC_DIR` (default `/app/iac`), `IAC_SNAPSHOT_SHA` (default `"unknown"`). The CAI client is `google.cloud.asset_v1.AssetServiceClient`.

**Step 1: Write failing tests** (`workers/infra_reader/tests/test_describe.py`)

Use FastAPI `TestClient` + `app.dependency_overrides[_verify_caller_dep]` to bypass auth (mirror how `workers/reader/tests` do it — inspect that file first). Monkeypatch the CAI client with a fake whose `search_all_resources` returns an iterable of objects with `.name`, `.asset_type`, `.location`. Cover:
- `/healthz` → `{"ok": True}`.
- `/describe` happy path: fake CAI returns the payment-demo Cloud Run service + one unmanaged service → response has `declared_in_iac>=1`, the payment-demo sample `iac=True`.
- **read_mask assertion:** capture the kwargs passed to `search_all_resources`; assert the `read_mask` paths are exactly `["name", "asset_type", "location"]` (pin whichever form the installed client requires — `FieldMask(paths=[...])` or a `read_mask=` string; verify by reading the client signature in this task).
- pagination: fake returns 2 pages → counts aggregate.
- `extra="forbid"`: POST `{"x": 1}` → 422.
- auth: with NO dependency override and a missing/invalid token → 401/403 (inspect reader's test for the exact pattern).
- degradation (CAI): fake `search_all_resources` raises `google.api_core.exceptions.PermissionDenied` → 200 with `{"error": "cloud_asset_unavailable", ...}` (NOT a 500). (200-not-4xx is deliberate — `worker_client.call` treats non-2xx as a transport failure; a 200 lets chat narrate partial degradation. Auth failures stay real 401/403.)
- degradation (declared parse): point `IAC_DIR` at a tmp dir containing one malformed `*.tf` → response still returns the live inventory AND carries `declared_set_status="parse_error"`.
- `iac_snapshot_sha` present from `IAC_SNAPSHOT_SHA` env.

**Step 2: Run — expect failure.** `uv run pytest workers/infra_reader/tests/ -q` → FAIL.

**Step 3: Implement `workers/infra_reader/main.py`**

Mirror `workers/reader/main.py`'s structure (the auth dependency, `install_trace_middleware`, env resolution, `ConfigDict(extra="forbid")`). Sketch:

```python
"""Infra-Reader Agent — read-only whole-project inventory worker (Phase B).

Enumerates the project's CAI-searchable resources and labels each declared-in-
IaC vs not, by parsing the baked-in iac/ dir. No tofu state, no KMS — zero
sensitive credential. SA holds only cloudasset.viewer + serviceUsageConsumer.
"""
import os
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, ConfigDict

from driftscribe_lib.auth import verify_caller
from driftscribe_lib.iac_hcl import extract_declared_identities
from driftscribe_lib.infra_inventory import CaiResource, build_inventory
from driftscribe_lib.logging import install_trace_middleware, setup as setup_logging

log = setup_logging("infra-reader-agent")

GCP_PROJECT = os.environ["GCP_PROJECT"]
OWN_URL = os.environ["OWN_URL"].rstrip("/")
ALLOWED_CALLERS = frozenset(
    e.strip() for e in os.environ["ALLOWED_CALLERS"].split(",") if e.strip()
)
IAC_DIR = Path(os.environ.get("IAC_DIR", "/app/iac"))
IAC_SNAPSHOT_SHA = os.environ.get("IAC_SNAPSHOT_SHA", "unknown")
_READ_MASK_PATHS = ["name", "asset_type", "location"]


def _verify_caller_dep(request: Request) -> str:
    return verify_caller(request, own_url=OWN_URL, allowed_callers=ALLOWED_CALLERS)


class DescribeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")  # zero-arg, see reader Layer 2


def _load_declared():
    """Load + parse the baked-in iac/*.tf. Returns (identities, parse_ok).

    parse_ok is False if ANY iac/*.tf failed to parse — the worker still
    returns the live inventory but flags declared_set_status=parse_error so
    chat knows the declared labeling may be incomplete. (CI's static gate
    prevents un-parseable HCL from landing in iac/, so this is defense in
    depth.)
    """
    files: dict[str, str] = {}
    if IAC_DIR.is_dir():
        for tf in sorted(IAC_DIR.glob("*.tf")):
            files[tf.name] = tf.read_text(encoding="utf-8")
    decls, parse_errors = extract_declared_identities(files)
    return decls, (len(parse_errors) == 0)


def _search_all(client) -> list[CaiResource]:
    from google.cloud import asset_v1
    request = asset_v1.SearchAllResourcesRequest(
        scope=f"projects/{GCP_PROJECT}",
        read_mask={"paths": _READ_MASK_PATHS},  # verify FieldMask form in Task 4 Step 1
    )
    out = []
    for r in client.search_all_resources(request=request):
        out.append(CaiResource(name=r.name, asset_type=r.asset_type, location=r.location))
    return out


app = FastAPI(title="DriftScribe Infra-Reader Agent")
install_trace_middleware(app)


@app.get("/healthz")
def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.post("/describe")
def describe(_body: DescribeRequest, caller: str = Depends(_verify_caller_dep)) -> dict:
    from google.api_core import exceptions as gax
    from google.cloud import asset_v1
    log.info("describe request from %s project=%s", caller, GCP_PROJECT)
    declared, parse_ok = _load_declared()
    try:
        client = asset_v1.AssetServiceClient()
        resources = _search_all(client)
    except gax.PermissionDenied as e:
        return {"error": "cloud_asset_unavailable", "detail": str(e), "project": GCP_PROJECT}
    except gax.GoogleAPICallError as e:
        return {"error": "cloud_asset_unavailable", "detail": str(e), "project": GCP_PROJECT}
    return build_inventory(
        resources, declared,
        project=GCP_PROJECT, iac_snapshot_sha=IAC_SNAPSHOT_SHA, declared_parse_ok=parse_ok,
    )
```

> **Executor:** before writing, READ `workers/reader/tests/test_read.py` for the exact auth-override + token-missing test idiom, and confirm the installed `google-cloud-asset` `SearchAllResourcesRequest` `read_mask` field shape (run `uv run python -c "from google.cloud import asset_v1; help(asset_v1.SearchAllResourcesRequest)"`). Adjust the `read_mask=` construction + the test assertion to the real shape. This is the one external-API contract in the plan — pin it against the installed library, not from memory.

**Step 4: Run — expect pass.** `uv run pytest workers/infra_reader/tests/ -q` → PASS.

**Step 5: Commit**

```bash
git add workers/infra_reader/
git commit -m "feat(iac): infra-reader worker — CAI enumeration + IaC labeling (read-only, masked, fail-soft)"
```

---

## Task 5: Worker Dockerfile + container import smoke [AGENT]

**Files:**
- Create: `workers/infra_reader/Dockerfile`, `workers/infra_reader/pyproject.toml`
- Create: `tests/unit/test_infra_reader_container_imports.py`

**Context:** Worker Dockerfiles install runtime deps explicitly (see `workers/reader/Dockerfile`). The infra-reader needs `python-hcl2` AND `google-cloud-asset` on top of the fastapi/uvicorn/google-auth base, and must `COPY iac/` into the image so the declared set is available at runtime. The `IAC_SNAPSHOT_SHA` is injected at deploy time (Task 9).

**Step 1: Write the Dockerfile** (mirror `workers/reader/Dockerfile`), with:
- `uv pip install --system "fastapi>=0.115" "uvicorn[standard]>=0.32" "google-cloud-asset>=3.25" "python-hcl2>=4.3" "google-auth>=2.35" "requests>=2.32"`
- `COPY driftscribe_lib/ ./driftscribe_lib/`
- `COPY workers/__init__.py`, `workers/infra_reader/__init__.py`, `workers/infra_reader/main.py`
- `COPY iac/ ./iac/`  ← the declared-set source (matches `IAC_DIR=/app/iac`)
- `CMD ["sh","-c","uvicorn workers.infra_reader.main:app --host 0.0.0.0 --port ${PORT:-8080}"]`

**Step 2: Write the import-smoke test** (`tests/unit/test_infra_reader_container_imports.py`): asserts `driftscribe_lib.iac_hcl`, `driftscribe_lib.infra_inventory`, and `google.cloud.asset_v1` all import (a proxy that the Dockerfile's declared dep set is complete). Parse the Dockerfile's `uv pip install` line and assert `google-cloud-asset` and `python-hcl2` appear (so a future dep added to `main.py` without a Dockerfile update fails here).

Run: `uv run pytest tests/unit/test_infra_reader_container_imports.py -q` → PASS.

**Step 3: Commit**

```bash
git add workers/infra_reader/Dockerfile workers/infra_reader/pyproject.toml tests/unit/test_infra_reader_container_imports.py
git commit -m "build(iac): infra-reader Dockerfile (hcl2+asset deps, bakes iac/) + container import smoke"
```

---

## Task 6: Coordinator tool wrapper [AGENT]

**Files:**
- Modify: `agent/adk_tools.py` (add `read_project_inventory_tool`)
- Create: `tests/unit/test_read_project_inventory_tool.py`

**Context:** Mirror `read_live_env_tool` — zero-arg, delegates to the worker via `worker_client.call`. The worker name is `infra_reader`; the endpoint `/describe` is registered in Task 7.

**Step 1: Write failing test** — monkeypatch `agent.worker_client.call` to assert it's called with `("infra_reader", {})` and returns the dict.

**Step 2: Run → FAIL.**

**Step 3: Implement** in `agent/adk_tools.py` (place near `read_live_env_tool`):

```python
def read_project_inventory_tool() -> dict:
    """Ask the Infra-Reader Agent for the whole-project resource inventory.

    No arguments — the worker has the target project pinned via env, and its
    DescribeRequest schema is ``extra="forbid"`` (Layer 2). Returns a bounded
    summary: counts by asset type, each resource labeled declared-in-IaC vs
    not, plus declared_not_found with reason codes. Read-only: the worker holds
    only cloudasset.viewer + serviceUsageConsumer — no mutation, no tofu state,
    no KMS. The summary is CAI-sourced (eventually consistent, partial
    coverage) — present it with its freshness_caveat, and present
    declared_not_found entries as "things to check," never confirmed drift.
    """
    return worker_client.call("infra_reader", {})
```

**Step 4: Run → PASS. Step 5: Commit.**

```bash
git add agent/adk_tools.py tests/unit/test_read_project_inventory_tool.py
git commit -m "feat(iac): read_project_inventory_tool (zero-arg coordinator wrapper -> infra_reader /describe)"
```

---

## Task 7: Wire tool + worker into registries, explore workload, AND its pins (ends green) [AGENT]

**Files:**
- Modify: `agent/workloads/registry.py` (`_TOOL_REGISTRY`, `_WORKER_REGISTRY`)
- Modify: `agent/worker_client.py` (`_WORKER_URL_ENV`, `WORKER_ENDPOINTS`)
- Modify: `agent/adk_agent.py` (import + `COORDINATOR_TOOLS` + `EXPLORE_WORKLOAD_TOOL_NAMES`)
- Modify: `workloads/explore/workload.yaml` (`enabled_tool_names`, `worker_names`)
- Modify: `tests/conftest.py` (`explore_workload_env` adds `INFRA_READER_URL`)
- Modify: `tests/unit/test_coordinator_tool_inventory.py` (`EXPECTED_TOOL_NAMES`)

**Context:** Add the symbolic tool `read_project_inventory` and worker `infra_reader`, AND update the capability pins in the SAME task so the task ends green (the wiring and its pin must change in lockstep — splitting them would commit a known-red state). The order in `EXPLORE_WORKLOAD_TOOL_NAMES` must match the YAML (tool-order pin) — append the new tool **last** in both.

**Steps:**
1. `registry.py`: `_TOOL_REGISTRY["read_project_inventory"] = read_project_inventory_tool` (add the import at top); `_WORKER_REGISTRY["infra_reader"] = WorkerSpec(url_env="INFRA_READER_URL")`.
2. `worker_client.py`: `_WORKER_URL_ENV["infra_reader"] = "INFRA_READER_URL"`; `WORKER_ENDPOINTS["infra_reader"] = "/describe"`.
3. `adk_agent.py`: import `read_project_inventory_tool`; append to `COORDINATOR_TOOLS`; append `"read_project_inventory"` to `EXPLORE_WORKLOAD_TOOL_NAMES`. Update the stale comment that says explore "adds NO new callable to COORDINATOR_TOOLS" (it does now — note it's still read-only).
4. `workloads/explore/workload.yaml`: append `- read_project_inventory` to `enabled_tool_names`; append `- infra_reader` to `worker_names`.
5. `tests/conftest.py`: in `explore_workload_env`, add `monkeypatch.setenv("INFRA_READER_URL", "https://infra-reader.test")` and update the docstring (now THREE read workers).
6. `tests/unit/test_coordinator_tool_inventory.py`: add `"read_project_inventory_tool"` to `EXPECTED_TOOL_NAMES`. **Do NOT** add `read_project_inventory` to `_MUTATION_TOOL_NAMES` or `infra_reader` to `_MUTATION_WORKER_NAMES` — it's read-only, which is the whole point.

**Verify (must end GREEN):**
Run: `uv run pytest tests/unit/test_coordinator_tool_inventory.py -q`
Expected: PASS — including `test_explore_workload_is_strictly_read_only` and `test_explore_workload_wires_no_mutation_worker` (proving the read-only guarantee SURVIVED the addition), `test_coordinator_tools_match_expected_set`, and the explore tool-order pin.

**Commit:**
```bash
git add agent/workloads/registry.py agent/worker_client.py agent/adk_agent.py \
        workloads/explore/workload.yaml tests/conftest.py tests/unit/test_coordinator_tool_inventory.py
git commit -m "feat(iac): wire read_project_inventory + infra_reader into explore (with capability pins)"
```

---

## Task 8: Explore system prompt — mention the whole-project read [AGENT]

**Files:**
- Modify: `workloads/explore/system_prompt.md`

**Context:** Tell the LLM about the new read capability and how to present it. Independent, green.

**Step 1:** Extend `workloads/explore/system_prompt.md` with a short paragraph: explore can now read the whole-project inventory via `read_project_inventory`; it is read-only; present results with the `freshness_caveat` (CAI is partial + eventually consistent) and treat `declared_not_found` as "things to check," NOT confirmed drift. Keep the existing read-only framing.

**Step 2: Confirm nothing regressed.**
Run: `uv run pytest tests/unit/test_coordinator_tool_inventory.py -q` → PASS.

**Step 3: Commit.**
```bash
git add workloads/explore/system_prompt.md
git commit -m "docs(iac): explore prompt — whole-project inventory is read-only, CAI-caveated"
```

---

## Task 9: Deploy wiring (Cloud Build) [OPERATOR — author only, DO NOT deploy]

**Files:**
- Modify: `infra/cloudbuild.yaml`

**Context:** Add the infra-reader build/push/deploy steps mirroring the `driftscribe-reader` service exactly, plus the coordinator's `INFRA_READER_URL` env var and the `IAC_SNAPSHOT_SHA` injection. **This file is not exercised by CI** — the review bar is "the diff mirrors the existing reader steps." The executor authors it; the operator runs the build.

**Steps:**
1. Add a docker build step for `driftscribe-infra-reader:${_TAG}` using `workers/infra_reader/Dockerfile` (build context repo root), mirroring the reader build step. Pass `IAC_SNAPSHOT_SHA` via a `--build-arg` set to `$COMMIT_SHA` **or** set it as a deploy env var (preferred — simpler: set `IAC_SNAPSHOT_SHA=$COMMIT_SHA` on the `gcloud run deploy` `--set-env-vars`). Use the deploy-env-var approach (Codex nit: inject via `COMMIT_SHA`).
2. Add the push step.
3. Add the `gcloud run deploy driftscribe-infra-reader` step mirroring the reader's deploy + the two-step OWN_URL writeback, with `--service-account=infra-reader-sa@$PROJECT_ID.iam.gserviceaccount.com`, `--set-env-vars=GCP_PROJECT=$PROJECT_ID,OWN_URL=…,ALLOWED_CALLERS=driftscribe-agent@…,IAC_SNAPSHOT_SHA=$COMMIT_SHA`.
4. Add `INFRA_READER_URL` to the coordinator deploy step's `--set-env-vars` (placeholder.invalid initially, like the other worker URLs) and to the post-deploy URL-writeback if that's how sibling URLs are set.
5. Add the new image to the `images:` list at the bottom of `cloudbuild.yaml` (so it's pushed/recorded like the others).

**Explicit completeness checklist (the diff must add ALL of):** build step · push step · `images:` entry · `gcloud run deploy` step + OWN_URL two-step writeback · `--service-account=infra-reader-sa@…` · the worker `--set-env-vars` (GCP_PROJECT, OWN_URL, ALLOWED_CALLERS, IAC_SNAPSHOT_SHA=$COMMIT_SHA) · coordinator `INFRA_READER_URL` placeholder + writeback. Cross-check against the `driftscribe-reader` blocks — every reader line should have an infra-reader analogue.

**Deploy-trigger caveat:** check whether `cloudbuild.yaml` is wired to an automatic deploy-on-push-to-main trigger. If it IS, this step must NOT merge before the operator has provisioned `infra-reader-sa` + granted its roles + enabled the CAI API — otherwise the first post-merge build deploys a worker whose SA doesn't exist / lacks roles. If so, either (a) land Task 9 in a separate follow-up PR the operator merges after bootstrapping, or (b) confirm with the operator that the deploy is manual (`gcloud builds submit`). Note this prominently in the PR description and the runbook.

**Commit:**
```bash
git add infra/cloudbuild.yaml
git commit -m "build(iac): cloudbuild steps for infra-reader (mirror reader; IAC_SNAPSHOT_SHA via COMMIT_SHA) [operator-deploy]"
```

---

## Task 10: Operator runbook [AGENT writes, OPERATOR runs]

**Files:**
- Create: `docs/runbooks/infra-reader.md`
- Modify: `iac/README.md` (one-line pointer, optional)

**Content:** Operator steps from design §8: (1) enable `cloudasset.googleapis.com`; (2) create `infra-reader-sa`, grant `roles/cloudasset.viewer` + `roles/serviceusage.serviceUsageConsumer` (or a custom role with `cloudasset.assets.searchAllResources` + `serviceusage.services.use`) — document this as the narrow, read-only invariant exception; (3) deploy via cloudbuild; (4) set `INFRA_READER_URL` on the coordinator, and set the **worker's** `ALLOWED_CALLERS` to include the coordinator's SA `driftscribe-agent@$PROJECT_ID.iam.gserviceaccount.com` (auth direction: the coordinator CALLS the worker, so the *worker* allowlists the *coordinator's* SA — the coordinator does NOT allowlist `infra-reader-sa`); (5) the recommendation to retain `iac/imports.tf` import blocks until Phase C for highest-confidence matching. Note the worker works even before the Phase A backend bootstrap (no state/KMS).

**Commit:**
```bash
git add docs/runbooks/infra-reader.md iac/README.md
git commit -m "docs(iac): infra-reader operator runbook (CAI API + SA roles + deploy + imports-retention)"
```

---

## Task 11: Finalize [AGENT]

**Steps:**
1. Full suite: `uv run pytest -q` → all green (was 654 unit at baseline; expect +N).
2. Lint: `uv run ruff check .` → clean (`uv run ruff format --check .` if the repo uses it).
3. Confirm the read-only invariants one more time: `uv run pytest tests/unit/test_coordinator_tool_inventory.py -q`.
4. Update memory `infra_iac_agent.md` + `MEMORY.md` index to record Phase B (PR #, status). Do NOT duplicate code facts.
5. Push the branch and open a PR (mirror Phase A's PR style). Verify CI green (static-gate must pass — note: this PR touches `iac/`? No — it only READS iac/. If `iac/` is unchanged the gate job may not run; the lint-test job is the relevant gate).
6. Use **superpowers:finishing-a-development-branch**.

---

## Risks / watch-items for the executor

- **The CAI `read_mask` field shape** is the one place memory can be wrong — pin it against the installed `google-cloud-asset` in Task 4 Step 1. Everything else is local logic.
- **Tool-order pin**: `EXPLORE_WORKLOAD_TOOL_NAMES` order must equal the YAML order (append last in both).
- **Read-only invariants are load-bearing**: never add `read_project_inventory`/`infra_reader` to the mutation sets. If a test wants you to, stop — the design says read-only.
- **Don't move gate policy** into `driftscribe_lib.iac_hcl` (Task 1) — primitives only; the golden-parity test guards this.
- **Don't deploy** (Task 9) or run any live GCP/IAM/API command — author only.
