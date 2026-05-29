# tofu-apply worker (Phase C4) — the SOLE MUTATOR. The first worker to bake in a
# pinned `tofu` binary and to shell out to it. Multi-stage so the download/verify
# tooling (unzip/curl) stays OUT of the runtime image.
#
# TOFU_VERSION MUST equal the C2 plan-builder's pin (opentofu/setup-opentofu in
# .github/workflows/iac.yml) and iac/.terraform.lock.hcl's provider line — the
# worker's fidelity gate refuses any plan whose metadata.opentofu_version differs
# from the baked binary. Bump deliberately, in lockstep with the plan-builder.

# ---- stage 1: fetch + checksum-verify the pinned tofu binary ----
FROM python:3.12-slim AS tofu-builder
ARG TOFU_VERSION=1.12.0
# REPO-CONTROLLED checksum (Codex review): the expected SHA-256 of
# tofu_${TOFU_VERSION}_linux_amd64.zip is pinned HERE, in-repo, NOT trusted from
# a fetched SHA256SUMS. `sha256sum -c` against a same-origin SUMS only proves the
# zip matches a SUMS from the same (untrusted) release; pinning the literal makes
# the checksum baseline repo-controlled + code-reviewed, so a swapped/MITM'd
# release artifact set fails the build. Update this in lockstep with TOFU_VERSION
# (source: the official tofu_<v>_SHA256SUMS). A `gpg --verify` of the release
# SUMS (pinned OpenTofu key fingerprint) remains an optional higher-assurance add.
ARG TOFU_SHA256=8d7650fd42b6d790f9f747604393ccd0a9035376bccc4f1688b905d7c5bb1137
RUN set -eux; \
    apt-get update && apt-get install -y --no-install-recommends ca-certificates curl unzip; \
    cd /tmp; \
    curl -fsSLO "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_linux_amd64.zip"; \
    echo "${TOFU_SHA256}  tofu_${TOFU_VERSION}_linux_amd64.zip" | sha256sum -c -; \
    unzip "tofu_${TOFU_VERSION}_linux_amd64.zip" tofu -d /usr/local/bin; \
    chmod +x /usr/local/bin/tofu; \
    /usr/local/bin/tofu version

# ---- stage 2: bake providers against the committed lockfile (hermetic apply) ----
FROM tofu-builder AS providers
WORKDIR /app
COPY iac/ ./iac/
# -backend=false: no GCS/state access at build; just install the hashicorp/google
# provider pinned by the committed .terraform.lock.hcl (-lockfile=readonly forbids
# any upgrade). The resulting iac/.terraform/ is copied into the runtime image so
# `tofu init` at apply time does NO registry fetch (no egress, pinned providers).
RUN cd iac && tofu init -backend=false -input=false -no-color -lockfile=readonly

# ---- stage 3: runtime ----
FROM python:3.12-slim
WORKDIR /app
ARG TOFU_VERSION=1.12.0
ENV TOFU_VERSION=${TOFU_VERSION}

RUN pip install --no-cache-dir uv
# Runtime deps mirror workers/rollback (fastapi/uvicorn/pydantic/firestore/
# google-auth/requests) PLUS google-cloud-storage (NEW — the artifact fetch) and
# httpx + pyjwt[crypto] (NEW, C5b-2 — driftscribe_lib.cf_access fetches the CF
# JWKS over httpx and verifies the RS256 operator JWT; [crypto] → cryptography).
RUN uv pip install --system \
    "fastapi>=0.115" \
    "uvicorn[standard]>=0.32" \
    "pydantic>=2.9" \
    "google-cloud-firestore>=2.19" \
    "google-cloud-storage>=2.18" \
    "google-auth>=2.35" \
    "requests>=2.32" \
    "httpx>=0.27" \
    "pyjwt[crypto]>=2.10"

# The verified tofu binary (only the binary — none of the build tooling).
COPY --from=tofu-builder /usr/local/bin/tofu /usr/local/bin/tofu

# driftscribe_lib carries approvals + iac_plan_denylist (the C4 promotion) +
# auth/logging — shipped via PYTHONPATH, the established worker pattern.
COPY driftscribe_lib/ ./driftscribe_lib/
COPY workers/__init__.py ./workers/__init__.py
COPY workers/tofu_apply/__init__.py ./workers/tofu_apply/__init__.py
COPY workers/tofu_apply/main.py ./workers/tofu_apply/main.py
COPY workers/tofu_apply/gcs_fetch.py ./workers/tofu_apply/gcs_fetch.py
COPY workers/tofu_apply/tofu_runner.py ./workers/tofu_apply/tofu_runner.py
# Bake iac/ + the pre-fetched providers (.terraform/) for hermetic init/apply.
# The baked .terraform.lock.hcl is also the fidelity-gate baseline the worker
# hashes and compares to metadata.provider_lockfile_sha256.
COPY --from=providers /app/iac/ ./iac/

ENV PYTHONPATH=/app
ENV PORT=8080
ENV IAC_DIR=/app/iac

CMD ["sh", "-c", "uvicorn workers.tofu_apply.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
