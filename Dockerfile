# syntax=docker/dockerfile:1

# ── Stage 1: install Python deps ─────────────────────────────────────────────
FROM python:3.12-slim AS deps

# Build tools needed by asyncpg C extension
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Stage 2: runtime image ────────────────────────────────────────────────────
FROM deps AS runtime

# Core package
COPY rules.py settings.py utils.py input_pipeline.py cli.py ./

# DB layer (moved from flat files into db/ subdirectory)
COPY db/ ./db/

# BIDS data is always bind-mounted at runtime
VOLUME ["/data"]

# bids-validator runs as a separate sidecar container (see docker-compose.yml)
ENTRYPOINT ["python", "cli.py"]
CMD ["--help"]
