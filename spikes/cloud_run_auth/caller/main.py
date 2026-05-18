"""Spike caller — mints an audience-bound Google ID token and calls the callee.

This is throwaway code for Task 11.0 of the v3.1 plan: proving that two
Cloud Run services can authenticate to each other via Google-signed ID tokens
whose audience is the callee's root URL.

The metadata-server fetch (`fetch_id_token`) caches tokens internally — we
don't add our own layer.
"""

import os

import httpx
from fastapi import FastAPI, HTTPException
from google.auth.transport import requests as gar
from google.oauth2 import id_token

CALLEE_URL = os.environ.get("CALLEE_URL", "").rstrip("/")

app = FastAPI(title="spike-caller")


@app.get("/")
def root():
    return {"service": "caller", "ok": True, "callee_url": CALLEE_URL or "<unset>"}


@app.post("/trigger")
def trigger():
    """Mint an ID token for CALLEE_URL and forward a POST to /work.

    Audience binding: `fetch_id_token` returns a token whose `aud` claim is
    set to the URL we pass in. The callee will reject any token whose aud
    doesn't match its own root URL, which is what makes this mechanism
    secure against token replay across services.
    """
    if not CALLEE_URL:
        raise HTTPException(status_code=500, detail="CALLEE_URL env not set")

    # `gar.Request()` is a transport wrapper for the auth lib — it has nothing
    # to do with the FastAPI request. Reusing it across calls is safe but we
    # construct fresh each call to keep this readable.
    try:
        token = id_token.fetch_id_token(gar.Request(), CALLEE_URL)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"fetch_id_token failed: {e}")

    headers = {"Authorization": f"Bearer {token}"}
    payload = {"hello": "from caller"}
    try:
        # `follow_redirects=False` on purpose — if Cloud Run redirects we want
        # to see it explicitly, not silently strip the Authorization header on
        # a cross-host hop.
        resp = httpx.post(
            f"{CALLEE_URL}/work",
            json=payload,
            headers=headers,
            timeout=10.0,
            follow_redirects=False,
        )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"callee call failed: {e}")

    # Surface the callee's response verbatim so curl-driven smoke tests can
    # observe status + body in one shot.
    try:
        body = resp.json()
    except ValueError:
        body = {"raw_text": resp.text}
    return {"callee_status": resp.status_code, "callee_response": body}


@app.post("/trigger-wrong-audience")
def trigger_wrong_audience():
    """Spike-only — mint a token with the WRONG audience and forward it.

    Used to prove that the callee (or Cloud Run's edge) rejects tokens whose
    aud claim doesn't match the callee URL. Audience is the caller's OWN URL
    here, which is something the metadata server happily produces — but it
    won't be valid for the callee.
    """
    if not CALLEE_URL:
        raise HTTPException(status_code=500, detail="CALLEE_URL env not set")
    wrong_aud = os.environ.get("WRONG_AUDIENCE", "https://example.com")
    try:
        token = id_token.fetch_id_token(gar.Request(), wrong_aud)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"fetch_id_token failed: {e}")
    try:
        resp = httpx.post(
            f"{CALLEE_URL}/work",
            json={"hello": "wrong-audience probe"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
            follow_redirects=False,
        )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"callee call failed: {e}")
    try:
        body = resp.json()
    except ValueError:
        body = {"raw_text": resp.text}
    return {
        "minted_audience": wrong_aud,
        "callee_status": resp.status_code,
        "callee_response": body,
    }
