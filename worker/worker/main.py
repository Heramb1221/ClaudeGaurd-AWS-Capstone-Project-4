"""
ClauseGuard worker entrypoint.

This process runs as a long-lived ECS task (RestartPolicy: Always via the
ECS service scheduler). It continuously long-polls SQS for contract
processing jobs, and for each job:

  1. Marks the contract as PROCESSING in DynamoDB.
  2. Downloads the raw file from S3.
  3. Extracts text via Textract.
  4. Runs the rule-based risk engine over the extracted text.
  5. Generates a PDF risk report and uploads it to S3.
  6. Writes clause results + final status to DynamoDB atomically.
  7. Deletes the SQS message only after all of the above succeeds.

If any step fails, the contract is marked FAILED with an error message and
the SQS message is still deleted (no infinite retry loop for a permanently
broken document) — but the failure is logged loudly for CloudWatch alarms.
"""

import os
import time
import traceback

from worker import db, report_service, risk_engine, s3_service, sqs_service, textract_service
from worker.config import WorkerConfig
from worker.logger import get_logger

logger = get_logger(__name__)


def process_job(job: dict) -> None:
    body = job["body"]
    receipt_handle = job["receipt_handle"]

    contract_id = body.get("contract_id")
    s3_key = body.get("s3_key")

    if contract_id is None or not s3_key:
        logger.error("Malformed job payload, skipping: %s", body)
        sqs_service.delete_message(receipt_handle)
        return

    logger.info("Processing contract_id=%s s3_key=%s", contract_id, s3_key)
    downloaded_path = None
    report_path = None

    try:
        db.mark_contract_processing(contract_id)

        contract = db.fetch_contract(contract_id)
        if contract is None:
            raise RuntimeError(f"Contract {contract_id} not found in database")

        # 1. Extract text via Textract (reads directly from S3)
        full_text = textract_service.extract_text_from_s3_document(s3_key)
        if not full_text.strip():
            raise RuntimeError("Textract returned no text — document may be empty or unreadable")

        # 2. Run the rule-based risk engine
        analysis = risk_engine.analyze_contract_text(full_text)

        # 3. Generate the PDF report
        report_path = report_service.generate_pdf_report(
            contract_filename=contract["original_filename"],
            overall_score=analysis["overall_score"],
            risk_level=analysis["risk_level"],
            clauses=analysis["clauses"],
            total_clauses_scanned=analysis["total_clauses_scanned"],
        )

        report_s3_key = f"reports/{contract_id}/risk-report.pdf"
        s3_service.upload_report(report_path, report_s3_key)

        # 4. Persist results
        db.save_clauses_and_complete(
            contract_id=contract_id,
            clauses=analysis["clauses"],
            overall_score=analysis["overall_score"],
            risk_level=analysis["risk_level"],
            report_s3_key=report_s3_key,
        )

        logger.info("Contract %s processed successfully", contract_id)

    except Exception as exc:  # noqa: BLE001 - we want to catch and record any failure
        logger.error("Contract %s failed: %s\n%s", contract_id, exc, traceback.format_exc())
        try:
            db.mark_contract_failed(contract_id, str(exc))
        except Exception:  # noqa: BLE001
            logger.error("Additionally failed to write failure state for contract %s", contract_id)

    finally:
        for path in (downloaded_path, report_path):
            if path and os.path.exists(path):
                os.remove(path)
        sqs_service.delete_message(receipt_handle)


def main() -> None:
    logger.info(
        "ClauseGuard worker starting. Region=%s Queue=%s",
        WorkerConfig.AWS_REGION,
        WorkerConfig.SQS_QUEUE_URL,
    )

    while True:
        job = sqs_service.receive_message()
        if job is None:
            time.sleep(WorkerConfig.POLL_IDLE_SLEEP_SECONDS)
            continue
        process_job(job)


if __name__ == "__main__":
    main()
