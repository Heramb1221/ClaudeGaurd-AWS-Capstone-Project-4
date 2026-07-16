"""
SQS polling helpers for the worker.

The worker uses long polling to keep API call volume (and cost) low, and
deletes messages only after the job has been fully processed and committed
to the database — giving at-least-once processing with safe retries if the
worker crashes mid-job (the message becomes visible again after the
visibility timeout expires).
"""

import json
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from worker.config import WorkerConfig
from worker.logger import get_logger

logger = get_logger(__name__)

_sqs_client = boto3.client("sqs", region_name=WorkerConfig.AWS_REGION)


def receive_message() -> Optional[dict]:
    """
    Long-polls SQS for a single job message. Returns a dict with the parsed
    body and the receipt handle, or None if no message was available.
    """
    try:
        response = _sqs_client.receive_message(
            QueueUrl=WorkerConfig.SQS_QUEUE_URL,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=WorkerConfig.SQS_WAIT_TIME_SECONDS,
            VisibilityTimeout=WorkerConfig.SQS_VISIBILITY_TIMEOUT,
        )
    except ClientError as exc:
        logger.error("SQS receive_message failed: %s", exc)
        return None

    messages = response.get("Messages", [])
    if not messages:
        return None

    raw = messages[0]
    try:
        body = json.loads(raw["Body"])
    except (KeyError, json.JSONDecodeError) as exc:
        logger.error("Received malformed SQS message, deleting it: %s", exc)
        delete_message(raw["ReceiptHandle"])
        return None

    return {"body": body, "receipt_handle": raw["ReceiptHandle"]}


def delete_message(receipt_handle: str) -> None:
    try:
        _sqs_client.delete_message(
            QueueUrl=WorkerConfig.SQS_QUEUE_URL,
            ReceiptHandle=receipt_handle,
        )
    except ClientError as exc:
        logger.error("Failed to delete SQS message: %s", exc)


def send_contract_job(contract_id: int, s3_key: str) -> None:
    """
    Used only by tests / manual re-queueing. The web service is the normal
    producer of these messages (see web/app/services/sqs_service.py).
    """
    _sqs_client.send_message(
        QueueUrl=WorkerConfig.SQS_QUEUE_URL,
        MessageBody=json.dumps({"contract_id": contract_id, "s3_key": s3_key}),
    )
