# Momentum System — container image
#
# Single image for every run mode (the app is a monolith run from the repo root
# with PYTHONPATH=/app, not a pip-installed wheel — see pyproject.toml
# `package = false`). The Kubernetes workloads override CMD:
#   - app Deployment : python run_live_paper.py            (loop + embedded dashboard)
#   - CronJobs       : python eod_replay.py / nightly_tune.py / momentum_cli.py journal
FROM python:3.11-slim

# psycopg2-binary ships its own libpq, so no build toolchain is needed; we only
# add a tiny client for healthchecks/debugging and clean up the apt cache.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first so they cache across code-only changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code.
COPY . .

# Run as a non-root user that owns the writable data dir (DuckDB research file,
# logs, learned_params.json, schwab tokens). In K8s this path is a mounted PVC.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/data \
    && chown -R app:app /app/data
USER app

EXPOSE 8010

# Default to the headless loop; the chart's app Deployment runs it WITH the
# dashboard, and the CronJobs override this entirely.
CMD ["python", "run_live_paper.py", "--no-dashboard"]
