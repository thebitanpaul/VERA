"""Step [2] -- hybrid retrieval over the governing protocol version.

Keyword scoring always runs; embedding similarity is blended in when an
embedding backend is configured. The module is deliberately conservative: it
would rather report "topic not in the current version" than surface a passage
that merely shares boilerplate vocabulary with the question.

It also surfaces the *same* section from superseded versions when the wording
differs, which is what lets synthesis disclose a cross-version change.
"""

from __future__ import annotations

import math
import os
import re
from typing import Any

from .config import settings
from .models import Passage, Retrieval

_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being", "of",
    "for", "to", "in", "on", "at", "by", "and", "or", "with", "without", "can",
    "do", "does", "did", "should", "would", "could", "may", "might", "must",
    "this", "that", "these", "those", "it", "as", "if", "any", "what", "which",
    "when", "how", "there", "under", "per", "trial", "protocol", "study",
    "patient", "patients", "allowed", "permitted", "ok", "okay",
}


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def content_terms(question: str) -> list[str]:
    """Query terms that carry topic meaning (stopwords / bare numbers dropped)."""
    seen: set[str] = set()
    out: list[str] = []
    for t in _tokens(question):
        if t in _STOPWORDS or t.isdigit() or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _section_text(sec: dict) -> str:
    return f"{sec.get('heading', '')}\n{sec.get('text', '')}"


def _term_hits(term: str, heading: str, body: str) -> tuple[int, int]:
    pattern = rf"\b{re.escape(term)}\b"
    return len(re.findall(pattern, heading)), len(re.findall(pattern, body))


def _try_embeddings(query: str, docs: list[str]) -> list[float] | None:
    """Optional embedding similarity, aligned to ``docs``.

    Returns ``None`` whenever no embedding backend is usable, in which case the
    pipeline runs on keyword retrieval alone. Uses Voyage AI when both the
    ``voyageai`` package and ``VOYAGE_API_KEY`` are present.
    """
    if not (settings.voyage_api_key or os.environ.get("VOYAGE_API_KEY")):
        return None
    try:
        import voyageai  # type: ignore
    except ImportError:
        return None
    try:
        vo = voyageai.Client()
        emb = vo.embed([query] + docs, model="voyage-3",
                       input_type="document").embeddings
    except Exception:  # noqa: BLE001 -- embeddings are strictly best-effort
        return None

    q = emb[0]
    q_norm = math.sqrt(sum(a * a for a in q)) or 1.0
    out = []
    for d in emb[1:]:
        d_norm = math.sqrt(sum(b * b for b in d)) or 1.0
        out.append(sum(a * b for a, b in zip(q, d)) / (q_norm * d_norm))
    return out


def retrieve(question: str, current: dict, older: list[dict]) -> Retrieval:
    """Retrieve the most relevant current-version sections, plus any superseded
    section on the same topic (for conflict disclosure)."""
    terms = content_terms(question)
    r = Retrieval()

    current_secs: list[dict[str, Any]] = current.get("sections", [])
    if not terms or not current_secs:
        r.reason = "no indexable query terms or no sections in the current version"
        return r

    # Vocabulary across ALL versions -- distinguishes "unknown topic" from
    # "topic that exists but was removed from the current version".
    corpus_vocab: set[str] = set()
    for rec in [current, *older]:
        for sec in rec.get("sections", []):
            corpus_vocab.update(_tokens(_section_text(sec)))
    indexable = [t for t in terms if t in corpus_vocab]
    if not indexable:
        r.reason = "topic not present in any version of the protocol"
        return r

    # IDF over the CURRENT version's sections. Boilerplate terms ("dose", "day",
    # "visit") appear in most sections and get near-zero weight; a rare, topic-
    # bearing term ("metformin") gets high weight. This keeps a common word from
    # faking a match against an otherwise-unknown topic.
    n = len(current_secs)
    df: dict[str, int] = {}
    for sec in current_secs:
        present = set(_tokens(_section_text(sec)))
        for t in indexable:
            if t in present:
                df[t] = df.get(t, 0) + 1

    def idf(t: str) -> float:
        return math.log((n + 1) / (df.get(t, 0) + 0.5))

    # A term is "salient" if it appears in the current version but not in the
    # majority of its sections -- i.e. it discriminates between topics.
    salient_cutoff = max(1, n // 2)
    salient = {t for t in indexable if 0 < df.get(t, 0) <= salient_cutoff}

    docs = [_section_text(s) for s in current_secs]
    embeds = _try_embeddings(question, docs)

    matched_in_current: set[str] = set()
    scored: list[Passage] = []
    for i, sec in enumerate(current_secs):
        heading = " ".join(_tokens(sec.get("heading", "")))
        body = " ".join(_tokens(sec.get("text", "")))
        kw = 0.0
        matched: set[str] = set()
        for t in indexable:
            h, b = _term_hits(t, heading, body)
            if h or b:
                matched.add(t)
            kw += idf(t) * (settings.heading_boost * h + b)
        matched_in_current |= matched
        score = kw
        if embeds is not None:
            # Blend keyword with embedding cosine; keyword stays dominant so a
            # lexical topic hit is never washed out by fuzzy similarity.
            score += 2.0 * max(embeds[i], 0.0)
        # Only keep a section that hits at least one salient term -- a match on
        # boilerplate alone is not evidence the section is on-topic.
        if score > 0 and (matched & salient):
            scored.append(Passage(
                trial_id=str(current.get("doc_id", "")),
                version=str(current.get("version", "")),
                section_id=str(sec.get("id", "")),
                heading=str(sec.get("heading", "")),
                text=str(sec.get("text", "")),
                effective_date=str(current.get("effective_date", "")),
                status="active",
                score=score,
            ))

    # Confidence = idf-weighted coverage of the query's indexable terms within
    # the current version. topic_in_current requires a *salient* match, so a
    # bare boilerplate hit (e.g. only "dose") does not clear the gate.
    total_idf = sum(idf(t) for t in indexable) or 1.0
    r.confidence = sum(idf(t) for t in indexable if df.get(t, 0) > 0) / total_idf
    r.topic_in_current = bool(matched_in_current & salient)

    if not r.topic_in_current:
        r.reason = ("topic not found in the current version "
                    "(no discriminative query term matched)")
        return r

    scored.sort(key=lambda p: p.score, reverse=True)
    if scored:
        floor = settings.relative_floor * scored[0].score
        scored = [p for p in scored if p.score >= floor]
    top = scored[:settings.top_k]

    r.passages = top + _conflicting_passages(top, current, older)
    return r


def _conflicting_passages(top: list[Passage], current: dict,
                          older: list[dict]) -> list[Passage]:
    """For each retrieved current section, surface the same section id from a
    superseded version when its wording differs."""
    older_by_id: dict[str, list[dict]] = {}
    for rec in older:
        for sec in rec.get("sections", []):
            older_by_id.setdefault(str(sec.get("id", "")), []).append(
                {**sec, "_version": rec.get("version"),
                 "_eff": rec.get("effective_date")}
            )

    def norm(s: str) -> str:
        return " ".join(s.split())

    conflicts: list[Passage] = []
    for p in top:
        for old in older_by_id.get(p.section_id, []):
            if norm(str(old.get("text", ""))) != norm(p.text):
                conflicts.append(Passage(
                    trial_id=str(current.get("doc_id", "")),
                    version=str(old.get("_version", "")),
                    section_id=p.section_id,
                    heading=str(old.get("heading", "")),
                    text=str(old.get("text", "")),
                    effective_date=str(old.get("_eff", "")),
                    status="superseded",
                    superseded_conflict=True,
                ))
    # Oldest first, so a reader sees the change chronologically.
    conflicts.sort(key=lambda p: (p.section_id, p.version))
    return conflicts
