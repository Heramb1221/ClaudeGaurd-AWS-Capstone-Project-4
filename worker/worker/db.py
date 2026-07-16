"""
Database access layer for the worker.

Uses boto3 DynamoDB directly (no ORM, no SQL). The worker performs a small,
well-defined set of operations: read a contract's file location, update
status, and write clause results back to the contract item.

Table: clauseguard-contracts  PK=user_id, SK=contract_id
  GSI: contract_id-index  PK=contract_id  — lets the worker look up by
  contract_id without knowing user_id (the SQS message only carries contract_id).
"""

from datetime import datetime
from decimal import Decimal
from typing import List, Optional

import boto3
from boto3.dynamodb.conditions import Key

from worker.config import WorkerConfig
from worker.logger import get_logger

logger = get_logger(__name__)

_resource = boto3.resource("dynamodb", region_name=WorkerConfig.AWS_REGION)
_contracts_table = _resource.Table("clauseguard-contracts")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _to_decimal(value) -> Decimal:
    """Convert float/int to Decimal for DynamoDB storage."""
    return Decimal(str(value))


# ---------------------------------------------------------------------------
# Public API (same signatures as the original psycopg2 version)
# ---------------------------------------------------------------------------

def fetch_contract(contract_id: str) -> Optional[dict]:
    """
    Looks up a contract by contract_id using the GSI.
    Returns a dict with id, user_id, original_filename, s3_key, status, or None.
    """
    response = _contracts_table.query(
        IndexName="contract_id-index",
        KeyConditionExpression=Key("contract_id").eq(contract_id),
        Limit=1,
    )
    items = response.get("Items", [])
    if not items:
        return None
    item = items[0]
    return {
        "id": item["contract_id"],
        "user_id": item["user_id"],
        "original_filename": item["original_filename"],
        "s3_key": item["s3_key"],
        "status": item["status"],
    }


def mark_contract_processing(contract_id: str) -> None:
    contract = fetch_contract(contract_id)
    if not contract:
        logger.warning("mark_contract_processing: contract %s not found", contract_id)
        return
    _contracts_table.update_item(
        Key={"user_id": contract["user_id"], "contract_id": contract_id},
        UpdateExpression="SET #s = :v",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":v": "PROCESSING"},
    )


def mark_contract_failed(contract_id: str, error_message: str) -> None:
    contract = fetch_contract(contract_id)
    if not contract:
        logger.warning("mark_contract_failed: contract %s not found", contract_id)
        return
    _contracts_table.update_item(
        Key={"user_id": contract["user_id"], "contract_id": contract_id},
        UpdateExpression="SET #s = :s, error_message = :e, processed_at = :t",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": "FAILED",
            ":e": error_message[:2000],
            ":t": _now_iso(),
        },
    )
    logger.info("Contract %s marked FAILED", contract_id)


def save_clauses_and_complete(
    contract_id: str,
    clauses: List[dict],
    overall_score: float,
    risk_level: str,
    report_s3_key: str,
) -> None:
    """
    Writes all clause data and marks the contract as PROCESSED in a single
    update_item call, keeping the operation atomic (no partial states visible).

    Clause dicts must have keys:
      clause_index, clause_text, category, severity, score, explanation
    """
    contract = fetch_contract(contract_id)
    if not contract:
        logger.warning("save_clauses_and_complete: contract %s not found", contract_id)
        return

    # Convert float scores to Decimal for DynamoDB
    dynamo_clauses = [
        {
            "clause_index": c["clause_index"],
            "clause_text": c["clause_text"],
            "category": c["category"],
            "severity": c["severity"],
            "score": _to_decimal(c["score"]),
            "explanation": c["explanation"],
        }
        for c in clauses
    ]

    _contracts_table.update_item(
        Key={"user_id": contract["user_id"], "contract_id": contract_id},
        UpdateExpression=(
            "SET #s = :s, overall_risk_score = :score, risk_level = :rl, "
            "report_s3_key = :rk, processed_at = :t, error_message = :null, "
            "clauses = :clauses"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": "PROCESSED",
            ":score": _to_decimal(overall_score),
            ":rl": risk_level,
            ":rk": report_s3_key,
            ":t": _now_iso(),
            ":null": None,
            ":clauses": dynamo_clauses,
        },
    )
    logger.info("Contract %s marked PROCESSED with %d clauses", contract_id, len(clauses))
