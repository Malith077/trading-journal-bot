"""
Unit tests for services/coach_service.py and the eval scorer.

The coach's trustworthiness rests on three mechanical guarantees, so those are
what these tests pin down:
  - grounding:  citations to non-existent principles are dropped, not shown
  - guardrail:  anti-Shull language is detected deterministically
  - abstention: thin entries don't get manufactured insight

Plus integrity checks on the corpus itself, and on the eval scorer, since a
broken scorer would report false confidence.
"""
import sys
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.append(str(Path(__file__).resolve().parent.parent))

from services import coach_service as cs
from services.coach_service import CoachError
from services.llm_service import LLMError
from scripts.run_coach_eval import score_case, report, CHALLENGE_MARKERS

CORPUS = cs.load_corpus()


@pytest.fixture
def corpus():
    return cs.load_corpus()


# ──────────────────────────────────────────────
# Corpus integrity
# ──────────────────────────────────────────────

class TestCorpusIntegrity:

    def test_loads(self, corpus):
        assert corpus["principles"]
        assert corpus["version"]

    def test_every_principle_is_complete(self, corpus):
        for p in corpus["principles"]:
            for field in ("id", "title", "principle", "rejects", "cues", "confidence"):
                assert p.get(field), f"{p.get('id')} missing {field}"
            assert p["confidence"] in ("high", "medium")
            assert isinstance(p["cues"], list) and p["cues"]

    def test_ids_are_unique_and_slug_like(self, corpus):
        ids = [p["id"] for p in corpus["principles"]]
        assert len(ids) == len(set(ids))
        for pid in ids:
            assert pid == pid.lower()
            assert " " not in pid

    def test_central_principle_present(self, corpus):
        """If this one is missing the whole framing collapses into generic advice."""
        assert "emotions-are-data" in cs.principle_ids(corpus)

    def test_provenance_warns_about_secondary_sources(self, corpus):
        """The honesty caveat must survive edits to the file."""
        assert "warning" in corpus["provenance"]
        assert "NOT transcribed" in corpus["provenance"]["compiled_by"]

    def test_forbidden_phrases_are_lowercase(self, corpus):
        """find_forbidden lowercases the haystack, so needles must be lowercase."""
        for phrase in cs.forbidden_phrases(corpus):
            assert phrase == phrase.lower()

    def test_missing_corpus_raises(self, tmp_path):
        with pytest.raises(CoachError, match="not found"):
            cs.load_corpus(tmp_path / "nope.json")

    def test_malformed_corpus_raises(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        with pytest.raises(CoachError, match="not valid JSON"):
            cs.load_corpus(bad)

    def test_empty_corpus_raises(self, tmp_path):
        empty = tmp_path / "empty.json"
        empty.write_text(json.dumps({"principles": []}))
        with pytest.raises(CoachError, match="no principles"):
            cs.load_corpus(empty)


# ──────────────────────────────────────────────
# The guardrail
# ──────────────────────────────────────────────

class TestForbiddenLanguage:

    def test_detects_plain_phrase(self, corpus):
        assert "eliminate emotion" in cs.find_forbidden(
            "You should eliminate emotion from your process.", corpus)

    def test_case_insensitive(self, corpus):
        assert cs.find_forbidden("ELIMINATE EMOTION entirely", corpus)

    def test_smart_apostrophes_are_folded(self, corpus):
        """Models emit typographic apostrophes; the check must not miss those."""
        assert cs.find_forbidden("Don’t let emotions drive you", corpus)
        assert cs.find_forbidden("Don't let emotions drive you", corpus)

    def test_collapses_whitespace(self, corpus):
        assert cs.find_forbidden("remove\n   emotion from trading", corpus)

    def test_discipline_framing_caught(self, corpus):
        assert cs.find_forbidden("Honestly you need more discipline here.", corpus)

    def test_shull_aligned_text_is_clean(self, corpus):
        text = ("That urgency is data. Name what it's telling you before you size, "
                "rather than trying to talk yourself out of feeling it.")
        assert cs.find_forbidden(text, corpus) == []

    def test_empty_text(self, corpus):
        assert cs.find_forbidden("", corpus) == []
        assert cs.find_forbidden(None, corpus) == []


# ──────────────────────────────────────────────
# Grounding enforcement
# ──────────────────────────────────────────────

def _feedback(**kwargs):
    return {"reading": "r", "observations": [], "probe": "p",
            "not_enough_signal": False, **kwargs}


class TestValidateFeedback:

    def test_valid_observation_kept(self, corpus):
        entry = "I felt furious for hours over a tiny loss."
        fb = _feedback(observations=[{
            "principle_id": "fec-fractal",
            "quote": "furious for hours",
            "point": "The size of the reaction outruns the size of the loss.",
        }])
        cleaned, warnings = cs.validate_feedback(fb, entry, corpus)
        assert len(cleaned["observations"]) == 1
        assert warnings == []

    def test_fabricated_principle_is_dropped(self, corpus):
        """The core anti-confabulation guarantee."""
        fb = _feedback(observations=[{
            "principle_id": "shulls-law-of-vibes",
            "quote": "x", "point": "Sounds authoritative, cites nothing real.",
        }])
        cleaned, warnings = cs.validate_feedback(fb, "x", corpus)
        assert cleaned["observations"] == []
        assert any("unknown principle" in w for w in warnings)

    def test_missing_principle_id_dropped(self, corpus):
        fb = _feedback(observations=[{"quote": "x", "point": "y"}])
        cleaned, _ = cs.validate_feedback(fb, "x", corpus)
        assert cleaned["observations"] == []

    def test_malformed_observation_dropped(self, corpus):
        fb = _feedback(observations=["just a string"])
        cleaned, warnings = cs.validate_feedback(fb, "x", corpus)
        assert cleaned["observations"] == []
        assert any("malformed" in w for w in warnings)

    def test_valid_and_invalid_mixed(self, corpus):
        entry = "I was exhausted and I chased it."
        fb = _feedback(observations=[
            {"principle_id": "fc-physical", "quote": "exhausted", "point": "a"},
            {"principle_id": "made-up-id", "quote": "chased", "point": "b"},
        ])
        cleaned, warnings = cs.validate_feedback(fb, entry, corpus)
        assert [o["principle_id"] for o in cleaned["observations"]] == ["fc-physical"]
        assert len(warnings) == 1

    def test_unfound_quote_warns_but_keeps(self, corpus):
        """Warn, don't drop — models legitimately normalise whitespace."""
        fb = _feedback(observations=[{
            "principle_id": "fc-physical",
            "quote": "words the trader never wrote",
            "point": "p",
        }])
        cleaned, warnings = cs.validate_feedback(fb, "I was tired.", corpus)
        assert len(cleaned["observations"]) == 1
        assert any("Quote not found" in w for w in warnings)

    def test_quote_matching_tolerates_case_and_whitespace(self, corpus):
        fb = _feedback(observations=[{
            "principle_id": "fc-physical",
            "quote": "Three   Hours  Sleep",
            "point": "p",
        }])
        _, warnings = cs.validate_feedback(fb, "three hours sleep and no food", corpus)
        assert not any("Quote not found" in w for w in warnings)

    def test_forbidden_language_surfaces_in_output(self, corpus):
        fb = _feedback(reading="You need to eliminate emotion from this.")
        cleaned, warnings = cs.validate_feedback(fb, "x", corpus)
        assert cleaned["forbidden_hits"] == ["eliminate emotion"]
        assert any("Anti-Shull" in w for w in warnings)

    def test_forbidden_language_scanned_inside_observations(self, corpus):
        fb = _feedback(observations=[{
            "principle_id": "fc-physical", "quote": "tired",
            "point": "Frankly you need more discipline.",
        }])
        cleaned, _ = cs.validate_feedback(fb, "tired", corpus)
        assert "you need more discipline" in cleaned["forbidden_hits"]

    def test_clean_feedback_has_no_hits(self, corpus):
        fb = _feedback(reading="The urgency here is information about you, not the market.")
        cleaned, warnings = cs.validate_feedback(fb, "x", corpus)
        assert cleaned["forbidden_hits"] == []
        assert warnings == []

    def test_missing_keys_are_filled(self, corpus):
        cleaned, _ = cs.validate_feedback({}, "x", corpus)
        assert cleaned["observations"] == []
        assert cleaned["reading"] == ""
        assert cleaned["not_enough_signal"] is False


# ──────────────────────────────────────────────
# Prompt construction
# ──────────────────────────────────────────────

class TestBuildPrompt:

    def test_injects_every_principle(self, corpus):
        """No retrieval step — the model sees the whole corpus every time."""
        prompt = cs.build_prompt("entry text", corpus)
        for p in corpus["principles"]:
            assert f"[{p['id']}]" in prompt
            assert p["principle"][:40] in prompt

    def test_includes_the_rejects_field(self, corpus):
        """What each principle rejects is what catches off-framework drift."""
        prompt = cs.build_prompt("e", corpus)
        assert "REJECTS:" in prompt

    def test_forbids_generic_trading_psychology(self, corpus):
        prompt = cs.build_prompt("e", corpus).lower()
        assert "only source of truth" in prompt
        assert "do not drift" in prompt
        assert "be more disciplined" in prompt  # named as the failure mode

    def test_demands_citations(self, corpus):
        prompt = cs.build_prompt("e", corpus)
        assert "Never invent an id" in prompt
        assert "principle_id" in prompt

    def test_permits_abstention(self, corpus):
        assert "not_enough_signal" in cs.build_prompt("e", corpus)

    def test_entry_is_embedded(self, corpus):
        assert "my unique entry text" in cs.build_prompt("my unique entry text", corpus)

    def test_context_is_appended(self, corpus):
        assert "PRIOR STATE" in cs.build_prompt("e", corpus, context="PRIOR STATE: anxious")


# ──────────────────────────────────────────────
# Coachability threshold
# ──────────────────────────────────────────────

class TestIsCoachable:

    def test_rejects_short_entries(self):
        assert not cs.is_coachable("ES 5810.25")
        assert not cs.is_coachable("")
        assert not cs.is_coachable(None)
        assert not cs.is_coachable("   ")

    def test_accepts_substantive_entries(self):
        assert cs.is_coachable(
            "Stopped out and went straight back in to make it back, then lost that one too.")

    def test_threshold_is_the_boundary(self):
        assert not cs.is_coachable("x" * (cs.MIN_ENTRY_CHARS - 1))
        assert cs.is_coachable("x" * cs.MIN_ENTRY_CHARS)


# ──────────────────────────────────────────────
# coach()
# ──────────────────────────────────────────────

class TestCoach:

    @pytest.mark.asyncio
    async def test_returns_validated_feedback(self, corpus):
        entry = "I was exhausted and took a trade I'd never normally look at."
        raw = json.dumps({
            "reading": "You were running on empty.",
            "observations": [{"principle_id": "fc-physical",
                              "quote": "exhausted",
                              "point": "Bodily state moved your risk perception."}],
            "probe": "What did that tiredness make easier to ignore?",
            "not_enough_signal": False,
        })
        with patch("services.coach_service.llm_service.complete",
                   new=AsyncMock(return_value=raw)):
            result = await cs.coach(entry, corpus=corpus)

        assert result["observations"][0]["principle_id"] == "fc-physical"
        assert result["forbidden_hits"] == []

    @pytest.mark.asyncio
    async def test_drops_fabricated_citation_end_to_end(self, corpus):
        raw = json.dumps({
            "reading": "r",
            "observations": [{"principle_id": "invented", "quote": "q", "point": "p"}],
            "probe": "", "not_enough_signal": False,
        })
        with patch("services.coach_service.llm_service.complete",
                   new=AsyncMock(return_value=raw)):
            result = await cs.coach("some entry text here", corpus=corpus)
        assert result["observations"] == []
        assert result["warnings"]

    @pytest.mark.asyncio
    async def test_flags_off_framework_response_end_to_end(self, corpus):
        raw = json.dumps({
            "reading": "You need to remove emotion from your trading entirely.",
            "observations": [], "probe": "", "not_enough_signal": False,
        })
        with patch("services.coach_service.llm_service.complete",
                   new=AsyncMock(return_value=raw)):
            result = await cs.coach("entry", corpus=corpus)
        assert "remove emotion" in result["forbidden_hits"]

    @pytest.mark.asyncio
    async def test_requests_json_mode(self, corpus):
        complete = AsyncMock(return_value='{"reading":"r","observations":[]}')
        with patch("services.coach_service.llm_service.complete", new=complete):
            await cs.coach("entry", corpus=corpus)
        assert complete.await_args.kwargs["json_mode"] is True

    @pytest.mark.asyncio
    async def test_llm_failure_raises_coach_error(self, corpus):
        with patch("services.coach_service.llm_service.complete",
                   new=AsyncMock(side_effect=LLMError("HTTP 500"))):
            with pytest.raises(CoachError, match="HTTP 500"):
                await cs.coach("entry", corpus=corpus)

    @pytest.mark.asyncio
    async def test_unparseable_response_degrades_to_empty(self, corpus):
        with patch("services.coach_service.llm_service.complete",
                   new=AsyncMock(return_value="I refuse to answer in JSON")):
            result = await cs.coach("entry", corpus=corpus)
        assert result["observations"] == []


# ──────────────────────────────────────────────
# The eval scorer itself
# ──────────────────────────────────────────────

class TestEvalScorer:
    """A broken scorer would report false confidence, so it gets tested too."""

    def _fb(self, ids=(), text="some point", **kwargs):
        return {"reading": text, "probe": "",
                "observations": [{"principle_id": i, "quote": "q", "point": text} for i in ids],
                "warnings": [], "forbidden_hits": [], **kwargs}

    def test_positive_case_passes_on_expected_citation(self, corpus):
        case = {"id": "c", "kind": "positive", "expect_principles": ["fc-physical"]}
        result = score_case(case, self._fb(["fc-physical"]), corpus)
        assert result["passed"] and result["citation_hit"]

    def test_positive_case_fails_on_wrong_citation(self, corpus):
        case = {"id": "c", "kind": "positive", "expect_principles": ["fc-physical"]}
        result = score_case(case, self._fb(["ec-precision"]), corpus)
        assert not result["passed"] and not result["citation_hit"]

    def test_partial_citation_counts_as_hit(self, corpus):
        """Expecting two principles, citing one of them, is a hit."""
        case = {"id": "c", "kind": "positive",
                "expect_principles": ["fc-physical", "ec-precision"]}
        result = score_case(case, self._fb(["fc-physical"]), corpus)
        assert result["citation_hit"]

    def test_forbidden_language_fails_the_case(self, corpus):
        case = {"id": "c", "kind": "positive", "expect_principles": ["fc-physical"]}
        fb = self._fb(["fc-physical"], text="You need more discipline here.")
        result = score_case(case, fb, corpus)
        assert not result["passed"]
        assert not result["clean_language"]

    def test_dropped_citation_marks_ungrounded(self, corpus):
        case = {"id": "c", "kind": "positive", "expect_principles": ["fc-physical"]}
        fb = self._fb(["fc-physical"])
        fb["warnings"] = ["Dropped observation citing unknown principle 'x'."]
        result = score_case(case, fb, corpus)
        assert not result["grounded"] and not result["passed"]

    def test_abstain_case_passes_when_declining(self, corpus):
        case = {"id": "c", "kind": "abstain"}
        fb = self._fb([], text="")
        fb["not_enough_signal"] = True
        result = score_case(case, fb, corpus)
        assert result["passed"] and result["abstained"]

    def test_abstain_case_fails_when_it_invents_insight(self, corpus):
        """Manufacturing psychology from a price note is the failure."""
        case = {"id": "c", "kind": "abstain"}
        fb = self._fb(["fec-fractal"], text="This reveals deep childhood patterns.")
        result = score_case(case, fb, corpus)
        assert not result["passed"]

    def test_trap_case_needs_pushback(self, corpus):
        case = {"id": "c", "kind": "trap", "expect_principles": ["emotions-are-data"],
                "must_challenge": True}
        agreeable = self._fb(["emotions-are-data"], text="Yes, good plan, stick to it.")
        assert not score_case(case, agreeable, corpus)["passed"]

        pushback = self._fb(["emotions-are-data"],
                            text="That urgency is data rather than noise to be suppressed.")
        assert score_case(case, pushback, corpus)["passed"]

    def test_challenge_is_none_when_not_required(self, corpus):
        case = {"id": "c", "kind": "positive", "expect_principles": ["fc-physical"]}
        assert score_case(case, self._fb(["fc-physical"]), corpus)["challenged"] is None

    def test_report_computes_rates(self, corpus):
        results = [
            {"id": "a", "kind": "positive", "passed": True, "citation_hit": True,
             "grounded": True, "clean_language": True, "forbidden_hits": []},
            {"id": "b", "kind": "positive", "passed": False, "citation_hit": False,
             "grounded": True, "clean_language": False, "forbidden_hits": ["remove emotion"]},
        ]
        summary = report(results)
        assert summary["score"] == 0.5
        assert summary["citation_rate"] == 0.5
        assert summary["forbidden_incidents"] == 1

    def test_challenge_markers_are_lowercase(self):
        """The scorer lowercases the haystack before matching."""
        for marker in CHALLENGE_MARKERS:
            assert marker == marker.lower()


# ──────────────────────────────────────────────
# The eval suite as data
# ──────────────────────────────────────────────

class TestEvalSuite:

    @pytest.fixture
    def suite(self):
        path = Path(__file__).resolve().parent.parent / "evals" / "shull_evals.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_cases_are_well_formed(self, suite, corpus):
        valid = cs.principle_ids(corpus)
        for case in suite["cases"]:
            assert case["id"] and case["input"] and case["kind"]
            assert case["kind"] in ("positive", "trap", "abstain")
            for pid in case.get("expect_principles", []):
                assert pid in valid, f"{case['id']} expects unknown principle {pid}"

    def test_ids_unique(self, suite):
        ids = [c["id"] for c in suite["cases"]]
        assert len(ids) == len(set(ids))

    def test_has_traps(self, suite):
        """Without traps the suite can't detect drift to generic advice."""
        traps = [c for c in suite["cases"] if c["kind"] == "trap"]
        assert len(traps) >= 3
        assert all(c.get("must_challenge") for c in traps)

    def test_has_abstain_cases(self, suite):
        assert any(c["kind"] == "abstain" for c in suite["cases"])

    def test_abstain_inputs_are_genuinely_thin(self, suite):
        """An abstain case with emotional content would be testing the wrong thing."""
        emotional = ["felt", "angry", "scared", "furious", "anxious", "frustrated"]
        for case in suite["cases"]:
            if case["kind"] == "abstain":
                lowered = case["input"].lower()
                assert not any(w in lowered for w in emotional), case["id"]

    def test_trap_inputs_actually_contain_their_bait(self, suite):
        """
        Each trap declares the anti-Shull phrase it plants, and must contain it.
        The bait is first-person trader speech ("I need to be more disciplined"),
        which is deliberately NOT the same list as the runtime guardrail — that
        one is second-person, because it scans what the coach says.
        """
        for case in suite["cases"]:
            if case["kind"] == "trap":
                assert case.get("bait"), f"{case['id']} declares no bait"
                assert case["bait"] in case["input"], \
                    f"{case['id']} doesn't contain its declared bait {case['bait']!r}"


class TestGuardrailScope:
    """
    The guardrail scans what the COACH says, never what the TRADER said.

    Quoting a trader's "I need to be more disciplined" back at them is exactly
    how the coach challenges it, so flagging that as drift would punish the
    correct behaviour.
    """

    def test_trader_words_in_quote_field_do_not_trip_guardrail(self, corpus):
        entry = "I keep letting emotions ruin this. I need to be more disciplined."
        fb = {
            "reading": "You're asking to shut down the thing that's informing you.",
            "observations": [{
                "principle_id": "emotions-are-data",
                "quote": "I need to be more disciplined",   # trader's own words
                "point": "That instinct is the one Shull argues against — the feeling "
                         "doesn't disappear when suppressed, it just acts unobserved.",
            }],
            "probe": "What is the fear actually telling you?",
            "not_enough_signal": False,
        }
        cleaned, warnings = cs.validate_feedback(fb, entry, corpus)
        assert cleaned["forbidden_hits"] == []
        assert not any("Anti-Shull" in w for w in warnings)
        assert len(cleaned["observations"]) == 1

    def test_same_phrase_in_point_field_does_trip_guardrail(self, corpus):
        """Asserted by the coach rather than quoted, it's drift."""
        fb = {
            "reading": "", "probe": "", "not_enough_signal": False,
            "observations": [{
                "principle_id": "emotions-are-data", "quote": "",
                "point": "Agreed — you need to be more disciplined about this.",
            }],
        }
        cleaned, _ = cs.validate_feedback(fb, "x", corpus)
        assert "you need to be more disciplined" in cleaned["forbidden_hits"]
