FROM python:3.12-slim
WORKDIR /app

# Copy only what the package install needs first (layer-caching friendly).
COPY pyproject.toml README.md ./
COPY assay/ ./assay/

RUN pip install --no-cache-dir ".[server,anthropic,openai]" psycopg[binary]

EXPOSE 8000
CMD ["assay", "serve", "--host", "0.0.0.0", "--port", "8000"]
