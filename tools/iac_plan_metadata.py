"""Build + serialize the C2 plan-builder metadata.json artifact.

Pure-stdlib helper called by the plan-builder workflow. The metadata
record is the input contract for the C3 plan-approval schema (see
docs/plans/2026-05-28-infra-iac-phase-c2-plan-builder.md §3) — DO NOT
rename a field without updating C3.

Determinism: every public function in this module is a pure function of
its arguments. ``serialize_metadata`` round-trips byte-identically given
the same input (sort_keys + fixed indent + no trailing whitespace).
"""
from __future__ import annotations
