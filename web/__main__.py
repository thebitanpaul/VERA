"""``python -m web`` — development server.

Production uses gunicorn against ``wsgi:app`` (see render.yaml); this entry
point exists only for local work.
"""

from vera.config import force_utf8_streams, settings

from . import create_app

if __name__ == "__main__":
    force_utf8_streams()
    app = create_app()
    print(f"VERA UI -> http://{settings.host}:{settings.port}")
    app.run(host=settings.host, port=settings.port, debug=settings.debug)
