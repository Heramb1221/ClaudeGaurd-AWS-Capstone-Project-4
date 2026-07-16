"""
SQS producer for the web service. After a contract is uploaded and its
metadata row is created, the web service publishes a small job message so
the worker service (running as a separate ECS service) can pick it up
asynchronously.
"""

import json

import boto3
from botocore.exceptions import ClientError

from app.config import WebConfig
from app.utils.logger import get_logger

logger = get_logger(__name__)

_sqs_client = boto3.client("sqs", region_name=WebConfig.AWS_REGION)


def enqueue_contract_job(contract_id: str, s3_key: str) -> None:
    try:
        _sqs_client.send_message(
            QueueUrl=WebConfig.SQS_QUEUE_URL,
            MessageBody=json.dumps({"contract_id": contract_id, "s3_key": s3_key}),
        )
        logger.info("Enqueued processing job for contract_id=%s", contract_id)
    except ClientError as exc:
        logger.error("Failed to enqueue job for contract_id=%s: %s", contract_id, exc)
        raise
