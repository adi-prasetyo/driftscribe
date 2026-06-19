import math
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Default TTL for the GET /infra/graph inventory cache. Named so the field
# default and the non-finite fallback can't drift apart.
_DEFAULT_INFRA_GRAPH_CACHE_TTL_S = 60.0
# Default TTL for the L2 (Firestore) layer of the /infra/graph cache. Longer than
# L1 because L2's job is to survive a scale-to-zero instance recycle: a freshly
# spun coordinator with an empty in-process L1 reads the Firestore doc and serves
# a warm map for this window instead of paying the ~25-35s live CAI enumeration.
_DEFAULT_INFRA_GRAPH_L2_CACHE_TTL_S = 900.0

# Default TTL for the IaC PR-source cache (the "view source" affordance on the
# /iac-approvals page). The real freshness key is the PR head_sha (a new commit
# changes it → miss → refetch), so this is just a backstop that lets a long-idle
# doc be re-pulled eventually; default one day. <= 0 disables the cache (every
# load fetches the source live from GitHub).
_DEFAULT_IAC_PR_SOURCE_CACHE_TTL_S = 86400.0


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

    # Phase C5e: coordinator-side read of the C2 plan-builder artifact + the
    # propose-on-approve POST. All default to safe empties so dev/test boot is
    # unchanged; the downstream routes fail-closed when a value is empty.
    #
    # The GCS bucket holding the C2 plan artifacts. Empty default ⇒ derive
    # ``{gcp_project}-tofu-artifacts`` at use site (see ``artifacts_bucket``),
    # matching the worker's ARTIFACT_BUCKET convention without embedding the
    # project name in two places.
    tofu_artifacts_bucket: str = ""
    # Comma-separated CI check names that MUST be green on the PR head before the
    # C5e merge step will merge. EMPTY ⇒ merge is disabled downstream (the merge
    # helper refuses rather than merging an unchecked head).
    iac_required_checks: str = ""
    # GitHub merge method for the C5e merge step (``squash`` | ``merge`` |
    # ``rebase``). Squash by default to keep the IaC history linear.
    iac_merge_method: str = "squash"
    # Exact Origin allowlist (scheme+host+port) for the C5e approval POST. CSRF
    # defense: CF Access does NOT stop a cross-site POST, so the POST handler
    # compares the request Origin to this value exactly. EMPTY ⇒ the POST refuses
    # with 403 (fail-closed — an unconfigured origin must never accept a POST).
    coordinator_origin: str = ""

    # In-process TTL (seconds) for the GET /infra/graph inventory cache. One live
    # Cloud Asset Inventory enumeration takes ~25-35s for a real estate, and the
    # Infrastructure panel re-fetches on every page load, so without a cache every
    # load spins for ~half a minute (measured live). Successful inventories are
    # cached for this window so reloads return instantly; <= 0 disables the cache
    # entirely (every request fetches live). See agent/main.py get_infra_graph.
    infra_graph_cache_ttl_s: float = _DEFAULT_INFRA_GRAPH_CACHE_TTL_S

    # In-process L1 (above) is per-instance and dies with the instance. L2 is a
    # Firestore-backed layer that survives scale-to-zero cold starts (see
    # _DEFAULT_INFRA_GRAPH_L2_CACHE_TTL_S). <= 0 disables L2 (every L1 miss falls
    # straight through to a live fetch). See agent/main.py get_infra_graph and
    # agent/infra_graph_cache_store.py.
    infra_graph_l2_cache_ttl_s: float = _DEFAULT_INFRA_GRAPH_L2_CACHE_TTL_S

    # TTL (seconds) for the IaC PR-source cache backing the approval page's "view
    # source" affordance. See _DEFAULT_IAC_PR_SOURCE_CACHE_TTL_S and
    # agent/iac_pr_source_cache.py. <= 0 disables the cache (every load fetches
    # the .tf source live from the GitHub API).
    iac_pr_source_cache_ttl_s: float = _DEFAULT_IAC_PR_SOURCE_CACHE_TTL_S

    # OIDC audience the POST /internal/infra-graph/refresh pre-warm endpoint
    # expects (the full endpoint URL Cloud Scheduler stamps as
    # --oidc-token-audience). EMPTY ⇒ the endpoint 503s (fail-closed, dormant
    # until pre-warm is provisioned — see infra/scripts/setup_secrets.sh
    # SETUP_INFRA_PREWARM). Set post-deploy like EVENTARC_AUDIENCE, never baked
    # empty into the deploy baseline (a full deploy would clobber an activated value).
    infra_prewarm_audience: str = ""

    @field_validator("infra_graph_cache_ttl_s")
    @classmethod
    def _finite_infra_graph_cache_ttl(cls, v: float) -> float:
        # nan/inf are valid floats but poison the monotonic expiry comparison
        # (`now - written_at > nan` is always False → the cache would never
        # expire, pinning a stale map forever). Fall back to the default rather
        # than trusting a non-finite TTL. A non-numeric env value still raises a
        # ValidationError, consistent with every other typed Settings field.
        return v if math.isfinite(v) else _DEFAULT_INFRA_GRAPH_CACHE_TTL_S

    @field_validator("infra_graph_l2_cache_ttl_s")
    @classmethod
    def _finite_infra_graph_l2_cache_ttl(cls, v: float) -> float:
        # Same non-finite footgun as L1 (`age > nan` is always False → never
        # expires), but for the persisted Firestore layer the stale-forever blast
        # radius is worse (it survives restarts), so coerce nan/inf to the default.
        return v if math.isfinite(v) else _DEFAULT_INFRA_GRAPH_L2_CACHE_TTL_S

    @field_validator("iac_pr_source_cache_ttl_s")
    @classmethod
    def _finite_iac_pr_source_cache_ttl(cls, v: float) -> float:
        # Same non-finite footgun as the infra-graph TTLs (`age > nan` is always
        # False → never expires); coerce nan/inf to the default.
        return v if math.isfinite(v) else _DEFAULT_IAC_PR_SOURCE_CACHE_TTL_S


def artifacts_bucket(settings: "Settings") -> str:
    """Resolve the C2 artifacts bucket name.

    Honours an explicit ``tofu_artifacts_bucket`` override; otherwise derives the
    deploy-convention ``{project}-tofu-artifacts`` from ``gcp_project`` (mirrors the
    ``tofu-apply`` worker's ARTIFACT_BUCKET) so the project name lives in exactly
    one config field.
    """
    return settings.tofu_artifacts_bucket or f"{settings.gcp_project}-tofu-artifacts"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached so /recheck doesn't re-parse .env on every request.

    Tests that need to vary settings should call ``get_settings.cache_clear()``
    after mutating env (a conftest fixture handles this for the integration
    test suite).
    """
    return Settings()
