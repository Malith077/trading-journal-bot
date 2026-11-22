"""
Unit tests for cogs/psych.py

Covers:
  - channel routing (trade_ vs missed_ trees)
  - /outcome persistence and guard rails
  - /state channel binding and save flow
  - StateView embed + ownership check
  - correlation entry collection
  - /psych_report rendering, including the conviction verdict
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from discord import ui

from cogs.psych import (
    Psych, StateView, OutcomeModal, _channel_dir, collect_correlation_entries,
)
from services import psych_service as ps


def button_named(view, needle):
    """@ui.button methods become bound Button children — fetch one by label."""
    for child in view.children:
        if isinstance(child, ui.Button) and child.label and needle in child.label:
            return child
    raise AssertionError(f"no button matching {needle!r} in {[getattr(c, 'label', c) for c in view.children]}")


@pytest.fixture
def cog():
    return Psych(MagicMock())


@pytest.fixture
def interaction():
    i = AsyncMock()
    i.response = AsyncMock()
    i.followup = AsyncMock()
    i.user = MagicMock()
    i.user.id = 42
    i.channel = MagicMock()
    i.channel.name = "trade_1_gc"
    return i


# ──────────────────────────────────────────────
# Channel routing
# ──────────────────────────────────────────────

class TestChannelRouting:

    def test_trade_channel_uses_trades_tree(self, tmp_path):
        with patch("cogs.psych.TRADES_DIR", tmp_path / "taken"), \
             patch("cogs.psych.MISSED_DIR", tmp_path / "missed"):
            assert _channel_dir("trade_1_gc") == tmp_path / "taken" / "trade_1_gc"

    def test_missed_channel_uses_missed_tree(self, tmp_path):
        with patch("cogs.psych.TRADES_DIR", tmp_path / "taken"), \
             patch("cogs.psych.MISSED_DIR", tmp_path / "missed"):
            assert _channel_dir("missed_1_gc") == tmp_path / "missed" / "missed_1_gc"


# ──────────────────────────────────────────────
# /outcome
# ──────────────────────────────────────────────

class TestOutcome:

    @pytest.mark.asyncio
    async def test_writes_outcome_file(self, cog, interaction, tmp_path):
        choice = MagicMock()
        choice.value = "win"
        with patch("cogs.psych.TRADES_DIR", tmp_path):
            await cog.outcome.callback(cog, interaction, choice, 2.5)

        saved = json.loads((tmp_path / "trade_1_gc" / "outcome.json").read_text())
        assert saved == {"outcome": "win", "r_multiple": 2.5}

    @pytest.mark.asyncio
    async def test_r_multiple_optional(self, cog, interaction, tmp_path):
        choice = MagicMock()
        choice.value = "breakeven"
        with patch("cogs.psych.TRADES_DIR", tmp_path):
            await cog.outcome.callback(cog, interaction, choice, None)

        saved = json.loads((tmp_path / "trade_1_gc" / "outcome.json").read_text())
        assert saved["r_multiple"] is None

    @pytest.mark.asyncio
    async def test_rejected_outside_trade_channels(self, cog, interaction, tmp_path):
        interaction.channel.name = "general"
        choice = MagicMock()
        choice.value = "win"
        with patch("cogs.psych.TRADES_DIR", tmp_path):
            await cog.outcome.callback(cog, interaction, choice, None)

        assert not (tmp_path / "general").exists()
        assert "trade channel" in interaction.response.send_message.call_args[0][0]

    @pytest.mark.asyncio
    async def test_works_in_missed_channel(self, cog, interaction, tmp_path):
        interaction.channel.name = "missed_2_es"
        choice = MagicMock()
        choice.value = "loss"
        with patch("cogs.psych.MISSED_DIR", tmp_path):
            await cog.outcome.callback(cog, interaction, choice, None)
        assert (tmp_path / "missed_2_es" / "outcome.json").exists()


class TestOutcomeModal:

    @pytest.mark.asyncio
    async def test_parses_r_multiple(self, tmp_path):
        modal = OutcomeModal("trade_1_gc", "win")
        modal.r_multiple._value = "2.5"
        i = AsyncMock()
        i.response = AsyncMock()
        i.response.is_done = MagicMock(return_value=False)

        with patch("cogs.psych.TRADES_DIR", tmp_path):
            await modal.on_submit(i)

        saved = json.loads((tmp_path / "trade_1_gc" / "outcome.json").read_text())
        assert saved["r_multiple"] == 2.5

    @pytest.mark.asyncio
    async def test_strips_r_suffix(self, tmp_path):
        modal = OutcomeModal("trade_1_gc", "win")
        modal.r_multiple._value = "3R"
        i = AsyncMock()
        i.response = AsyncMock()
        i.response.is_done = MagicMock(return_value=False)

        with patch("cogs.psych.TRADES_DIR", tmp_path):
            await modal.on_submit(i)
        assert json.loads((tmp_path / "trade_1_gc" / "outcome.json").read_text())["r_multiple"] == 3.0

    @pytest.mark.asyncio
    async def test_unparseable_r_still_saves_outcome(self, tmp_path):
        """A typo in the R field must not lose the outcome tag."""
        modal = OutcomeModal("trade_1_gc", "loss")
        modal.r_multiple._value = "about two"
        i = AsyncMock()
        i.response = AsyncMock()
        i.response.is_done = MagicMock(return_value=True)

        with patch("cogs.psych.TRADES_DIR", tmp_path):
            await modal.on_submit(i)

        saved = json.loads((tmp_path / "trade_1_gc" / "outcome.json").read_text())
        assert saved["outcome"] == "loss"
        assert saved["r_multiple"] is None


# ──────────────────────────────────────────────
# /state
# ──────────────────────────────────────────────

class TestState:

    @pytest.mark.asyncio
    async def test_binds_to_trade_channel(self, cog, interaction):
        await cog.state.callback(cog, interaction, None)
        view = interaction.response.send_message.call_args.kwargs["view"]
        assert view.channel_name == "trade_1_gc"

    @pytest.mark.asyncio
    async def test_standalone_outside_trade_channels(self, cog, interaction):
        """A check-in in #general is logged, but bound to no trade."""
        interaction.channel.name = "general"
        await cog.state.callback(cog, interaction, None)
        view = interaction.response.send_message.call_args.kwargs["view"]
        assert view.channel_name is None

    @pytest.mark.asyncio
    async def test_conviction_passed_through(self, cog, interaction):
        await cog.state.callback(cog, interaction, 8)
        view = interaction.response.send_message.call_args.kwargs["view"]
        assert view.conviction == 8


class TestStateView:

    def test_embed_renders_with_nothing_set(self):
        view = StateView(1, None)
        embed = view.build_embed()
        assert "Mark-to-Market" in embed.title
        fields = {f.name: f.value for f in embed.fields}
        assert fields["fC · Physical"] == "*not set*"

    def test_embed_reflects_selections(self):
        view = StateView(1, "trade_1_gc")
        view.feelings = "tired"
        view.emotions = ["anxious"]
        view.fear = -3
        embed = view.build_embed()
        fields = {f.name: f.value for f in embed.fields}
        assert "Tired / low energy" in fields["fC · Physical"]
        assert "Anxious" in fields["eC · Emotional"]
        assert "(-3)" in fields["Fear spectrum"]

    def test_embed_shows_impulse_read(self):
        view = StateView(1, None)
        view.probes = {"urgent": True, "recover": True, "body_tense": True}
        names = [f.name for f in view.build_embed().fields]
        assert any("impulse" in n for n in names)

    def test_to_state_builds_record(self):
        view = StateView(1, "trade_1_gc")
        view.feelings = "sharp"
        view.emotions = ["calm"]
        view.fear = 2
        view.conviction = 7
        state = view.to_state(author="trader")
        assert state["fear_zone"] == "fomo"
        assert state["conviction"] == 7
        assert state["channel"] == "trade_1_gc"
        assert state["author"] == "trader"

    @pytest.mark.asyncio
    async def test_other_users_cannot_edit(self):
        view = StateView(author_id=1, channel_name=None)
        i = AsyncMock()
        i.user = MagicMock()
        i.user.id = 999
        assert await view.interaction_check(i) is False
        i.response.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_owner_can_edit(self):
        view = StateView(author_id=1, channel_name=None)
        i = AsyncMock()
        i.user = MagicMock()
        i.user.id = 1
        assert await view.interaction_check(i) is True

    @pytest.mark.asyncio
    async def test_save_writes_log_and_channel_state(self, tmp_path):
        view = StateView(1, "trade_1_gc")
        view.feelings = "tired"
        view.emotions = ["anxious"]
        view.fear = -4

        i = AsyncMock()
        i.user = "trader"  # only str() is taken of it

        log = tmp_path / "psych_log.jsonl"
        with patch("cogs.psych.PSYCH_LOG_PATH", log), \
             patch("cogs.psych.TRADES_DIR", tmp_path / "trades"):
            await button_named(view, "Save").callback(i)

        assert view.saved is True
        states = await ps.read_states(log)
        assert len(states) == 1
        assert states[0]["fear_zone"] == "loss_fear"

        bound = json.loads((tmp_path / "trades" / "trade_1_gc" / "psych.json").read_text())
        assert bound["fear_spectrum"] == -4

    @pytest.mark.asyncio
    async def test_save_without_channel_only_writes_log(self, tmp_path):
        view = StateView(1, None)
        i = AsyncMock()
        i.user = "trader"

        log = tmp_path / "psych_log.jsonl"
        with patch("cogs.psych.PSYCH_LOG_PATH", log), \
             patch("cogs.psych.TRADES_DIR", tmp_path / "trades"):
            await button_named(view, "Save").callback(i)

        assert len(await ps.read_states(log)) == 1
        assert not (tmp_path / "trades").exists()


# ──────────────────────────────────────────────
# Correlation collection
# ──────────────────────────────────────────────

class TestCollectEntries:

    @pytest.mark.asyncio
    async def test_requires_both_state_and_outcome(self, tmp_path):
        trades = tmp_path / "trades"
        missed = tmp_path / "missed"

        both = trades / "trade_1_gc"
        both.mkdir(parents=True)
        (both / "psych.json").write_text(json.dumps(ps.build_state(fear=-3)))
        (both / "outcome.json").write_text(json.dumps({"outcome": "loss", "r_multiple": -1.0}))

        state_only = trades / "trade_2_es"
        state_only.mkdir()
        (state_only / "psych.json").write_text(json.dumps(ps.build_state(fear=1)))

        outcome_only = trades / "trade_3_nq"
        outcome_only.mkdir()
        (outcome_only / "outcome.json").write_text(json.dumps({"outcome": "win"}))

        missed.mkdir()
        with patch("cogs.psych.TRADES_DIR", trades), patch("cogs.psych.MISSED_DIR", missed):
            entries = await collect_correlation_entries()

        assert len(entries) == 1
        assert entries[0]["channel"] == "trade_1_gc"
        assert entries[0]["outcome"] == "loss"
        assert entries[0]["r_multiple"] == -1.0

    @pytest.mark.asyncio
    async def test_spans_both_trees(self, tmp_path):
        trades, missed = tmp_path / "trades", tmp_path / "missed"
        for base, name in ((trades, "trade_1_gc"), (missed, "missed_1_gc")):
            d = base / name
            d.mkdir(parents=True)
            (d / "psych.json").write_text(json.dumps(ps.build_state()))
            (d / "outcome.json").write_text(json.dumps({"outcome": "win"}))

        with patch("cogs.psych.TRADES_DIR", trades), patch("cogs.psych.MISSED_DIR", missed):
            entries = await collect_correlation_entries()
        assert {e["channel"] for e in entries} == {"trade_1_gc", "missed_1_gc"}

    @pytest.mark.asyncio
    async def test_missing_directories(self, tmp_path):
        with patch("cogs.psych.TRADES_DIR", tmp_path / "nope"), \
             patch("cogs.psych.MISSED_DIR", tmp_path / "nada"):
            assert await collect_correlation_entries() == []

    @pytest.mark.asyncio
    async def test_corrupt_json_skipped(self, tmp_path):
        trades = tmp_path / "trades"
        d = trades / "trade_1_gc"
        d.mkdir(parents=True)
        (d / "psych.json").write_text("{broken")
        (d / "outcome.json").write_text(json.dumps({"outcome": "win"}))

        with patch("cogs.psych.TRADES_DIR", trades), patch("cogs.psych.MISSED_DIR", tmp_path / "m"):
            assert await collect_correlation_entries() == []


# ──────────────────────────────────────────────
# /psych_report
# ──────────────────────────────────────────────

class TestPsychReport:

    @pytest.mark.asyncio
    async def test_empty_prompts_for_data(self, cog, interaction, tmp_path):
        with patch("cogs.psych.TRADES_DIR", tmp_path / "a"), \
             patch("cogs.psych.MISSED_DIR", tmp_path / "b"):
            await cog.psych_report.callback(cog, interaction, False)
        assert "Nothing to correlate" in interaction.followup.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_renders_report(self, cog, interaction, tmp_path):
        trades = tmp_path / "trades"
        for i, (fear, outcome, conviction) in enumerate([
            (-4, "loss", 2), (-3, "loss", 3), (0, "win", 9), (1, "win", 10),
        ], start=1):
            d = trades / f"trade_{i}_gc"
            d.mkdir(parents=True)
            state = ps.build_state(fear=fear, emotions=["anxious"], conviction=conviction)
            (d / "psych.json").write_text(json.dumps(state))
            (d / "outcome.json").write_text(json.dumps({"outcome": outcome, "r_multiple": 1.0}))

        with patch("cogs.psych.TRADES_DIR", trades), patch("cogs.psych.MISSED_DIR", tmp_path / "m"):
            await cog.psych_report.callback(cog, interaction, False)

        embed = interaction.followup.send.call_args.kwargs["embed"]
        assert "Psychological Edge Report" in embed.title
        fields = {f.name: f.value for f in embed.fields}
        assert "By fear spectrum" in fields
        assert "Fear of losing" in fields["By fear spectrum"]
        # Conviction tracked reality here, so the verdict should say so.
        assert "tracks reality" in fields["Conviction calibration"]

    @pytest.mark.asyncio
    async def test_report_flags_uncalibrated_conviction(self, cog, interaction, tmp_path):
        """The interesting finding: feeling sure and being right are unrelated."""
        trades = tmp_path / "trades"
        for i, (outcome, conviction) in enumerate([
            ("loss", 9), ("loss", 10), ("win", 1), ("win", 2),
        ], start=1):
            d = trades / f"trade_{i}_gc"
            d.mkdir(parents=True)
            (d / "psych.json").write_text(json.dumps(ps.build_state(conviction=conviction)))
            (d / "outcome.json").write_text(json.dumps({"outcome": outcome}))

        with patch("cogs.psych.TRADES_DIR", trades), patch("cogs.psych.MISSED_DIR", tmp_path / "m"):
            await cog.psych_report.callback(cog, interaction, False)

        fields = {f.name: f.value for f in interaction.followup.send.call_args.kwargs["embed"].fields}
        assert "does not predict outcome" in fields["Conviction calibration"]

    def test_fmt_handles_missing_metrics(self):
        bucket = {"n": 3, "win_rate": None, "avg_r": None}
        assert Psych._fmt(bucket) == "**3** trade(s)"

    def test_fmt_includes_metrics(self):
        bucket = {"n": 4, "win_rate": 0.75, "avg_r": 1.25}
        text = Psych._fmt(bucket)
        assert "75% win" in text and "+1.25R avg" in text

    @pytest.mark.asyncio
    async def test_deep_synthesis_invoked_when_requested(self, cog, interaction, tmp_path):
        trades = tmp_path / "trades"
        d = trades / "trade_1_gc"
        d.mkdir(parents=True)
        (d / "psych.json").write_text(json.dumps(ps.build_state(fear=0)))
        (d / "outcome.json").write_text(json.dumps({"outcome": "win"}))

        synth = {"headline": "You lose money when rushed.",
                 "patterns": ["p"], "fractal_probe": ["q"], "experiments": ["e"]}

        with patch("cogs.psych.TRADES_DIR", trades), \
             patch("cogs.psych.MISSED_DIR", tmp_path / "m"), \
             patch("cogs.psych.llm_service.complete_json", new=AsyncMock(return_value=synth)) as mock_llm:
            await cog.psych_report.callback(cog, interaction, True)

        mock_llm.assert_awaited_once()
        # The synthesis must use the reasoning model.
        assert mock_llm.await_args.kwargs["deep"] is True
        prompt = mock_llm.await_args[0][0]
        assert "Denise Shull" in prompt
        assert "Emotions are DATA" in prompt

    @pytest.mark.asyncio
    async def test_deep_synthesis_handles_empty_result(self, cog, interaction, tmp_path):
        trades = tmp_path / "trades"
        d = trades / "trade_1_gc"
        d.mkdir(parents=True)
        (d / "psych.json").write_text(json.dumps(ps.build_state()))
        (d / "outcome.json").write_text(json.dumps({"outcome": "win"}))

        empty = {"headline": "", "patterns": [], "fractal_probe": [], "experiments": []}
        with patch("cogs.psych.TRADES_DIR", trades), \
             patch("cogs.psych.MISSED_DIR", tmp_path / "m"), \
             patch("cogs.psych.llm_service.complete_json", new=AsyncMock(return_value=empty)):
            await cog.psych_report.callback(cog, interaction, True)

        status = interaction.followup.send.return_value
        assert "didn't return a usable synthesis" in status.edit.call_args.kwargs["content"]


class TestRethink:

    @pytest.mark.asyncio
    async def test_rethink_embed(self, cog, interaction):
        await cog.rethink.callback(cog, interaction)
        embed = interaction.response.send_message.call_args.kwargs["embed"]
        assert "Rethink" in embed.title
        text = " ".join(f"{f.name} {f.value}" for f in embed.fields).lower()
        # Shull's sequence: feel it, locate it, trace it, then decide.
        assert "remind you of" in text
        assert "body" in text
        # And never the framing she rejects.
        assert "be less emotional" not in text
