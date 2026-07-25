"""Prompt construction, shared verbatim across every LLM backend.

Keeping the prompts in one module means swapping providers cannot silently
change VERA's safety instructions. Passage text is truncated here (not in each
backend) so no backend can accidentally send an unbounded request.
"""

from __future__ import annotations

from .config import settings
from .models import Passage
from .registry import RegistryEntry

SYNTH_SYSTEM = """You are VERA, a clinical-trial protocol assistant. You answer \
questions for trial coordinators about what a protocol currently requires.

Hard rules:
- Answer ONLY from the passages provided. Never use outside knowledge or \
inference beyond the passage text. If the passages do not contain the answer, \
say so plainly.
- Cite your source inline for every factual claim, naming trial, version, \
section, and effective_date, e.g. "(Trial 412 v3.0 §5.2, eff. 2025-03-01)".
- Passages marked SUPERSEDED are earlier versions included ONLY so you can \
disclose change. If a topic differs between the current and a superseded \
version, state what the current protocol says, then explicitly note that it \
changed from the earlier version (with that version's citation).
- Be concise and unambiguous. Do not hedge beyond what the passages support.
- Answer in at most 200 words. Do not restate the question."""

VERIFY_SYSTEM = """You are VERA's verifier: an independent auditor. You did NOT \
write the answer. Check it strictly against the passages. A claim is grounded \
only if the passage text directly states it -- not if it is merely plausible. \
Missing or wrong citations (trial, version, section, effective_date) count as a \
failure. Be adversarial: it is safer to fail a borderline answer than to pass an \
unsupported one. Reply with the JSON object only."""

# JSON Schema (draft form) -- used by the Anthropic backend's structured output.
VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "grounded": {"type": "boolean"},
        "unsupported_claims": {"type": "array", "items": {"type": "string"}},
        "citations_present": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["grounded", "unsupported_claims", "citations_present", "reason"],
    "additionalProperties": False,
}

# Gemini's responseSchema uses an OpenAPI subset with upper-case type names.
GEMINI_VERIFY_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "grounded": {"type": "BOOLEAN"},
        "unsupported_claims": {"type": "ARRAY", "items": {"type": "STRING"}},
        "citations_present": {"type": "BOOLEAN"},
        "reason": {"type": "STRING"},
    },
    "required": ["grounded", "unsupported_claims", "citations_present", "reason"],
}


def _clip(text: str, limit: int) -> str:
    """Collapse whitespace and hard-cap a passage. The cap is a cost guardrail:
    a pathological source document must not be able to inflate a request."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[:limit].rstrip() + " …[truncated]"


def format_passages(passages: list[Passage]) -> str:
    limit = settings.max_passage_chars
    blocks = []
    for i, p in enumerate(passages, 1):
        tag = "SUPERSEDED (for conflict disclosure)" if p.superseded_conflict else "CURRENT"
        blocks.append(
            f"[Passage {i}] {tag}\n"
            f"  trial: {p.trial_id}\n"
            f"  version: {p.version}\n"
            f"  section: {p.section_id} — {p.heading}\n"
            f"  effective_date: {p.effective_date}\n"
            f"  status: {p.status}\n"
            f"  text: {_clip(p.text, limit)}"
        )
    return "\n\n".join(blocks)


def synth_prompt(question: str, passages: list[Passage],
                 governing: RegistryEntry) -> str:
    return (
        f"Question: {question}\n\n"
        f"Governing version (resolved deterministically by the registry): "
        f"Trial {governing.trial_id} v{governing.version}, "
        f"effective {governing.effective_date}.\n\n"
        f"Retrieved passages:\n\n{format_passages(passages)}\n\n"
        f"Answer the question using only these passages, following the rules."
    )


def verify_prompt(question: str, answer: str, passages: list[Passage]) -> str:
    return (
        f"Question: {question}\n\n"
        f"Proposed answer:\n{answer}\n\n"
        f"Passages the answer must be grounded in:\n\n{format_passages(passages)}"
    )
