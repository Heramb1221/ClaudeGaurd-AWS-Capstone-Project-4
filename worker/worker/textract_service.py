"""
Amazon Textract integration.

Contracts can be multi-page PDFs (including scanned/photographed pages), so
we use Textract's asynchronous document-text-detection API, which reads
directly from S3 and supports multi-page documents. The synchronous API
would only support single-page documents, which is not realistic for
real-world contracts.
"""

import time

import boto3
from botocore.exceptions import ClientError

from worker.config import WorkerConfig
from worker.logger import get_logger

logger = get_logger(__name__)

_textract_client = boto3.client("textract", region_name=WorkerConfig.AWS_REGION)

POLL_INTERVAL_SECONDS = 3
MAX_POLL_ATTEMPTS = 100  # ~5 minutes ceiling for very large documents


class TextractError(Exception):
    pass


def extract_text_locally_via_pypdf(s3_key: str) -> str:
    import io
    from pypdf import PdfReader
    from worker.s3_service import get_object_bytes

    logger.info("Extracting text locally via pypdf for S3 key: %s", s3_key)
    pdf_bytes = get_object_bytes(s3_key)
    reader = PdfReader(io.BytesIO(pdf_bytes))
    full_text_pages = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            full_text_pages.append(text)
    return "\n\n".join(full_text_pages)


def extract_text_from_s3_document(s3_key: str) -> str:
    """
    Starts an async Textract job against the document already stored in the
    ClauseGuard S3 bucket, polls until completion, and returns the full
    extracted text. Falls back to local pypdf extraction if Amazon Textract
    is not subscribed or fails.
    """
    try:
        start_response = _textract_client.start_document_text_detection(
            DocumentLocation={
                "S3Object": {
                    "Bucket": WorkerConfig.S3_BUCKET_NAME,
                    "Name": s3_key,
                }
            }
        )
    except Exception as exc:
        logger.warning(
            "Amazon Textract job failed to start (%s). Falling back to local PDF text extraction.",
            exc,
        )
        try:
            return extract_text_locally_via_pypdf(s3_key)
        except Exception as local_exc:
            logger.error("Local PDF extraction fallback failed: %s", local_exc)
            raise TextractError(
                f"Failed to start Textract job, and local PDF extraction fallback failed: {local_exc}"
            ) from local_exc

    job_id = start_response["JobId"]
    logger.info("Started Textract job %s for %s", job_id, s3_key)

    full_text_pages = []
    next_token = None

    for attempt in range(MAX_POLL_ATTEMPTS):
        kwargs = {"JobId": job_id}
        if next_token:
            kwargs["NextToken"] = next_token

        try:
            result = _textract_client.get_document_text_detection(**kwargs)
        except Exception as exc:
            logger.warning(
                "Failed to get Textract job status (%s). Falling back to local PDF text extraction.",
                exc,
            )
            return extract_text_locally_via_pypdf(s3_key)

        status = result["JobStatus"]

        if status == "IN_PROGRESS":
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        if status == "FAILED":
            logger.warning(
                "Textract job %s failed: %s. Falling back to local PDF text extraction.",
                job_id,
                result.get("StatusMessage"),
            )
            return extract_text_locally_via_pypdf(s3_key)

        if status == "SUCCEEDED":
            page_lines = [
                block["Text"]
                for block in result.get("Blocks", [])
                if block["BlockType"] == "LINE"
            ]
            full_text_pages.append("\n".join(page_lines))

            next_token = result.get("NextToken")
            if not next_token:
                return "\n\n".join(full_text_pages)
            # More pages of Textract results to fetch for the same job; loop again immediately
            continue

        raise TextractError(f"Unexpected Textract job status: {status}")

    logger.warning("Textract job did not complete in time. Falling back to local PDF text extraction.")
    return extract_text_locally_via_pypdf(s3_key)
