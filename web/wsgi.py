"""
WSGI entrypoint. Gunicorn (used in the Docker image) imports `app` from
this module: `gunicorn wsgi:app`.
"""

from app import create_app

app = create_app()

if __name__ == "__main__":
    # Local development only — production runs via gunicorn (see Dockerfile)
    app.run(host="0.0.0.0", port=5000, debug=False)
