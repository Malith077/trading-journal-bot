"""
Unit tests for services/psych_service.py

Covers Denise Shull's framework as modelled here:
  - fear spectrum: clamping, zones, labels, gauge
  - intuition vs impulse classification
  - state record construction and JSONL round-trip
  - correlation of emotional state against outcomes
  - conviction calibration (does felt certainty predict being right?)
  - prompt context formatting
"""
import json
import pytest

from services import psych_service as ps


# ──────────────────────────────────────────────
# Fear spectrum
# ──────────────────────────────────────────────

class TestFearSpectrum:

    def test_clamp(self):
        assert ps.clamp_fear(99) == ps.FEAR_MAX
        assert ps.clamp_fear(-99) == ps.FEAR_MIN
        assert ps.clamp_fear(3) == 3

    @pytest.mark.parametrize("value,zone", [
        (-5, "loss_fear"), (-3, "loss_fear"), (-2, "loss_fear"),
        (-1, "balanced"), (0, "balanced"), (1, "balanced"),
        (2, "fomo"), (4, "fomo"), (5, "fomo"),
    ])
    def test_zones(self, value, zone):
        assert ps.fear_zone(value) == zone

    def test_zones_cover_the_whole_range(self):
        for v in range(ps.FEAR_MIN, ps.FEAR_MAX + 1):
            assert ps.fear_zone(v) in {"loss_fear", "balanced", "fomo"}

    def test_label_shows_signed_value(self):
        assert "(+3)" in ps.fear_label(3)
        assert "(-4)" in ps.fear_label(-4)
        assert "Fear of missing out" in ps.fear_label(3)
        assert "Fear of losing" in ps.fear_label(-4)

    def test_bar_marks_one_position(self):
        bar = ps.fear_bar(0)
        assert bar.count("●") == 1
        assert ps.fear_bar(ps.FEAR_MIN).index("●") < ps.fear_bar(ps.FEAR_MAX).index("●")


# ──────────────────────────────────────────────
# Intuition vs impulse
# ──────────────────────────────────────────────

class TestSignalClassification:

    def test_no_probes_reads_as_intuition(self):
        signal, fired = ps.classify_signal({})
        assert signal == ps.SIGNAL_INTUITION
        assert fired == 0

    def test_one_or_two_probes_is_mixed(self):
        assert ps.classify_signal({"urgent": True})[0] == ps.SIGNAL_MIXED
        assert ps.classify_signal({"urgent": True, "recover": True})[0] == ps.SIGNAL_MIXED

    def test_three_or_more_reads_as_impulse(self):
        signal, fired = ps.classify_signal(
            {"urgent": True, "recover": True, "body_tense": True}
        )
        assert signal == ps.SIGNAL_IMPULSE
        assert fired == 3

    def test_all_probes_fired(self):
        probes = {k: True for k in ps.IMPULSE_PROBE_KEYS}
        signal, fired = ps.classify_signal(probes)
        assert signal == ps.SIGNAL_IMPULSE
        assert fired == len(ps.IMPULSE_PROBE_KEYS)

    def test_unknown_keys_ignored(self):
        assert ps.classify_signal({"not_a_probe": True})[0] == ps.SIGNAL_INTUITION

    def test_false_probes_dont_count(self):
        probes = {k: False for k in ps.IMPULSE_PROBE_KEYS}
        assert ps.classify_signal(probes) == (ps.SIGNAL_INTUITION, 0)

    def test_every_signal_has_label_and_guidance(self):
        for signal in (ps.SIGNAL_INTUITION, ps.SIGNAL_MIXED, ps.SIGNAL_IMPULSE):
            assert ps.SIGNAL_LABELS[signal]
            assert ps.SIGNAL_GUIDANCE[signal]

    def test_guidance_never_tells_trader_to_suppress_emotion(self):
        """Shull explicitly rejects the 'control your emotions' framing."""
        banned = ["be less emotional", "control your emotion", "remove emotion",
                  "don't feel", "stop feeling"]
        for text in ps.SIGNAL_GUIDANCE.values():
            lowered = text.lower()
            for phrase in banned:
                assert phrase not in lowered, f"{phrase!r} found in guidance"


# ──────────────────────────────────────────────
# Taxonomy integrity
# ──────────────────────────────────────────────

class TestTaxonomies:

    def test_lookup_maps_match_source_lists(self):
        assert len(ps.EMOTION_LABELS) == len(ps.EMOTIONAL_CONTEXT)
        assert len(ps.FEELINGS_LABELS) == len(ps.FEELINGS_CONTEXT)
        assert len(ps.OUTCOME_LABELS) == len(ps.OUTCOMES)

    def test_emotion_keys_unique(self):
        keys = [k for k, _, _, _ in ps.EMOTIONAL_CONTEXT]
        assert len(keys) == len(set(keys))

    def test_discord_select_limits_respected(self):
        """Discord allows at most 25 options per select."""
        assert len(ps.EMOTIONAL_CONTEXT) <= 25
        assert len(ps.FEELINGS_CONTEXT) <= 25
        assert len(ps.IMPULSE_PROBES) <= 25
        assert (ps.FEAR_MAX - ps.FEAR_MIN + 1) <= 25

    def test_probe_text_fits_select_option_label(self):
        for _, text in ps.IMPULSE_PROBES:
            assert len(text) <= 100


# ──────────────────────────────────────────────
# State records
# ──────────────────────────────────────────────

class TestBuildState:

    def test_normalises_fields(self):
        state = ps.build_state(
            feelings="tired",
            emotions=["anxious", "anxious", "eager"],
            fear=99,
            conviction=7,
            probes={"urgent": True},
            reminds_me_of="last Thursday",
            channel="trade_1_gc",
        )
        assert state["feelings_context"] == "tired"
        assert state["emotional_context"] == ["anxious", "eager"]  # deduped + sorted
        assert state["fear_spectrum"] == ps.FEAR_MAX               # clamped
        assert state["fear_zone"] == "fomo"
        assert state["signal"] == ps.SIGNAL_MIXED
        assert state["probes_fired"] == 1
        assert state["fractal_context"] == "last Thursday"
        assert state["channel"] == "trade_1_gc"

    def test_all_probe_keys_recorded_even_when_unset(self):
        state = ps.build_state(probes={"urgent": True})
        assert set(state["impulse_probes"]) == set(ps.IMPULSE_PROBE_KEYS)
        assert state["impulse_probes"]["urgent"] is True
        assert state["impulse_probes"]["recover"] is False

    def test_defaults_are_safe(self):
        state = ps.build_state()
        assert state["emotional_context"] == []
        assert state["fear_spectrum"] == 0
        assert state["signal"] == ps.SIGNAL_INTUITION
        assert state["timestamp"]

    def test_timestamp_can_be_supplied(self):
        state = ps.build_state(timestamp="2026-01-01T00:00:00+00:00")
        assert state["timestamp"] == "2026-01-01T00:00:00+00:00"


class TestStateLog:

    @pytest.mark.asyncio
    async def test_append_and_read_roundtrip(self, tmp_path):
        log = tmp_path / "psych_log.jsonl"
        await ps.append_state(log, ps.build_state(fear=-3, emotions=["anxious"]))
        await ps.append_state(log, ps.build_state(fear=4, emotions=["eager"]))

        states = await ps.read_states(log)
        assert len(states) == 2
        assert states[0]["fear_zone"] == "loss_fear"
        assert states[1]["fear_zone"] == "fomo"

    @pytest.mark.asyncio
    async def test_read_missing_file(self, tmp_path):
        assert await ps.read_states(tmp_path / "nope.jsonl") == []

    @pytest.mark.asyncio
    async def test_corrupt_lines_are_skipped(self, tmp_path):
        log = tmp_path / "psych_log.jsonl"
        log.write_text('{"fear_spectrum": 1}\nnot json\n\n{"fear_spectrum": 2}\n')
        states = await ps.read_states(log)
        assert [s["fear_spectrum"] for s in states] == [1, 2]

    @pytest.mark.asyncio
    async def test_creates_parent_directory(self, tmp_path):
        log = tmp_path / "nested" / "deep" / "psych_log.jsonl"
        await ps.append_state(log, ps.build_state())
        assert log.exists()

    @pytest.mark.asyncio
    async def test_non_dict_lines_skipped(self, tmp_path):
        log = tmp_path / "psych_log.jsonl"
        log.write_text('[1,2,3]\n{"fear_spectrum": 5}\n')
        assert len(await ps.read_states(log)) == 1


# ──────────────────────────────────────────────
# Correlation
# ──────────────────────────────────────────────

def _entry(outcome, **kwargs):
    return {"outcome": outcome, **kwargs}


class TestCorrelate:

    def test_empty(self):
        stats = ps.correlate([])
        assert stats["overall"]["n"] == 0
        assert stats["overall"]["win_rate"] is None

    def test_entries_without_outcome_are_ignored(self):
        stats = ps.correlate([{"fear_zone": "fomo"}, _entry("win", fear_zone="fomo")])
        assert stats["overall"]["n"] == 1

    def test_invalid_outcome_ignored(self):
        stats = ps.correlate([_entry("maybe"), _entry("win")])
        assert stats["overall"]["n"] == 1

    def test_overall_win_rate_excludes_breakeven(self):
        """Breakeven counts as a trade but isn't a decided win or loss."""
        stats = ps.correlate([_entry("win"), _entry("loss"), _entry("breakeven")])
        overall = stats["overall"]
        assert overall["n"] == 3
        assert overall["decided"] == 2
        assert overall["win_rate"] == 0.5

    def test_by_fear_zone(self):
        stats = ps.correlate([
            _entry("win", fear_zone="balanced"),
            _entry("win", fear_zone="balanced"),
            _entry("loss", fear_zone="fomo"),
            _entry("loss", fear_zone="fomo"),
        ])
        assert stats["by_fear_zone"]["balanced"]["win_rate"] == 1.0
        assert stats["by_fear_zone"]["fomo"]["win_rate"] == 0.0

    def test_fear_zone_derived_from_spectrum_when_absent(self):
        stats = ps.correlate([_entry("win", fear_spectrum=-4)])
        assert stats["by_fear_zone"]["loss_fear"]["n"] == 1

    def test_by_emotion_counts_each_tagged_emotion(self):
        stats = ps.correlate([
            _entry("loss", emotional_context=["anxious", "impatient"]),
            _entry("win", emotional_context=["calm"]),
        ])
        assert stats["by_emotion"]["anxious"]["n"] == 1
        assert stats["by_emotion"]["impatient"]["n"] == 1
        assert stats["by_emotion"]["calm"]["win_rate"] == 1.0

    def test_by_signal(self):
        stats = ps.correlate([
            _entry("win", signal=ps.SIGNAL_INTUITION),
            _entry("loss", signal=ps.SIGNAL_IMPULSE),
        ])
        assert stats["by_signal"][ps.SIGNAL_INTUITION]["win_rate"] == 1.0
        assert stats["by_signal"][ps.SIGNAL_IMPULSE]["win_rate"] == 0.0

    def test_average_r_multiple(self):
        stats = ps.correlate([
            _entry("win", fear_zone="balanced", r_multiple=2.0),
            _entry("loss", fear_zone="balanced", r_multiple=-1.0),
        ])
        assert stats["by_fear_zone"]["balanced"]["avg_r"] == 0.5

    def test_missing_r_multiple_excluded_from_average(self):
        stats = ps.correlate([
            _entry("win", fear_zone="balanced", r_multiple=3.0),
            _entry("win", fear_zone="balanced"),  # no R logged
        ])
        assert stats["by_fear_zone"]["balanced"]["avg_r"] == 3.0
        assert stats["by_fear_zone"]["balanced"]["n"] == 2

    def test_non_numeric_r_multiple_ignored(self):
        stats = ps.correlate([_entry("win", fear_zone="balanced", r_multiple="2R")])
        assert stats["by_fear_zone"]["balanced"]["avg_r"] is None


class TestConvictionCalibration:

    def test_no_data(self):
        result = ps.calibrate_conviction([])
        assert result["predictive"] is None
        assert result["n"] == 0

    def test_predictive_when_high_beats_low(self):
        pairs = [(9, "win"), (10, "win"), (2, "loss"), (1, "loss")]
        result = ps.calibrate_conviction(pairs)
        assert result["predictive"] is True
        assert result["buckets"]["high"]["win_rate"] == 1.0
        assert result["buckets"]["low"]["win_rate"] == 0.0

    def test_not_predictive_when_high_does_not_beat_low(self):
        """Felt certainty that doesn't track reality is noise, not information."""
        pairs = [(9, "loss"), (10, "loss"), (2, "win"), (1, "win")]
        result = ps.calibrate_conviction(pairs)
        assert result["predictive"] is False

    def test_insufficient_evidence_returns_none(self):
        assert ps.calibrate_conviction([(9, "win")])["predictive"] is None
        assert ps.calibrate_conviction([(9, "win"), (10, "win")])["predictive"] is None

    def test_bucket_boundaries(self):
        result = ps.calibrate_conviction([(4, "win"), (5, "win"), (7, "win"), (8, "win")])
        assert result["buckets"]["low"]["n"] == 1      # 4
        assert result["buckets"]["medium"]["n"] == 2   # 5, 7
        assert result["buckets"]["high"]["n"] == 1     # 8

    def test_correlate_feeds_conviction_only_for_decided_trades(self):
        stats = ps.correlate([
            _entry("win", conviction=9),
            _entry("breakeven", conviction=9),  # not decided — excluded
        ])
        assert stats["conviction"]["n"] == 1


# ──────────────────────────────────────────────
# Prompt context
# ──────────────────────────────────────────────

class TestFormatStateContext:

    def test_empty_state_returns_empty_string(self):
        assert ps.format_state_context({}) == ""
        assert ps.format_state_context(None) == ""

    def test_renders_all_layers(self):
        state = ps.build_state(
            feelings="tired",
            emotions=["anxious"],
            fear=-4,
            conviction=3,
            probes={"urgent": True, "recover": True, "body_tense": True},
            reminds_me_of="the drawdown in March",
            note="third red day",
        )
        text = ps.format_state_context(state, {"outcome": "loss", "r_multiple": -1.0})

        assert "fC (physical): Tired / low energy" in text
        assert "eC (emotional): Anxious" in text
        assert "-4 on a -5" in text
        assert "impulse" in text
        assert "Conviction at the time: 3/10" in text
        assert "the drawdown in March" in text
        assert "third red day" in text
        assert "Loss at -1R" in text

    def test_outcome_optional(self):
        text = ps.format_state_context(ps.build_state(fear=2))
        assert "Actual result" not in text

    def test_analyst_guidance_rejects_suppression_framing(self):
        guidance = ps.SHULL_ANALYST_GUIDANCE.lower()
        assert "emotions are data" in guidance
        assert "fear spectrum" in guidance
        # It must instruct the model *against* the discipline framing.
        assert "rejects that framing" in guidance
