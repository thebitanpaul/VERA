"""The LLM seam: one interface, several implementations.

Everything the pipeline knows about a model provider is declared here, including
the error taxonomy. The distinction between :class:`RetryableBackendError` and
:class:`FatalBackendError` is a cost control, not a nicety -- retrying a bad key
or an unsupported model burns quota and fixes nothing, so only transient
failures are retried.
"""

from __future__ import annotations

import time
from typing import Callable, TypeVar

from ..config import settings
from ..models import Passage, Usage
from ..registry import RegistryEntry

T = TypeVar("T")


class BackendError(RuntimeError):
    """A backend call failed. Carries a message safe to show a coordinator."""

    def __init__(self, message: str, *, hint: str = "", status: int | None = None):
        super().__init__(message)
        self.hint = hint
        self.status = status

    def display(self) -> str:
        return f"{self}" + (f" — {self.hint}" if self.hint else "")


class RetryableBackendError(BackendError):
    """Transient: rate limit, timeout, or a 5xx. Worth one bounded retry."""


class FatalBackendError(BackendError):
    """Permanent for this configuration: bad key, unknown model, bad request.

    Never retried -- a retry costs quota and cannot succeed.
    """


class LLMBackend:
    """Synthesis + verification behind a single seam.

    Implementations must record token usage on :attr:`usage` so cost is
    observable, and must respect the output caps in :data:`settings`.
    """

    name = "base"

    def __init__(self) -> None:
        self.usage = Usage()

    @property
    def model(self) -> str:
        """Identifier of the concrete model in use (blank for the mock)."""
        return ""

    def synthesize(self, question: str, passages: list[Passage],
                   governing: RegistryEntry) -> str:
        raise NotImplementedError

    def verify(self, question: str, answer: str, passages: list[Passage]) -> dict:
        raise NotImplementedError


def with_retries(fn: Callable[[], T], *, what: str) -> T:
    """Run ``fn``, retrying only :class:`RetryableBackendError` with backoff.

    Bounded by ``settings.max_retries`` so a rate-limited provider cannot turn
    one question into an unbounded stream of billed requests.
    """
    delay = settings.retry_base_delay
    last: RetryableBackendError | None = None
    for attempt in range(settings.max_retries + 1):
        try:
            return fn()
        except RetryableBackendError as exc:
            last = exc
            if attempt == settings.max_retries:
                break
            time.sleep(delay)
            delay *= 2
    assert last is not None
    raise RetryableBackendError(
        f"{what} failed after {settings.max_retries + 1} attempts: {last}",
        hint=last.hint, status=last.status)
