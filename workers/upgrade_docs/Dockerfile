FROM python:3.12-slim
WORKDIR /app

RUN pip install --no-cache-dir uv

# Install the worker's runtime deps directly (no `pip install .`). The
# version specifiers below are floors, not pins — uv resolves to the latest
# compatible release at build time. workers/upgrade_docs/pyproject.toml
# mirrors these floors as canonical documentation. (Pinning via a per-worker
# uv.lock is a follow-up if reproducibility becomes a concern.)
#
# Note vs. workers/upgrade_reader/Dockerfile: same dep set — the upgrade
# workers both talk to GitHub (lockfile read + PR write). PyGithub is used
# via `driftscribe_lib.github.get_repo` + `open_docs_pr`.
RUN uv pip install --system \
    "fastapi>=0.115" \
    "uvicorn[standard]>=0.32" \
    "google-auth>=2.35" \
    "requests>=2.32" \
    "PyGithub>=2.4"

# `driftscribe_lib` is a sibling package of the worker source. We expose it
# inside the container via PYTHONPATH rather than pip-installing it — the
# library doesn't have its own pyproject.toml (it's a sub-package of the root
# `driftscribe` distribution; see Phase 11.2). The build context is the repo
# root so these relative paths resolve correctly.
COPY driftscribe_lib/ ./driftscribe_lib/
# Copy the workers package marker + only the upgrade_docs source. Sibling
# workers (workers/reader, workers/docs, workers/rollback, workers/notifier,
# workers/upgrade_reader) intentionally aren't bundled into this image —
# each worker is its own deployable. Tests are also excluded.
COPY workers/__init__.py ./workers/__init__.py
COPY workers/upgrade_docs/__init__.py ./workers/upgrade_docs/__init__.py
COPY workers/upgrade_docs/main.py ./workers/upgrade_docs/main.py

ENV PYTHONPATH=/app
ENV PORT=8080

# Shell form so $PORT expands at runtime — Cloud Run injects PORT and expects
# the container to listen on its chosen value.
CMD ["sh", "-c", "uvicorn workers.upgrade_docs.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
