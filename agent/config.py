from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    dry_run: bool = True
    gcp_project: str = ""
    target_region: str = "asia-northeast1"
    target_service: str = "payment-demo"
    contract_path: str = "demo/ops-contract.yaml"
    github_repo: str = ""
    github_token: str = ""
    debug_config_url: str = ""
    google_api_key: str = ""
    use_adk: bool = False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached so /recheck doesn't re-parse .env on every request.

    Tests that need to vary settings should call ``get_settings.cache_clear()``
    after mutating env (a conftest fixture handles this for the integration
    test suite).
    """
    return Settings()
