"""End-to-end pipeline behaviour: the three example questions, the abstention
gates, the cache, and the degrade-loudly fallback."""

from __future__ import annotations

import pytest

from vera.backends.base import FatalBackendError, LLMBackend, RetryableBackendError
from vera.pipeline import answer_question, cache_stats, clear_cache
from vera.prompts import format_passages, synth_prompt


# ------------------------------------------------------------ happy paths
def test_metformin_is_answered_with_change_disclosure():
    res = answer_question("Is concomitant metformin permitted?", "412",
                          backend_name="mock")
    assert res.answered
    assert res.governing.version == "3.0"
    assert "2000 mg/day" in res.answer
    assert "Change disclosure" in res.answer
    assert res.versions_cited == ["3.0", "1.0", "2.0"]
    assert res.verdict["grounded"]


def test_cohort_b_washout_is_answered():
    res = answer_question("What is the washout period for Cohort B?", "412",
                          backend_name="mock")
    assert res.answered
    assert "14" in res.answer
    assert any(p.section_id == "6" for p in res.passages)


def test_unknown_drug_abstains_without_calling_the_model():
    res = answer_question("Is canagliflozin allowed?", "412", backend_name="mock")
    assert not res.answered
    assert res.reason == "topic not present in any version of the protocol"
    assert res.usage.calls == 0          # abstention is free
    assert not res.passages


# ----------------------------------------------------------------- gates
def test_abstains_before_the_first_effective_date():
    res = answer_question("Is concomitant metformin permitted?", "412",
                          as_of="2020-01-01", backend_name="mock")
    assert not res.answered
    assert res.governing is None
    assert res.reason_kind == "date_before_first_version"


def test_as_of_resolves_the_version_in_force_then():
    """Asked as of 2023, the answer must come from v1.0, which excluded metformin."""
    res = answer_question("Is concomitant metformin permitted?", "412",
                          as_of="2023-07-01", backend_name="mock")
    assert res.answered
    assert res.governing.version == "1.0"
    assert res.historical is True
    assert "excluded" in res.answer


def test_as_of_mid_v2_resolves_v2():
    res = answer_question("Is concomitant metformin permitted?", "412",
                          as_of="2024-06-01", backend_name="mock")
    assert res.governing.version == "2.0"
    assert res.historical is True


def test_date_before_the_first_version_is_an_input_problem():
    res = answer_question("Is concomitant metformin permitted?", "412",
                          as_of="2020-01-01", backend_name="mock")
    assert not res.answered
    assert res.governing is None
    assert res.reason_kind == "date_before_first_version"
    assert res.first_effective_date == "2023-06-01"
    assert res.backend == ""          # nothing ran, so nothing to blame
    assert res.usage.calls == 0


def test_unknown_trial_abstains():
    res = answer_question("Anything?", "999", backend_name="mock")
    assert not res.answered
    assert res.reason_kind == "unknown_trial"


def test_answers_are_isolated_per_trial():
    q = "Is concomitant metformin permitted?"
    a = answer_question(q, "412", backend_name="mock")
    b = answer_question(q, "778", backend_name="mock")
    assert "2000 mg/day" in a.answer and "2000 mg/day" not in b.answer
    assert "without a dose ceiling" in b.answer
    assert {p.trial_id for p in a.passages} == {"412"}
    assert {p.trial_id for p in b.passages} == {"778"}


def test_verifier_rejection_abstains():
    class Rejecting(LLMBackend):
        name = "rejecting"

        def synthesize(self, question, passages, governing):
            return "Metformin is banned."

        def verify(self, question, answer, passages):
            return {"grounded": False, "citations_present": False,
                    "unsupported_claims": ["Metformin is banned."],
                    "reason": "contradicts the passage"}

    res = answer_question("Is concomitant metformin permitted?", "412",
                          backend=Rejecting())
    assert not res.answered
    assert "verifier rejected" in res.reason
    assert res.unsupported == ["Metformin is banned."]


# --------------------------------------------------------------- fallback
def test_backend_failure_degrades_to_mock_but_reports_the_error():
    class Broken(LLMBackend):
        name = "broken"

        def synthesize(self, question, passages, governing):
            raise RetryableBackendError("rate limited", hint="try later")

        def verify(self, question, answer, passages):
            raise AssertionError("never reached")

    res = answer_question("Is concomitant metformin permitted?", "412",
                          backend=Broken())
    assert res.answered                              # coordinator is not blocked
    assert res.backend == "mock (fallback from broken)"
    assert "rate limited" in res.backend_error       # ...but the failure is visible
    assert "try later" in res.backend_error


def test_mock_failure_is_not_swallowed():
    class BrokenMock(LLMBackend):
        name = "mock"

        def synthesize(self, question, passages, governing):
            raise FatalBackendError("boom")

        def verify(self, question, answer, passages):
            raise AssertionError("never reached")

    with pytest.raises(FatalBackendError):
        answer_question("Is concomitant metformin permitted?", "412",
                        backend=BrokenMock())


# ------------------------------------------------------------------ cache
def test_repeated_question_is_served_from_cache():
    clear_cache()
    q = "Is concomitant metformin permitted?"
    first = answer_question(q, "412", backend_name="mock")
    second = answer_question(q, "412", backend_name="mock")
    assert not first.usage.cached
    assert second.usage.cached
    assert second.answer == first.answer
    assert cache_stats()["entries"] == 1


def test_cache_key_separates_trial_date_and_backend():
    clear_cache()
    q = "Is concomitant metformin permitted?"
    answer_question(q, "412", backend_name="mock")
    answer_question(q, "412", as_of="2023-07-01", backend_name="mock")
    assert cache_stats()["entries"] == 2


def test_cache_is_case_insensitive_on_the_question():
    clear_cache()
    answer_question("Is concomitant metformin permitted?", "412", backend_name="mock")
    hit = answer_question("IS CONCOMITANT METFORMIN PERMITTED?", "412",
                          backend_name="mock")
    assert hit.usage.cached


def test_no_cache_flag_bypasses_the_cache():
    clear_cache()
    q = "Is concomitant metformin permitted?"
    answer_question(q, "412", backend_name="mock")
    assert not answer_question(q, "412", backend_name="mock",
                               use_cache=False).usage.cached


def test_degraded_answers_are_not_cached():
    """A fallback answer must not be pinned; the next call should retry live."""
    class Broken(LLMBackend):
        name = "broken"

        def synthesize(self, question, passages, governing):
            raise RetryableBackendError("down")

        def verify(self, question, answer, passages):
            raise AssertionError("never reached")

    clear_cache()
    answer_question("Is concomitant metformin permitted?", "412", backend=Broken())
    assert cache_stats()["entries"] == 0


# ---------------------------------------------------------------- prompts
def test_passage_text_is_truncated_to_the_cap(monkeypatch):
    from vera import config, prompts
    from vera.models import Passage
    monkeypatch.setattr(prompts, "settings", config.Settings(max_passage_chars=50))
    huge = Passage("412", "3.0", "5.2", "H", "x" * 5000, "2025-03-01", "active")
    out = format_passages([huge])
    assert "…[truncated]" in out
    assert len(out) < 400


def test_synth_prompt_states_the_governing_version():
    res = answer_question("Is concomitant metformin permitted?", "412",
                          backend_name="mock")
    prompt = synth_prompt("Is metformin permitted?", res.passages, res.governing)
    assert "Trial 412 v3.0" in prompt
    assert "SUPERSEDED (for conflict disclosure)" in prompt


# -------------------------------------------------------------- serialising
def test_result_to_dict_is_json_safe():
    import json
    res = answer_question("Is concomitant metformin permitted?", "412",
                          backend_name="mock")
    payload = json.loads(json.dumps(res.to_dict()))
    assert payload["answered"] is True
    assert payload["governing"]["version"] == "3.0"
    assert payload["usage"]["calls"] == 0     # mock makes no billed calls
    assert payload["passages"][0]["citation"].startswith("Trial 412 v3.0")
