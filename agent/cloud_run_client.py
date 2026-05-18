# agent/cloud_run_client.py — re-export shim. Phase 11.2 moved the
# implementation to driftscribe_lib.cloud_run so the workers (Phase 11.3+)
# can share it. Keep the agent.cloud_run_client import path stable for
# existing call sites.
from driftscribe_lib.cloud_run import read_live_env

__all__ = ["read_live_env"]
