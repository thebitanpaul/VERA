"""Anthropic Claude backend.

Mirrors the Gemini backend's guarantees: one client per backend instance, an
explicit output cap on every call, usage recorded, and errors classified into
retryable vs fatal so a bad key is never retried against quota.
"""

from __future__ import annotations

import json

from ..config import settings
from ..models import Passage
from ..prompts import (
    SYNTH_SYSTEM,
    VERIFY_SCHEMA,
    VERIFY_SYSTEM,
    synth_prompt,
    verify_prompt,
)
from ..registry import RegistryEntry
from .base import FatalBackendError, LLMBackend, RetryableBackendError, with_retries


class AnthropicBackend(LLMBackend):
    """Claude via the official SDK (``ANTHROPIC_API_KEY`` or an ``ant`` profile)."""

    name = "anthropic"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        super().__init__()
        self._model = model or settings.anthropic_model
        self._api_key = api_key if api_key is not None else settings.anthropic_api_key
        self._sdk_client = None

    @property
    def model(self) -> str:
        return self._model

    def _client(self):
        # Built once per backend instance; the SDK holds a connection pool.
        if self._sdk_client is None:
            try:
                import anthropic
            except ImportError:
                raise FatalBackendError(
                    "the 'anthropic' package is required for the anthropic backend",
                    hint="pip install anthropic") from None
            kwargs = {"api_key": self._api_key} if self._api_key else {}
            self._sdk_client = anthropic.Anthropic(**kwargs)
        return self._sdk_client

    def _classify(self, exc: Exception) -> Exception:
        try:
            import anthropic
        except ImportError:
            return RetryableBackendError(str(exc))
        if isinstance(exc, (anthropic.RateLimitError, anthropic.APITimeoutError,
                            anthropic.InternalServerError,
                            anthropic.APIConnectionError)):
            return RetryableBackendError(f"Anthropic transient failure: {exc}")
        if isinstance(exc, anthropic.AuthenticationError):
            return FatalBackendError(
                "Anthropic rejected the API key",
                hint="check ANTHROPIC_API_KEY at https://console.anthropic.com/")
        if isinstance(exc, anthropic.NotFoundError):
            return FatalBackendError(
                f"Anthropic model '{self._model}' not found",
                hint="set ANTHROPIC_MODEL to a model your account can access")
        if isinstance(exc, anthropic.APIStatusError):
            return FatalBackendError(f"Anthropic request failed: {exc}")
        return RetryableBackendError(str(exc))

    def _message(self, *, system: str, prompt: str, max_tokens: int,
                 schema: dict | None):
        output_config: dict = {"effort": "medium"}
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": schema}

        def attempt():
            try:
                resp = self._client().messages.create(
                    model=self._model,
                    max_tokens=max_tokens,
                    thinking={"type": "adaptive"},
                    output_config=output_config,
                    system=system,
                    messages=[{"role": "user", "content": prompt}],
                )
            except Exception as exc:  # noqa: BLE001 -- normalised below
                raise self._classify(exc) from None
            u = getattr(resp, "usage", None)
            if u is not None:
                self.usage.add(input_tokens=getattr(u, "input_tokens", 0) or 0,
                               output_tokens=getattr(u, "output_tokens", 0) or 0)
            text = "".join(b.text for b in resp.content
                           if getattr(b, "type", "") == "text").strip()
            if not text:
                raise RetryableBackendError(
                    f"Claude returned no text (stop_reason="
                    f"{getattr(resp, 'stop_reason', None)})")
            return text

        return with_retries(attempt, what=f"Anthropic {self._model}")

    def synthesize(self, question: str, passages: list[Passage],
                   governing: RegistryEntry) -> str:
        return self._message(system=SYNTH_SYSTEM,
                             prompt=synth_prompt(question, passages, governing),
                             max_tokens=settings.synth_max_output_tokens,
                             schema=None)

    def verify(self, question: str, answer: str, passages: list[Passage]) -> dict:
        text = self._message(system=VERIFY_SYSTEM,
                             prompt=verify_prompt(question, answer, passages),
                             max_tokens=settings.verify_max_output_tokens,
                             schema=VERIFY_SCHEMA)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise RetryableBackendError(
                f"verifier returned malformed JSON: {exc}") from None
