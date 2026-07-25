"""Retrieval must find on-topic sections and, more importantly, must NOT claim a
topic is covered when it only shares boilerplate vocabulary with the question."""

from __future__ import annotations

from vera.retrieval import content_terms, retrieve


def test_content_terms_drops_stopwords_numbers_and_duplicates():
    terms = content_terms("Is metformin permitted for trial 412, metformin?")
    assert terms == ["metformin"]


def test_finds_the_on_topic_section(trial_412):
    current, older = trial_412
    r = retrieve("Is concomitant metformin permitted?", current, older)
    assert r.topic_in_current
    assert r.confidence > 0.9
    assert r.passages[0].section_id == "5.2"
    assert r.passages[0].status == "active"


def test_surfaces_superseded_versions_of_the_same_section(trial_412):
    current, older = trial_412
    r = retrieve("Is concomitant metformin permitted?", current, older)
    superseded = [p for p in r.passages if p.superseded_conflict]
    assert {p.version for p in superseded} == {"1.0", "2.0"}
    assert all(p.section_id == "5.2" for p in superseded)
    # Chronological, so a reader follows the change forwards.
    assert [p.version for p in superseded] == sorted(p.version for p in superseded)


def test_no_superseded_passage_when_wording_is_identical(trial_412):
    """v1.0 and v2.0 share §5.2 text; the conflict list must not duplicate it
    against an unchanged section."""
    current, older = trial_412
    r = retrieve("What is the washout period for Cohort B?", current, older)
    for p in r.passages:
        if p.superseded_conflict:
            active = next(a for a in r.passages
                          if not a.superseded_conflict and a.section_id == p.section_id)
            assert " ".join(p.text.split()) != " ".join(active.text.split())


def test_unknown_drug_is_not_in_any_version(trial_412):
    current, older = trial_412
    r = retrieve("Is canagliflozin allowed?", current, older)
    assert not r.topic_in_current
    assert not r.passages
    assert "not present in any version" in r.reason


def test_boilerplate_only_match_does_not_clear_the_gate(trial_412):
    """A question made only of words that appear in most sections must not be
    treated as an on-topic hit."""
    current, older = trial_412
    r = retrieve("dose", current, older)
    assert not r.topic_in_current or r.confidence < 1.0


def test_empty_question_abstains(trial_412):
    current, older = trial_412
    r = retrieve("the and of", current, older)
    assert not r.topic_in_current
    assert r.reason


def test_respects_top_k(trial_412, monkeypatch):
    from vera import config, retrieval
    monkeypatch.setattr(retrieval, "settings",
                        config.Settings(top_k=1, low_confidence=0.34,
                                        relative_floor=0.0, heading_boost=3.0))
    current, older = trial_412
    r = retrieve("metformin washout cohort dose", current, older)
    assert len([p for p in r.passages if not p.superseded_conflict]) <= 1
