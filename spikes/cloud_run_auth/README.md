# Spike 11.0 — Cloud Run-to-Cloud Run auth via Google-signed ID tokens

**Status:** Validated 2026-05-19. Mechanism is solid; we'll use it for the
v3.1 multi-agent architecture (coordinator → workers).

Two minimal FastAPI services were deployed:

| Service | URL | SA | Auth flag |
|---|---|---|---|
| `spike-caller` | https://spike-caller-u272wv52kq-an.a.run.app | `spike-caller-sa@driftscribe-hack-2026.iam.gserviceaccount.com` | `--allow-unauthenticated` |
| `spike-callee` | https://spike-callee-u272wv52kq-an.a.run.app | `spike-callee-sa@driftscribe-hack-2026.iam.gserviceaccount.com` | `--no-allow-unauthenticated` + `roles/run.invoker` granted to the caller SA |

## What we proved

**Five behaviors, all as expected for the worker design:**

| # | Setup | Observed status | Where rejected | Body |
|---|---|---|---|---|
| 1 | Caller-on-Cloud-Run mints token with `aud=$CALLEE_URL`, calls `/work` | **200** | n/a (success) | `{"who": "spike-caller-sa@...", "echoed": {"hello": "from caller"}}` |
| 2 | User gcloud identity token (`aud=32555940559.apps.googleusercontent.com`) sent to `/work` | **401** | App code | `{"detail": "missing bearer token"}` — see "Surprise" below |
| 3 | No token sent to `/work` | **403** | Cloud Run edge | Google's static HTML "403 Forbidden" — app never sees the request |
| 4 | Caller mints token with wrong audience (`https://example.com`) and sends to `/work` | **401** | Cloud Run edge | Google's static HTML "401 Unauthorized" — app never sees it |
| 5 | Caller's audience-correct token, but callee's `ALLOWED_CALLERS` env doesn't include the caller's email | **403** | App code | `{"detail": "caller 'spike-caller-sa@...' not in ALLOWED_CALLERS"}` |

Case 1 confirms that `google.oauth2.id_token.fetch_id_token` on Cloud Run
(using ADC + the metadata server) mints an audience-bound token the callee
accepts. Cases 3 & 4 confirm Cloud Run's IAM edge does its job before our
app code runs. Cases 2 & 5 confirm the app-layer checks are reachable.

## Code snippets to copy into worker design

### Caller — mint and send

```python
from google.auth.transport import requests as gar
from google.oauth2 import id_token
import httpx

def call_worker(worker_root_url: str, payload: dict) -> httpx.Response:
    """Audience binding requires the root URL — strip any path component
    BEFORE passing to fetch_id_token. The metadata server caches tokens
    per-audience, so we don't add our own cache layer."""
    token = id_token.fetch_id_token(gar.Request(), worker_root_url)
    return httpx.post(
        f"{worker_root_url}/work",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
```

### Callee — verify

```python
from fastapi import HTTPException, Request
from google.auth.transport import requests as gar
from google.oauth2 import id_token

OWN_URL = os.environ["OWN_URL"].rstrip("/")
ALLOWED_CALLERS = set(os.environ["ALLOWED_CALLERS"].split(","))

def verify_caller(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    token = auth.removeprefix("Bearer ").strip()
    try:
        claims = id_token.verify_oauth2_token(token, gar.Request(), audience=OWN_URL)
    except ValueError as e:
        raise HTTPException(401, f"invalid token: {e}")
    email = claims.get("email")
    if email not in ALLOWED_CALLERS:
        raise HTTPException(403, f"caller {email!r} not allowed")
    return email
```

### Dependencies (don't forget)

`google-auth` does **not** transitively install `requests`, but
`google.auth.transport.requests` (used by both `fetch_id_token` and
`verify_oauth2_token` for cert fetching) requires it. Every worker's
`pyproject.toml` must list:

```toml
"google-auth>=2.35",
"requests>=2.32",
```

## Gotchas observed

1. **`requests` is not a transitive dep of `google-auth`.** First deploy
   crashed with `ModuleNotFoundError: No module named 'requests'` because
   `google.auth.transport.requests` is the default transport and it
   `import requests` at the top. Add `requests` to every worker's deps.

2. **`--allow-unauthenticated` deploy flag is silently ignored when the
   build SA can't `setIamPolicy`.** The default Cloud Build SA for this
   project is `<project-number>-compute@developer.gserviceaccount.com`,
   which has `roles/editor`. Editor includes `run.developer` but NOT
   `run.setIamPolicy`. The `gcloud run deploy --allow-unauthenticated`
   line logs a warning ("Setting IAM policy failed") and exits 0, leaving
   the service up but with NO `allUsers` binding — every request returns
   403 from the Cloud Run edge. Fix: either grant the build SA
   `roles/run.admin`, or set the binding out-of-band as a one-time step.
   For the spike I did the latter (see prerequisites).

3. **The `roles/run.invoker` grant on the callee must be a service-level
   IAM binding, not a project-level one.** Project-level
   `serviceAccount:spike-caller-sa@... roles/run.invoker` would also work
   but grants invoker on *every* Cloud Run service in the project. The
   per-service `gcloud run services add-iam-policy-binding` keeps the
   blast radius small.

4. **Audience MUST be the root URL.** Passing `f"{CALLEE_URL}/work"` to
   `fetch_id_token` would produce a token the callee rejects — the IAM
   gate and `verify_oauth2_token` both match on service root URL, not
   path. The spike's caller code uses `CALLEE_URL = os.environ.get(
   "CALLEE_URL", "").rstrip("/")` to be defensive about trailing slashes.

5. **Surprise — user gcloud token to a Cloud Run service the user owns:
   Cloud Run lets the request through but strips the Authorization
   header.** Test case 2 expected a 403 from Cloud Run's edge (because
   the audience is wrong). Instead, Cloud Run let the request through
   (because the user has `roles/owner` on the project, so any token
   they present satisfies IAM-level invoker check), but the original
   Authorization header — bearing a token with the wrong audience — was
   stripped before the request reached our container. App saw no
   bearer token and returned 401. **Worker-design implication:** the
   in-app verification is a real defense-in-depth check, because the
   "valid IAM grant + wrong audience token" path *can* reach the app
   with no token attached. Don't skip the in-app check just because
   `--no-allow-unauthenticated` is set.

6. **Service URL is assigned on first deploy.** The callee's `OWN_URL`
   env can't be set at first deploy because we don't know the URL yet.
   The spike's `cloudbuild.yaml` deploys once with `OWN_URL=https://placeholder`,
   reads the assigned URL back via `gcloud run services describe`, then
   `gcloud run services update` to set the real value. Two revisions on
   first deploy; subsequent deploys are single-revision because the URL
   is stable.

7. **First call to `fetch_id_token` from a cold Cloud Run instance hits
   the metadata server (~50–100 ms).** Subsequent calls within the same
   instance use a cached token (Google's auth lib does this for us). Not
   a problem for our latency budget; mentioned in case future workers
   are latency-sensitive.

## Prerequisites for re-running this spike

These are one-time setup steps the cloudbuild.yaml does NOT perform
(because the default build SA lacks permissions for them):

```bash
PROJECT=driftscribe-hack-2026

# 1. Service accounts (idempotent)
for sa in spike-caller-sa spike-callee-sa; do
  gcloud iam service-accounts describe ${sa}@${PROJECT}.iam.gserviceaccount.com >/dev/null 2>&1 \
    || gcloud iam service-accounts create ${sa} --display-name="Spike 11.0 ${sa}"
done

# 2. After the FIRST build deploys spike-callee, grant invoker:
gcloud run services add-iam-policy-binding spike-callee \
  --region=asia-northeast1 \
  --member=serviceAccount:spike-caller-sa@${PROJECT}.iam.gserviceaccount.com \
  --role=roles/run.invoker

# 3. After the FIRST build deploys spike-caller, make it publicly callable:
gcloud run services add-iam-policy-binding spike-caller \
  --region=asia-northeast1 \
  --member=allUsers \
  --role=roles/run.invoker

# 4. Build + deploy
gcloud builds submit --config=spikes/cloud_run_auth/cloudbuild.yaml \
  --substitutions=_TAG=$(git rev-parse --short HEAD) .
```

## Validation commands (run after deploy)

```bash
CALLER_URL=$(gcloud run services describe spike-caller --region=asia-northeast1 --format='value(status.url)')
CALLEE_URL=$(gcloud run services describe spike-callee --region=asia-northeast1 --format='value(status.url)')

# Test 1: happy path
curl -sS -X POST "$CALLER_URL/trigger" -w '\nHTTP %{http_code}\n'
#   Expect: HTTP 200, callee_status 200, who = spike-caller-sa@...

# Test 2: user gcloud token (wrong audience, but user has project owner)
curl -sS -X POST "$CALLEE_URL/work" \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  -H 'Content-Type: application/json' -d '{}' -w '\nHTTP %{http_code}\n'
#   Expect: HTTP 401, {"detail":"missing bearer token"} (Cloud Run strips header)

# Test 3: no token
curl -sS -X POST "$CALLEE_URL/work" -d '{}' -w '\nHTTP %{http_code}\n'
#   Expect: HTTP 403 from Cloud Run edge (static HTML)

# Test 4: wrong-audience token (minted by caller for example.com)
curl -sS -X POST "$CALLER_URL/trigger-wrong-audience" -w '\nHTTP %{http_code}\n'
#   Expect: HTTP 200 from caller, but callee_status 401 (Cloud Run edge rejects)

# Test 5: valid audience but caller not in allowlist
gcloud run services update spike-callee --region=asia-northeast1 \
  --update-env-vars=ALLOWED_CALLERS=nobody@example.com
sleep 5
curl -sS -X POST "$CALLER_URL/trigger" -w '\nHTTP %{http_code}\n'
#   Expect: HTTP 200 from caller, callee_status 403, "not in ALLOWED_CALLERS"

# Restore allowlist
gcloud run services update spike-callee --region=asia-northeast1 \
  --update-env-vars=ALLOWED_CALLERS=spike-caller-sa@driftscribe-hack-2026.iam.gserviceaccount.com
```

## Cleanup commands (run after the spike)

```bash
PROJECT=driftscribe-hack-2026
REGION=asia-northeast1

# Delete the Cloud Run services
gcloud run services delete spike-caller --region=$REGION --quiet
gcloud run services delete spike-callee --region=$REGION --quiet

# Delete the container images (optional — costs are minimal)
for img in spike-caller spike-callee; do
  gcloud artifacts docker images delete \
    asia-northeast1-docker.pkg.dev/$PROJECT/driftscribe/$img \
    --quiet --delete-tags || true
done

# Delete the service accounts (optional — they have no resources attached)
for sa in spike-caller-sa spike-callee-sa; do
  gcloud iam service-accounts delete ${sa}@${PROJECT}.iam.gserviceaccount.com --quiet
done
```

## Confidence call

**High confidence** that audience-bound Google ID tokens (`fetch_id_token` +
`verify_oauth2_token`) are the right inter-service auth for the workers.
The mechanism is layered: Cloud Run IAM rejects unauthorized callers AND
wrong-audience tokens at the edge (cases 3 & 4 above), and the in-app
verification adds defense-in-depth for the surprise case (#5 in gotchas)
where Cloud Run pass-through can strip the original Authorization header.

No fallback to shared HMAC headers is needed. Proceed with Task 11.1 as
planned.
