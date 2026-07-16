"""
Authentication routes: registration, login, logout.
"""

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from app.services import db_service
from app.utils.auth_utils import hash_password, hash_token, issue_token, verify_password
from app.utils.logger import get_logger
from app.utils.validators import is_valid_email, is_valid_full_name, is_valid_password

logger = get_logger(__name__)

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/register", methods=["GET", "POST"])
def register_page():
    if request.method == "GET":
        return render_template("register.html")

    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    full_name = request.form.get("full_name", "").strip()

    if not is_valid_email(email):
        flash("Please enter a valid email address.", "error")
        return render_template("register.html"), 400

    if not is_valid_full_name(full_name):
        flash("Please enter your full name (2-255 characters).", "error")
        return render_template("register.html"), 400

    if not is_valid_password(password):
        flash("Password must be at least 8 characters and include a letter and a number.", "error")
        return render_template("register.html"), 400

    if db_service.get_user_by_email(email) is not None:
        flash("An account with that email already exists.", "error")
        return render_template("register.html"), 409

    password_hash, password_salt = hash_password(password)
    user_id = db_service.create_user(email, password_hash, password_salt, full_name)
    logger.info("New user registered: user_id=%s", user_id)

    flash("Account created successfully. Please log in.", "success")
    return redirect(url_for("auth.login_page"))


@auth_bp.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "GET":
        return render_template("login.html")

    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")

    user = db_service.get_user_by_email(email)
    if user is None or not verify_password(password, user["password_hash"], user["password_salt"]):
        flash("Invalid email or password.", "error")
        return render_template("login.html"), 401

    token, token_hash, expires_at = issue_token(user["id"])
    db_service.store_token(user["id"], token_hash, expires_at)

    session["auth_token"] = token
    session.permanent = True

    logger.info("User logged in: user_id=%s", user["id"])
    return redirect(url_for("dashboard.dashboard_page"))


@auth_bp.route("/logout", methods=["POST"])
def logout():
    token = session.pop("auth_token", None)
    if token:
        db_service.revoke_token(hash_token(token))
    return redirect(url_for("auth.login_page"))
