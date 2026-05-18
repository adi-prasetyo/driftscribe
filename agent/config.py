from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # extra="ignore" so .env files can carry unrelated keys (notes, sibling-tool
    # config, alternate names for the same value like GEMINI_API_KEY vs the
    # canonical GOOGLE_API_KEY) without crashing Settings on boot. Default in
    # pydantic-settings 2.x is "forbid", which is unfriendly for shared .env
    # files. We pay the silent-typo cost here intentionally.
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
    google_api_key: str = ""
    use_adk: bool = False
    # Operator-facing shared token guarding /recheck (and future /chat in
    # Phase 11.7). Cloud Run injects this from Secret Manager secret
    # ``coordinator-shared-token`` via cloudbuild.yaml's --set-secrets (Cloud
    # Run validates the secret reference at deploy time and refuses the
    # revision if the secret is missing). The empty default here only matters
    # at runtime: if --set-secrets is removed, the guard fail-closes with 503
    # rather than silently accepting anything — see agent/auth.py.
    driftscribe_token: str = ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached so /recheck doesn't re-parse .env on every request.

    Tests that need to vary settings should call ``get_settings.cache_clear()``
    after mutating env (a conftest fixture handles this for the integration
    test suite).
    """
    return Settings()
