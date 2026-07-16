"""
Configuration for the ClauseGuard web service, loaded entirely from
environment variables. No secrets are ever hardcoded here.

NOTE: DB_* variables have been removed. ClauseGuard now uses DynamoDB,
which is accessed via the ECS task IAM role — no database credentials needed.
"""

import os


class ConfigError(Exception):
    pass


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(
            f"Required environment variable '{name}' is not set. "
            f"Check your .env file or ECS task definition."
        )
    return value


class WebConfig:
    # Flask
    SECRET_KEY = _require("FLASK_SECRET_KEY")
    ENV = os.environ.get("FLASK_ENV", "production")
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_UPLOAD_MB", "10")) * 1024 * 1024

    # AWS
    AWS_REGION = os.environ.get("AWS_REGION", "ap-south-1")

    # S3
    S3_BUCKET_NAME = _require("S3_BUCKET_NAME")
    PRESIGNED_URL_EXPIRY_SECONDS = int(os.environ.get("PRESIGNED_URL_EXPIRY_SECONDS", "900"))

    # SQS
    SQS_QUEUE_URL = _require("SQS_QUEUE_URL")

    # Auth
    AUTH_TOKEN_TTL_HOURS = int(os.environ.get("AUTH_TOKEN_TTL_HOURS", "24"))
    PERMANENT_SESSION_LIFETIME = AUTH_TOKEN_TTL_HOURS * 3600
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = False  # HTTP only — no TLS/HTTPS on this ALB
    ALLOWED_UPLOAD_EXTENSIONS = {".pdf"}

    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
