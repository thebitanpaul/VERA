"""HTTP surface for the VERA web UI.

Every endpoint is a thin adapter over :mod:`vera`. Validation lives here;
pipeline semantics do not.
"""

from __future__ import annotations

import datetime as _dt
import logging

from flask import (
    Blueprint,
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
    return render_template("index.html", version=__version__)


@bp.get("/robots.txt")
def robots():
    """Crawler directives. Must live at the domain root, so it cannot just be a
    file under ``/static`` — it points crawlers at the sitemap on the parent
    site, which is what lists this subdomain."""
    return send_from_directory(current_app.static_folder, "robots.txt",
                               mimetype="text/plain")


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
