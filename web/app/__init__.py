"""
Flask application factory for the ClauseGuard web service.
"""

from flask import Flask

from app.config import WebConfig
from app.utils.logger import get_logger

logger = get_logger(__name__)


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(WebConfig)

    # Basic security headers on every response
    @app.after_request
    def set_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response

    from app.routes.auth import auth_bp
    from app.routes.contracts import contracts_bp
    from app.routes.dashboard import dashboard_bp

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(contracts_bp)

    @app.route("/healthz")
    def healthz():
        # Used by the ALB target group health check
        return {"status": "ok"}, 200

    @app.errorhandler(404)
    def not_found(_error):
        return {"error": "Not found"}, 404

    @app.errorhandler(500)
    def server_error(error):
        logger.error("Unhandled server error: %s", error)
        return {"error": "Internal server error"}, 500

    logger.info("ClauseGuard web app initialized")
    return app
