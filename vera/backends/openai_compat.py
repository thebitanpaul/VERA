"""Backends for providers speaking the OpenAI chat-completions dialect.

OpenAI (GPT) and xAI (Grok) share a wire format, so they share an
implementation and differ only in base URL, default model, and branding. Uses
urllib, so adding these providers costs no new dependency.

The same guardrails as the Gemini backend apply: an explicit output cap on every
call, usage recorded, empty responses turned into named errors, and a
retryable/fatal split so a bad key is never retried against quota.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from ..config import settings
from ..models import Passage
from ..prompts import SYNTH_SYSTEM, VERIFY_SYSTEM, synth_prompt, verify_prompt
from ..registry import RegistryEntry
from .base import FatalBackendError, LLMBackend, RetryableBackendError, with_retries


class OpenAICompatBackend(LLMBackend):
    """Shared implementation for /v1/chat/completions providers."""

    name = "openai-compat"
    base_url = ""
    default_model = ""
    console_url = ""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        super().__init__()
        self._api_key = (api_key if api_key is not None else self._key_from_settings())
        self._model = model or self._model_from_settings()
        if not self._api_key:
            raise FatalBackendError(
                f"no API key configured for the {self.name} backend",
                hint=f"add one in the UI's Model settings, or set it in .env")

    # -- subclass hooks -----------------------------------------------------
    def _key_from_settings(self) -> str:
        raise NotImplementedError

    def _model_from_settings(self) -> str:
        raise NotImplementedError

    @property
    def model(self) -> str:
        return self._model

    # -- transport ----------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}"}

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"), headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=settings.request_timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise self._classify(exc) from None
        except urllib.error.URLError as exc:
            raise RetryableBackendError(
                f"could not reach the {self.name} API ({exc.reason})",
                hint="check network connectivity") from None
        except TimeoutError:
            raise RetryableBackendError(
                f"{self.name} request timed out after {settings.request_timeout}s",
                hint="raise VERA_REQUEST_TIMEOUT or use a smaller model") from None

    def _classify(self, exc: urllib.error.HTTPError) -> Exception:
        try:
            err = json.loads(exc.read().decode("utf-8")).get("error", {})
            message = str(err.get("message") or err or "").strip()
        except Exception:  # noqa: BLE001 -- error body may not be JSON
            message = exc.reason or ""
        first = message.split("\n", 1)[0][:300]
        code = exc.code

        if code == 429:
            if "insufficient_quota" in message or "exceeded your current quota" in message:
                return FatalBackendError(
                    f"{self.name} account has no remaining quota",
                    hint=f"add credit at {self.console_url}", status=429)
            return RetryableBackendError(f"{self.name} rate limit: {first}", status=429)
        if code in (500, 502, 503, 504):
            return RetryableBackendError(f"{self.name} server error {code}: {first}",
                                         status=code)
        if code in (401, 403):
            return FatalBackendError(
                f"{self.name} rejected the API key ({code})",
                hint=f"check the key at {self.console_url}", status=code)
        if code == 404:
            return FatalBackendError(
                f"{self.name} model '{self._model}' not found or not available to you",
                hint="pick a different model in Model settings", status=404)
        return FatalBackendError(f"{self.name} request failed ({code}): {first}",
                                 status=code)

    # -- request assembly ---------------------------------------------------
    def _call(self, system: str, prompt: str, *, max_tokens: int,
              json_mode: bool = False) -> str:
        def attempt() -> str:
            body: dict[str, Any] = {
                "model": self._model,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": prompt}],
                "temperature": 0.0,
                "max_tokens": max_tokens,
            }
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            try:
                payload = self._post(body)
            except FatalBackendError as exc:
                text = str(exc).lower()
                # Reasoning models reject `max_tokens` and `temperature`; retry
                # once with the parameters they do accept rather than failing.
                if exc.status == 400 and ("max_completion_tokens" in text
                                          or "temperature" in text):
                    body.pop("max_tokens", None)
                    body.pop("temperature", None)
                    body["max_completion_tokens"] = max_tokens
                    payload = self._post(body)
                else:
                    raise
            self._record_usage(payload)
            return self._extract_text(payload, max_tokens)

        return with_retries(attempt, what=f"{self.name} {self._model}")

    def _record_usage(self, payload: dict[str, Any]) -> None:
        u = payload.get("usage") or {}
        details = u.get("completion_tokens_details") or {}
        self.usage.add(
            input_tokens=int(u.get("prompt_tokens") or 0),
            output_tokens=int(u.get("completion_tokens") or 0),
            thinking_tokens=int(details.get("reasoning_tokens") or 0),
        )

    def _extract_text(self, payload: dict[str, Any], max_tokens: int) -> str:
        choices = payload.get("choices") or []
        if not choices:
            raise RetryableBackendError(f"{self.name} returned no choices")
        choice = choices[0]
        finish = choice.get("finish_reason")
        text = str((choice.get("message") or {}).get("content") or "").strip()
        if text:
            return text

        if finish == "length":
            spent = self.usage.thinking_tokens
            raise FatalBackendError(
                f"{self.name} spent the entire {max_tokens}-token output budget "
                f"on reasoning ({spent} reasoning tokens) and returned no answer",
                hint="raise VERA_SYNTH_MAX_TOKENS or choose a non-reasoning model")
        if finish == "content_filter":
            raise FatalBackendError(f"{self.name} stopped for policy reasons")
        raise RetryableBackendError(
            f"{self.name} returned an empty answer (finish_reason={finish})")

    @staticmethod
    def _parse_json(text: str) -> dict:
        """Tolerate a fenced ```json block, which some models emit anyway."""
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
            if cleaned.rstrip().endswith("```"):
                cleaned = cleaned.rstrip()[:-3]
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise RetryableBackendError(
                f"verifier returned malformed JSON: {exc}") from None

    # -- LLMBackend ---------------------------------------------------------
    def synthesize(self, question: str, passages: list[Passage],
                   governing: RegistryEntry) -> str:
        return self._call(SYNTH_SYSTEM, synth_prompt(question, passages, governing),
                          max_tokens=settings.synth_max_output_tokens).strip()

    def verify(self, question: str, answer: str, passages: list[Passage]) -> dict:
        schema_hint = (
            '\n\nReply with a JSON object with exactly these keys: '
            '"grounded" (boolean), "unsupported_claims" (array of strings), '
            '"citations_present" (boolean), "reason" (string).')
        text = self._call(VERIFY_SYSTEM + schema_hint,
                          verify_prompt(question, answer, passages),
                          max_tokens=settings.verify_max_output_tokens,
                          json_mode=True)
        return self._parse_json(text)


class OpenAIBackend(OpenAICompatBackend):
    """OpenAI GPT models."""

    name = "openai"
    base_url = "https://api.openai.com/v1"
    default_model = "gpt-4o-mini"
    console_url = "https://platform.openai.com/api-keys"

    def _key_from_settings(self) -> str:
        return settings.openai_api_key

    def _model_from_settings(self) -> str:
        return settings.openai_model


class XAIBackend(OpenAICompatBackend):
    """xAI Grok models."""

    name = "xai"
    base_url = "https://api.x.ai/v1"
    default_model = "grok-3-mini"
    console_url = "https://console.x.ai/"

    def _key_from_settings(self) -> str:
        return settings.xai_api_key

    def _model_from_settings(self) -> str:
        return settings.xai_model
