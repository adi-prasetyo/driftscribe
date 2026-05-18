FROM python:3.12-slim
WORKDIR /app

# Single-file app — true two-stage layer caching (deps separate from source)
# would need a manifest-only install step, which doesn't compose with
# `py-modules = ["main"]` + `pip install .`. Accept the tradeoff: rebuild of
# the dependency layer on every main.py edit is cheap for an image this size.
COPY pyproject.toml main.py ./
RUN pip install --no-cache-dir .

ENV PORT=8080
EXPOSE 8080
# Shell form so $PORT expands at runtime. Cloud Run injects PORT and expects
# the container to listen on whatever it chose; hardcoding --port 8080 would
# break the moment Cloud Run picks a different value. ${PORT:-8080} keeps the
# local-dev `docker run` default working.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
