"""
Configuration for the ClauseGuard worker service.

All values are pulled from environment variables so that the same Docker
image can be deployed to any environment (local, staging, production)
without ever containing secrets or hardcoded values.

NOTE: DB_* variables have been removed. ClauseGuard now uses DynamoDB,
which is accessed via the ECS task IAM role — no database credentials needed.
"""

import os


class ConfigError(Exception):
    """Raised when a required environment variable is missing."""


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(
            f"Required environment variable '{name}' is not set. "
            f"Check your .env file or ECS task definition."
        )
    return value


class WorkerConfig:
    # AWS
    AWS_REGION = os.environ.get("AWS_REGION", "ap-south-1")

    # SQS
    SQS_QUEUE_URL = _require("SQS_QUEUE_URL")
    SQS_WAIT_TIME_SECONDS = int(os.environ.get("SQS_WAIT_TIME_SECONDS", "20"))
    SQS_VISIBILITY_TIMEOUT = int(os.environ.get("SQS_VISIBILITY_TIMEOUT", "300"))
    SQS_MAX_MESSAGES = int(os.environ.get("SQS_MAX_MESSAGES", "5"))

    # S3
    S3_BUCKET_NAME = _require("S3_BUCKET_NAME")

    # Textract
    TEXTRACT_MAX_PAGES = int(os.environ.get("TEXTRACT_MAX_PAGES", "20"))

    # Worker behaviour
    POLL_IDLE_SLEEP_SECONDS = int(os.environ.get("POLL_IDLE_SLEEP_SECONDS", "2"))
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
