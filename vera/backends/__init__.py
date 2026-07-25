"""Backend selection and provider metadata.

``auto`` prefers a configured real provider and falls back to the deterministic
mock, so the system runs with or without credentials.

A caller may also supply :class:`Credentials` at request time — that is what lets
the web UI switch provider or rotate a key without redeploying. Runtime keys are
never written to disk, never logged, and never returned in a response.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from ..config import settings
from .anthropic import AnthropicBackend
from .base import (
    BackendError,
    FatalBackendError,
    LLMBackend,
    RetryableBackendError,
    with_retries,
)
from .gemini import GeminiBackend
from .mock import MockBackend
from .openai_compat import OpenAIBackend, OpenAICompatBackend, XAIBackend

BACKENDS: dict[str, type[LLMBackend]] = {
    "mock": MockBackend,
    "gemini": GeminiBackend,
    "anthropic": AnthropicBackend,
    "openai": OpenAIBackend,
    "xai": XAIBackend,
}

BACKEND_CHOICES = ["auto", *BACKENDS]

# Order in which `auto` prefers a configured provider.
_AUTO_ORDER = ("gemini", "anthropic", "openai", "xai")


@dataclass(frozen=True)
class ProviderInfo:
    """Everything the UI needs to render one provider option."""

    id: str
    label: str
    needs_key: bool
    default_model: str
    key_url: str
    key_hint: str


PROVIDERS: list[ProviderInfo] = [
    ProviderInfo("mock", "Mock (no key, deterministic)", False, "", "",
                 "Composes the answer from the retrieved passages. No network."),
    ProviderInfo("gemini", "Google Gemini", True, "gemini-flash-latest",
                 "https://aistudio.google.com/apikey",
                 "Free tier available. Leave the model blank unless you know it "
                 "is served on your plan."),
    ProviderInfo("openai", "OpenAI (GPT)", True, "gpt-4o-mini",
                 "https://platform.openai.com/api-keys",
                 "Requires billing credit; there is no free tier."),
    ProviderInfo("xai", "xAI (Grok)", True, "grok-3-mini",
                 "https://console.x.ai/",
                 "Requires an xAI account with credit."),
    ProviderInfo("anthropic", "Anthropic (Claude)", True, "claude-opus-4-5",
                 "https://console.anthropic.com/settings/keys",
                 "Requires the 'anthropic' package, which is installed."),
]


@dataclass(frozen=True)
class Credentials:
    """A provider + key supplied at request time by a browser session."""

    provider: str
    api_key: str = ""
    model: str = ""

    def fingerprint(self) -> str:
        """Short non-reversible id, safe to use in a cache key or a log line.

        Never expose the key itself: this is what keeps one session's cached
        answers from being keyed by another session's secret.
        """
        if not self.api_key:
            return ""
        return hashlib.sha256(self.api_key.encode("utf-8")).hexdigest()[:12]


def _configured(name: str) -> bool:
    return bool({
        "gemini": settings.gemini_api_key,
        "anthropic": settings.anthropic_api_key,
        "openai": settings.openai_api_key,
        "xai": settings.xai_api_key,
    }.get(name))


def resolve_backend_name(name: str = "auto") -> str:
    """Map ``auto`` onto a concrete backend using the configured credentials."""
    if name != "auto":
        return name
    for candidate in _AUTO_ORDER:
        if _configured(candidate):
            return candidate
    return "mock"


def get_backend(name: str = "auto",
                credentials: Credentials | None = None) -> LLMBackend:
    """Instantiate a backend by name, optionally with request-time credentials.

    Raises :class:`FatalBackendError` when a real backend is requested but cannot
    be constructed (missing key, retired model). ``auto`` degrades to the mock
    instead of raising, so the app still starts unconfigured.
    """
    if credentials is not None and credentials.provider not in ("", "auto"):
        name = credentials.provider

    resolved = resolve_backend_name(name)
    try:
        cls = BACKENDS[resolved]
    except KeyError:
        raise FatalBackendError(
            f"unknown backend '{resolved}'; "
            f"choose one of {', '.join(BACKEND_CHOICES)}") from None

    kwargs: dict[str, str] = {}
    if credentials is not None and resolved != "mock":
        if credentials.api_key:
            kwargs["api_key"] = credentials.api_key
        if credentials.model:
            kwargs["model"] = credentials.model

    try:
        return cls(**kwargs)
    except FatalBackendError:
        if name == "auto":
            return MockBackend()
        raise


# ---------------------------------------------------------------------------
# Key validation -- a zero-token round trip, so "Test key" costs nothing
# ---------------------------------------------------------------------------
def _http_json(url: str, headers: dict[str, str]) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def validate_credentials(provider: str, api_key: str) -> dict:
    """Check a key by listing the provider's models. Consumes no tokens.

    Returns ``{"ok": bool, "detail": str, "models": [...]}``. The model list lets
    the UI show what the key can actually reach instead of guessing.
    """
    provider = (provider or "").strip()
    if provider == "mock":
        return {"ok": True, "detail": "The mock backend needs no key.", "models": []}
    if provider not in BACKENDS:
        return {"ok": False, "detail": f"Unknown provider '{provider}'.", "models": []}
    if not api_key:
        return {"ok": False, "detail": "No API key supplied.", "models": []}

    try:
        if provider == "gemini":
            data = _http_json(
                "https://generativelanguage.googleapis.com/v1beta/models"
                f"?key={api_key}&pageSize=200", {})
            models = [m["name"].split("/")[-1] for m in data.get("models", [])
                      if "generateContent" in m.get("supportedGenerationMethods", [])]
        elif provider == "anthropic":
            data = _http_json("https://api.anthropic.com/v1/models?limit=100",
                              {"x-api-key": api_key,
                               "anthropic-version": "2023-06-01"})
            models = [m["id"] for m in data.get("data", [])]
        else:  # openai / xai -- same dialect
            base = BACKENDS[provider].base_url  # type: ignore[attr-defined]
            data = _http_json(f"{base}/models",
                              {"Authorization": f"Bearer {api_key}"})
            models = [m["id"] for m in data.get("data", [])]
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
            detail = str((body.get("error") or {}).get("message") or body)[:200]
        except Exception:  # noqa: BLE001
            detail = exc.reason or f"HTTP {exc.code}"
        if exc.code in (401, 403):
            detail = f"Key rejected ({exc.code}): {detail}"
        return {"ok": False, "detail": detail, "models": []}
    except Exception as exc:  # noqa: BLE001 -- network, DNS, timeout
        return {"ok": False, "detail": f"Could not reach the provider: {exc}",
                "models": []}

    return {"ok": True,
            "detail": f"Key works — {len(models)} models available.",
            "models": sorted(models)}


__all__ = [
    "BACKENDS", "BACKEND_CHOICES", "PROVIDERS", "BackendError", "Credentials",
    "FatalBackendError", "LLMBackend", "ProviderInfo", "RetryableBackendError",
    "AnthropicBackend", "GeminiBackend", "MockBackend", "OpenAIBackend",
    "OpenAICompatBackend", "XAIBackend", "get_backend", "resolve_backend_name",
    "validate_credentials", "with_retries",
]
