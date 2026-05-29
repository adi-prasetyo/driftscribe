from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # extra="ignore" so .env files can carry unrelated keys (notes, sibling-tool
    # config, stale entries from earlier phases) without crashing Settings on
    # boot. Default in pydantic-settings 2.x is "forbid", which is unfriendly
    # for shared .env files. We pay the silent-typo cost here intentionally.
    # Phase 14.5: the coordinator no longer reads any LLM API key — Vertex AI
    # auth flows through ADC (the Cloud Run service account in prod, or
    # `gcloud auth application-default login` locally), driven by the
    # GOOGLE_GENAI_USE_VERTEXAI / GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION
    # env vars that google-genai picks up automatically. These are
    # SDK-consumed, NOT Settings-consumed — deliberately not declared as
    # fields below (do not "fix" their absence; the SDK reads `os.environ`).
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    dry_run: bool = True
    gcp_project: str = ""
    target_region: str = "asia-northeast1"
    target_service: str = "payment-demo"
    contract_path: str = "demo/ops-contract.yaml"
    # Filesystem root for resolving contract docs.file paths. In dev this is "."
    # (the repo working copy); in the deployed container Phase 8 bakes the docs
    # into /contract and sets DOCS_ROOT=/contract so the agent finds them.
    docs_root: str = "."
    github_repo: str = ""
    github_token: str = ""
    debug_config_url: str = ""
    use_adk: bool = False
    # Operator-facing shared token guarding /recheck (and future /chat in
    # Phase 11.7). Cloud Run injects this from Secret Manager secret
    # ``coordinator-shared-token`` via cloudbuild.yaml's --set-secrets (Cloud
    # Run validates the secret reference at deploy time and refuses the
    # revision if the secret is missing). The empty default here only matters
    # at runtime: if --set-secrets is removed, the guard fail-closes with 503
    # rather than silently accepting anything — see agent/auth.py.
    driftscribe_token: str = ""
    # Coordinator's own Cloud Run URL (e.g. ``https://driftscribe-agent-xyz.a.run.app``).
    # The ``/eventarc`` handler verifies Eventarc-minted ID tokens against this
    # audience via ``google.oauth2.id_token.verify_oauth2_token``. Set by the
    # deploy step in Phase 14.3 after the service URL is known; empty default
    # so test/dev boot doesn't crash. At request time, an empty value forces
    # the handler to 503 (fail-closed) — see agent/main.py /eventarc.
    eventarc_audience: str = ""
    # Cloudflare Access integration: when both values are non-empty, the
    # ``verify_token`` dependency will accept a valid ``Cf-Access-Jwt-Assertion``
    # header as proof of authentication in lieu of ``X-DriftScribe-Token``.
    # See driftscribe_lib/cf_access.py for the verification details and trust boundary.
    # Empty defaults so local dev + unit tests behave exactly like the
    # pre-CF-Access build (CF Access path is disabled).
    cf_access_team_domain: str = ""
    cf_access_aud_tag: str = ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached so /recheck doesn't re-parse .env on every request.

    Tests that need to vary settings should call ``get_settings.cache_clear()``
    after mutating env (a conftest fixture handles this for the integration
    test suite).
    """
    return Settings()
