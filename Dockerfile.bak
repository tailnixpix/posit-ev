# ─────────────────────────────────────────────────────────────────────────────
# Posit+EV — Dockerfile
#
# Multi-stage build:
#   builder  — installs Python deps into a clean venv
#   runtime  — copies only the venv + app source; no build tools in prod image
#
# Build:   docker build -t posit-ev .
# Run:     docker compose up -d
# ─────────────────────────────────────────────────────────────────────────────

# ── Stage 1: builder ─────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# System deps needed to compile some wheels (psycopg2, bcrypt, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Create an isolated venv so we can copy it cleanly into the runtime stage
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install deps first (layer-cached unless requirements.txt changes)
COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt


# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Non-root user for security
RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

# Pull venv from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy application source
COPY --chown=appuser:appuser . .

# /data is the volume mount point for the SQLite database file.
# The DB path is controlled by DATABASE_URL in .env:
#   DATABASE_URL=sqlite:////data/sports_ev.db
RUN mkdir -p /data && chown appuser:appuser /data

USER appuser

# Expose the port uvicorn listens on (overridden by PORT env var if set)
EXPOSE 8000

# Health check — hits the landing page; fails fast if app is down
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/')" \
    || exit 1

# Start uvicorn.  Workers=1 keeps APScheduler's in-process jobs on a single
# process so the EV cache refresh and newsletter scheduler run exactly once.
CMD ["uvicorn", "web.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--log-level", "info", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
