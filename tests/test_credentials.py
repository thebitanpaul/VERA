"""Runtime provider selection and bring-your-own-key.

The point of this feature is that a deployed instance can switch provider or
rotate a key with no redeploy. The point of these tests is that doing so never
leaks the key.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import urllib.error

import pytest

from vera.backends import (
    BACKENDS,
    PROVIDERS,
    Credentials,
    FatalBackendError,
    RetryableBackendError,
    get_backend,
    validate_credentials,
)
from vera.backends.openai_compat import OpenAIBackend, XAIBackend
from vera.models import Passage
from vera.pipeline import answer_question, cache_stats, clear_cache
from vera.registry import RegistryEntry

GOVERNING = RegistryEntry("412", "3.0", dt.date(2025, 3, 1), "active", "2.0", "x.md")
PASSAGES = [Passage("412", "3.0", "5.2", "Concomitant Metformin",
                    "Metformin is permitted up to 2000 mg/day.",
                    "2025-03-01", "active")]


# ------------------------------------------------------------- provider list
def test_every_provider_has_a_backend():
    for p in PROVIDERS:
        assert p.id in BACKENDS
    assert {p.id for p in PROVIDERS} == set(BACKENDS)


def test_only_mock_needs_no_key():
    assert [p.id for p in PROVIDERS if not p.needs_key] == ["mock"]


# --------------------------------------------------------------- credentials
def test_fingerprint_is_short_and_not_the_key():
    c = Credentials("gemini", "super-secret-key")
    fp = c.fingerprint()
    assert len(fp) == 12
    assert "secret" not in fp
    assert fp != c.api_key
    assert Credentials("gemini", "super-secret-key").fingerprint() == fp


def test_empty_key_has_no_fingerprint():
    assert Credentials("mock", "").fingerprint() == ""


def test_credentials_select_the_backend():
    b = get_backend("auto", Credentials("openai", "sk-test", "gpt-4o-mini"))
    assert b.name == "openai"
    assert b.model == "gpt-4o-mini"


def test_credentials_model_override():
    b = get_backend("auto", Credentials("xai", "xai-test", "grok-4"))
    assert b.name == "xai"
    assert b.model == "grok-4"


def test_credentials_without_a_key_are_fatal_for_a_real_provider():
    with pytest.raises(FatalBackendError):
        get_backend("openai", Credentials("openai", ""))


def test_mock_provider_ignores_a_supplied_key():
    b = get_backend("auto", Credentials("mock", "sk-whatever"))
    assert b.name == "mock"


# --------------------------------------------------------------- cache split
def test_different_keys_do_not_share_cached_answers():
    """Two sessions with different keys must not read each other's entries."""
    clear_cache()
    q = "Is concomitant metformin permitted?"
    answer_question(q, "412", credentials=Credentials("mock", "key-A"))
    answer_question(q, "412", credentials=Credentials("mock", "key-B"))
    assert cache_stats()["entries"] == 2


def test_same_key_reuses_the_cache():
    clear_cache()
    q = "Is concomitant metformin permitted?"
    creds = Credentials("mock", "key-A")
    answer_question(q, "412", credentials=creds)
    second = answer_question(q, "412", credentials=creds)
    assert second.usage.cached
    assert cache_stats()["entries"] == 1


def test_result_never_carries_the_key():
    res = answer_question("Is concomitant metformin permitted?", "412",
                          credentials=Credentials("mock", "sk-do-not-leak"))
    assert "sk-do-not-leak" not in json.dumps(res.to_dict())


# ------------------------------------------------ openai-compatible backends
def _http_error(code, message):
    body = json.dumps({"error": {"message": message}}).encode()
    return urllib.error.HTTPError("u", code, "err", {}, io.BytesIO(body))


@pytest.mark.parametrize("cls,expected_host", [
    (OpenAIBackend, "api.openai.com"),
    (XAIBackend, "api.x.ai"),
])
def test_base_urls(cls, expected_host):
    assert expected_host in cls.base_url


def test_openai_records_usage_including_reasoning(monkeypatch):
    b = OpenAIBackend(api_key="sk-test", model="gpt-4o-mini")
    monkeypatch.setattr(b, "_post", lambda body: {
        "choices": [{"finish_reason": "stop",
                     "message": {"content": "Permitted."}}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 30,
                  "completion_tokens_details": {"reasoning_tokens": 120}},
    })
    assert b.synthesize("q", PASSAGES, GOVERNING) == "Permitted."
    assert b.usage.input_tokens == 900
    assert b.usage.output_tokens == 30
    assert b.usage.thinking_tokens == 120


def test_openai_reasoning_starvation_raises(monkeypatch):
    """Same guardrail as Gemini: budget spent on reasoning, no visible answer."""
    b = OpenAIBackend(api_key="sk-test", model="o4-mini")
    monkeypatch.setattr(b, "_post", lambda body: {
        "choices": [{"finish_reason": "length", "message": {"content": ""}}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 2048,
                  "completion_tokens_details": {"reasoning_tokens": 2048}},
    })
    with pytest.raises(FatalBackendError, match="reasoning"):
        b.synthesize("q", PASSAGES, GOVERNING)


def test_openai_always_caps_output():
    b = OpenAIBackend(api_key="sk-test")
    sent = {}
    b._post = lambda body: (sent.update(body) or {
        "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]})
    b.synthesize("q", PASSAGES, GOVERNING)
    assert sent["max_tokens"] > 0
    assert sent["temperature"] == 0.0


@pytest.mark.parametrize("code,message,expected", [
    (429, "Rate limit reached", RetryableBackendError),
    (429, "You exceeded your current quota", FatalBackendError),
    (401, "Incorrect API key provided", FatalBackendError),
    (404, "The model does not exist", FatalBackendError),
    (503, "overloaded", RetryableBackendError),
])
def test_openai_error_classification(code, message, expected):
    b = OpenAIBackend(api_key="sk-test")
    assert isinstance(b._classify(_http_error(code, message)), expected)


def test_verify_tolerates_a_fenced_json_block(monkeypatch):
    b = XAIBackend(api_key="xai-test")
    fenced = '```json\n{"grounded": true, "unsupported_claims": [], ' \
             '"citations_present": true, "reason": "ok"}\n```'
    monkeypatch.setattr(b, "_post", lambda body: {
        "choices": [{"finish_reason": "stop", "message": {"content": fenced}}]})
    verdict = b.verify("q", "a", PASSAGES)
    assert verdict["grounded"] is True


# ------------------------------------------------------------ key validation
def test_validate_mock_needs_no_key():
    assert validate_credentials("mock", "")["ok"] is True


def test_validate_unknown_provider():
    result = validate_credentials("llama", "x")
    assert result["ok"] is False
    assert "Unknown provider" in result["detail"]


def test_validate_requires_a_key():
    result = validate_credentials("openai", "")
    assert result["ok"] is False
    assert "No API key" in result["detail"]


# ------------------------------------------------------------- HTTP surface
def test_providers_endpoint(client):
    d = client.get("/api/providers").get_json()
    assert d["server_default"] == "mock"
    assert d["allow_client_credentials"] is True
    ids = [p["id"] for p in d["providers"]]
    assert ids == ["mock", "gemini", "openai", "xai", "anthropic"]
    # The catalogue must describe keys, never expose one.
    assert "api_key" not in json.dumps(d)


def test_ask_accepts_a_provider_override(client):
    d = client.post("/api/ask", json={
        "question": "Is concomitant metformin permitted?",
        "trial": "412", "provider": "mock"}).get_json()
    assert d["answered"] is True
    assert d["backend"] == "mock"


def test_ask_rejects_an_unknown_provider(client):
    r = client.post("/api/ask", json={
        "question": "q", "trial": "412", "provider": "llama"})
    assert r.status_code == 400


def test_ask_rejects_an_absurd_key(client):
    r = client.post("/api/ask", json={
        "question": "q", "trial": "412", "provider": "gemini",
        "api_key": "x" * 5000})
    assert r.status_code == 400


def test_ask_never_echoes_the_key(client):
    body = client.post("/api/ask", json={
        "question": "Is concomitant metformin permitted?", "trial": "412",
        "provider": "mock", "api_key": "sk-secret-value"}).get_data(as_text=True)
    assert "sk-secret-value" not in body


def test_client_keys_can_be_disabled(client, monkeypatch):
    import dataclasses

    import web.routes as routes
    # Settings is frozen, so rebuild it rather than mutating in place.
    locked = dataclasses.replace(routes.settings, allow_client_credentials=False)
    monkeypatch.setattr(routes, "settings", locked)
    r = client.post("/api/ask", json={"question": "q", "trial": "412",
                                      "provider": "gemini", "api_key": "k"})
    assert r.status_code == 400
    assert "does not accept" in r.get_json()["error"]


def test_validate_key_endpoint_for_mock(client):
    d = client.post("/api/validate-key", json={"provider": "mock"}).get_json()
    assert d["ok"] is True


def test_validate_key_endpoint_requires_provider(client):
    assert client.post("/api/validate-key", json={}).status_code == 400
