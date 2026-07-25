"""Security headers, applied to every response.

This exists because of one specific asset the app handles: a visitor's own LLM
provider API key, entered in the Model settings panel and held in that
browser's ``localStorage`` (see ``static/app.js``). localStorage is readable by
any script running on the page, so the main risk to that key is XSS — a script
tag smuggled into a response. These headers are the server-side defence:

- A strict CSP with a per-request nonce means only the one inline script VERA
  itself renders (the theme-flash guard) can run; any injected `<script>` tag,
  even one reflected back through a bug elsewhere, is refused by the browser.
- ``frame-ancestors 'none'`` stops the page being framed for clickjacking.
- ``Referrer-Policy: no-referrer`` keeps the URL (and by extension the
  question text) out of any Referer header if a passage citation is ever
  rendered as a live link.

``style-src`` still allows ``'unsafe-inline'``: the app sets small inline
``style="..."`` attributes for per-item colours (e.g. a status dot), and
disallowing that is a larger refactor for no security gain — inline *styles*
cannot execute script. ``script-src`` does not carry that allowance.
"""

from __future__ import annotations

import base64
import os

from flask import Flask, g


def _new_nonce() -> str:
    return base64.b64encode(os.urandom(16)).decode("ascii")


def init_security(app: Flask) -> None:
    @app.before_request
    def _assign_nonce() -> None:
        g.csp_nonce = _new_nonce()

    @app.context_processor
    def _inject_nonce() -> dict:
        return {"csp_nonce": getattr(g, "csp_nonce", "")}

    @app.after_request
    def _set_headers(resp):
        nonce = getattr(g, "csp_nonce", "")
        resp.headers["Content-Security-Policy"] = (
            f"default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            f"style-src 'self' 'unsafe-inline'; "
            f"img-src 'self' data:; "
            f"connect-src 'self'; "
            f"font-src 'self'; "
            f"object-src 'none'; "
            f"base-uri 'self'; "
            f"form-action 'self'; "
            f"frame-ancestors 'none'"
        )
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        # Blocks nothing this app uses; explicit is cheap insurance against a
        # future dependency reaching for camera/mic/geolocation by default.
        resp.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )
        return resp
