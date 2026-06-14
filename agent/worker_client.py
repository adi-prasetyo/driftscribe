"""Coordinator → worker HTTP client (Phase 11.7).

This module is the coordinator's *only* outbound mutation seam. Every ADK
tool that needs to change a system (Cloud Run env, docs PRs, rollback
proposals, notifications) routes through here; the legacy direct-GCP /
direct-GitHub code paths in :mod:`agent.adk_tools` are gone in 11.7.

Three jobs:

1. **Mint an audience-bound Google ID token** via
   :func:`driftscribe_lib.auth.mint_id_token`. The audience MUST be the
   worker's root URL (no trailing slash, no endpoint path) — Cloud Run
   validates the audience claim against the receiving service's URL,
   not the URL the client *called*. Mixing this up silently breaks
   inter-service auth on custom domains.

2. **POST JSON to the worker's canonical endpoint.** Worker endpoints
   are intentionally hardcoded in :data:`WORKER_ENDPOINTS` rather than
   caller-supplied — exposing a "call arbitrary worker endpoint" tool
   to the LLM would be a Layer 0 violation.

3. **Surface errors as a structured :class:`WorkerClientError`.** Status
   code is preserved (4xx vs 5xx vs the synthetic 503 we return for
   transport / config failures), and the body is truncated to bound
   what the chat handler echoes back to the operator.

Worker URLs are read lazily via :func:`_worker_url` rather than at
import time. Codex review of the 11.7 plan flagged that module-level
env reads make pytest order-sensitive (a test that monkeypatches
``READER_URL`` *after* this module has already cached the value gets
silently ignored). Lazy lookup keeps tests predictable while costing
one ``os.environ.get`` per call — cheap.
"""
from __future__ import annotations

import os
import time
from typing import Final

import httpx

from driftscribe_lib.auth import mint_id_token
from driftscribe_lib.logging import current_trace_id_or_new


# Per-worker env var name → fixed at boot for the deployed service via
# cloudbuild.yaml's two-step OWN_URL pattern (deploy with placeholder, then
# gcloud services update with the real URL after every worker is up).
#
# Phase 17.C.4: ``upgrade_reader`` / ``upgrade_docs`` join the table for
# the upgrade workload's coordinator wiring. The two new env vars
# (``UPGRADE_READER_URL`` / ``UPGRADE_DOCS_URL``) match the env var
# names already registered in :data:`agent.workloads.registry._WORKER_REGISTRY`
# — the worker_client and the workload registry are two consumers of
# the same source of truth.
_WORKER_URL_ENV: Final[dict[str, str]] = {
    "reader": "READER_URL",
    "docs": "DOCS_URL",
    "rollback": "ROLLBACK_URL",
    "notifier": "NOTIFIER_URL",
    "upgrade_reader": "UPGRADE_READER_URL",
    "upgrade_docs": "UPGRADE_DOCS_URL",
    "infra_reader": "INFRA_READER_URL",
    # Phase C5a: wire the coordinator to the tofu-apply worker — the sole
    # infra mutator. The canonical endpoint is /propose (see
    # WORKER_ENDPOINTS); /apply and /deny are reached via the named
    # wrappers below, never the default path.
    "tofu_apply": "TOFU_APPLY_URL",
    # Phase D2: wire the coordinator to the tofu-editor worker — the
    # agent-authoring seam that commits validated iac/-only file writes and
    # opens ONE PR. Its canonical (and only) endpoint is /open-pr (see
    # WORKER_ENDPOINTS); the LLM never selects a path.
    "tofu_editor": "TOFU_EDITOR_URL",
}


# Each worker has exactly ONE *canonical* coordinator-facing endpoint.
# A few workers expose extra endpoints reached via named wrappers that
# hardcode the path — :func:`call_execute` / :func:`call_deny` for the
# rollback worker's /execute & /deny, and :func:`call_close_pr` /
# :func:`call_merge_pr` for the upgrade_docs worker's /close & /merge. We
# never let the caller (and especially never let the LLM) pick the
# endpoint path *freely* — the path is fixed inside each wrapper, which
# is what keeps this a Layer 0-safe surface.
#
# Phase 17.C.4: the upgrade workers' canonical endpoints are ``/read``
# (matching :func:`workers.upgrade_reader.main.read`) and ``/patch``
# (matching :func:`workers.upgrade_docs.main.patch`). Keep these
# hardcoded — exposing endpoint-path selection to the LLM would be a
# Layer 0 violation, same as for the drift workers above.
WORKER_ENDPOINTS: Final[dict[str, str]] = {
    "reader": "/read",
    "docs": "/patch",
    "rollback": "/propose",
    "notifier": "/notify",
    "upgrade_reader": "/read",
    "upgrade_docs": "/patch",
    "infra_reader": "/describe",
    # Phase C5a: /propose is the tofu-apply worker's canonical (default)
    # endpoint — the "ask permission" path that creates a pending plan
    # approval. The mutating /apply and the cleanup /deny are reached only
    # via :func:`call_apply` / :func:`call_plan_deny`, which hardcode the
    # path, so the LLM-facing surface can never select them.
    "tofu_apply": "/propose",
    # Phase D2: /open-pr is the tofu-editor worker's sole canonical endpoint —
    # the editor exposes no other path, so the default is the only path. The
    # LLM never selects a path; :func:`call_open_infra_pr` routes here.
    "tofu_editor": "/open-pr",
}


# Bound the body we surface back to the chat caller. Worker responses
# may contain stack traces, internal URLs, or PII during failures —
# truncate to a sane length so a single 502 doesn't echo 50KB of detail
# into the operator's chat reply.
_ERROR_BODY_TRUNCATE: Final[int] = 500


# Outbound HTTP timeout. Cloud Run cold starts on workers can take a
# couple of seconds, and the docs worker's PR creation hits the GitHub
# API, but 30s is plenty headroom — anything past that is almost
# certainly a hang we'd rather fail fast on. Workers that legitimately
# take longer get a per-worker default via ``_WORKER_DEFAULT_TIMEOUTS``
# below; only the "short" workers with no entry in that map keep this value.
_HTTPX_TIMEOUT: Final[float] = 30.0


# Phase C5e: the tofu-apply worker's /apply runs a real ``tofu apply`` that can
# take up to its Cloud Run ``--timeout=900`` (see infra/cloudbuild.tofu-apply.yaml).
# The default 30s read timeout would misread a long-but-successful apply as a
# transport failure — which, after the worker has already burned the approval and
# mutated live infra, is exactly the ambiguous/non-recoverable case the C5e state
# machine must avoid (a coordinator timeout-then-skip-merge while infra actually
# changed = silent divergence). Give /apply a read timeout comfortably above the
# worker's wall clock (+ margin); keep a tight connect/write/pool so we still fail
# fast on a real transport problem before the worker starts working.
_APPLY_HTTPX_TIMEOUT: Final = httpx.Timeout(
    connect=10.0, read=920.0, write=30.0, pool=10.0
)


# Backlog-3 residual (2026-06-12): the infra-reader's /describe pages the
# whole CAI estate (~467 resources today) and legitimately takes ~25-30s
# solo — one live fetch was misread as a transport failure at 30.1s
# against the 30s default. Same misclassification class as C5e/apply, at
# lower stakes: describe is read-only and RECOVERABLE, so we size the read
# budget from observed wall clock (~3x worst, headroom for loaded slots +
# estate growth), NOT from the worker's Cloud Run ceiling the way /apply
# must. Upper bound: the operator-facing /infra/graph rides the
# Cloudflare-proxied custom domain (~100s proxied-response budget — see
# the SSE heartbeat comment in agent/main.py), so the coordinator's
# response must finish under ~100s; 90s read + overhead fits, 120 would
# not. connect stays tight so a down worker still fails fast.
_DESCRIBE_HTTPX_TIMEOUT: Final = httpx.Timeout(
    connect=10.0, read=90.0, write=30.0, pool=10.0
)

# Per-worker DEFAULT timeouts, consulted by ``call`` only when the caller
# passes no explicit ``timeout=``. Keyed like WORKER_ENDPOINTS; any worker
# not listed gets _HTTPX_TIMEOUT. Endpoint-specific overrides (call_apply)
# keep passing explicitly and win over this map by construction.
_WORKER_DEFAULT_TIMEOUTS: Final[dict[str, "float | httpx.Timeout"]] = {
    "infra_reader": _DESCRIBE_HTTPX_TIMEOUT,
}


class WorkerClientError(Exception):
    """Structured error for any worker-side or transport-side failure.

    Carries enough context for the caller (``/chat`` handler, approval
    POST handler) to decide whether to surface the failure to the
    operator and what status code to map it to. We deliberately do NOT
    raise :class:`fastapi.HTTPException` here — the client module is
    framework-agnostic; the handler maps to HTTPException at the
    boundary.

    Attributes:
        status_code: the HTTP status from the worker, or 503 for
            transport / config failures the client manufactured.
        body: the response body (truncated). Empty string when no
            response was received.
        worker: the worker name (``"reader"`` etc.) — useful for logs.
    """

    def __init__(self, status_code: int, body: str, worker: str):
        truncated = (body or "")[:_ERROR_BODY_TRUNCATE]
        super().__init__(f"{worker} returned {status_code}: {truncated}")
        self.status_code = status_code
        self.body = truncated
        self.worker = worker


def _worker_url(worker: str) -> str:
    """Resolve the worker's base URL from env. Raises if unset.

    Lazy lookup (not module-level) so tests can monkeypatch env per-test
    without import-order side effects. The Cloud Run deployment sets
    these via the post-deploy step in cloudbuild.yaml; missing config at
    runtime is a deploy bug, not a per-request error.
    """
    env_name = _WORKER_URL_ENV.get(worker)
    if env_name is None:
        raise WorkerClientError(503, f"unknown worker {worker!r}", worker)
    url = os.environ.get(env_name, "").rstrip("/")
    if not url:
        raise WorkerClientError(
            503,
            f"worker {worker!r} URL not configured ({env_name} unset/empty)",
            worker,
        )
    return url


def probe_worker_health(worker: str, *, timeout: float = 10.0) -> dict:
    """Read-only reachability probe: GET the worker's canonical POST endpoint.

    Phase C5c. This is the per-worker primitive behind the coordinator's
    ``GET /iac-apply/reachability`` diagnostic, which exists to answer ONE
    question after the coordinator is moved onto Direct VPC egress: can it
    actually reach its downstream ``*.run.app`` workers (in particular the
    internal-ingress ``tofu_apply`` mutator), or does the rewritten ``run.app``
    private DNS zone blackhole / get ingress-rejected? The endpoint loops this
    helper over every configured worker; this function probes exactly one.

    **Why GET the canonical POST path and not ``/healthz``.** Cloud Run's GFE
    reserves ``z``-suffixed paths and returns its own ``404`` for ``/healthz``
    *before the request reaches the app* (the same quirk that forced the
    coordinator to expose ``/health`` as its external alias). So ``/healthz``
    can never yield the app's ``200`` over the network, and — fatally for an
    internal-ingress service — a ``404`` is indistinguishable from an ingress
    rejection. We therefore GET the worker's canonical endpoint
    (:data:`WORKER_ENDPOINTS`, a POST-only path): the app answers **405 Method
    Not Allowed**, which is returned ONLY after the request has traversed the
    full network → Cloud Run ingress → IAM (invoker) → app stack. So:

    * ``reachable`` (got ANY HTTP response) — the route + TLS reached a Cloud
      Run GFE (a transport error means a DNS/route blackhole instead).
    * ``app_reached`` (response status NOT in {401, 403, 404}) — the request
      passed the ingress gate AND IAM and hit the app router. For the internal
      ``tofu_apply`` this is the load-bearing proof that VPC routing delivers
      the call AS INTERNAL. ``405`` (GET on a POST route) is the expected hit; a
      ``404`` is a pre-app ingress/GFE reject, and ``401/403`` is an auth/IAM
      reject that a real ``/propose``|``/apply`` call would hit identically — so
      neither is a green cutover signal (Codex C5c review).

    GET on a POST-only route is inert — there is no handler, so no side effect
    (this never POSTs to the mutator). NEVER exposed as an ADK tool; like
    :func:`call_apply`, it is an internal diagnostic reached only by the
    coordinator's server-side reachability route. The path is taken verbatim
    from :data:`WORKER_ENDPOINTS`, so there is no caller endpoint selection.

    Args:
        worker: one of the keys in :data:`_WORKER_URL_ENV`.
        timeout: per-probe httpx timeout (seconds). Defaults to 10s — short
            enough that a blackholed route fails the diagnostic quickly rather
            than hanging the fan-out.

    Returns a flat dict (never raises through) with keys ``worker``, ``target``,
    ``probed_path``, ``reachable``, ``app_reached``, ``status_code``,
    ``latency_ms``, ``error``:

    * URL unset → ``reachable``/``app_reached`` False, ``error="url_unset"``
      (the :class:`WorkerClientError` from :func:`_worker_url` is caught).
    * Got an HTTP response → ``reachable=True``,
      ``app_reached=(status not in {401, 403, 404})``, ``status_code`` set,
      ``error=None``.
    * Token-mint / transport failure → ``reachable``/``app_reached`` False,
      ``error`` carrying the class + short message.
    """
    path = WORKER_ENDPOINTS[worker]
    try:
        base = _worker_url(worker)
    except WorkerClientError:
        # URL unset/empty (or unknown worker) — a diagnostic result, not a
        # failure to propagate. The route's gate treats this as not-reachable.
        return {
            "worker": worker,
            "target": None,
            "probed_path": path,
            "reachable": False,
            "app_reached": False,
            "status_code": None,
            "latency_ms": None,
            "error": "url_unset",
        }

    # Audience is the worker ROOT url (same rule as :func:`call`) — Cloud Run
    # validates the aud claim against the receiving service's URL. Minting hits
    # the metadata server, so it can fail (auth/transport) — catch it too: a
    # diagnostic that 500s and loses every per-worker result defeats its purpose.
    started = time.monotonic()
    try:
        token = mint_id_token(base)
    except Exception as e:  # noqa: BLE001 — diagnostic must never raise through
        return {
            "worker": worker,
            "target": base,
            "probed_path": path,
            "reachable": False,
            "app_reached": False,
            "status_code": None,
            "latency_ms": None,
            "error": f"token_mint_failed: {type(e).__name__}: {e}",
        }
    try:
        r = httpx.get(
            f"{base}{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
    except httpx.HTTPError as e:
        # ConnectError / TimeoutException / any transport-layer failure — we
        # never received an HTTP response, so the route is (or appears) broken.
        # Surface the class + a short message so the operator can tell a DNS
        # blackhole from a connect timeout, without echoing a full stack trace.
        return {
            "worker": worker,
            "target": base,
            "probed_path": path,
            "reachable": False,
            "app_reached": False,
            "status_code": None,
            "latency_ms": None,
            "error": f"{type(e).__name__}: {e}",
        }

    latency_ms = int((time.monotonic() - started) * 1000)
    # reachable=True on any HTTP status — the route + TLS reached a Cloud Run
    # GFE. app_reached requires the request to have reached the app router past
    # BOTH the ingress gate AND IAM: 405 (GET on a POST route) is the expected
    # hit. A 404 is the GFE / ingress pre-app reject; 401/403 is an auth/IAM
    # rejection — and crucially a real /propose|/apply call would hit the SAME
    # rejection, so 401/403 is NOT a green cutover signal (Codex C5c review).
    return {
        "worker": worker,
        "target": base,
        "probed_path": path,
        "reachable": True,
        "app_reached": r.status_code not in (401, 403, 404),
        "status_code": r.status_code,
        "latency_ms": latency_ms,
        "error": None,
    }


def call(
    worker: str,
    payload: dict,
    *,
    endpoint: str | None = None,
    timeout: "float | httpx.Timeout | None" = None,
) -> dict:
    """POST ``payload`` to the named worker. Return parsed JSON response.

    Audience binding: the ID token's ``aud`` claim is the worker's root
    URL (``base``), not the full endpoint URL. Cloud Run validates the
    audience against the receiving service's URL — feeding the endpoint
    URL here would silently work today (Cloud Run strips the path for
    the audience check) but breaks if we ever move to custom domains.

    Args:
        worker: one of ``"reader" | "docs" | "rollback" | "notifier"``.
        payload: JSON-serializable dict matching the worker's request
            schema. The worker's pydantic model enforces
            ``extra="forbid"`` so a typo here surfaces as a 422.
        endpoint: override the default endpoint. Only set by the named
            wrappers below (:func:`call_execute`, :func:`call_deny`,
            :func:`call_close_pr`, :func:`call_merge_pr`), each of which
            hardcodes a fixed path. ADK tools never pass this argument
            directly — they go through a wrapper, so the LLM can't pick
            an arbitrary endpoint.
        timeout: per-call httpx timeout override. ``None`` (the default)
            consults :data:`_WORKER_DEFAULT_TIMEOUTS` for a per-worker
            budget; if the worker has no entry there the fallback is
            :data:`_HTTPX_TIMEOUT` (30s). Long-budget cases: infra_reader
            gets :data:`_DESCRIBE_HTTPX_TIMEOUT` (90s read — CAI estate
            paging); call_apply passes :data:`_APPLY_HTTPX_TIMEOUT`
            explicitly (920s read — ``tofu apply`` can run for minutes). An
            explicit ``timeout=`` argument always wins over the map.

    Raises:
        WorkerClientError: with status_code preserved from the worker
        on non-2xx, or 503 for transport / config failures.
    """
    base = _worker_url(worker)
    path = endpoint or WORKER_ENDPOINTS[worker]
    # Audience is the *root* URL, not base+path — see the docstring.
    token = mint_id_token(base)
    # Phase 15.2: propagate the coordinator's per-request trace id to
    # the worker so a single trace id correlates logs across the call
    # chain. The ContextVar is set by the trace middleware in
    # ``driftscribe_lib.logging``; on the rare path where worker_client
    # is invoked outside a request scope (e.g. a CLI smoke test) the
    # ContextVar is empty — ``current_trace_id_or_new`` mints a fresh
    # one. It also validates the ContextVar value matches our 32-char
    # hex format, so a stray ``set_trace_id("not-a-uuid")`` somewhere
    # in the codebase cannot leak a malformed id downstream.
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Trace-Id": current_trace_id_or_new(),
    }
    try:
        with httpx.Client(
            timeout=timeout if timeout is not None else _WORKER_DEFAULT_TIMEOUTS.get(worker, _HTTPX_TIMEOUT)
        ) as client:
            r = client.post(f"{base}{path}", json=payload, headers=headers)
    except httpx.RequestError as e:
        # Connection refused, DNS failure, timeout — anything that
        # prevented a response. The synthetic 503 disambiguates this
        # from a real worker-returned 5xx in the caller's error path.
        raise WorkerClientError(
            503, f"{worker} unreachable: {type(e).__name__}: {e}", worker
        ) from e

    if not 200 <= r.status_code < 300:
        raise WorkerClientError(r.status_code, r.text, worker)

    # Defensive: workers always return JSON on 2xx, but a misconfigured
    # proxy or future cache layer could insert HTML. Surface a 503-ish
    # error rather than crash the chat handler.
    try:
        return r.json()
    except ValueError as e:
        raise WorkerClientError(
            502, f"{worker} returned non-JSON body: {e}", worker
        ) from e


def call_execute(approval_id: str, approval_token: str) -> dict:
    """Special-case wrapper for the rollback worker's ``/execute`` endpoint.

    Kept as a separate function (rather than letting a tool call
    ``call("rollback", ..., endpoint="/execute")``) so the approve-path
    code in :mod:`agent.main` reads as a single named operation, and
    the LLM-facing tools never get the option to hit /execute directly.
    The LLM only ever calls /propose; the operator's button press is
    what triggers /execute via the coordinator's approval POST handler.
    """
    return call(
        "rollback",
        {"approval_id": approval_id, "approval_token": approval_token},
        endpoint="/execute",
    )


def call_deny(approval_id: str, approval_token: str) -> dict:
    """Special-case wrapper for the rollback worker's ``/deny`` endpoint.

    Phase 11.9 (Codex review of 11.7, critical finding #1): the
    coordinator no longer transactionally flips ``pending → denied``
    itself, because doing so required no token validation and turned the
    reject button into a HITL availability vector. The token verification
    has to live on the worker (the only service with the HMAC key), so
    the deny operation moves there too. Mirrors :func:`call_execute` in
    shape — both decision paths now go through audience-bound ID-token
    auth to the same worker.

    The LLM never calls this directly; the operator's Reject click in
    the coordinator's approval POST handler is what triggers it.
    """
    return call(
        "rollback",
        {"approval_id": approval_id, "approval_token": approval_token},
        endpoint="/deny",
    )


def call_close_pr(target_repo: str, pr_number: int, reason: str) -> dict:
    """Wrapper for the upgrade_docs worker's ``/close`` endpoint.

    Unlike :func:`call_execute` / :func:`call_deny` (operator-triggered),
    this one IS reachable from an ADK tool
    (:func:`agent.adk_tools.upgrade_close_pr_tool`). Keeping the endpoint
    fixed here — rather than letting the tool pass ``endpoint=`` — means
    the LLM-facing surface never gets a way to pick the worker path. The
    worker re-validates ``target_repo`` and the PR's eligibility (label /
    branch / base) defensively; this wrapper just routes.
    """
    return call(
        "upgrade_docs",
        {"target_repo": target_repo, "pr_number": pr_number, "reason": reason},
        endpoint="/close",
    )


def call_merge_pr(target_repo: str, pr_number: int) -> dict:
    """Wrapper for the upgrade_docs worker's ``/merge`` endpoint.

    Like :func:`call_close_pr`, this IS reachable from an ADK tool
    (:func:`agent.adk_tools.upgrade_merge_pr_tool`) — keeping the endpoint
    fixed here, rather than letting the tool pass ``endpoint=``, means the
    LLM-facing surface never gets a way to pick the worker path. The
    payload is intentionally minimal (no merge method, no check list): the
    worker pins the merge strategy and the required-check allowlist as
    deploy policy and re-validates ``target_repo`` + the PR's eligibility
    and readiness defensively. This wrapper just routes.
    """
    return call(
        "upgrade_docs",
        {"target_repo": target_repo, "pr_number": pr_number},
        endpoint="/merge",
    )


def call_open_infra_pr(
    target_repo: str, branch: str, title: str, body: str, files: list[dict],
    *, dispatch_plan_builder: bool = False
) -> dict:
    """Wrapper for the tofu-editor worker's ``/open-pr`` endpoint (Phase D2).

    ``/open-pr`` is the tofu-editor worker's sole canonical (default) endpoint —
    it commits the validated, ``iac/``-only ``files`` onto an ``infra/`` branch
    and opens ONE PR. Like :func:`call_close_pr` / :func:`call_merge_pr`, this IS
    reachable from an ADK tool (the agent-authoring tool added in a later phase),
    so keeping the routing fixed here — rather than letting the tool assemble the
    request — is what keeps the LLM-facing surface narrow: the model supplies
    only the content (``target_repo`` selection, ``branch``, ``title``, ``body``,
    ``files``), never the path, and never the ``base``.

    ``base`` is pinned to ``"main"`` in code: the editor only ever targets the
    default branch, so the LLM does not (and must not) get to pick the base
    branch, the label, or the worker endpoint. The worker re-validates
    ``target_repo`` against its env-pinned ``TARGET_REPO`` and runs every
    ``iac/``-path / branch / static-gate policy check BEFORE any GitHub call;
    this wrapper just routes.

    ``dispatch_plan_builder``: when True, the worker will fire a
    ``workflow_dispatch`` on the C2 plan-builder (``iac.yml`` at ``main``) for
    the new PR number — fail-soft if the dispatch fails. The worker hardcodes the
    workflow filename, ref, and inputs; this flag is the only caller-controlled
    gate. Pass ``True`` when the autonomy mode is ``propose_apply`` (auto-dispatch
    is appropriate); leave False (the default) for all other modes.

    Uses the DEFAULT endpoint (``/open-pr``) — no ``endpoint=`` override, since
    it is the editor's only path.
    """
    return call(
        "tofu_editor",
        {
            "target_repo": target_repo,
            "branch": branch,
            "base": "main",
            "title": title,
            "body": body,
            "files": files,
            "dispatch_plan_builder": dispatch_plan_builder,
        },
    )


def call_propose(
    artifact_uri_metadata: str,
    generation_metadata: str,
    approver: str,
    operator_jwt: str | None,
    generation_iac_tree: str | None = None,
) -> dict:
    """Wrapper for the tofu-apply worker's ``/propose`` endpoint (Phase C5a).

    ``/propose`` is the tofu-apply worker's canonical/default endpoint —
    the "ask permission" path that creates a pending plan approval bound to
    the named plan artifact. We still route through a named wrapper (rather
    than letting a tool call ``call("tofu_apply", ...)`` directly) so the
    payload shape and the choice of endpoint are fixed in code: the LLM
    never gets to assemble an arbitrary tofu-apply request.

    Like :func:`call_execute` / :func:`call_apply`, this is NEVER exposed as
    an ADK tool. The tofu-apply worker is the sole infra mutator, and the
    decision to propose-then-apply is the operator's, not the model's — this
    wrapper is invoked only by the coordinator's server-side approval POST
    handler (added in a later phase), never from anything the LLM can reach.

    ``operator_jwt`` is included in the body ONLY when it is not ``None``.
    The worker's ``ProposeRequest`` schema is ``extra="forbid"`` and does not
    grow an ``operator_jwt`` field until C5b; conditionally omitting the key
    when ``None`` keeps this wrapper wire-compatible with the current worker
    while letting C5b start forwarding the trusted operator identity without
    touching this call site again.

    Uses the DEFAULT endpoint (``/propose``) — no ``endpoint=`` override.
    """
    payload: dict = {
        "artifact_uri_metadata": artifact_uri_metadata,
        "generation_metadata": generation_metadata,
        "approver": approver,
    }
    if operator_jwt is not None:
        payload["operator_jwt"] = operator_jwt
    # C6: the iac-tree.json sidecar generation — forwarded only when the coordinator
    # has it (create-class plans). Omitted when None so the wrapper stays
    # wire-compatible with a worker that predates the field (extra="forbid").
    if generation_iac_tree is not None:
        payload["generation_iac_tree"] = generation_iac_tree
    return call("tofu_apply", payload)


def call_apply(
    approval_id: str,
    approval_token: str,
    operator_jwt: str | None,
    generation_iac_tree: str | None = None,
) -> dict:
    """Wrapper for the tofu-apply worker's ``/apply`` endpoint (Phase C5a).

    ``/apply`` is the mutating path — it consumes a pending plan approval and
    runs ``tofu apply``, making the tofu-apply worker the sole service that
    ever changes live infra. Hardcoding ``endpoint="/apply"`` here (rather
    than exposing endpoint selection) is what keeps the "do the thing" path
    off the LLM-facing surface entirely: the model only ever drives the
    upstream plan-builder, never the applier.

    NEVER exposed as an ADK tool. The operator's Approve click in the
    coordinator's server-side approval POST handler is the only trigger; the
    handler validates the approval token before calling this.

    ``operator_jwt`` inclusion mirrors :func:`call_propose` exactly: the key
    is added to the body ONLY when it is not ``None`` (the worker's
    ``TokenRequest`` is ``extra="forbid"`` and the ``operator_jwt`` field is
    not added until C5b). ``approval_id`` and ``approval_token`` are always
    present.

    Long timeout (Phase C5e, BLOCKER fix): unlike every other worker call,
    ``/apply`` runs a real ``tofu apply`` that can take up to the worker's
    Cloud Run ``--timeout=900``. We pass :data:`_APPLY_HTTPX_TIMEOUT` (920s
    read) so the coordinator does not misread a long-but-successful apply as a
    transport failure. This is apply-then-merge CORRECTNESS, not just latency:
    a premature client timeout after the worker has already burned the approval
    and mutated live infra would make the coordinator skip the merge, leaving
    the applied infra change unmerged in the PR — a silent divergence. The
    default 30s stays on :func:`call_propose` / :func:`call_plan_deny`, which
    never run ``tofu apply``.
    """
    payload: dict = {
        "approval_id": approval_id,
        "approval_token": approval_token,
    }
    if operator_jwt is not None:
        payload["operator_jwt"] = operator_jwt
    # C6: forward the sidecar generation for a create-class apply (omitted when None).
    if generation_iac_tree is not None:
        payload["generation_iac_tree"] = generation_iac_tree
    return call("tofu_apply", payload, endpoint="/apply", timeout=_APPLY_HTTPX_TIMEOUT)


def get_baked_iac_hash() -> dict:
    """GET the tofu-apply worker's OWN baked ``iac/``-tree hash (C6c re-bake readiness).

    Lets the coordinator confirm the operator re-baked the worker from the merged
    ``main`` BEFORE driving a create-class resume — a clearer, cheaper pre-check than
    burning a ``/propose`` the worker's hash gate would refuse. Read-only; NEVER an
    ADK tool. Raises :class:`WorkerClientError` (the caller treats any failure as
    best-effort and falls through to the apply-time gate, which is the real guard)."""
    base = _worker_url("tofu_apply")
    token = mint_id_token(base)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Trace-Id": current_trace_id_or_new(),
    }
    try:
        with httpx.Client(timeout=_HTTPX_TIMEOUT) as client:
            r = client.get(f"{base}/baked-iac-hash", headers=headers)
    except httpx.RequestError as e:
        raise WorkerClientError(
            503, f"tofu_apply unreachable: {type(e).__name__}: {e}", "tofu_apply"
        ) from e
    if not 200 <= r.status_code < 300:
        raise WorkerClientError(r.status_code, r.text, "tofu_apply")
    try:
        return r.json()
    except ValueError as e:
        raise WorkerClientError(502, f"tofu_apply returned non-JSON body: {e}", "tofu_apply") from e


def call_plan_deny(approval_id: str, approval_token: str) -> dict:
    """Wrapper for the tofu-apply worker's ``/deny`` endpoint (Phase C5a).

    Named ``call_plan_deny`` rather than ``call_deny`` because the latter is
    already taken by the rollback worker's deny wrapper
    (:func:`call_deny`) — the two deny operations target different workers
    and must not collide.

    Cleanup-only. Under propose-on-approve, the operator's "Reject" is a
    coordinator-side audit event and there is normally no approval to deny
    yet (none is created until the operator Approves and ``/propose`` runs).
    ``/deny`` is retained ONLY to clean up the rare orphaned pending approval
    — the case where ``/propose`` succeeded but the subsequent ``/apply``
    failed, leaving a pending plan approval behind. Because this is pure
    cleanup of an approval the coordinator already minted, it takes NO
    ``operator_jwt``: there is no operator-identity binding to forward for a
    cleanup, unlike :func:`call_propose` / :func:`call_apply`.

    NEVER exposed as an ADK tool — invoked only by the coordinator's
    server-side approval POST handler. Hardcodes ``endpoint="/deny"`` so the
    LLM-facing surface can never select it.
    """
    return call(
        "tofu_apply",
        {"approval_id": approval_id, "approval_token": approval_token},
        endpoint="/deny",
    )
