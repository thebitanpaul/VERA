"""Deterministic backend that mimics an LLM without a network call.

Synthesis is extractive: the answer is composed straight from the passage text
(never inventing facts) with inline citations and cross-version change
disclosure. Verification therefore always passes -- the answer is grounded by
construction.

This is the default when no provider key is configured, which keeps the demo,
the test suite, and CI fully reproducible. The retrieval-level abstention gates
still fire, so the safety behaviour under test is the real behaviour.
"""

from __future__ import annotations

from ..models import Passage
from ..registry import RegistryEntry
from .base import LLMBackend

PREFIX = "[mock LLM synthesis — grounded extractively from passages]"


class MockBackend(LLMBackend):
    name = "mock"

    def synthesize(self, question: str, passages: list[Passage],
                   governing: RegistryEntry) -> str:
        current = [p for p in passages if not p.superseded_conflict]
        conflicts = [p for p in passages if p.superseded_conflict]
        lines = [PREFIX]
        for p in current:
            lines.append(f"\nPer {p.citation()}:\n  {' '.join(p.text.split())}")
        for p in conflicts:
            lines.append(
                f"\nChange disclosure — this differs from the earlier {p.citation()}, "
                f"which read:\n  {' '.join(p.text.split())}")
        return "\n".join(lines)

    def verify(self, question: str, answer: str, passages: list[Passage]) -> dict:
        return {
            "grounded": True,
            "unsupported_claims": [],
            "citations_present": True,
            "reason": "Mock verifier: answer is composed verbatim from cited passages.",
        }
