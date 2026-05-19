FROM python:3.12-slim
WORKDIR /app

RUN pip install --no-cache-dir uv

# Install the worker's runtime deps directly (no `pip install .`). The
# version specifiers below are floors, not pins — uv resolves to the latest
# compatible release at build time. workers/notifier/pyproject.toml mirrors
# these floors as canonical documentation. (Pinning via a per-worker uv.lock
# is a follow-up if reproducibility becomes a concern.)
RUN uv pip install --system \
    "fastapi>=0.115" \
    "uvicorn[standard]>=0.32" \
    "pydantic>=2.9" \
    "httpx>=0.27" \
    "google-auth>=2.35" \
    "requests>=2.32"

# `driftscribe_lib` is a sibling package of the worker source. We expose it
# inside the container via PYTHONPATH rather than pip-installing it — the
# library doesn't have its own pyproject.toml (it's a sub-package of the root
# `driftscribe` distribution; see Phase 11.2). The build context is the repo
# root so these relative paths resolve correctly.
COPY driftscribe_lib/ ./driftscribe_lib/
# Copy the workers package marker + only the notifier's source. Sibling
# workers (reader / docs / rollback) live in their own directories and
# intentionally aren't bundled into the notifier image — each worker is its
# own deployable. Tests are also excluded.
COPY workers/__init__.py ./workers/__init__.py
COPY workers/notifier/__init__.py ./workers/notifier/__init__.py
COPY workers/notifier/main.py ./workers/notifier/main.py

ENV PYTHONPATH=/app
ENV PORT=8080

# Shell form so $PORT expands at runtime — Cloud Run injects PORT and expects
# the container to listen on its chosen value.
CMD ["sh", "-c", "uvicorn workers.notifier.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
