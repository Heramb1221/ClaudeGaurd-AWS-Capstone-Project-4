# ClauseGuard worker service
# Base: slim Python image to keep ECR image size and cold-start time down
FROM python:3.12-slim

# psycopg2-binary needs libpq at runtime; install minimal system deps only
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Run as a non-root user for defense in depth
RUN groupadd --system clauseguard && useradd --system --gid clauseguard clauseguard

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY worker/ ./worker/

USER clauseguard

# The worker has no HTTP surface — it's a long-running polling process,
# so there is no EXPOSE/HEALTHCHECK port. ECS uses the container's process
# liveness (not an HTTP probe) as the health signal for this service.
CMD ["python", "-m", "worker.main"]
