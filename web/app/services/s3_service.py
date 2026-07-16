"""
S3 access for the web service: uploading raw contract files and generating
presigned URLs so the browser can download files directly from S3 without
ever exposing the bucket publicly or proxying large files through Flask.
"""

import uuid

import boto3
from botocore.exceptions import ClientError

from app.config import WebConfig
from app.utils.logger import get_logger

logger = get_logger(__name__)

_s3_client = boto3.client("s3", region_name=WebConfig.AWS_REGION)


def build_contract_s3_key(user_id: int, sanitized_filename: str) -> str:
    unique_id = uuid.uuid4().hex[:12]
    return f"contracts/{user_id}/{unique_id}_{sanitized_filename}"


def upload_fileobj(file_stream, s3_key: str, content_type: str = "application/pdf") -> None:
    try:
        _s3_client.upload_fileobj(
            file_stream,
            WebConfig.S3_BUCKET_NAME,
            s3_key,
            ExtraArgs={"ContentType": content_type},
        )
        logger.info("Uploaded contract to s3://%s/%s", WebConfig.S3_BUCKET_NAME, s3_key)
    except ClientError as exc:
        logger.error("Failed to upload %s: %s", s3_key, exc)
        raise


def generate_presigned_download_url(s3_key: str, download_filename: str) -> str:
    try:
        url = _s3_client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": WebConfig.S3_BUCKET_NAME,
                "Key": s3_key,
                "ResponseContentDisposition": f'attachment; filename="{download_filename}"',
            },
            ExpiresIn=WebConfig.PRESIGNED_URL_EXPIRY_SECONDS,
        )
        return url
    except ClientError as exc:
        logger.error("Failed to presign %s: %s", s3_key, exc)
        raise
