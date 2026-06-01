"""Worker URL env-var parity between the two registries (Phase D2-3).

Two independent code-side tables map a symbolic worker name to the env
var carrying its URL:

- :data:`agent.worker_client._WORKER_URL_ENV` — read by the runtime path
  that actually mints the ID token and calls the worker.
- :data:`agent.workloads.registry._WORKER_REGISTRY` — read by
  ``load_workload`` to resolve a manifest's ``worker_names`` and fail-load
  loudly if a URL env var is unset.

They are two consumers of the same source of truth (the worker's URL env
var name). If they ever diverge for a shared worker, a workload could
fail-load against one env var name while the runtime call reads another —
a silent misroute. This test pins them equal.

The audit point is the ``tofu_editor`` entry added in Phase D2 (the
provision workload's write surface), but the loop covers EVERY worker
name shared between the two tables so any future drift is caught.
"""
from __future__ import annotations

import agent.workloads.registry as registry
from agent import worker_client


def test_tofu_editor_url_env_parity() -> None:
    """The provision workload's tofu-editor worker must resolve to the
    same URL env var in both registries — and that var must be
    ``TOFU_EDITOR_URL`` (the name Phase D2's infra/deploy pins)."""
    assert (
        worker_client._WORKER_URL_ENV["tofu_editor"]
        == registry._WORKER_REGISTRY["tofu_editor"].url_env
        == "TOFU_EDITOR_URL"
    )


def test_all_shared_worker_url_env_names_agree() -> None:
    """For every worker name present in BOTH tables, the URL env var name
    must agree. Catches any future drift between worker_client and the
    workload registry — not just the Phase D2 ``tofu_editor`` entry.

    (The two tables don't have identical key sets — worker_client uses the
    bare drift names ``reader``/``docs``/``rollback`` while the registry
    uses the ``drift_*`` prefixed forms — so we only compare the shared
    keys. The shared set includes ``infra_reader``, ``notifier``,
    ``upgrade_reader``, ``upgrade_docs``, and ``tofu_editor``.)
    """
    client_env = worker_client._WORKER_URL_ENV
    registry_env = {
        name: spec.url_env for name, spec in registry._WORKER_REGISTRY.items()
    }
    shared = set(client_env) & set(registry_env)
    assert shared, "expected at least one worker name shared between the two tables"
    assert "tofu_editor" in shared, (
        "tofu_editor must be present in both worker tables (Phase D2)"
    )
    for name in sorted(shared):
        assert client_env[name] == registry_env[name], (
            f"worker {name!r} URL env var diverges: "
            f"worker_client has {client_env[name]!r} but the workload "
            f"registry has {registry_env[name]!r}. The two tables are "
            f"consumers of the same source of truth and must agree."
        )
