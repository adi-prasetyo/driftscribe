# tofu-editor worker (Phase D) — the iac/-only multi-file PR opener and the
# sole new WRITE SURFACE Phase D introduces. This worker authors HCL *text* and
# runs exactly ONE tofu subcommand: `tofu fmt` on each authored .tf BEFORE
# committing, so agent HCL is canonical and the required `tofu` CI check
# (`tofu -chdir=iac fmt -check`) passes without a manual fixup commit. `tofu fmt`
# is a purely syntactic, OFFLINE transform (stdin→stdout) — no providers, state,
# network, or filesystem writes — so the image bakes ONLY the binary (no provider
# prefetch, unlike workers/tofu_apply).
#
# TOFU_VERSION is pinned in lockstep with workers/tofu_apply + the C2 plan-builder
# (opentofu/setup-opentofu) so authoring, planning, and applying all use one tofu.

# ---- stage 1: fetch + checksum-verify the pinned tofu binary ----
FROM python:3.12-slim AS tofu-builder
ARG TOFU_VERSION=1.12.0
# tofu_${TOFU_VERSION}_linux_amd64.zip is pinned HERE, in-repo, NOT trusted from
# the network: the SHA256 below is checked against the downloaded artifact, so a
# tampered/substituted release fails the build. Update this in lockstep with
# TOFU_VERSION (source: the official tofu_<v>_SHA256SUMS).
ARG TOFU_SHA256=8d7650fd42b6d790f9f747604393ccd0a9035376bccc4f1688b905d7c5bb1137
RUN set -eux; \
    apt-get update && apt-get install -y --no-install-recommends ca-certificates curl unzip; \
    curl -fsSLO "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_linux_amd64.zip"; \
    echo "${TOFU_SHA256}  tofu_${TOFU_VERSION}_linux_amd64.zip" | sha256sum -c -; \
    unzip "tofu_${TOFU_VERSION}_linux_amd64.zip" tofu -d /usr/local/bin; \
    chmod +x /usr/local/bin/tofu; \
    /usr/local/bin/tofu version

# ---- runtime image ----
# Same base-image pin as the sibling GitHub-write workers (workers/upgrade_docs).
FROM python:3.12-slim
WORKDIR /app

# The verified tofu binary (only the binary — none of the build/download tooling
# stays in the runtime image).
COPY --from=tofu-builder /usr/local/bin/tofu /usr/local/bin/tofu

RUN pip install --no-cache-dir uv

# Install the worker's runtime deps directly (no `pip install .`). The version
# specifiers below are floors, not pins — uv resolves to the latest compatible
# release at build time. workers/tofu_editor/pyproject.toml mirrors these floors
# as canonical documentation. Same dep set as workers/upgrade_docs (both talk to
# GitHub via PyGithub and verify inbound ID tokens via google-auth + requests),
# PLUS python-hcl2: unlike upgrade_docs, this worker runs the D1-4 in-process
# static gate (main.py -> tools.iac_static_gate -> driftscribe_lib.iac_hcl ->
# `import hcl2`). The PyPI package for `import hcl2` is python-hcl2 (`hcl2` is
# only the import name). Omitting it passes unit tests via the root env but
# ModuleNotFound's at boot in the deployed image — do NOT remove.
RUN uv pip install --system \
    "fastapi>=0.115" \
    "uvicorn[standard]>=0.32" \
    "google-auth>=2.35" \
    "requests>=2.32" \
    "PyGithub>=2.4" \
    "python-hcl2>=4.3"

# `driftscribe_lib` is a sibling package of the worker source, exposed inside
# the container via PYTHONPATH rather than pip-installed (it has no pyproject of
# its own — it's a sub-package of the root distribution; see Phase 11.2). The
# build context is the repo root so these relative paths resolve. The worker
# imports driftscribe_lib.{github,auth,logging,iac_editor_policy,iac_hcl}.
COPY driftscribe_lib/ ./driftscribe_lib/

# The `tools` package: the worker runs the SAME AGENT-mode static gate CI runs
# (`from tools.iac_static_gate import GateInput, GateMode, evaluate`), so the
# gate's content policy is identical in-worker and in-CI by construction. Copy
# it as a PACKAGE (tools/__init__.py + tools/iac_static_gate.py), not a loose
# module, so `from tools.iac_static_gate import ...` resolves. iac_static_gate
# imports driftscribe_lib.iac_hcl AND driftscribe_lib.iac_plan_denylist
# (ADOPTABLE_RESOURCE_TYPES) plus stdlib — no other tools/* submodule needed.
COPY tools/__init__.py ./tools/__init__.py
COPY tools/iac_static_gate.py ./tools/iac_static_gate.py

# Copy the workers package marker + only the tofu_editor source. Sibling workers
# (reader, docs, rollback, notifier, upgrade_reader, upgrade_docs, tofu_apply,
# infra_reader) intentionally aren't bundled — each worker is its own
# deployable. The coordinator (`agent/`) is NOT copied at all: worker isolation
# means the image must not contain coordinator authority code (see the worker's
# test_no_agent_import.py). Tests are also excluded.
COPY workers/__init__.py ./workers/__init__.py
COPY workers/tofu_editor/__init__.py ./workers/tofu_editor/__init__.py
COPY workers/tofu_editor/main.py ./workers/tofu_editor/main.py

ENV PYTHONPATH=/app
ENV PORT=8080

# Shell form so $PORT expands at runtime — Cloud Run injects PORT and expects
# the container to listen on its chosen value.
CMD ["sh", "-c", "uvicorn workers.tofu_editor.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
