FROM python:3.12-slim
WORKDIR /app

COPY pyproject.toml main.py ./
RUN pip install --no-cache-dir .

ENV PORT=8080
EXPOSE 8080
# Shell form so $PORT expands at runtime — Cloud Run injects PORT and the
# container must listen on whatever value it picked.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
