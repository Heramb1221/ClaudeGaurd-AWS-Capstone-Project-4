"""
Dashboard routes: the landing page and the authenticated dashboard showing
contract history and risk trends.
"""

from flask import Blueprint, g, redirect, render_template, url_for

from app.services import db_service
from app.utils.decorators import login_required

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
def index():
    return redirect(url_for("dashboard.dashboard_page"))


@dashboard_bp.route("/dashboard")
@login_required
def dashboard_page():
    user_id = g.current_user["id"]
    summary = db_service.get_dashboard_summary(user_id)
    contracts = db_service.list_contracts_for_user(user_id)
    return render_template(
        "dashboard.html", summary=summary, contracts=contracts, user=g.current_user
    )
