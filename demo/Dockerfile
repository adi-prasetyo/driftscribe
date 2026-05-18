FROM python:3.12-slim
WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml ./
COPY main.py ./
RUN pip install --no-cache-dir .

ENV PORT=8080
EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
