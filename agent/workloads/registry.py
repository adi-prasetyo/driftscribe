"""Workload registry — the authority on what a workload can touch.

Phase 17.A.1. The coordinator routes work for a named workload by:

1. Loading the YAML manifest into a :class:`WorkloadSpec` (symbolic
   names only — no URLs, no secrets, no repos).
2. Resolving those symbolic names against the three code-side
   allowlists below: :data:`TOOL_REGISTRY`, :data:`WORKER_REGISTRY`,
   :data:`ACTION_REGISTRY`.
3. Caching the resolved :class:`WorkloadResolution` per workload name.

The security property is the inverse of "YAML drives behavior":
*flipping a YAML value can choose from the allowlist, but it cannot
introduce a new URL, secret, repo, or callable.* Codex-flagged blocker
— see Phase 17 plan header §"Architecture".

Failure modes (all raised at *load* time, never at first agent call):

- :class:`UnknownWorkloadError` — `load_workload("kubernetes")` etc.
- :class:`UnknownToolError` — YAML names a tool not in the allowlist,
  or names a tool that's been reserved but not yet implemented (e.g.
  ``upgrade_read_dependencies`` before Phase 17.C ships).
- :class:`UnknownWorkerError` — YAML names a worker not in the allowlist.
- :class:`UnknownActionError` — YAML names an action not in the allowlist.
- :class:`MissingWorkerEnvError` — a referenced worker's URL env var is
  unset. Drift workers must be set for the coordinator to function;
  upgrade workers are *optional at module-import time* but must be set
  before `load_workload("upgrade")` is called (17.E wires these in).

Why lazy resolution (not module-load resolution):
- The Phase 11.7 worker_client deliberately reads worker URLs lazily
  to avoid pytest order-dependence (a test that monkeypatches
  ``READER_URL`` after the module imported gets silently ignored).
  We follow the same pattern here. "Module-load time" in the plan
  means *once per process*, not *literally at Python import*. The
  cache below pins the once-per-process property.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Final

import yaml
from pydantic import BaseModel, ConfigDict

from agent.adk_tools import (
    load_contract_tool,
    notify_tool,
    patch_docs_tool,
    propose_rollback_tool,
    read_live_env_tool,
    search_recent_prs_tool,
)
from agent.workloads.spec import WorkloadSpec


# --------------------------------------------------------------------------- #
# Custom exceptions
# --------------------------------------------------------------------------- #


class UnknownWorkloadError(KeyError):
    """Raised when ``load_workload(name)`` is called with a name that has
    no manifest under ``workloads/<name>/workload.yaml`` (or that's not
    in the allowed :class:`WorkloadSpec.name` literal).

    Subclasses ``KeyError`` so callers using ``dict``-shaped lookups in
    surrounding code can catch it with the same idiom.
    """


class UnknownToolError(KeyError):
    """Raised when a workload YAML names a tool that's not in
    :data:`TOOL_REGISTRY`, OR that's reserved but not yet implemented
    (the entry exists with value ``None``). Both cases must fail loudly
    at load time — the second case especially, because letting the
    coordinator boot with a ``None`` callable in its tool set would
    crash at first LLM call rather than at deploy time."""


class UnknownWorkerError(KeyError):
    """Raised when a workload YAML names a worker that's not in
    :data:`WORKER_REGISTRY`. Worker URLs and audiences are the agent's
    actual mutation surface; an unknown name here is the most
    consequential authority leak the YAML could attempt."""


class UnknownActionError(KeyError):
    """Raised when a workload YAML names an action not in
    :data:`ACTION_REGISTRY`. Actions name decision outcomes
    (``docs_pr``, ``rollback``, ``escalation``…) and gate the
    validator's accept-set for this workload."""


class MissingWorkerEnvError(RuntimeError):
    """Raised when a workload references a worker whose URL env var
    is unset. Drift workers are required at coordinator boot; upgrade
    workers are optional until 17.E wires them in.

    Carries the env var name in the message so deployment failures
    are self-diagnosing in Cloud Run logs."""


# --------------------------------------------------------------------------- #
# Data shapes — symbolic names resolved against these
# --------------------------------------------------------------------------- #


class WorkerEndpoint(BaseModel):
    """A worker's authority record: URL, service-account identity, and
    audience the coordinator must mint ID tokens against.

    ``audience`` is the ID-token ``aud`` claim — for Cloud Run this
    equals the worker's root URL (no trailing slash, no endpoint path).
    Worker_client.py already enforces this invariant on outbound calls;
    we surface it here so the workload manifest's resolution makes the
    audience explicit and inspectable, which matters for audit and for
    the 17.A.3 coordinator wiring.

    ``sa_email`` is the worker's runtime service account — informational
    here (audit, logging) since the actual IAM check happens server-side
    on the worker when it validates the inbound ID token.
    """

    model_config = ConfigDict(frozen=True)

    url: str
    sa_email: str
    audience: str


@dataclass(frozen=True)
class ActionSpec:
    """A decision-action symbol with operator-facing metadata.

    Minimal-by-design: just enough to populate operator UI labels and
    to flag which actions need HITL approval before execution.

    - ``name``: symbolic id used in YAML (matches
      :class:`agent.models.DecisionAction` values for drift).
    - ``display_name``: human-readable label.
    - ``requires_approval``: ``True`` for actions that *must* gate
      through the Firestore approval flow (currently only ``rollback``).
      The classifier and validator both consult this to enforce HITL.
    """

    name: str
    display_name: str
    requires_approval: bool = False


@dataclass(frozen=True)
class WorkloadResolution:
    """The fully resolved result of `load_workload(name)`.

    Holds:

    - the parsed :class:`WorkloadSpec`,
    - a ``tools`` dict mapping symbolic tool name → real callable,
    - a ``workers`` dict mapping symbolic worker name → :class:`WorkerEndpoint`,
    - an ``actions`` dict mapping symbolic action name → :class:`ActionSpec`,
    - ``system_prompt`` — the loaded prompt text,
    - ``contract_path`` — absolute path to the contract YAML, if any,
    - ``workload_dir`` — absolute path to ``workloads/<name>/``.
    """

    spec: WorkloadSpec
    tools: dict[str, Callable]
    workers: dict[str, WorkerEndpoint]
    actions: dict[str, ActionSpec]
    system_prompt: str
    contract_path: Path | None
    workload_dir: Path


# --------------------------------------------------------------------------- #
# TOOL_REGISTRY — the allowlist of LLM-callable tools
# --------------------------------------------------------------------------- #
#
# Drift tools map to the existing Phase 11.7 callables. Tool functions
# weren't renamed to the workload-prefixed symbolic names because the
# rename would touch :mod:`agent.adk_tools` and :mod:`agent.adk_agent`
# (out of scope for this task per the Phase 17.A.1 boundary). The
# symbolic prefix is the authority layer; the underlying callable name
# is incidental.
#
# Tools deferred to future sub-phases are listed as ``None``. This is
# intentional — keeping the slot reserved by name lets the registry
# distinguish "I don't know that tool at all" (`UnknownToolError`,
# probably a YAML typo or an attempt at capability widening) from "I
# know that tool but it's not implemented yet" (`UnknownToolError` with
# a clearer message pointing at the future sub-phase). The two outcomes
# converge at the same exception class because both must fail load.
#
# Sub-phase mapping for the placeholders:
# - upgrade_read_dependencies, upgrade_propose_pr → 17.C
# - search_developer_docs, retrieve_developer_doc → 17.B (MCP attach)
# - get_session_state, set_session_state → 17.B (coordinator memory)

TOOL_REGISTRY: Final[dict[str, Callable | None]] = {
    # Drift workload — Phase 11.7 callables, wired today.
    "drift_read_live_env":     read_live_env_tool,
    "drift_patch_docs":        patch_docs_tool,
    "drift_propose_rollback":  propose_rollback_tool,
    "notify":                  notify_tool,
    "load_contract":           load_contract_tool,
    "search_recent_prs":       search_recent_prs_tool,
    # Upgrade workload — reserved, implemented in 17.C.
    "upgrade_read_dependencies": None,
    "upgrade_propose_pr":        None,
    # Developer Knowledge MCP — reserved, implemented in 17.B.
    "search_developer_docs":     None,
    "retrieve_developer_doc":    None,
    # Coordinator session memory — reserved, implemented in 17.B.
    "get_session_state":         None,
    "set_session_state":         None,
}


# --------------------------------------------------------------------------- #
# WORKER_REGISTRY — the allowlist of callable workers
# --------------------------------------------------------------------------- #
#
# Each entry maps a symbolic worker name to the env-var name carrying
# its URL plus the service-account identity it runs as. We don't build
# the :class:`WorkerEndpoint` here at module load — env vars are read
# lazily inside `_resolve_worker` so test monkeypatching and Cloud Run
# late-binding (the cloudbuild.yaml two-step OWN_URL pattern) both work.
#
# Drift worker URL env names match the Phase 11.7
# :data:`agent.worker_client._WORKER_URL_ENV` table — we're not
# introducing new env vars, just a second registry that references the
# same source of truth. Upgrade worker env vars are introduced here
# (UPGRADE_READER_URL, UPGRADE_DOCS_URL); they'll be exported by the
# 17.E deploy infra.


@dataclass(frozen=True)
class _WorkerSpec:
    """Internal record: how to materialize a `WorkerEndpoint` from env."""
    url_env: str
    sa_email: str


_WORKER_SPECS: Final[dict[str, _WorkerSpec]] = {
    # Drift workers — must be set for the coordinator to function.
    "drift_reader":   _WorkerSpec(url_env="READER_URL",
                                  sa_email="reader-worker-sa"),
    "drift_docs":     _WorkerSpec(url_env="DOCS_URL",
                                  sa_email="docs-worker-sa"),
    "drift_rollback": _WorkerSpec(url_env="ROLLBACK_URL",
                                  sa_email="rollback-worker-sa"),
    # Shared across workloads.
    "notifier":       _WorkerSpec(url_env="NOTIFIER_URL",
                                  sa_email="notifier-worker-sa"),
    # Upgrade workers — optional at module import. Required at
    # `load_workload("upgrade")` time (17.E wires the env vars).
    "upgrade_reader": _WorkerSpec(url_env="UPGRADE_READER_URL",
                                  sa_email="upgrade-reader-worker-sa"),
    "upgrade_docs":   _WorkerSpec(url_env="UPGRADE_DOCS_URL",
                                  sa_email="upgrade-docs-worker-sa"),
}


# Exposed for tests / external introspection. We surface the *spec*
# table here rather than a frozen dict of materialized
# :class:`WorkerEndpoint`s because the actual values need env vars that
# may not be set at import time.
WORKER_REGISTRY: Final[dict[str, _WorkerSpec]] = _WORKER_SPECS


# --------------------------------------------------------------------------- #
# ACTION_REGISTRY — the allowlist of decision actions
# --------------------------------------------------------------------------- #
#
# Names mirror :class:`agent.models.DecisionAction` for drift. New
# actions added by future workloads (e.g. upgrade's ``upgrade_pr``)
# get added here.

ACTION_REGISTRY: Final[dict[str, ActionSpec]] = {
    "docs_pr":      ActionSpec("docs_pr",      "Docs PR",            requires_approval=False),
    "drift_issue":  ActionSpec("drift_issue",  "Drift issue",        requires_approval=False),
    "escalation":   ActionSpec("escalation",   "Escalate to human",  requires_approval=False),
    "no_op":        ActionSpec("no_op",        "No action needed",   requires_approval=False),
    "rollback":     ActionSpec("rollback",     "Rollback (HITL)",    requires_approval=True),
    # Upgrade workload — reserved, used from 17.C onward.
    "upgrade_pr":   ActionSpec("upgrade_pr",   "Dependency upgrade PR",
                               requires_approval=False),
}


# --------------------------------------------------------------------------- #
# Loader — `load_workload(name)`
# --------------------------------------------------------------------------- #


# Module-level cache. Read-only after first population per workload name.
# Tests clear this via the fixture in test_workload_registry.py.
_WORKLOAD_CACHE: dict[str, WorkloadResolution] = {}


def _repo_root() -> Path:
    """The directory containing the top-level ``workloads/`` tree.

    Computed from this file's location (``agent/workloads/registry.py``
    → two parents up → repo root). Kept as a function (not a module
    constant) so tests that monkeypatch don't trip on a stale value
    after a directory move.
    """
    return Path(__file__).resolve().parents[2]


def _resolve_tool(name: str) -> Callable:
    """Resolve a symbolic tool name to a callable. Raises clearly on
    unknown names and on reserved-but-not-yet-implemented names.

    The two error messages diverge intentionally — "unknown" vs "not yet
    implemented" — because the operator response differs. Unknown is
    probably a YAML typo or an attempted capability widening; not-yet
    is a sub-phase ordering issue.
    """
    if name not in TOOL_REGISTRY:
        raise UnknownToolError(
            f"tool {name!r} is not in TOOL_REGISTRY — "
            f"workload YAML may only reference allowlisted tool names. "
            f"Known: {sorted(TOOL_REGISTRY)}"
        )
    callable_obj = TOOL_REGISTRY[name]
    if callable_obj is None:
        raise UnknownToolError(
            f"tool {name!r} is reserved but not yet implemented "
            f"(see Phase 17 plan: search_developer_docs/retrieve_developer_doc "
            f"land in 17.B; upgrade_* in 17.C; get/set_session_state in 17.B)"
        )
    return callable_obj


def _resolve_worker(name: str) -> WorkerEndpoint:
    """Resolve a symbolic worker name to a `WorkerEndpoint`. Reads the
    URL from the worker spec's env var and raises if unset."""
    spec = _WORKER_SPECS.get(name)
    if spec is None:
        raise UnknownWorkerError(
            f"worker {name!r} is not in WORKER_REGISTRY — "
            f"workload YAML may only reference allowlisted worker names. "
            f"Known: {sorted(_WORKER_SPECS)}"
        )
    url = os.environ.get(spec.url_env, "").rstrip("/")
    if not url:
        raise MissingWorkerEnvError(
            f"worker {name!r} URL not configured: "
            f"env var {spec.url_env} is unset or empty. "
            f"For drift this should be set by the coordinator's deploy "
            f"step; for upgrade this will be set by Phase 17.E."
        )
    return WorkerEndpoint(url=url, sa_email=spec.sa_email, audience=url)


def _resolve_action(name: str) -> ActionSpec:
    if name not in ACTION_REGISTRY:
        raise UnknownActionError(
            f"action {name!r} is not in ACTION_REGISTRY. "
            f"Known: {sorted(ACTION_REGISTRY)}"
        )
    return ACTION_REGISTRY[name]


def _load_from_path(yaml_path: Path) -> WorkloadResolution:
    """Load a workload from an explicit YAML path. Used by tests for
    targeted error-injection without touching the on-disk
    ``workloads/`` tree; also the implementation backend for the
    public :func:`load_workload`.

    Resolves every symbolic name and reads the system prompt + contract
    path. Raises on the first failure (no partial state)."""
    raw = yaml.safe_load(yaml_path.read_text())
    spec = WorkloadSpec.model_validate(raw)

    workload_dir = yaml_path.parent

    # System prompt is required.
    prompt_path = workload_dir / spec.system_prompt_file
    if not prompt_path.exists():
        raise FileNotFoundError(
            f"system prompt for workload {spec.name!r} not found: {prompt_path}"
        )
    system_prompt = prompt_path.read_text()

    # Contract path is resolved relative to the workload dir, but only
    # the path is checked — actual contract parsing stays in
    # :func:`agent.contract.load_contract` where the existing tests
    # cover it.
    contract_path: Path | None = None
    if spec.contract_file is not None:
        contract_path = (workload_dir / spec.contract_file).resolve()

    tools = {n: _resolve_tool(n) for n in spec.enabled_tool_names}
    workers = {n: _resolve_worker(n) for n in spec.worker_names}
    actions = {n: _resolve_action(n) for n in spec.action_names}

    return WorkloadResolution(
        spec=spec,
        tools=tools,
        workers=workers,
        actions=actions,
        system_prompt=system_prompt,
        contract_path=contract_path,
        workload_dir=workload_dir.resolve(),
    )


def load_workload(name: str) -> WorkloadResolution:
    """Load and resolve the workload named ``name``. Cached per process.

    The cache makes repeated calls free (read-only after first load).
    Tests that need to monkeypatch env between calls must clear
    ``_WORKLOAD_CACHE`` directly — the cache key is just the workload
    name, so two tests with different env states would otherwise share
    a stale cache entry.

    Raises:
        UnknownWorkloadError: no manifest under
            ``workloads/<name>/workload.yaml``.
        UnknownToolError / UnknownWorkerError / UnknownActionError:
            symbolic name not in the allowlist (or reserved/None for
            tools).
        MissingWorkerEnvError: a referenced worker's URL env var unset.
    """
    if name in _WORKLOAD_CACHE:
        return _WORKLOAD_CACHE[name]

    yaml_path = _repo_root() / "workloads" / name / "workload.yaml"
    if not yaml_path.exists():
        raise UnknownWorkloadError(
            f"no workload manifest for {name!r}: expected {yaml_path}"
        )
    resolution = _load_from_path(yaml_path)
    _WORKLOAD_CACHE[name] = resolution
    return resolution
