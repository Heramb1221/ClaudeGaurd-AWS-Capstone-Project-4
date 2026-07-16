"""
Shared route decorators.
"""

from functools import wraps

from flask import g, redirect, session, url_for

from app.services import db_service
from app.utils.auth_utils import hash_token, verify_token
from app.utils.logger import get_logger

logger = get_logger(__name__)


def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        token = session.get("auth_token")
        user_id = verify_token(token) if token else None

        if user_id is None:
            session.pop("auth_token", None)
            return redirect(url_for("auth.login_page"))

        if db_service.is_token_revoked(hash_token(token)):
            session.pop("auth_token", None)
            return redirect(url_for("auth.login_page"))

        user = db_service.get_user_by_id(user_id)
        if user is None:
            session.pop("auth_token", None)
            return redirect(url_for("auth.login_page"))

        g.current_user = user
        return view_func(*args, **kwargs)

    return wrapped
