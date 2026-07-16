# ClauseGuard web service
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN groupadd --system clauseguard && useradd --system --gid clauseguard clauseguard

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY wsgi.py .

USER clauseguard

EXPOSE 8080

# Gunicorn with a small worker count suited to a t3.micro-equivalent Fargate task
# (0.5 vCPU / 1GB by default — see infrastructure/provision.py)
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "2", "--timeout", "60", "wsgi:app"]
