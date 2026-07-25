"""WSGI entry point.

    gunicorn wsgi:app

Render's start command targets this module. Keeping it at the repo root means
the platform needs no PYTHONPATH juggling to find the ``vera`` and ``web``
packages.
"""

from web import create_app

app = create_app()

if __name__ == "__main__":
    from vera.config import settings
    app.run(host=settings.host, port=settings.port, debug=settings.debug)
