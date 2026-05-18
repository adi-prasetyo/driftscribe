"""Payment-demo FastAPI app — minimal target for DriftScribe's drift demo.

Beat C of the demo (the "uncertain" path) depends on an intentional asymmetry:
`NEW_THING` is reachable via the Cloud Run Admin API (so DriftScribe's
`cloud_run_client.read_live_env` can observe it set on the service) but it is
NOT exposed by `/debug/config` here. The agent therefore sees a Cloud Run var
that the running app's safe-keys allowlist never acknowledges, which is what
forces the operator to investigate. Do NOT add `NEW_THING` to `SAFE_KEYS`.
"""

import logging
import os
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI

# Allowlist of non-secret runtime config keys that may appear in /debug/config.
# Keep this set narrow and audit additions: anything here is publicly readable
# by anyone who can hit the endpoint.
SAFE_KEYS = {"PAYMENT_MODE", "FEATURE_NEW_CHECKOUT", "FEATURE_BETA_UI"}

# Belt-and-suspenders: inline duplicate of agent.secret_guard.SECRET_NAME_PATTERN.
# The demo app intentionally does NOT depend on the agent package (separate
# Cloud Run service, separate pyproject.toml), so we duplicate the regex here
# instead of importing it. Canonical version lives at agent/secret_guard.py —
# keep this in sync if that regex changes. The startup assertion catches the
# "someone added API_KEY to SAFE_KEYS because it seemed useful" mistake at boot
# rather than waiting for code review.
_SECRET_NAME_PATTERN = re.compile(
    r"(SECRET|TOKEN|KEY|PASSWORD|PASSWD|CRED|PRIVATE|AUTH|BEARER|JWT|SIGNATURE"
    r"|SALT|DSN|OAUTH|URL|URI|CONNECTION|CONNSTR)",
    re.IGNORECASE,
)


def _is_secret_name(name: str) -> bool:
    return bool(_SECRET_NAME_PATTERN.search(name))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Guard: refuse to boot if SAFE_KEYS grew to include a secret-named var.
    offenders = sorted(k for k in SAFE_KEYS if _is_secret_name(k))
    if offenders:
        raise RuntimeError(
            f"SAFE_KEYS contains secret-named entries: {offenders}. "
            "Remove them — /debug/config must never expose secrets."
        )
    cfg = {k: os.environ.get(k, "<unset>") for k in SAFE_KEYS}
    logging.info(
        "Runtime config loaded: %s",
        " ".join(f"{k}={v}" for k, v in cfg.items()),
    )
    yield


app = FastAPI(title="payment-demo", lifespan=lifespan)


@app.get("/")
def root():
    return {"service": "payment-demo", "ok": True}


@app.get("/debug/config")
def debug_config():
    return {
        "service": "payment-demo",
        "config": {k: os.environ.get(k, "<unset>") for k in SAFE_KEYS},
        "revision": os.environ.get("K_REVISION", "local"),
    }
