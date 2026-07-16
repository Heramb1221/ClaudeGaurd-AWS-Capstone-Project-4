"""
Database access layer for the web service.

Uses boto3 DynamoDB directly (no ORM, no SQL). All state is stored in three
DynamoDB tables:

  clauseguard-users       PK=email, GSI: user_id-index (PK=user_id)
  clauseguard-contracts   PK=user_id, SK=contract_id, GSI: contract_id-index
  clauseguard-tokens      PK=token_hash, TTL=expires_at
"""

from datetime import datetime
from decimal import Decimal
from typing import List, Optional
import uuid

import boto3
from boto3.dynamodb.conditions import Key

from app.config import WebConfig

_resource = boto3.resource("dynamodb", region_name=WebConfig.AWS_REGION)

_users_table = _resource.Table("clauseguard-users")
_contracts_table = _resource.Table("clauseguard-contracts")
_tokens_table = _resource.Table("clauseguard-tokens")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _parse_iso(val: Optional[str]) -> Optional[datetime]:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val)
    except ValueError:
        return None


def _decimal_to_float(obj):
    """Recursively convert Decimal → float so callers get plain Python types."""
    if isinstance(obj, list):
        return [_decimal_to_float(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _decimal_to_float(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return float(obj)
    return obj


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def create_user(email: str, password_hash: str, password_salt: str, full_name: str) -> str:
    """Creates a new user and returns the generated user_id (UUID string)."""
    user_id = str(uuid.uuid4())
    _users_table.put_item(Item={
        "email": email.lower().strip(),
        "user_id": user_id,
        "password_hash": password_hash,
        "password_salt": password_salt,
        "full_name": full_name.strip(),
        "created_at": _now_iso(),
    })
    return user_id


def get_user_by_email(email: str) -> Optional[dict]:
    response = _users_table.get_item(Key={"email": email.lower().strip()})
    item = response.get("Item")
    if item is None:
        return None
    return {
        "id": item["user_id"],
        "email": item["email"],
        "password_hash": item["password_hash"],
        "password_salt": item["password_salt"],
        "full_name": item["full_name"],
    }


def get_user_by_id(user_id: str) -> Optional[dict]:
    response = _users_table.query(
        IndexName="user_id-index",
        KeyConditionExpression=Key("user_id").eq(user_id),
        Limit=1,
    )
    items = response.get("Items", [])
    if not items:
        return None
    item = items[0]
    return {"id": item["user_id"], "email": item["email"], "full_name": item["full_name"]}


# ---------------------------------------------------------------------------
# Auth tokens
# ---------------------------------------------------------------------------

def store_token(user_id: str, token_hash: str, expires_at_epoch: int) -> None:
    _tokens_table.put_item(Item={
        "token_hash": token_hash,
        "user_id": user_id,
        "expires_at": expires_at_epoch,  # DynamoDB TTL attribute (epoch seconds)
        "revoked": False,
    })


def is_token_revoked(token_hash: str) -> bool:
    response = _tokens_table.get_item(Key={"token_hash": token_hash})
    item = response.get("Item")
    # Unknown token → treat as revoked (defensive default)
    if item is None:
        return True
    return bool(item.get("revoked", False))


def revoke_token(token_hash: str) -> None:
    _tokens_table.update_item(
        Key={"token_hash": token_hash},
        UpdateExpression="SET revoked = :v",
        ExpressionAttributeValues={":v": True},
    )


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------

def create_contract(user_id: str, original_filename: str, s3_key: str) -> str:
    """Creates a new contract and returns the generated contract_id (UUID string)."""
    contract_id = str(uuid.uuid4())
    _contracts_table.put_item(Item={
        "user_id": user_id,
        "contract_id": contract_id,
        "original_filename": original_filename,
        "s3_key": s3_key,
        "report_s3_key": None,
        "status": "PENDING",
        "overall_risk_score": None,
        "risk_level": None,
        "error_message": None,
        "created_at": _now_iso(),
        "processed_at": None,
        "clauses": [],
    })
    return contract_id


def list_contracts_for_user(user_id: str) -> List[dict]:
    response = _contracts_table.query(
        KeyConditionExpression=Key("user_id").eq(user_id),
    )
    items = _decimal_to_float(response.get("Items", []))
    # Sort newest first
    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return [
        {
            "id": item["contract_id"],
            "original_filename": item["original_filename"],
            "status": item["status"],
            "overall_risk_score": item.get("overall_risk_score"),
            "risk_level": item.get("risk_level"),
            "created_at": _parse_iso(item.get("created_at")),
            "processed_at": _parse_iso(item.get("processed_at")),
        }
        for item in items
    ]


def get_contract_for_user(contract_id: str, user_id: str) -> Optional[dict]:
    response = _contracts_table.get_item(
        Key={"user_id": user_id, "contract_id": contract_id}
    )
    item = response.get("Item")
    if item is None:
        return None
    item = _decimal_to_float(item)
    return {
        "id": item["contract_id"],
        "user_id": item["user_id"],
        "original_filename": item["original_filename"],
        "s3_key": item["s3_key"],
        "report_s3_key": item.get("report_s3_key"),
        "status": item["status"],
        "overall_risk_score": item.get("overall_risk_score"),
        "risk_level": item.get("risk_level"),
        "error_message": item.get("error_message"),
        "created_at": _parse_iso(item.get("created_at")),
        "processed_at": _parse_iso(item.get("processed_at")),
    }


def list_clauses_for_contract(contract_id: str, user_id: str) -> List[dict]:
    response = _contracts_table.get_item(
        Key={"user_id": user_id, "contract_id": contract_id}
    )
    item = response.get("Item")
    if item is None:
        return []
    clauses = _decimal_to_float(item.get("clauses", []))
    # Sort by score descending, then clause_index ascending (mirrors original SQL query)
    clauses.sort(key=lambda c: (-c.get("score", 0), c.get("clause_index", 0)))
    return clauses


def get_dashboard_summary(user_id: str) -> dict:
    """Aggregate stats computed from the contracts query result."""
    response = _contracts_table.query(
        KeyConditionExpression=Key("user_id").eq(user_id),
    )
    items = _decimal_to_float(response.get("Items", []))

    total_contracts = len(items)
    processed = [i for i in items if i["status"] == "PROCESSED"]
    processed_contracts = len(processed)
    high_risk_contracts = sum(1 for i in items if i.get("risk_level") == "HIGH")
    scores = [i["overall_risk_score"] for i in processed if i.get("overall_risk_score") is not None]
    avg_risk_score = sum(scores) / len(scores) if scores else 0.0

    processed.sort(key=lambda x: x.get("processed_at") or "", reverse=True)
    recent_processed = [
        {
            "id": i["contract_id"],
            "original_filename": i["original_filename"],
            "overall_risk_score": i.get("overall_risk_score"),
            "risk_level": i.get("risk_level"),
            "processed_at": _parse_iso(i.get("processed_at")),
        }
        for i in processed[:10]
    ]

    return {
        "total_contracts": total_contracts,
        "processed_contracts": processed_contracts,
        "high_risk_contracts": high_risk_contracts,
        "avg_risk_score": avg_risk_score,
        "recent_processed": recent_processed,
    }
