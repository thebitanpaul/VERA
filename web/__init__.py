"""Flask application factory for the VERA web UI.

The UI is a thin front-end over the real pipeline: it calls
:func:`vera.answer_question` and VERA's escalation sinks. Nothing about the
pipeline is re-implemented here, so the CLI and the browser cannot drift apart.
"""

from __future__ import annotations

import logging

from flask import Flask

from vera.config import settings


def create_app(config: dict | None = None) -> Flask:
    """Build the WSGI application. ``wsgi.py`` and ``python -m web`` both use this."""
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config.update(
        JSON_SORT_KEYS=False,
        # Protocol answers must never be served stale from a proxy.
        SEND_FILE_MAX_AGE_DEFAULT=0 if settings.debug else 3600,
        MAX_CONTENT_LENGTH=64 * 1024,   # a question is small; reject anything else
    )
    if config:
        app.config.update(config)

    logging.basicConfig(
        level=logging.DEBUG if settings.debug else logging.INFO,
        format="[%(levelname)s] %(name)s: %(message)s")

    from .routes import bp
    app.register_blueprint(bp)
    return app


__all__ = ["create_app"]
