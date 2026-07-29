"""HTTP surface for the VERA web UI.

Every endpoint is a thin adapter over :mod:`vera`. Validation lives here;
pipeline semantics do not.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os

from flask import (
    Blueprint,
    Response,
    current_app,
    jsonify,
    render_template,
    request,
    send_from_directory,
)

from vera import __version__
from vera.backends import (
    BACKEND_CHOICES,
    PROVIDERS,
    Credentials,
    FatalBackendError,
    resolve_backend_name,
    validate_credentials,
)
from vera.config import settings
from vera.escalation import build_work_item, default_title, get_escalation_sink
from vera.pipeline import answer_question, cache_stats, load_manifest, trial_versions
from vera.registry import _norm_trial_id, build_registry

log = logging.getLogger(__name__)
bp = Blueprint("vera_web", __name__)

MAX_QUESTION_CHARS = 2000
MAX_KEY_CHARS = 512

# The public origin, used for the canonical link and the sitemap's <loc>. Both
# have to be absolute, and the app cannot infer the scheme reliably: Render
# terminates TLS at its edge, so gunicorn sees plain http and
# ``request.url_root`` would advertise ``http://`` to crawlers.
PUBLIC_BASE_URL = os.environ.get(
    "VERA_PUBLIC_BASE_URL", "https://vera.phiuture.com").rstrip("/")

# robots.txt and sitemap.xml are the two files a crawler fetches *before* it is
# willing to crawl anything, and Google reads an unreachable robots.txt as
# "disallow everything" — not as "no restrictions". This service runs on a plan
# that idles the instance out, so a cold start can outlast a crawler's patience.
# A long TTL plus ``stale-if-error`` lets the CDN keep answering these two
# requests from cache while the origin is asleep, waking, or failing.
CRAWLER_CACHE_CONTROL = "no-cache, no-store, max-age=0, s-maxage=0, must-revalidate"


def _bad_request(message: str):
    return jsonify({"error": message}), 400


def _json_body() -> dict:
    return request.get_json(silent=True) or {}


def _client_credentials(data: dict) -> tuple[Credentials | None, str | None]:
    """Read an optional per-request provider/key/model from the body.

    Returns ``(credentials, error)``. The key exists only for the life of the
    request: it is never logged, never written to disk, and never echoed back.
    """
    provider = str(data.get("provider") or "").strip()
    api_key = str(data.get("api_key") or "").strip()
    model = str(data.get("model") or "").strip()

    if not (provider or api_key or model):
        return None, None
    if not settings.allow_client_credentials:
        return None, ("this deployment does not accept client-supplied "
                      "credentials (VERA_ALLOW_CLIENT_KEYS=0)")
    if provider and provider not in BACKEND_CHOICES:
        return None, f"provider must be one of {', '.join(BACKEND_CHOICES)}"
    if len(api_key) > MAX_KEY_CHARS:
        return None, "api_key is implausibly long"
    if len(model) > 128:
        return None, "model name is too long"
    return Credentials(provider=provider or "auto", api_key=api_key,
                       model=model), None


@bp.get("/")
def index():
    return render_template("index.html", version=__version__,
                           canonical_url=f"{PUBLIC_BASE_URL}/")


@bp.get("/robots.txt")
def robots():
    """Crawler directives. Must live at the domain root, so it cannot just be a
    file under ``/static``."""
    resp = send_from_directory(current_app.static_folder, "robots.txt",
                               mimetype="text/plain")
    resp.headers["Cache-Control"] = CRAWLER_CACHE_CONTROL
    # send_from_directory adds its own Expires from SEND_FILE_MAX_AGE_DEFAULT,
    # which would contradict the Cache-Control above. Caches prefer
    # Cache-Control, so this only removes a misleading header.
    resp.headers.pop("Expires", None)
    return resp


@bp.get("/sitemap.xml")
def sitemap():
    """This host's own sitemap.

    The parent site's sitemap also lists this subdomain, but a cross-host entry
    is only honoured when both hosts are verified in the same Search Console
    account. Serving a same-host sitemap removes that dependency: the one URL
    here is the whole site — the UI is a single page and everything else under
    ``/api/`` is a JSON surface, not a document.
    """
    try:
        mtime = os.path.getmtime(
            os.path.join(current_app.root_path, "templates", "index.html"))
        lastmod = _dt.date.fromtimestamp(mtime).isoformat()
    except OSError:
        lastmod = _dt.date.today().isoformat()

    body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            '  <url>\n'
            f'    <loc>{PUBLIC_BASE_URL}/</loc>\n'
            f'    <lastmod>{lastmod}</lastmod>\n'
            '    <changefreq>weekly</changefreq>\n'
            '    <priority>0.8</priority>\n'
            '  </url>\n'
            '</urlset>\n')
    resp = Response(body, mimetype="application/xml")
    resp.headers["Cache-Control"] = CRAWLER_CACHE_CONTROL
    return resp


@bp.get("/healthz")
def healthz():
    """Liveness + readiness. Render polls this; it must not call an LLM."""
    try:
        registry = build_registry()
        trials = registry.trial_ids
        manifest_ok = settings.manifest_path.exists()
    except Exception as exc:  # noqa: BLE001 -- health must always answer
        log.exception("health check failed")
        return jsonify({"status": "error", "detail": str(exc)}), 503
    return jsonify({
        "status": "ok",
        "version": __version__,
        "trials": trials,
        "manifest": manifest_ok,
        "backend": resolve_backend_name(settings.backend),
        "cache": cache_stats(),
    })


@bp.get("/api/providers")
def providers():
    """Provider catalogue for the UI's model settings, plus which of them the
    server already has a key for (never the key itself)."""
    return jsonify({
        "allow_client_credentials": settings.allow_client_credentials,
        "server_default": resolve_backend_name(settings.backend),
        "providers": [
            {"id": p.id, "label": p.label, "needs_key": p.needs_key,
             "default_model": p.default_model, "key_url": p.key_url,
             "hint": p.key_hint,
             "server_configured": p.id == "mock" or bool({
                 "gemini": settings.gemini_api_key,
                 "anthropic": settings.anthropic_api_key,
                 "openai": settings.openai_api_key,
                 "xai": settings.xai_api_key,
             }.get(p.id))}
            for p in PROVIDERS
        ],
    })


@bp.post("/api/validate-key")
def validate_key():
    """Check a provider key without spending tokens (a models-list call)."""
    data = _json_body()
    provider = str(data.get("provider") or "").strip()
    api_key = str(data.get("api_key") or "").strip()

    if not settings.allow_client_credentials and api_key:
        return _bad_request("this deployment does not accept client-supplied keys")
    if len(api_key) > MAX_KEY_CHARS:
        return _bad_request("api_key is implausibly long")
    if not provider:
        return _bad_request("provider is required")

    # Fall back to the server's own key so "Test" also verifies a Render secret.
    if not api_key:
        api_key = {
            "gemini": settings.gemini_api_key,
            "anthropic": settings.anthropic_api_key,
            "openai": settings.openai_api_key,
            "xai": settings.xai_api_key,
        }.get(provider, "")

    result = validate_credentials(provider, api_key)
    # Cap the model list: some accounts can see hundreds.
    result["models"] = result.get("models", [])[:200]
    return jsonify(result), (200 if result["ok"] else 400)


@bp.get("/api/trials")
def trials():
    """Trials the registry governs, each with its full version timeline."""
    registry = build_registry()
    as_of = (request.args.get("as_of") or _dt.date.today().isoformat()).strip()
    try:
        _dt.date.fromisoformat(as_of)
    except ValueError:
        return _bad_request("as_of must be an ISO date (YYYY-MM-DD)")

    out = []
    for tid in registry.trial_ids:
        governing = registry.resolve_current_version(tid, as_of)
        first = registry.first_effective_date(tid)
        out.append({
            "id": tid,
            "governing": governing.version if governing else None,
            "historical": registry.is_historical(governing),
            # Lower bound for the date picker: before this, no protocol existed.
            "first_effective_date": str(first) if first else None,
            "versions": [
                {"version": e.version,
                 "status": e.status,
                 "effective_date": str(e.effective_date),
                 "supersedes": e.supersedes,
                 "governing": bool(governing and e.version == governing.version)}
                for e in registry.versions(tid)
            ],
        })
    return jsonify({"as_of": as_of, "trials": out})


@bp.get("/api/trials/<trial_id>/sections")
def sections(trial_id: str):
    """Section headings of the governing version — powers the UI's topic hints."""
    as_of = (request.args.get("as_of") or _dt.date.today().isoformat()).strip()
    governing = build_registry().resolve_current_version(trial_id, as_of)
    if governing is None:
        return jsonify({"trial": _norm_trial_id(trial_id), "sections": []})
    record = trial_versions(load_manifest(), trial_id).get(governing.version, {})
    return jsonify({
        "trial": _norm_trial_id(trial_id),
        "version": governing.version,
        "sections": [{"id": s.get("id"), "heading": s.get("heading")}
                     for s in record.get("sections", [])],
    })


@bp.post("/api/ask")
def ask():
    data = _json_body()
    question = str(data.get("question") or "").strip()
    trial = str(data.get("trial") or "").strip()
    backend = str(data.get("backend") or settings.backend).strip()
    as_of = str(data.get("as_of") or "").strip() or None

    if not question or not trial:
        return _bad_request("question and trial are required")
    if len(question) > MAX_QUESTION_CHARS:
        return _bad_request(f"question exceeds {MAX_QUESTION_CHARS} characters")
    if backend not in BACKEND_CHOICES:
        return _bad_request(f"backend must be one of {', '.join(BACKEND_CHOICES)}")
    if as_of:
        try:
            _dt.date.fromisoformat(as_of)
        except ValueError:
            return _bad_request("as_of must be an ISO date (YYYY-MM-DD)")

    credentials, cred_error = _client_credentials(data)
    if cred_error:
        return _bad_request(cred_error)

    try:
        res = answer_question(question, trial, as_of, backend,
                              credentials=credentials)
    except FatalBackendError as exc:
        # Configuration problem, not a protocol problem — say which.
        return jsonify({"error": exc.display(), "kind": "backend_config"}), 502
    except Exception as exc:  # noqa: BLE001 -- last resort; never leak a traceback
        log.exception("unhandled failure answering %r", question)
        detail = str(exc) if current_app.debug else "internal error"
        return jsonify({"error": f"{type(exc).__name__}: {detail}"}), 500

    return jsonify(res.to_dict())


@bp.post("/api/escalate")
def escalate():
    data = _json_body()
    question = str(data.get("question") or "").strip()
    trial = str(data.get("trial") or "").strip()
    reason = str(data.get("reason") or "").strip()
    if not question or not trial:
        return _bad_request("question and trial are required")

    item = build_work_item(default_title(trial), trial, question, reason)
    try:
        result = get_escalation_sink().send(item)
    except OSError as exc:
        log.exception("escalation sink failed")
        return jsonify({"error": f"could not raise the work item: {exc}"}), 500
    return jsonify(result)
