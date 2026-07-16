"""
Authentication utilities for the ClauseGuard web service.

Passwords: hashed with PBKDF2-HMAC-SHA256 with a per-user random salt
(never plaintext, never a fast hash like MD5/SHA1).

Sessions: a compact HMAC-signed token (not a full JWT library dependency)
containing the user id and an expiry timestamp. The server also stores a
hash of each issued token in the `auth_tokens` table so tokens can be
revoked (e.g. on logout) even though they are stateless/self-verifying.
"""

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional, Tuple

from app.config import WebConfig

PBKDF2_ITERATIONS = 260_000
SALT_BYTES = 16


def hash_password(plain_password: str) -> Tuple[str, str]:
    """Returns (password_hash, salt), both hex-encoded."""
    salt = os.urandom(SALT_BYTES)
    derived = hashlib.pbkdf2_hmac(
        "sha256", plain_password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return derived.hex(), salt.hex()


def verify_password(plain_password: str, password_hash: str, salt_hex: str) -> bool:
    salt = bytes.fromhex(salt_hex)
    derived = hashlib.pbkdf2_hmac(
        "sha256", plain_password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return hmac.compare_digest(derived.hex(), password_hash)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def issue_token(user_id: int) -> Tuple[str, str, int]:
    """
    Creates a signed session token for the given user.

    Returns (token, token_hash_for_storage, expires_at_epoch_seconds).
    """
    expires_at = int(time.time()) + WebConfig.AUTH_TOKEN_TTL_HOURS * 3600
    payload = {"uid": user_id, "exp": expires_at}
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_b64 = _b64url_encode(payload_bytes)

    signature = hmac.new(
        WebConfig.SECRET_KEY.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256
    ).digest()
    signature_b64 = _b64url_encode(signature)

    token = f"{payload_b64}.{signature_b64}"
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

    return token, token_hash, expires_at


def verify_token(token: str) -> Optional[int]:
    """
    Verifies the token's signature and expiry. Returns the user_id if valid,
    otherwise None. Does NOT check the revocation list — callers that need
    revocation support (e.g. logout) should also check `auth_tokens.revoked`
    against the stored token hash.
    """
    try:
        payload_b64, signature_b64 = token.split(".", 1)
    except ValueError:
        return None

    expected_signature = hmac.new(
        WebConfig.SECRET_KEY.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256
    ).digest()
    expected_signature_b64 = _b64url_encode(expected_signature)

    if not hmac.compare_digest(signature_b64, expected_signature_b64):
        return None

    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return None

    if payload.get("exp", 0) < time.time():
        return None

    return payload.get("uid")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
