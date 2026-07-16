"""
Contract routes: upload a new contract, view contract detail/status, and
download the generated PDF risk report via a presigned S3 URL.
"""

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from app.services import db_service, s3_service, sqs_service
from app.utils.decorators import login_required
from app.utils.logger import get_logger
from app.utils.validators import is_allowed_upload_filename, sanitize_filename

logger = get_logger(__name__)

contracts_bp = Blueprint("contracts", __name__)


@contracts_bp.route("/contracts/upload", methods=["GET", "POST"])
@login_required
def upload_contract():
    if request.method == "GET":
        return render_template("upload.html")

    uploaded_file = request.files.get("contract_file")

    if uploaded_file is None or uploaded_file.filename == "":
        flash("Please choose a PDF file to upload.", "error")
        return render_template("upload.html"), 400

    if not is_allowed_upload_filename(uploaded_file.filename):
        flash("Only PDF files are supported.", "error")
        return render_template("upload.html"), 400

    safe_filename = sanitize_filename(uploaded_file.filename)
    user_id = g.current_user["id"]
    s3_key = s3_service.build_contract_s3_key(user_id, safe_filename)

    try:
        s3_service.upload_fileobj(uploaded_file.stream, s3_key)
    except Exception:
        flash("Upload failed. Please try again in a moment.", "error")
        return render_template("upload.html"), 502

    contract_id = db_service.create_contract(user_id, safe_filename, s3_key)

    try:
        sqs_service.enqueue_contract_job(contract_id, s3_key)
    except Exception:
        # The contract row exists but processing could not be queued.
        # It stays in PENDING status; the user can see this on the detail page.
        logger.error("Contract %s created but failed to enqueue job", contract_id)
        flash(
            "Your file was uploaded but analysis could not be started. "
            "Please contact support if this persists.",
            "error",
        )

    return redirect(url_for("contracts.contract_detail", contract_id=contract_id))


@contracts_bp.route("/contracts/<string:contract_id>")
@login_required
def contract_detail(contract_id: str):
    user_id = g.current_user["id"]
    contract = db_service.get_contract_for_user(contract_id, user_id)

    if contract is None:
        flash("Contract not found.", "error")
        return redirect(url_for("dashboard.dashboard_page"))

    clauses = []
    if contract["status"] == "PROCESSED":
        clauses = db_service.list_clauses_for_contract(contract_id, user_id)

    return render_template("contract_detail.html", contract=contract, clauses=clauses)


@contracts_bp.route("/contracts/<string:contract_id>/report")
@login_required
def download_report(contract_id: str):
    user_id = g.current_user["id"]
    contract = db_service.get_contract_for_user(contract_id, user_id)

    if contract is None or contract["status"] != "PROCESSED" or not contract["report_s3_key"]:
        flash("Report is not available yet.", "error")
        return redirect(url_for("contracts.contract_detail", contract_id=contract_id))

    download_name = f"{contract['original_filename']}-risk-report.pdf"
    url = s3_service.generate_presigned_download_url(contract["report_s3_key"], download_name)
    return redirect(url)
