"""The VERA pipeline -- one code path shared by the CLI and the web UI.

    [1] REGISTRY   deterministic: which version governs this trial as of a date
    [2] RETRIEVAL  keyword (+ optional embeddings), filtered to that version
    [3] SYNTHESIS  LLM answers only from the retrieved passages
    [4] VERIFIER   independent grounding + citation audit

Steps [1], [2]'s gates and the abstention decision are deterministic; only the
prose and the claim audit go through a model. That keeps the safety-critical
logic out of the model's hands.

Answers are memoised per process (see :func:`answer_question`). Repeating a
question is free -- the previous implementation billed two LLM calls on every
click of the same button.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import threading
import time
from collections import OrderedDict
from typing import Any

from .backends import (
    BackendError,
    Credentials,
    LLMBackend,
    get_backend,
    resolve_backend_name,
)
from .backends.mock import MockBackend
from .config import settings
from .ingest import build_manifest
from .models import Result
from .registry import _norm_trial_id, build_registry
from .retrieval import retrieve

log = logging.getLogger(__name__)

ABSTAIN = "INSUFFICIENT EVIDENCE — escalating"


# ---------------------------------------------------------------------------
# Manifest access (cached on the file's mtime so edits are picked up)
# ---------------------------------------------------------------------------
_manifest_lock = threading.Lock()
_manifest_cache: tuple[float, dict[str, Any]] | None = None


def load_manifest(force: bool = False) -> dict[str, Any]:
    """Load the ingested manifest, building it on demand if absent.

    Cached: without this, every request re-parsed a multi-megabyte PDF.
    """
    global _manifest_cache
    path = settings.manifest_path
    with _manifest_lock:
        if path.exists():
            mtime = path.stat().st_mtime
            if not force and _manifest_cache and _manifest_cache[0] == mtime:
                return _manifest_cache[1]
            manifest = json.loads(path.read_text(encoding="utf-8"))
            _manifest_cache = (mtime, manifest)
            return manifest

        log.info("manifest missing at %s — building it", path)
        manifest = build_manifest()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        _manifest_cache = (path.stat().st_mtime, manifest)
        return manifest


def trial_versions(manifest: dict, trial_id: str) -> dict[str, dict]:
    """Return ``{version -> record}`` for the given trial (``doc_id == trial_id``)."""
    tid = _norm_trial_id(trial_id)
    for trial in manifest.get("trials", []):
        if str(trial.get("doc_id")) == tid:
            return {str(v["version"]): v for v in trial["versions"]}
    return {}


# ---------------------------------------------------------------------------
# Answer cache -- bounded LRU keyed on everything that can change the answer
# ---------------------------------------------------------------------------
_cache_lock = threading.Lock()
_answer_cache: "OrderedDict[tuple, Result]" = OrderedDict()


def clear_cache() -> None:
    with _cache_lock:
        _answer_cache.clear()


def cache_stats() -> dict[str, int]:
    with _cache_lock:
        return {"entries": len(_answer_cache), "capacity": settings.cache_size}


def _cache_get(key: tuple) -> Result | None:
    if settings.cache_size <= 0:
        return None
    with _cache_lock:
        hit = _answer_cache.get(key)
        if hit is not None:
            _answer_cache.move_to_end(key)
        return hit


def _cache_put(key: tuple, value: Result) -> None:
    if settings.cache_size <= 0:
        return
    with _cache_lock:
        _answer_cache[key] = value
        _answer_cache.move_to_end(key)
        while len(_answer_cache) > settings.cache_size:
            _answer_cache.popitem(last=False)


def _as_cached(res: Result) -> Result:
    """A shallow copy flagged as a cache hit, so usage reporting stays honest."""
    import copy
    clone = copy.copy(res)
    clone.usage = copy.copy(res.usage)
    clone.usage.cached = True
    clone.elapsed_ms = 0
    return clone


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def answer_question(question: str, trial_id: str, as_of: str | None = None,
                    backend_name: str = "auto", *, use_cache: bool = True,
                    backend: LLMBackend | None = None,
                    credentials: "Credentials | None" = None) -> Result:
    """Run steps [1]-[4] and return a structured :class:`Result`.

    No printing and no escalation: the caller decides how to present the result
    and whether to raise a work item.

    ``credentials`` lets a caller supply a provider and API key for this request
    only -- the mechanism behind the UI's model settings. The key is never
    logged, never persisted, and enters the cache key only as a short hash.
    """
    started = time.perf_counter()
    question = question.strip()
    as_of_date = as_of or _dt.date.today().isoformat()

    if backend is not None:
        resolved_name, model_id, key_print = backend.name, backend.model, ""
    elif credentials is not None:
        resolved_name = resolve_backend_name(credentials.provider or backend_name)
        model_id = credentials.model
        key_print = credentials.fingerprint()
    else:
        resolved_name = resolve_backend_name(backend_name)
        model_id = ""
        key_print = ""

    key = (question.lower(), _norm_trial_id(trial_id), as_of_date, resolved_name,
           model_id or settings.gemini_model, settings.anthropic_model, key_print)
    if use_cache and backend is None:
        cached = _cache_get(key)
        if cached is not None:
            log.debug("cache hit for %r", question)
            return _as_cached(cached)

    res = Result(question=question, trial_id=_norm_trial_id(trial_id),
                 as_of=as_of_date)

    # [1] Registry: resolve the governing version deterministically.
    registry = build_registry()
    res.governing = registry.resolve_current_version(trial_id, as_of_date)
    if res.governing is None:
        # Distinguish "you asked before this protocol existed" (the coordinator
        # picked an out-of-range date) from "no such trial" (a real gap). Both
        # abstain, but only the first is the user's input to correct.
        first = registry.first_effective_date(trial_id)
        if first is not None:
            res.reason = (f"{as_of_date} is before Trial {res.trial_id}'s first "
                          f"protocol version took effect on {first}")
            res.reason_kind = "date_before_first_version"
            res.first_effective_date = str(first)
        else:
            res.reason = (f"the registry governs no trial with id "
                          f"{res.trial_id}")
            res.reason_kind = "unknown_trial"
        return _finish(res, started, key, use_cache and backend is None)
    res.historical = registry.is_historical(res.governing)

    # [2] Retrieval, filtered to the current version.
    manifest = load_manifest()
    versions = trial_versions(manifest, trial_id)
    current = versions.get(res.governing.version)
    if current is None:
        res.reason = (f"current version v{res.governing.version} is not present "
                      f"in the ingested manifest")
        return _finish(res, started, key, use_cache and backend is None)
    older = [v for k, v in versions.items() if k != res.governing.version]

    r = retrieve(question, current, older)
    res.confidence = r.confidence
    res.passages = r.passages

    if not r.topic_in_current:
        res.reason = r.reason or "topic not found in the current version"
        return _finish(res, started, key, use_cache and backend is None)
    if r.confidence < settings.low_confidence or not r.passages:
        res.reason = (f"retrieval confidence too low "
                      f"(coverage {r.confidence:.2f} < {settings.low_confidence:.2f})")
        return _finish(res, started, key, use_cache and backend is None)

    # [3] Synthesis + [4] independent verification.
    llm = backend or get_backend(backend_name, credentials)
    res.backend = llm.name
    res.model = llm.model
    try:
        res.answer = llm.synthesize(question, r.passages, res.governing)
        res.verdict = llm.verify(question, res.answer, r.passages)
    except BackendError as exc:
        res.backend_error = exc.display()
        if llm.name == "mock":
            raise
        # Degrade to the deterministic backend so an outage never blocks a
        # coordinator -- but say so loudly rather than pretending it worked.
        log.warning("backend %s failed: %s — falling back to mock", llm.name,
                    exc.display())
        fallback = MockBackend()
        res.backend = f"mock (fallback from {llm.name})"
        res.model = ""
        res.answer = fallback.synthesize(question, r.passages, res.governing)
        res.verdict = fallback.verify(question, res.answer, r.passages)
    finally:
        res.usage = llm.usage

    if not res.verdict.get("grounded") or not res.verdict.get("citations_present"):
        res.reason = ("verifier rejected the answer: "
                      + res.verdict.get("reason", "claims not fully backed by passages"))
        res.unsupported = res.verdict.get("unsupported_claims", [])
        return _finish(res, started, key, use_cache and backend is None)

    res.answered = True
    return _finish(res, started, key, use_cache and backend is None)


def _finish(res: Result, started: float, key: tuple, cacheable: bool) -> Result:
    res.elapsed_ms = int((time.perf_counter() - started) * 1000)
    # A degraded answer is not cached: the next call should get a real attempt
    # once the provider recovers.
    if cacheable and not res.backend_error:
        _cache_put(key, res)
    return res
