"""Backend contract tests, with the emphasis on the cost guardrails.

These encode the bug this codebase actually hit: an uncapped Gemini request
whose whole output budget went to reasoning, returning a candidate with no
``parts`` — which the old client swallowed into a silent mock fallback.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from vera.backends import get_backend, resolve_backend_name
from vera.backends.base import (
    FatalBackendError,
    RetryableBackendError,
    with_retries,
)
from vera.backends.gemini import GeminiBackend
from vera.backends.mock import MockBackend
from vera.models import Passage
from vera.registry import RegistryEntry
import datetime as dt

GOVERNING = RegistryEntry("412", "3.0", dt.date(2025, 3, 1), "active", "2.0", "x.md")
PASSAGES = [Passage("412", "3.0", "5.2", "Concomitant Metformin",
                    "Metformin is permitted up to 2000 mg/day.",
                    "2025-03-01", "active")]


# --------------------------------------------------------------- selection
def test_auto_falls_back_to_mock_without_keys():
    assert resolve_backend_name("auto") == "mock"
    assert get_backend("auto").name == "mock"


def test_auto_prefers_gemini_when_a_key_is_present(monkeypatch):
    from vera import config
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")
    cfg = config.reload_settings()
    monkeypatch.setattr("vera.backends.settings", cfg)
    assert resolve_backend_name("auto") == "gemini"


def test_unknown_backend_is_fatal():
    with pytest.raises(FatalBackendError):
        get_backend("llama")


def test_explicit_gemini_without_a_key_is_fatal():
    with pytest.raises(FatalBackendError, match="GEMINI_API_KEY"):
        get_backend("gemini")


# ------------------------------------------------------------------- mock
def test_mock_is_grounded_and_cited():
    b = MockBackend()
    answer = b.synthesize("Is metformin permitted?", PASSAGES, GOVERNING)
    assert "2000 mg/day" in answer
    assert "Trial 412 v3.0 §5.2" in answer
    verdict = b.verify("q", answer, PASSAGES)
    assert verdict["grounded"] and verdict["citations_present"]


# ---------------------------------------------------------- model guardrail
@pytest.mark.parametrize("model", ["gemini-2.0-flash", "gemini-2.5-flash",
                                   "gemini-1.5-pro"])
def test_retired_free_tier_models_are_rejected_before_any_call(model):
    """These ids return 429 'limit: 0'. Failing at construction means the user
    gets the real explanation instead of a bogus 'quota exhausted'."""
    with pytest.raises(FatalBackendError, match="free tier"):
        GeminiBackend(api_key="AIza-test", model=model)


# ----------------------------------------------------- response extraction
def _gemini(monkeypatch, payload):
    b = GeminiBackend(api_key="AIza-test", model="gemini-flash-latest")
    monkeypatch.setattr(b, "_post", lambda body: payload)
    return b


def test_thinking_starvation_raises_instead_of_returning_empty(monkeypatch):
    """The exact failure that used to burn tokens and return nothing."""
    b = _gemini(monkeypatch, {
        "candidates": [{"finishReason": "MAX_TOKENS", "content": {"role": "model"}}],
        "usageMetadata": {"promptTokenCount": 900, "thoughtsTokenCount": 2048},
    })
    with pytest.raises(FatalBackendError, match="reasoning"):
        b.synthesize("q", PASSAGES, GOVERNING)
    assert b.usage.thinking_tokens == 2048     # the spend is still reported


def test_blocked_prompt_is_fatal(monkeypatch):
    b = _gemini(monkeypatch, {"promptFeedback": {"blockReason": "SAFETY"}})
    with pytest.raises(FatalBackendError, match="blocked"):
        b.synthesize("q", PASSAGES, GOVERNING)


def test_successful_call_records_usage(monkeypatch):
    b = _gemini(monkeypatch, {
        "candidates": [{"finishReason": "STOP",
                        "content": {"parts": [{"text": "Permitted."}]}}],
        "usageMetadata": {"promptTokenCount": 800, "candidatesTokenCount": 40,
                          "thoughtsTokenCount": 60},
    })
    assert b.synthesize("q", PASSAGES, GOVERNING) == "Permitted."
    assert (b.usage.input_tokens, b.usage.output_tokens,
            b.usage.thinking_tokens) == (800, 40, 60)
    assert b.usage.total_tokens == 900


def test_generation_config_always_caps_output_and_thinking():
    b = GeminiBackend(api_key="AIza-test", model="gemini-flash-latest")
    gen = b._generation_config(2048, None)
    assert gen["maxOutputTokens"] == 2048
    assert gen["thinkingConfig"] == {"thinkingLevel": "low"}
    assert gen["temperature"] == 0.0


# ----------------------------------------------------- error classification
def _http_error(code, message):
    body = json.dumps({"error": {"code": code, "message": message}}).encode()
    import io
    return urllib.error.HTTPError("u", code, "err", {}, io.BytesIO(body))


@pytest.mark.parametrize("code,message,expected", [
    (429, "Quota exceeded ...\n* limit: 0, model: x", FatalBackendError),
    (429, "Quota exceeded ...\n* limit: 15, model: x", RetryableBackendError),
    (503, "overloaded", RetryableBackendError),
    (500, "boom", RetryableBackendError),
    (401, "bad key", FatalBackendError),
    (403, "forbidden", FatalBackendError),
    (404, "no such model", FatalBackendError),
    (400, "API key not valid", FatalBackendError),
])
def test_http_errors_classified(code, message, expected):
    b = GeminiBackend(api_key="AIza-test", model="gemini-flash-latest")
    assert isinstance(b._classify(_http_error(code, message)), expected)


def test_zero_limit_429_is_not_retried():
    """Retrying a retired model costs latency and cannot succeed."""
    b = GeminiBackend(api_key="AIza-test", model="gemini-flash-latest")
    err = b._classify(_http_error(429, "Quota exceeded\n* limit: 0, model: x"))
    assert isinstance(err, FatalBackendError)
    assert "quota of 0" in str(err)


# -------------------------------------------------------------- retry loop
def test_with_retries_stops_after_the_configured_budget(monkeypatch):
    from vera.backends import base
    monkeypatch.setattr(base.time, "sleep", lambda s: None)
    calls = []

    def always_fails():
        calls.append(1)
        raise RetryableBackendError("429")

    with pytest.raises(RetryableBackendError):
        with_retries(always_fails, what="test")
    assert len(calls) == base.settings.max_retries + 1


def test_with_retries_does_not_retry_fatal(monkeypatch):
    from vera.backends import base
    monkeypatch.setattr(base.time, "sleep", lambda s: None)
    calls = []

    def fatal():
        calls.append(1)
        raise FatalBackendError("bad key")

    with pytest.raises(FatalBackendError):
        with_retries(fatal, what="test")
    assert len(calls) == 1


def test_with_retries_returns_on_recovery(monkeypatch):
    from vera.backends import base
    monkeypatch.setattr(base.time, "sleep", lambda s: None)
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise RetryableBackendError("429")
        return "ok"

    assert with_retries(flaky, what="test") == "ok"
    assert attempts["n"] == 2
