"""Self-protection denylist for DriftScribe IaC plan.json (design doc §5.2).

Pure, deterministic, fail-closed policy over an OpenTofu
``tofu show -json plan.tfplan`` document. Refuses any non-no-op change
targeting the DriftScribe control plane (Cloud Run services + their SAs,
state/artifact buckets and their objects, control-plane secrets and their
versions, the plan-builder KMS key + key ring), any IAM/WIF change, and
any state-mutating action (``delete`` / ``forget`` / replace) — even on
unrelated resources, in v1.

Scaffold only at this commit (Task 2 of the C1 plan). Subsequent tasks
fill in load_plan_json + per-rule evaluators behind the same shape.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Violation:
    """A single denylist violation.

    ``rule`` is a short machine identifier (one of the 14 rule IDs listed
    in the C1 plan §3); ``detail`` is a human-readable message that names
    the offending resource address + action tuple.
    """

    rule: str
    detail: str


@dataclass(frozen=True)
class DenylistInput:
    """Input to :func:`evaluate` — the parsed plan.json dict."""

    plan: dict


def evaluate(di: DenylistInput) -> list[Violation]:
    """Return all violations (empty list = pass).

    Fail-closed: any structural surprise in the plan-JSON yields a
    :class:`Violation` rather than an exception. The library never raises
    on policy concerns; only genuine programming errors bubble up.
    """
    return []
