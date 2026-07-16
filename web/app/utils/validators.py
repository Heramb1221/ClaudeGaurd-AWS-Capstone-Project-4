"""
Input validation helpers used across routes.
"""

import os
import re

from app.config import WebConfig

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_email(email: str) -> bool:
    return bool(email) and bool(EMAIL_PATTERN.match(email.strip()))


def is_valid_password(password: str) -> bool:
    """Minimum 8 characters, at least one letter and one digit."""
    if not password or len(password) < 8:
        return False
    has_letter = any(c.isalpha() for c in password)
    has_digit = any(c.isdigit() for c in password)
    return has_letter and has_digit


def is_valid_full_name(name: str) -> bool:
    return bool(name) and 2 <= len(name.strip()) <= 255


def is_allowed_upload_filename(filename: str) -> bool:
    if not filename:
        return False
    _, ext = os.path.splitext(filename.lower())
    return ext in WebConfig.ALLOWED_UPLOAD_EXTENSIONS


def sanitize_filename(filename: str) -> str:
    """Strips directory components and disallowed characters from an uploaded filename."""
    base = os.path.basename(filename)
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    return base[:255] if base else "upload.pdf"
