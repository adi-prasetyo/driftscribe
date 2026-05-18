"""Spike callee — verifies a Google ID token and echoes the caller's body.

The IAM layer (`--no-allow-unauthenticated` + `roles/run.invoker` granted to
the caller's SA) rejects unauthenticated traffic at Cloud Run's edge before
this app ever sees the request. The in-app verification here is a defense-
in-depth check that proves audience+email enforcement is happening at the
*application* layer too — so we can run the same code locally or in a
worker context where IAM might be the only gate.
"""

import os

from fastapi import FastAPI, HTTPException, Request
from google.auth.transport import requests as gar
from google.oauth2 import id_token

OWN_URL = os.environ.get("OWN_URL", "").rstrip("/")
ALLOWED_CALLERS = {
    e.strip() for e in os.environ.get("ALLOWED_CALLERS", "").split(",") if e.strip()
}

app = FastAPI(title="spike-callee")


@app.get("/")
def root():
    return {"service": "callee", "ok": True}


def _extract_bearer(request: Request) -> str:
    """Pull `Bearer <token>` out of the Authorization header or raise 401.

    Cloud Run's IAM gate normally strips unauthenticated requests, but we
    re-check here so the verification logic is identical regardless of where
    the request originates.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    return auth.removeprefix("Bearer ").strip()


@app.post("/work")
async def work(request: Request):
    if not OWN_URL:
        raise HTTPException(status_code=500, detail="OWN_URL env not set")

    token = _extract_bearer(request)

    # `verify_oauth2_token` does cert fetch + signature + exp + aud checks in
    # one call. If audience doesn't match, it raises ValueError — that's the
    # "wrong audience" rejection we promised the spike would observe.
    try:
        claims = id_token.verify_oauth2_token(token, gar.Request(), audience=OWN_URL)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=f"invalid token: {e}")

    email = claims.get("email")
    if email not in ALLOWED_CALLERS:
        # 403 (vs 401) signals "you authenticated, but you're not on the list"
        # — distinct from the token-invalid case so logs are unambiguous.
        raise HTTPException(
            status_code=403,
            detail=f"caller {email!r} not in ALLOWED_CALLERS",
        )

    try:
        body = await request.json()
    except Exception:
        body = None

    return {"who": email, "echoed": body}
