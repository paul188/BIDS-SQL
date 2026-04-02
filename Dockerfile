# ── Stage 1: install bids-validator (official Node image) ────────────────────
# Isolated stage so Node never lands in the final image — only the validator
# binary and its node_modules are copied across.
FROM node:20-slim AS node-builder
RUN npm install -g bids-validator

# ── Stage 2: base Python environment (official Python image) ─────────────────
FROM python:3.11-slim AS base

# Build tools needed by asyncpg / psycopg2 C extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy Node runtime + bids-validator from the node-builder stage
COPY --from=node-builder /usr/local/bin/node          /usr/local/bin/node
COPY --from=node-builder /usr/local/bin/bids-validator /usr/local/bin/bids-validator
COPY --from=node-builder /usr/local/lib/node_modules  /usr/local/lib/node_modules

WORKDIR /app

# ── Stage 2: install Python deps ─────────────────────────────────────────────
FROM base AS deps

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Stage 3: final runtime image ─────────────────────────────────────────────
FROM deps AS runtime

# Copy application code
COPY base.py db.py models.py rules.py settings.py utils.py \
     input_pipeline.py cli.py ./

# BIDS data is always bind-mounted at runtime; this is just a documented mount point
VOLUME ["/data"]

# Default entrypoint is the CLI so the container can be called like a binary:
#   docker compose run --rm bids-sql /data/my_study
#   docker compose run --rm bids-sql --rules
ENTRYPOINT ["python", "cli.py"]
CMD ["--help"]
