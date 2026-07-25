"""Google Gemini backend (REST, stdlib only -- no extra dependency).

Cost and correctness notes
--------------------------
Three things about the current Gemini API make a naive client both expensive and
silently broken. All three are handled here:

1. **Thinking is on by default and is billed.** Reasoning tokens are reported
   separately as ``thoughtsTokenCount`` but count against ``maxOutputTokens``.
   An uncapped request can spend thousands of tokens per call.

2. **Thinking can starve the visible answer.** When the budget is exhausted
   during reasoning the candidate comes back with ``finishReason: MAX_TOKENS``
   and *no* ``parts`` key at all. A client that reaches straight for
   ``parts[0]["text"]`` raises ``KeyError`` -- tokens spent, nothing returned.
   :func:`_extract_text` detects this and raises an actionable error instead.

3. **Legacy model ids have a free-tier quota of zero.** ``gemini-2.0-flash`` and
   friends now return ``429 … limit: 0`` for keys created after their retirement
   from the free tier, which reads exactly like an exhausted quota. The default
   model is therefore the rolling ``gemini-flash-latest`` alias, and a zero-limit
   429 is reported as a *fatal* model-selection problem rather than retried.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from ..config import settings
from ..models import Passage
from ..prompts import (
    GEMINI_VERIFY_SCHEMA,
    SYNTH_SYSTEM,
    VERIFY_SYSTEM,
    synth_prompt,
    verify_prompt,
)
from ..registry import RegistryEntry
from .base import FatalBackendError, LLMBackend, RetryableBackendError, with_retries

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"

# Model ids whose free-tier allowance is now zero. Selecting one produces a 429
# that looks like an exhausted quota but is really a retired model.
RETIRED_FREE_TIER_MODELS = {
    "gemini-1.5-flash", "gemini-1.5-flash-8b", "gemini-1.5-pro",
    "gemini-2.0-flash", "gemini-2.0-flash-001", "gemini-2.0-flash-lite",
    "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro",
}

_MODEL_HINT = ("set GEMINI_MODEL to a currently served model — "
               "'gemini-flash-latest' tracks the newest free-tier flash model")


class GeminiBackend(LLMBackend):
    """Gemini via ``generateContent``."""

    name = "gemini"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        super().__init__()
        self._api_key = api_key if api_key is not None else settings.gemini_api_key
        self._model = model or settings.gemini_model
        if not self._api_key:
            raise FatalBackendError(
                "GEMINI_API_KEY is not set",
                hint="add it to .env or the platform's environment variables")
        if self._model in RETIRED_FREE_TIER_MODELS:
            # Fail loudly at construction rather than after a billed 429.
            raise FatalBackendError(
                f"GEMINI_MODEL '{self._model}' is no longer served on the Gemini "
                f"free tier (its quota is 0)", hint=_MODEL_HINT)

    @property
    def model(self) -> str:
        return self._model

    # -- transport ----------------------------------------------------------
    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{API_ROOT}/{self._model}:generateContent?key={self._api_key}"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=settings.request_timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise self._classify(exc) from None
        except urllib.error.URLError as exc:
            raise RetryableBackendError(
                f"could not reach the Gemini API ({exc.reason})",
                hint="check network connectivity") from None
        except TimeoutError:
            raise RetryableBackendError(
                f"Gemini request timed out after {settings.request_timeout}s",
                hint="raise VERA_REQUEST_TIMEOUT or use a smaller model") from None

    def _classify(self, exc: urllib.error.HTTPError) -> Exception:
        """Turn an HTTP error into a retryable/fatal decision plus a real hint."""
        try:
            err = json.loads(exc.read().decode("utf-8")).get("error", {})
            message = str(err.get("message", "")).strip()
        except Exception:  # noqa: BLE001 -- error body may not be JSON
            message = exc.reason or ""
        first_line = message.split("\n", 1)[0][:300]
        code = exc.code

        if code == 429:
            # "limit: 0" means the model is not served on this tier at all --
            # retrying only wastes time, so treat it as a configuration error.
            if "limit: 0" in message:
                return FatalBackendError(
                    f"model '{self._model}' has a free-tier quota of 0 "
                    f"(it is not served on your plan)", hint=_MODEL_HINT, status=429)
            return RetryableBackendError(
                f"Gemini rate limit: {first_line}",
                hint="free-tier quota; retry shortly or switch backend", status=429)
        if code in (500, 502, 503, 504):
            return RetryableBackendError(f"Gemini server error {code}: {first_line}",
                                         status=code)
        if code in (401, 403):
            return FatalBackendError(
                f"Gemini rejected the API key ({code})",
                hint="regenerate the key at https://aistudio.google.com/apikey",
                status=code)
        if code == 404:
            return FatalBackendError(
                f"Gemini model '{self._model}' not found or not available to you",
                hint=_MODEL_HINT, status=404)
        return FatalBackendError(f"Gemini request failed ({code}): {first_line}",
                                 status=code)

    # -- request assembly ---------------------------------------------------
    def _generation_config(self, max_tokens: int,
                           schema: dict | None) -> dict[str, Any]:
        gen: dict[str, Any] = {
            "temperature": 0.0,
            # Always capped. Includes thinking tokens on reasoning models, so it
            # is sized with headroom rather than trimmed to the answer length.
            "maxOutputTokens": max_tokens,
            # Explicit, minimal reasoning. Protocol Q&A is extraction, not
            # puzzle-solving; leaving this unset is what makes cost unpredictable.
            "thinkingConfig": {"thinkingLevel": settings.thinking_level},
        }
        if schema is not None:
            gen["responseMimeType"] = "application/json"
            gen["responseSchema"] = schema
        return gen

    def _call(self, system: str, prompt: str, *, max_tokens: int,
              schema: dict | None = None) -> str:
        def attempt() -> str:
            body = {
                "system_instruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": self._generation_config(max_tokens, schema),
            }
            try:
                payload = self._post(body)
            except FatalBackendError as exc:
                # Older models reject thinkingConfig outright. Retry once without
                # it so an explicitly pinned legacy model still works.
                if exc.status == 400 and "thinking" in str(exc).lower():
                    gen = self._generation_config(max_tokens, schema)
                    gen.pop("thinkingConfig", None)
                    body["generationConfig"] = gen
                    payload = self._post(body)
                else:
                    raise
            self._record_usage(payload)
            return self._extract_text(payload, max_tokens)

        return with_retries(attempt, what=f"Gemini {self._model}")

    def _record_usage(self, payload: dict[str, Any]) -> None:
        u = payload.get("usageMetadata") or {}
        self.usage.add(
            input_tokens=int(u.get("promptTokenCount") or 0),
            output_tokens=int(u.get("candidatesTokenCount") or 0),
            thinking_tokens=int(u.get("thoughtsTokenCount") or 0),
        )

    def _extract_text(self, payload: dict[str, Any], max_tokens: int) -> str:
        """Pull the answer out, converting every empty-response shape into a
        specific error rather than a ``KeyError`` swallowed upstream."""
        candidates = payload.get("candidates") or []
        if not candidates:
            blocked = (payload.get("promptFeedback") or {}).get("blockReason")
            if blocked:
                raise FatalBackendError(
                    f"Gemini blocked the prompt ({blocked})",
                    hint="the protocol text tripped a safety filter")
            raise RetryableBackendError("Gemini returned no candidates")

        cand = candidates[0]
        finish = cand.get("finishReason")
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()

        if text:
            return text

        # Empty answer. Name the actual cause -- this is the failure that used to
        # surface as "tokens consumed, nothing returned".
        if finish == "MAX_TOKENS":
            spent = self.usage.thinking_tokens
            raise FatalBackendError(
                f"Gemini spent the entire {max_tokens}-token output budget on "
                f"reasoning ({spent} thinking tokens) and returned no answer",
                hint="raise VERA_SYNTH_MAX_TOKENS or set VERA_THINKING_LEVEL=low")
        if finish in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"):
            raise FatalBackendError(f"Gemini stopped for policy reasons ({finish})")
        raise RetryableBackendError(
            f"Gemini returned an empty answer (finishReason={finish})")

    # -- LLMBackend ---------------------------------------------------------
    def synthesize(self, question: str, passages: list[Passage],
                   governing: RegistryEntry) -> str:
        return self._call(
            SYNTH_SYSTEM, synth_prompt(question, passages, governing),
            max_tokens=settings.synth_max_output_tokens).strip()

    def verify(self, question: str, answer: str, passages: list[Passage]) -> dict:
        text = self._call(
            VERIFY_SYSTEM, verify_prompt(question, answer, passages),
            max_tokens=settings.verify_max_output_tokens,
            schema=GEMINI_VERIFY_SCHEMA)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise RetryableBackendError(
                f"verifier returned malformed JSON: {exc}") from None
