"""Shared data structures for the VERA pipeline.

These types are the contract between retrieval, synthesis, verification and the
two front-ends (CLI and web). Keeping them in one module means the Flask layer
never re-implements pipeline concepts -- it only serialises them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .registry import RegistryEntry


@dataclass
class Passage:
    """One protocol section retrieved as evidence."""

    trial_id: str
    version: str
    section_id: str
    heading: str
    text: str
    effective_date: str
    status: str                          # "active" | "superseded"
    score: float = 0.0
    superseded_conflict: bool = False    # surfaced only for conflict disclosure

    def citation(self) -> str:
        return (f"Trial {self.trial_id} v{self.version} §{self.section_id} "
                f"({self.heading}), effective {self.effective_date}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "trial": self.trial_id,
            "version": self.version,
            "section": self.section_id,
            "heading": self.heading,
            "text": self.text,
            "effective_date": str(self.effective_date),
            "status": self.status,
            "score": round(self.score, 3),
            "superseded_conflict": self.superseded_conflict,
            "citation": self.citation(),
        }


@dataclass
class Retrieval:
    """Outcome of step [2] -- passages plus the signals the gates run on."""

    passages: list[Passage] = field(default_factory=list)
    confidence: float = 0.0
    topic_in_current: bool = False
    reason: str = ""                     # populated when retrieval decides to abstain


@dataclass
class Usage:
    """Token accounting for one question, aggregated across LLM calls.

    Exposed in the API and CLI so cost is observable rather than a surprise on
    the billing page. ``thinking`` is tracked separately because it is the
    component that silently dominates on reasoning models.
    """

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cached: bool = False

    def add(self, input_tokens: int = 0, output_tokens: int = 0,
            thinking_tokens: int = 0) -> None:
        self.calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.thinking_tokens += thinking_tokens

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.thinking_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "thinking_tokens": self.thinking_tokens,
            "total_tokens": self.total_tokens,
            "cached": self.cached,
        }


@dataclass
class Result:
    """Structured outcome of the pipeline -- shared by the CLI and the web UI."""

    question: str = ""
    trial_id: str = ""
    governing: RegistryEntry | None = None
    as_of: str = ""
    backend: str = ""
    model: str = ""
    answered: bool = False
    answer: str = ""
    passages: list[Passage] = field(default_factory=list)
    verdict: dict = field(default_factory=dict)
    confidence: float = 0.0
    reason: str = ""                                  # abstention reason, prose
    # Machine-readable abstention category, so the UI can tell a bad input apart
    # from a system problem instead of showing one generic failure.
    #   ""                          -> answered, or a protocol-content abstention
    #   "date_before_first_version" -> the as-of date precedes the whole trial
    #   "unknown_trial"             -> no such trial in the registry
    reason_kind: str = ""
    first_effective_date: str = ""    # set with "date_before_first_version"
    historical: bool = False          # governing version has since been superseded
    unsupported: list[str] = field(default_factory=list)
    backend_error: str = ""                           # set when a live backend failed
    usage: Usage = field(default_factory=Usage)
    elapsed_ms: int = 0

    @property
    def versions_cited(self) -> list[str]:
        seen: list[str] = []
        for p in self.passages:
            if p.version not in seen:
                seen.append(p.version)
        return seen

    def to_dict(self) -> dict[str, Any]:
        gov = self.governing
        return {
            "question": self.question,
            "trial": self.trial_id,
            "answered": self.answered,
            "answer": self.answer,
            "backend": self.backend,
            "model": self.model,
            "confidence": round(self.confidence, 3),
            "governing": ({"trial": gov.trial_id,
                           "version": gov.version,
                           "effective_date": str(gov.effective_date),
                           "status": gov.status,
                           "supersedes": gov.supersedes} if gov else None),
            "as_of": self.as_of,
            "historical": self.historical,
            "passages": [p.to_dict() for p in self.passages],
            "verdict": self.verdict,
            "reason": self.reason,
            "reason_kind": self.reason_kind,
            "first_effective_date": self.first_effective_date,
            "unsupported": self.unsupported,
            "backend_error": self.backend_error,
            "usage": self.usage.to_dict(),
            "elapsed_ms": self.elapsed_ms,
        }
