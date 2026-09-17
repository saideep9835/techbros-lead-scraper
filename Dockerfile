FROM python:3.12-slim

# tzdata is required: the app pins business hours and run dates to
# America/Chicago via zoneinfo, which has no database on a slim image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copied first so dependency layers cache across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Don't run as root. The data directory is a mounted volume, so it needs to be
# writable by this user.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8080/login || exit 1

# One worker with threads, deliberately:
#  - scrapes and enrichment run as background threads inside the worker
#  - SQLite takes a single writer
#  - the login rate limiter keeps its counts in memory
# Multiple workers would break all three. --timeout 0 because a scrape can run
# for many minutes without touching the request that started it.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", \
     "--timeout", "0", "--access-logfile", "-", "app:app"]
