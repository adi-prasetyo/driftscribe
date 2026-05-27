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

The three registries are exposed as :class:`types.MappingProxyType`
views over private mutable dicts. ``Final`` only blocks rebinding;
``MappingProxyType`` blocks in-place mutation too, which is what we
actually want for a security allowlist.

Failure modes (all raised at *load* time, never at first agent call):

- :class:`UnknownWorkloadError` — `load_workload("kubernetes")` etc.
- :class:`UnknownToolError` — YAML names a tool not in the allowlist
  at all (probably a typo or attempted capability widening). 500-shaped
  at the handler layer: the deploy is broken.
- :class:`ReservedToolNotImplementedError` — YAML names a tool that
  IS in the allowlist but whose callable is still ``None`` (e.g.
  ``upgrade_read_dependencies`` before Phase 17.C ships). Subclasses
  :class:`UnknownToolError` for backward compat. 503-shaped at the
  handler layer: wait for the next phase. Phase 17.A.3 (Codex review)
  introduced the split so a drift YAML typo doesn't surface as the
  same "workload not deployed" message as an upgrade-not-yet error.
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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal, Mapping

import yaml
from pydantic import BaseModel, ConfigDict

from agent.adk_tools import (
    load_contract_tool,
    notify_tool,
    patch_docs_tool,
    propose_rollback_tool,
    read_live_env_tool,
    read_project_inventory_tool,
    search_recent_prs_tool,
    upgrade_close_pr_tool,
    upgrade_merge_pr_tool,
    upgrade_propose_pr_tool,
    upgrade_read_dependencies_tool,
)
from agent.mcp.developer_knowledge import (
    retrieve_developer_doc,
    search_developer_docs,
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


class WorkloadManifestMismatchError(RuntimeError):
    """Raised when the parsed ``WorkloadSpec.name`` in a YAML manifest
    does not match the directory the manifest lives under.

    Phase 17.A (Codex review, Fix Important #2b): the loader treats
    ``workloads/<name>/workload.yaml`` as authoritative for *which*
    workload is being loaded; the YAML's ``name:`` field must agree.
    If an operator typos the YAML (e.g. ``workloads/drift/workload.yaml``
    declares ``name: upgrade``), every other registry lookup would
    silently route against the wrong manifest. We fail loud at load
    time instead — a deploy bug, surfaced before first request.

    Carries both names in the message so the operator can fix the
    typo without grepping source.
    """


class WorkloadPathTraversalError(ValueError):
    """Raised when the ``name`` argument to :func:`load_workload`
    resolves to a path outside the ``workloads/`` root.

    Phase 17.A (Codex review, Fix Important #2c): defense in depth.
    The :class:`WorkloadSpec.name` ``Literal`` already constrains
    callers that go through the typed API, but :func:`load_workload`
    takes a bare ``str``. A future caller that forwards an unvalidated
    request body field would otherwise be vulnerable to
    ``name="../etc/passwd"``-style path escapes. We fail closed here
    rather than relying on ``FileNotFoundError`` (which leaks the
    attempted path).

    Subclasses ``ValueError`` so callers using value-shaped catches
    pick it up with the same idiom.
    """


class UnknownToolError(KeyError):
    """Raised when a workload YAML names a tool that's not in
    :data:`TOOL_REGISTRY` at all. This is a deploy bug — a typo or an
    attempted capability widening that should have been caught at code
    review. The coordinator must fail loudly at load time rather than
    letting the LLM see an undefined symbol.

    Distinct from :class:`ReservedToolNotImplementedError`: that's the
    "we know about this tool, but its callable hasn't shipped yet"
    case. Both fail load, but the operator response differs — a typo
    is 500-shaped (broken deploy), a reserved-not-yet is 503-shaped
    (this build of the system isn't wired for that workload yet).
    Phase 17.A.3 (Codex review): the split lets the coordinator
    surface those two cases distinctly without collapsing every drift
    YAML typo into a misleading "not deployed" message.
    """


class ReservedToolNotImplementedError(UnknownToolError):
    """Raised when a workload YAML names a tool that's reserved in
    :data:`TOOL_REGISTRY` but whose callable is still ``None``. This
    is the "we know about this tool, but its implementation lands in a
    later sub-phase" case — e.g. ``upgrade_read_dependencies`` and
    ``upgrade_propose_pr`` before 17.C, ``get_session_state`` and
    ``set_session_state`` before 17.B's coordinator-memory work.
    (``search_developer_docs`` and ``retrieve_developer_doc`` were in
    this category before 17.B.2; they're real callables now.)

    Subclasses :class:`UnknownToolError` for backward compatibility:
    callers that catch the parent still catch this. The distinct class
    lets handlers (in :mod:`agent.main`) map this to 503 ("workload
    not deployed in this build") while still letting bare
    :class:`UnknownToolError` map to 500 (deploy bug / typo)."""


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


class UnknownUpgradeTargetError(KeyError):
    """Raised when an upgrade contract YAML references a ``target_name``
    that has no entry in :data:`UPGRADE_TARGET_REGISTRY`.

    Phase 17.C.1 (Codex 2026-05-20 blocker): the upgrade workload's
    ``target_repo``, ``lockfile_path``, and ``advisory_source`` are
    authority fields that must live in code, NOT in YAML. The
    ``contract.yaml`` carries only the symbolic ``target_name`` —
    flipping a YAML value can choose from this allowlist but cannot
    redirect the agent at a different repository. An unknown name here
    is exactly the failure mode that pin protects against, surfaced at
    load time so the coordinator never boots a misrouted upgrade
    workload.

    Subclasses ``KeyError`` so callers using dict-shaped lookups catch
    it with the same idiom — matches the existing
    :class:`UnknownToolError` / :class:`UnknownWorkerError` /
    :class:`UnknownActionError` convention.
    """


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
    """A worker's authority record: URL and audience the coordinator
    must mint ID tokens against.

    ``audience`` is the ID-token ``aud`` claim — for Cloud Run this
    equals the worker's root URL (no trailing slash, no endpoint path).
    Worker_client.py already enforces this invariant on outbound calls;
    we surface it here so the workload manifest's resolution makes the
    audience explicit and inspectable, which matters for audit and for
    the 17.A.3 coordinator wiring.
    """

    model_config = ConfigDict(frozen=True)

    url: str
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
class WorkerSpec:
    """Public record for an entry in :data:`WORKER_REGISTRY`.

    Holds the env-var name that carries the worker's URL. Materialized
    into a :class:`WorkerEndpoint` by :func:`_resolve_worker` at load
    time, when the env var is actually read.
    """
    url_env: str


@dataclass(frozen=True)
class WorkloadResolution:
    """The fully resolved result of `load_workload(name)`.

    Holds:

    - the parsed :class:`WorkloadSpec`,
    - a ``tools`` mapping symbolic tool name → real callable,
    - a ``workers`` mapping symbolic worker name → :class:`WorkerEndpoint`,
    - an ``actions`` mapping symbolic action name → :class:`ActionSpec`,
    - ``system_prompt`` — the loaded ``/recheck`` prompt text,
    - ``chat_system_prompt`` — the loaded ``/chat`` prompt text (falls
      back to ``system_prompt`` when ``WorkloadSpec.chat_system_prompt_file``
      is ``None``; see Phase 17.C.4 plan §"Resolve SYSTEM_PROMPT_CHAT
      deferral" for the Option A rationale),
    - ``contract_path`` — absolute path to the contract YAML, if any,
    - ``workload_dir`` — absolute path to ``workloads/<name>/``.

    Phase 17.A (Codex review, Fix Important #2a): the three name→object
    fields are exposed as :class:`types.MappingProxyType` views so a
    caller that grabs a reference cannot widen the workload's authority
    by in-place mutation (``resolution.tools["x"] = ...``). ``frozen=True``
    on the dataclass only blocks reassignment of the field itself; the
    proxy blocks mutation through the field. Same property pin as the
    top-level :data:`TOOL_REGISTRY` / :data:`WORKER_REGISTRY` /
    :data:`ACTION_REGISTRY` allowlists.
    """

    spec: WorkloadSpec
    tools: Mapping[str, Callable]
    workers: Mapping[str, WorkerEndpoint]
    actions: Mapping[str, ActionSpec]
    system_prompt: str
    chat_system_prompt: str
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
# distinguish "I don't know that tool at all" (:class:`UnknownToolError`,
# probably a YAML typo or an attempt at capability widening) from "I
# know that tool but it's not implemented yet"
# (:class:`ReservedToolNotImplementedError`, a subclass of the above
# pointing at the future sub-phase). Both fail load; the handler layer
# in :mod:`agent.main` discriminates between them when picking 500 vs
# 503. Phase 17.A.3 (Codex review) introduced the subclass split so a
# drift YAML typo (500-shaped) doesn't collapse to the same response as
# an upgrade-not-yet error (503-shaped).
#
# Sub-phase mapping for the placeholders (historical — kept for the
# next person walking the registry top-to-bottom):
# - upgrade_read_dependencies, upgrade_propose_pr → wired in 17.C.4
#   (this PR). The callables live in :mod:`agent.adk_tools`; their
#   authority-clean tool surface derives ``target_repo`` /
#   ``lockfile_path`` / ``branch`` / ``base`` / ``title`` server-side
#   from ``UPGRADE_TARGET_REGISTRY`` rather than letting the LLM pick
#   them — see the callables' docstrings for the Codex 2026-05-20
#   follow-up rationale.
# - search_developer_docs, retrieve_developer_doc → wired in 17.B.2
#   (MCP attach).
# - get_session_state, set_session_state → reserved for 17.B's
#   coordinator-memory work; remain ``None`` so the
#   :class:`ReservedToolNotImplementedError` distinction stays meaningful
#   if a future workload YAML enables them.

_TOOL_REGISTRY: Final[dict[str, Callable | None]] = {
    # Drift workload — Phase 11.7 callables, wired today.
    "drift_read_live_env":     read_live_env_tool,
    # Read-only whole-project inventory (infra-IaC initiative). Backed by
    # the infra_reader worker (cloudasset.viewer only) — see the
    # ``read_project_inventory`` worker mapping below and
    # :func:`agent.adk_tools.read_project_inventory_tool`. Strictly
    # read-only: exposed by the explore workload, never a mutation set.
    "read_project_inventory":  read_project_inventory_tool,
    "drift_patch_docs":        patch_docs_tool,
    "drift_propose_rollback":  propose_rollback_tool,
    "notify":                  notify_tool,
    "load_contract":           load_contract_tool,
    "search_recent_prs":       search_recent_prs_tool,
    # Upgrade workload — implemented in 17.C.4. Both callables are
    # authority-clean: their LLM-facing signatures expose only the
    # decision content (package_name / target_version / advisory_url /
    # body) and never the routing fields. See
    # :func:`agent.adk_tools.upgrade_read_dependencies_tool` and
    # :func:`agent.adk_tools.upgrade_propose_pr_tool` for the full
    # rationale.
    "upgrade_read_dependencies": upgrade_read_dependencies_tool,
    "upgrade_propose_pr":        upgrade_propose_pr_tool,
    # Close an upgrade PR this workload opened. Authority-clean: the
    # LLM picks pr_number + reason; the worker re-validates the PR's
    # eligibility (driftscribe label + upgrade/ branch + main base).
    "upgrade_close_pr":          upgrade_close_pr_tool,
    # Merge an upgrade PR this workload opened. Authority-clean: the LLM
    # picks pr_number; the worker re-validates eligibility AND fails
    # closed on CI (required check green on head + no conflict) before a
    # deploy-pinned squash merge.
    "upgrade_merge_pr":          upgrade_merge_pr_tool,
    # Developer Knowledge MCP — wired in 17.B.2. The callables apply
    # 10s wall-clock timeout, 60s response cache, 5-doc/4000-char
    # truncation, and fail-closed translation of MCP timeouts to a
    # structured error result — see ``agent.mcp.developer_knowledge``.
    "search_developer_docs":     search_developer_docs,
    "retrieve_developer_doc":    retrieve_developer_doc,
    # Coordinator session memory — reserved, implemented in 17.B.
    "get_session_state":         None,
    "set_session_state":         None,
}

# Public, read-only view. MappingProxyType blocks in-place mutation
# (`TOOL_REGISTRY["x"] = ...` raises TypeError) so the allowlist
# property survives any caller that grabs a reference.
TOOL_REGISTRY: Final[Mapping[str, Callable | None]] = MappingProxyType(_TOOL_REGISTRY)


# --------------------------------------------------------------------------- #
# WORKER_REGISTRY — the allowlist of callable workers
# --------------------------------------------------------------------------- #
#
# Each entry maps a symbolic worker name to a :class:`WorkerSpec` carrying
# the env-var name for its URL. We don't build the :class:`WorkerEndpoint`
# here at module load — env vars are read lazily inside `_resolve_worker`
# so test monkeypatching and Cloud Run late-binding (the cloudbuild.yaml
# two-step OWN_URL pattern) both work.
#
# Drift worker URL env names match the Phase 11.7
# :data:`agent.worker_client._WORKER_URL_ENV` table — we're not
# introducing new env vars, just a second registry that references the
# same source of truth. Upgrade worker env vars are introduced here
# (UPGRADE_READER_URL, UPGRADE_DOCS_URL); they'll be exported by the
# 17.E deploy infra.

_WORKER_REGISTRY: Final[dict[str, WorkerSpec]] = {
    # Drift workers — must be set for the coordinator to function.
    "drift_reader":   WorkerSpec(url_env="READER_URL"),
    "drift_docs":     WorkerSpec(url_env="DOCS_URL"),
    "drift_rollback": WorkerSpec(url_env="ROLLBACK_URL"),
    # Infra-IaC read-only worker (whole-project inventory). Read-only by
    # construction (cloudasset.viewer + serviceUsageConsumer only); wired
    # into the chat-only ``explore`` workload, never a mutation surface.
    "infra_reader":   WorkerSpec(url_env="INFRA_READER_URL"),
    # Shared across workloads.
    "notifier":       WorkerSpec(url_env="NOTIFIER_URL"),
    # Upgrade workers — optional at module import. Required at
    # `load_workload("upgrade")` time (17.E wires the env vars).
    "upgrade_reader": WorkerSpec(url_env="UPGRADE_READER_URL"),
    "upgrade_docs":   WorkerSpec(url_env="UPGRADE_DOCS_URL"),
}

# Public, read-only view. See note on TOOL_REGISTRY above.
WORKER_REGISTRY: Final[Mapping[str, WorkerSpec]] = MappingProxyType(_WORKER_REGISTRY)


# --------------------------------------------------------------------------- #
# ACTION_REGISTRY — the allowlist of decision actions
# --------------------------------------------------------------------------- #
#
# Names mirror :class:`agent.models.DecisionAction` for drift. New
# actions added by future workloads (e.g. upgrade's ``upgrade_pr``)
# get added here.

_ACTION_REGISTRY: Final[dict[str, ActionSpec]] = {
    "docs_pr":      ActionSpec("docs_pr",      "Docs PR",            requires_approval=False),
    "drift_issue":  ActionSpec("drift_issue",  "Drift issue",        requires_approval=False),
    "escalation":   ActionSpec("escalation",   "Escalate to human",  requires_approval=False),
    "no_op":        ActionSpec("no_op",        "No action needed",   requires_approval=False),
    "rollback":     ActionSpec("rollback",     "Rollback (HITL)",    requires_approval=True),
    # Upgrade workload — reserved, used from 17.C onward.
    "upgrade_pr":   ActionSpec("upgrade_pr",   "Dependency upgrade PR",
                               requires_approval=False),
}

# Public, read-only view. See note on TOOL_REGISTRY above.
ACTION_REGISTRY: Final[Mapping[str, ActionSpec]] = MappingProxyType(_ACTION_REGISTRY)


# --------------------------------------------------------------------------- #
# UPGRADE_TARGET_REGISTRY — the allowlist of upgrade-workload targets
# --------------------------------------------------------------------------- #
#
# Codex 2026-05-20 blocker, Phase 17.C.1: the upgrade workload's
# ``target_repo``, ``lockfile_path``, and ``advisory_source`` are
# authority fields — they decide *which repository the agent reads
# dependencies from and writes upgrade PRs to*. They must NOT live in
# YAML for the same reason worker URLs don't (a YAML flip would
# redirect the agent at a different repo). The workload's
# ``contract.yaml`` references the entry via the symbolic
# ``target_name`` field (Literal-constrained at the pydantic layer);
# the real authority lives in the dict below.
#
# Phase 17 invariant pinned by ``test_upgrade_target_registry``:
# ``UPGRADE_TARGET_REGISTRY["phase17_demo"].target_repo`` must agree
# with ``Settings.github_repo`` when configured together —
# ``search_recent_prs_tool`` reads the latter to detect duplicate
# upgrade PRs, and the upgrade workers target the former. If they
# diverge, the agent would search PRs in the wrong repo. Future
# targets may legitimately diverge (customer-owned demo repos); revisit
# the pin if 17.C grows more entries.
#
# Worker-side defense in depth (deferred to 17.C.2): the upgrade
# workers must NOT import this module — they bundle ``driftscribe_lib/``
# and the worker source only, and ``agent.workloads.registry`` drags
# in coordinator-only deps via ``agent.adk_tools``. The pattern for
# 17.C.2 will be: each worker reads its target_repo from an env var
# pinned at deploy time (``UPGRADE_TARGET_REPO``); the worker
# re-validates request payloads against that env value; and a CI
# guard (separate test) compares the env-pinned worker value against
# ``UPGRADE_TARGET_REGISTRY["phase17_demo"].target_repo`` so the two
# can't silently drift. Codex 2026-05-20 review flagged the module
# placement; the resolution stays here in the coordinator authority
# layer because that's where the rest of the workload registry lives.


@dataclass(frozen=True)
class UpgradeTarget:
    """Public record for an entry in :data:`UPGRADE_TARGET_REGISTRY`.

    Holds the authority fields the upgrade workload uses to pick a
    target repository, lockfile path, and vulnerability advisory feed.
    Frozen so a caller that grabs a reference cannot mutate fields —
    same security pin as the ``MappingProxyType`` on the registry
    itself. Mirrors the ``@dataclass(frozen=True)`` style used for
    :class:`ActionSpec` and :class:`WorkerSpec`.

    Attributes:
        target_repo: GitHub ``<owner>/<repo>`` slug the upgrade workers
            read dependencies from and open PRs against. **Authority
            field** — must agree with the worker's env-pinned
            ``UPGRADE_TARGET_REPO`` (defense in depth: worker
            re-validates at request time per 17.C.2).
        lockfile_path: Repo-relative path to the lockfile the workers
            parse. Phase 17 scope is npm ``package.json`` only — the
            ``upgrade-reader`` worker enforces a regex on this value at
            request time (17.C.2). Storing it here lets the coordinator
            present the file to the LLM without an extra worker round
            trip.
        advisory_source: Which vulnerability advisory feed the workers
            query. ``"github"`` only for Phase 17 v1; ``"osv"`` is
            reserved for post-submission work. The pydantic-layer
            ``Literal`` makes adding a third source require an explicit
            code change.
    """

    target_repo: str
    lockfile_path: str
    advisory_source: Literal["github", "osv"]


# Single source of truth for Phase 17. Add new entries here when
# additional demo targets become real — and revisit the
# ``target_repo == Settings.github_repo`` test pin if the new target
# legitimately diverges from the drift repo (e.g. a customer-owned
# demo).
_UPGRADE_TARGET_REGISTRY: Final[dict[str, UpgradeTarget]] = {
    "phase17_demo": UpgradeTarget(
        # Same repo as drift — Phase 17 demos the upgrade workload
        # against the bundled ``demo/upgrade-target/`` directory.
        target_repo="adi-prasetyo/driftscribe",
        lockfile_path="demo/upgrade-target/package.json",
        advisory_source="github",
    ),
}

# Public, read-only view. See note on TOOL_REGISTRY above for the
# rationale — same security pin: ``MappingProxyType`` blocks the
# in-place mutation that bare ``Final`` would still allow.
UPGRADE_TARGET_REGISTRY: Final[Mapping[str, UpgradeTarget]] = MappingProxyType(
    _UPGRADE_TARGET_REGISTRY
)


def resolve_upgrade_target(name: str) -> UpgradeTarget:
    """Resolve a symbolic upgrade-target name to its
    :class:`UpgradeTarget` record.

    Phase 20: consults ``UPGRADE_TARGET_REPO_OVERRIDE`` at resolution
    time so the agent-side ``target_repo`` agrees with the worker-side
    ``UPGRADE_TARGET_REPO`` env pin when the E2E build redirects both
    to ``driftscribe-e2e-target``. The override never affects
    ``lockfile_path`` / ``advisory_source`` — those still come from
    the registry, on the assumption that the E2E target repo's
    lockfile lives at the same path. On prod, the coordinator's
    cloudbuild env stamps ``UPGRADE_TARGET_REPO_OVERRIDE`` with the
    same value as the registry default (the substitution default
    equals the registry singleton's ``target_repo``); the override
    branch fires but returns a freshly-constructed UpgradeTarget
    whose fields equal the singleton's. Frozen-dataclass equality is
    field-based, so callers comparing with ``==`` see identical
    results — only ``is`` would diverge.

    Raises:
        UnknownUpgradeTargetError: ``name`` is not in
            :data:`UPGRADE_TARGET_REGISTRY`. Mirrors the
            :func:`_resolve_tool` / :func:`_resolve_worker` shape —
            fail loud at load time, never at first agent call.
    """
    if name not in UPGRADE_TARGET_REGISTRY:
        raise UnknownUpgradeTargetError(
            f"upgrade target {name!r} is not in UPGRADE_TARGET_REGISTRY — "
            f"workload contract YAML may only reference allowlisted "
            f"target names. Known: {sorted(UPGRADE_TARGET_REGISTRY)}"
        )
    base = UPGRADE_TARGET_REGISTRY[name]
    override = os.environ.get("UPGRADE_TARGET_REPO_OVERRIDE")
    if override:
        # Parity with the worker-side UPGRADE_TARGET_REPO env pin —
        # required so the agent's tool args match what the worker
        # accepts. See infra/cloudbuild.yaml's coordinator deploy step
        # (UPGRADE_TARGET_REPO_OVERRIDE=$_UPGRADE_TARGET_REPO).
        return UpgradeTarget(
            target_repo=override,
            lockfile_path=base.lockfile_path,
            advisory_source=base.advisory_source,
        )
    return base


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
        # Phase 17.A.3 (Codex review): distinct subclass so handlers can
        # map "reserved but not yet shipped" to 503 while still letting
        # genuine unknown-name typos surface as 500.
        raise ReservedToolNotImplementedError(
            f"tool {name!r} is reserved but not yet implemented "
            f"(see Phase 17 plan: upgrade_* land in 17.C; "
            f"get/set_session_state in 17.B's coordinator-memory work. "
            f"search_developer_docs/retrieve_developer_doc shipped in "
            f"17.B.2 — if this message is firing for those, the deploy "
            f"is broken.)"
        )
    return callable_obj


def _resolve_worker(name: str) -> WorkerEndpoint:
    """Resolve a symbolic worker name to a `WorkerEndpoint`. Reads the
    URL from the worker spec's env var and raises if unset."""
    spec = WORKER_REGISTRY.get(name)
    if spec is None:
        raise UnknownWorkerError(
            f"worker {name!r} is not in WORKER_REGISTRY — "
            f"workload YAML may only reference allowlisted worker names. "
            f"Known: {sorted(WORKER_REGISTRY)}"
        )
    url = os.environ.get(spec.url_env, "").rstrip("/")
    if not url:
        raise MissingWorkerEnvError(
            f"worker {name!r} URL not configured: "
            f"env var {spec.url_env} is unset or empty. "
            f"For drift this should be set by the coordinator's deploy "
            f"step; for upgrade this will be set by Phase 17.E."
        )
    return WorkerEndpoint(url=url, audience=url)


def _resolve_action(name: str) -> ActionSpec:
    if name not in ACTION_REGISTRY:
        raise UnknownActionError(
            f"action {name!r} is not in ACTION_REGISTRY. "
            f"Known: {sorted(ACTION_REGISTRY)}"
        )
    return ACTION_REGISTRY[name]


def _load_from_path(
    yaml_path: Path, *, expected_name: str | None = None
) -> WorkloadResolution:
    """Load a workload from an explicit YAML path. Used by tests for
    targeted error-injection without touching the on-disk
    ``workloads/`` tree; also the implementation backend for the
    public :func:`load_workload`.

    Resolves every symbolic name and reads the system prompt + contract
    path. Raises on the first failure (no partial state).

    If ``expected_name`` is provided, the parsed
    :class:`WorkloadSpec.name` must match it — otherwise
    :class:`WorkloadManifestMismatchError` is raised. The public
    :func:`load_workload` always passes this so a typo in the YAML
    ``name:`` field cannot silently mismatch its on-disk location.
    Phase 17.A Codex review (Fix Important #2b)."""
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    spec = WorkloadSpec.model_validate(raw)

    if expected_name is not None and spec.name != expected_name:
        raise WorkloadManifestMismatchError(
            f"workload manifest at {yaml_path} declares "
            f"name={spec.name!r} but was loaded as {expected_name!r}. "
            f"The directory name and the YAML ``name:`` field must agree — "
            f"this is a deploy bug (typo in the YAML, or the file is in "
            f"the wrong directory)."
        )

    workload_dir = yaml_path.parent

    # System prompt is required.
    prompt_path = workload_dir / spec.system_prompt_file
    if not prompt_path.exists():
        raise FileNotFoundError(
            f"system prompt for workload {spec.name!r} not found: {prompt_path}"
        )
    system_prompt = prompt_path.read_text(encoding="utf-8")

    # Chat system prompt (Phase 17.C.4 — Option A): optional override
    # for the ``/chat`` surface. When the YAML pins
    # ``chat_system_prompt_file:`` the file must exist on disk
    # (RuntimeError otherwise — a deploy bug, same shape as a missing
    # ``system_prompt_file``). When unset, we fall back to
    # ``system_prompt`` so a workload that wants the same prompt on
    # both surfaces doesn't need a duplicate file.
    if spec.chat_system_prompt_file is not None:
        chat_prompt_path = workload_dir / spec.chat_system_prompt_file
        if not chat_prompt_path.exists():
            raise RuntimeError(
                f"chat system prompt for workload {spec.name!r} not found: "
                f"{chat_prompt_path} (declared via chat_system_prompt_file "
                f"in {yaml_path})"
            )
        chat_system_prompt = chat_prompt_path.read_text(encoding="utf-8")
    else:
        chat_system_prompt = system_prompt

    # Contract path is resolved relative to the workload dir, but only
    # the path is checked — actual contract parsing stays in
    # :func:`agent.contract.load_contract` where the existing tests
    # cover it.
    contract_path: Path | None = None
    if spec.contract_file is not None:
        contract_path = (workload_dir / spec.contract_file).resolve()

    # Build the resolution maps then freeze them with MappingProxyType so
    # callers can't widen the workload's authority by in-place mutation.
    # See WorkloadResolution docstring for the security rationale.
    tools = MappingProxyType({n: _resolve_tool(n) for n in spec.enabled_tool_names})
    workers = MappingProxyType({n: _resolve_worker(n) for n in spec.worker_names})
    actions = MappingProxyType({n: _resolve_action(n) for n in spec.action_names})

    return WorkloadResolution(
        spec=spec,
        tools=tools,
        workers=workers,
        actions=actions,
        system_prompt=system_prompt,
        chat_system_prompt=chat_system_prompt,
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

    Phase 17.A (Codex review, Fix Important #2c): the ``name`` arg is
    validated to keep ``workloads/<name>/workload.yaml`` under the
    ``workloads/`` root. Today ``WorkloadSpec.name: Literal["drift",
    "upgrade", "explore"]`` protects callers that go through the typed
    pydantic request models, but :func:`load_workload` itself takes a
    bare ``str`` — defense-in-depth.

    Raises:
        WorkloadPathTraversalError: ``name`` resolves to a path
            outside the workloads root (e.g. ``"../etc/passwd"``).
        UnknownWorkloadError: no manifest under
            ``workloads/<name>/workload.yaml``.
        WorkloadManifestMismatchError: the YAML's ``name:`` field
            doesn't match ``name``.
        UnknownToolError / UnknownWorkerError / UnknownActionError:
            symbolic name not in the allowlist (or reserved/None for
            tools).
        MissingWorkerEnvError: a referenced worker's URL env var unset.
    """
    if name in _WORKLOAD_CACHE:
        return _WORKLOAD_CACHE[name]

    candidate = _workload_yaml_path(name)
    resolution = _load_from_path(candidate, expected_name=name)
    _WORKLOAD_CACHE[name] = resolution
    return resolution


def _workload_yaml_path(name: str) -> Path:
    """Resolve ``workloads/<name>/workload.yaml`` with the path-traversal
    guard, returning the path. Shared by :func:`load_workload` and
    :func:`workload_contract_path` so the traversal/existence checks have
    a single source of truth.

    Raises:
        WorkloadPathTraversalError: ``name`` resolves outside the root.
        UnknownWorkloadError: no manifest at the expected location.
    """
    workloads_root = (_repo_root() / "workloads").resolve()
    candidate = (workloads_root / name / "workload.yaml").resolve()
    # ``is_relative_to`` requires Python 3.9+ — already pinned by
    # pyproject.toml. The check fails closed: any ``name`` that
    # resolves outside the workloads root raises, regardless of
    # whether the target file exists. This denies path-traversal
    # without leaking the attempted path through ``FileNotFoundError``.
    if not candidate.is_relative_to(workloads_root):
        raise WorkloadPathTraversalError(
            f"workload {name!r} resolves outside the workloads root — "
            f"refusing to load."
        )
    if not candidate.exists():
        raise UnknownWorkloadError(
            f"no workload manifest for {name!r}: expected {candidate}"
        )
    return candidate


def workload_contract_path(name: str) -> Path | None:
    """Resolve a workload's contract path WITHOUT resolving its workers.

    Same path-traversal safety and name-match check as
    :func:`load_workload`, but it parses only the manifest and returns
    the resolved ``contract_file`` path (or ``None`` if the workload
    declares no contract). It deliberately skips tool/worker/action
    resolution.

    Why this exists: a read-only consumer must be able to obtain the
    upgrade workload's contract path (to derive the dependency-read
    target repo/lockfile) WITHOUT triggering resolution of the upgrade
    workload's mutation workers (``upgrade_docs``) or the notifier —
    whose URL env vars may be unset in a deploy that only runs the
    chat-only ``explore`` workload. Going through full
    ``load_workload("upgrade")`` would couple a read tool to those write
    workers' env, breaking read-only/partial-deploy isolation
    (Codex review 2026-05-25). See
    :func:`agent.adk_tools._get_upgrade_target`.
    """
    yaml_path = _workload_yaml_path(name)
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    spec = WorkloadSpec.model_validate(raw)
    if spec.name != name:
        raise WorkloadManifestMismatchError(
            f"workload manifest at {yaml_path} declares name={spec.name!r} "
            f"but was requested as {name!r}."
        )
    if spec.contract_file is None:
        return None
    return (yaml_path.parent / spec.contract_file).resolve()
