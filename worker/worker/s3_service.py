"""
S3 access for the worker: downloading the raw contract file and uploading
the generated risk report PDF.
"""

import os
import tempfile

import boto3
from botocore.exceptions import ClientError

from worker.config import WorkerConfig
from worker.logger import get_logger

logger = get_logger(__name__)

_s3_client = boto3.client("s3", region_name=WorkerConfig.AWS_REGION)


def download_file(s3_key: str) -> str:
    """
    Downloads an object from the ClauseGuard S3 bucket to a local temp file
    and returns the local file path.
    """
    suffix = os.path.splitext(s3_key)[1] or ".bin"
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(tmp_fd)

    try:
        _s3_client.download_file(WorkerConfig.S3_BUCKET_NAME, s3_key, tmp_path)
        logger.info("Downloaded s3://%s/%s to %s", WorkerConfig.S3_BUCKET_NAME, s3_key, tmp_path)
        return tmp_path
    except ClientError as exc:
        logger.error("Failed to download s3://%s/%s: %s", WorkerConfig.S3_BUCKET_NAME, s3_key, exc)
        raise


def upload_report(local_path: str, report_s3_key: str) -> None:
    """
    Uploads the generated PDF risk report back to S3 under a `reports/` prefix.
    """
    try:
        _s3_client.upload_file(
            local_path,
            WorkerConfig.S3_BUCKET_NAME,
            report_s3_key,
            ExtraArgs={"ContentType": "application/pdf"},
        )
        logger.info("Uploaded report to s3://%s/%s", WorkerConfig.S3_BUCKET_NAME, report_s3_key)
    except ClientError as exc:
        logger.error("Failed to upload report %s: %s", report_s3_key, exc)
        raise


def get_object_bytes(s3_key: str) -> bytes:
    """Reads an S3 object directly into memory (used for Textract's bytes API on small files)."""
    response = _s3_client.get_object(Bucket=WorkerConfig.S3_BUCKET_NAME, Key=s3_key)
    return response["Body"].read()
