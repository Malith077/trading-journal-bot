"""
Unit tests for cogs/missed.py

Covers:
  - extract_missed_json(): clean JSON, markdown-wrapped, regex fallback, garbage
  - review.json persistence (modal + reason dropdown)
  - Missed.sync_missed(): category found/not found
  - Missed.analyze_missed(): no directory, up-to-date, new entries found
  - Missed.run_analysis_loop(): successful parse, fallback on failure
  - Missed.missed_stats(): empty, tagged, untagged
  - Missed.missed_trade(): numbering, category bootstrap, overflow
  - Separation from the taken-trades pipeline
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from cogs.missed import (
    extract_missed_json, Missed, MissedReviewModal, MissReasonSelect,
    MISS_REASONS, MISS_REASON_LABELS, MISSED_INSIGHT_KEYS, empty_missed_insights,
    MAX_RETRIES,
)
from services.llm_service import LLMError


# ──────────────────────────────────────────────
# extract_missed_json() tests
# ──────────────────────────────────────────────

class TestExtractMissedJson:

    def test_clean_json(self):
        raw = '{"miss_reasons": ["asleep"], "setup_patterns": ["asia sweep"], "action_items": ["set alert"]}'
        result = extract_missed_json(raw)
        assert result["miss_reasons"] == ["asleep"]
        assert result["setup_patterns"] == ["asia sweep"]
        assert result["action_items"] == ["set alert"]

    def test_markdown_wrapped_json(self):
        raw = '```json\n{"miss_reasons": ["hesitated"], "setup_patterns": [], "action_items": []}\n```'
        result = extract_missed_json(raw)
        assert result["miss_reasons"] == ["hesitated"]

    def test_json_with_surrounding_text(self):
        raw = 'Analysis:\n{"miss_reasons": ["was not at desk"], "setup_patterns": [], "action_items": []}\nDone!'
        result = extract_missed_json(raw)
        assert "was not at desk" in result["miss_reasons"]

    def test_totally_unparseable_uses_missed_schema(self):
        result = extract_missed_json("This is not JSON at all.")
        assert result == {"miss_reasons": [], "setup_patterns": [], "action_items": []}
        # Must NOT fall back to the taken-trade schema
        assert "good_habits" not in result
        assert "mistakes" not in result

    def test_empty_string(self):
        assert extract_missed_json("") == empty_missed_insights()

    def test_fallback_does_not_share_state(self):
        """Each fallback must be a fresh dict, not the module-level constant."""
        first = extract_missed_json("garbage")
        first["miss_reasons"].append("polluted")
        second = extract_missed_json("garbage")
        assert second["miss_reasons"] == []
        assert empty_missed_insights()["miss_reasons"] == []

    def test_json_with_unicode(self):
        raw = '{"miss_reasons": ["Waiting for C2 → C3 closure"], "setup_patterns": [], "action_items": []}'
        result = extract_missed_json(raw)
        assert "→" in result["miss_reasons"][0]


# ──────────────────────────────────────────────
# Missed cog command tests
# ──────────────────────────────────────────────

class TestMissedCog:

    @pytest.fixture
    def bot(self):
        bot = MagicMock()
        bot.loop = MagicMock()
        return bot

    @pytest.fixture
    def missed_cog(self, bot):
        return Missed(bot)

    @pytest.fixture
    def interaction(self):
        interaction = AsyncMock()
        interaction.guild = MagicMock()
        interaction.response = AsyncMock()
        interaction.followup = AsyncMock()
        return interaction

    # --- sync_missed ---

    @pytest.mark.asyncio
    async def test_sync_missed_no_category(self, missed_cog, interaction):
        interaction.guild.categories = []
        await missed_cog.sync_missed.callback(missed_cog, interaction)
        interaction.followup.send.assert_called_once()
        assert "Couldn't find category" in interaction.followup.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_sync_missed_ignores_fractal_trades(self, missed_cog, interaction):
        """A Fractal_Trades category must not be picked up by the missed sync."""
        fractal = MagicMock()
        fractal.name = "Fractal_Trades"
        interaction.guild.categories = [fractal]

        await missed_cog.sync_missed.callback(missed_cog, interaction)
        assert "Couldn't find category" in interaction.followup.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_sync_missed_with_category(self, missed_cog, interaction, tmp_path):
        mock_attachment = AsyncMock()
        mock_attachment.filename = "chart.png"
        mock_attachment.save = AsyncMock()

        mock_message = MagicMock()
        mock_message.id = 12345
        mock_message.content = "Saw the setup, was asleep"
        mock_message.created_at = MagicMock()
        mock_message.created_at.isoformat.return_value = "2026-01-01T00:00:00"
        mock_message.author.name = "trader"
        mock_message.attachments = [mock_attachment]

        mock_channel = MagicMock()
        mock_channel.name = "missed_1_gc"

        async def mock_history(**kwargs):
            yield mock_message
        mock_channel.history = mock_history

        mock_category = MagicMock()
        mock_category.name = "Missed_Trades"
        mock_category.text_channels = [mock_channel]
        interaction.guild.categories = [mock_category]

        with patch("cogs.missed.MISSED_DIR", tmp_path):
            await missed_cog.sync_missed.callback(missed_cog, interaction)

        assert interaction.followup.send.call_count >= 2
        assert "sync complete" in interaction.followup.send.call_args_list[-1][0][0].lower()

        history = json.loads((tmp_path / "missed_1_gc" / "history.json").read_text())
        assert history[0]["message_id"] == "12345"
        assert history[0]["images"] == ["12345_chart.png"]

    # --- analyze_missed ---

    @pytest.mark.asyncio
    async def test_analyze_missed_no_directory(self, missed_cog, interaction):
        with patch("cogs.missed.MISSED_DIR", Path("/nonexistent/path")):
            await missed_cog.analyze_missed.callback(missed_cog, interaction)
        interaction.followup.send.assert_called_once()
        assert "No missed trades directory found" in interaction.followup.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_analyze_missed_up_to_date(self, missed_cog, interaction, tmp_path):
        missed_dir = tmp_path / "missed"
        channel_dir = missed_dir / "missed_1_gc"
        channel_dir.mkdir(parents=True)
        (channel_dir / "history.json").write_text(json.dumps([{"message_id": "100", "content": "old"}]))

        tracker = tmp_path / "tracker.txt"
        tracker.write_text("100")

        with patch("cogs.missed.MISSED_DIR", missed_dir), \
             patch("cogs.missed.MISSED_TRACKER_PATH", tracker), \
             patch("cogs.missed.FAILED_MISSED_PATH", tmp_path / "failed.json"):
            await missed_cog.analyze_missed.callback(missed_cog, interaction)

        assert any("up to date" in str(c).lower() for c in interaction.followup.send.call_args_list)

    @pytest.mark.asyncio
    async def test_analyze_missed_finds_new_entries(self, missed_cog, interaction, tmp_path):
        missed_dir = tmp_path / "missed"
        channel_dir = missed_dir / "missed_1_gc"
        channel_dir.mkdir(parents=True)
        (channel_dir / "history.json").write_text(json.dumps([{"message_id": "200", "content": "new miss"}]))

        tracker = tmp_path / "tracker.txt"
        tracker.write_text("100")

        with patch("cogs.missed.MISSED_DIR", missed_dir), \
             patch("cogs.missed.MISSED_TRACKER_PATH", tracker), \
             patch("cogs.missed.FAILED_MISSED_PATH", tmp_path / "failed.json"), \
             patch.object(missed_cog.bot.loop, "create_task"):
            await missed_cog.analyze_missed.callback(missed_cog, interaction)

        assert any("missed trade(s) to process" in str(c) for c in interaction.followup.send.call_args_list)

    # --- missed_trade ---

    @pytest.mark.asyncio
    async def test_missed_trade_calculates_correct_number(self, missed_cog, interaction):
        cat = MagicMock()
        cat.name = "Missed_Trades"
        ch1 = MagicMock()
        ch1.name = "missed_4_gc"
        ch2 = MagicMock()
        ch2.name = "missed_11_es"
        cat.text_channels = [ch1, ch2]
        interaction.guild.categories = [cat]

        new_channel = MagicMock()
        new_channel.mention = "#missed_12_nq"
        cat.create_text_channel = AsyncMock(return_value=new_channel)

        await missed_cog.missed_trade.callback(missed_cog, interaction, "nq")

        cat.create_text_channel.assert_awaited_once_with(name="missed_12_nq")

    @pytest.mark.asyncio
    async def test_missed_trade_numbering_ignores_taken_trades(self, missed_cog, interaction):
        """trade_N_* channels must not influence missed_N_* numbering."""
        cat = MagicMock()
        cat.name = "Missed_Trades"
        stray = MagicMock()
        stray.name = "trade_99_gc"
        cat.text_channels = [stray]
        interaction.guild.categories = [cat]

        new_channel = MagicMock()
        cat.create_text_channel = AsyncMock(return_value=new_channel)

        await missed_cog.missed_trade.callback(missed_cog, interaction, "gc")

        cat.create_text_channel.assert_awaited_once_with(name="missed_1_gc")

    @pytest.mark.asyncio
    async def test_missed_trade_creates_category_when_absent(self, missed_cog, interaction):
        """Unlike /new_trade, a missing Missed_Trades category is bootstrapped."""
        interaction.guild.categories = []

        new_cat = MagicMock()
        new_cat.name = "Missed_Trades"
        new_cat.text_channels = []
        new_cat.create_text_channel = AsyncMock(return_value=MagicMock())
        interaction.guild.create_category = AsyncMock(return_value=new_cat)

        await missed_cog.missed_trade.callback(missed_cog, interaction, "es")

        interaction.guild.create_category.assert_awaited_once_with("Missed_Trades")
        new_cat.create_text_channel.assert_awaited_once_with(name="missed_1_es")

    @pytest.mark.asyncio
    async def test_missed_trade_creates_new_category_when_full(self, missed_cog, interaction):
        cat = MagicMock()
        cat.name = "Missed_Trades"
        cat.text_channels = [MagicMock(name=f"missed_{i}_gc") for i in range(50)]
        for i, ch in enumerate(cat.text_channels):
            ch.name = f"missed_{i+1}_gc"
        interaction.guild.categories = [cat]

        overflow = MagicMock()
        overflow.create_text_channel = AsyncMock(return_value=MagicMock())
        interaction.guild.create_category = AsyncMock(return_value=overflow)

        await missed_cog.missed_trade.callback(missed_cog, interaction, "cl")

        interaction.guild.create_category.assert_awaited_once_with("Missed_Trades2")
        overflow.create_text_channel.assert_awaited_once_with(name="missed_51_cl")

    @pytest.mark.asyncio
    async def test_missed_trade_sanitises_asset_name(self, missed_cog, interaction):
        cat = MagicMock()
        cat.name = "Missed_Trades"
        cat.text_channels = []
        cat.create_text_channel = AsyncMock(return_value=MagicMock())
        interaction.guild.categories = [cat]

        await missed_cog.missed_trade.callback(missed_cog, interaction, "Micro Gold-Futures")

        cat.create_text_channel.assert_awaited_once_with(name="missed_1_micro_gold_futures")

    @pytest.mark.asyncio
    async def test_mt_alias_calls_same_logic(self, missed_cog, interaction):
        with patch.object(missed_cog, "_do_missed_trade", new=AsyncMock()) as mock_do:
            await missed_cog.mt.callback(missed_cog, interaction, "gc")
        mock_do.assert_awaited_once_with(interaction, "gc")


# ──────────────────────────────────────────────
# Auto-sync listener tests
# ──────────────────────────────────────────────

class TestMissedAutoSync:

    @pytest.fixture
    def missed_cog(self):
        bot = MagicMock()
        bot.loop = MagicMock()
        return Missed(bot)

    def _message(self, category_name, is_bot=False, guild=True):
        message = MagicMock()
        message.author.bot = is_bot
        message.guild = MagicMock() if guild else None
        if message.guild:
            message.guild.id = 1
        if category_name is None:
            message.channel.category = None
        else:
            message.channel.category = MagicMock()
            message.channel.category.name = category_name
        return message

    @pytest.mark.asyncio
    async def test_ignores_bot_messages(self, missed_cog):
        await missed_cog.on_message(self._message("Missed_Trades", is_bot=True))
        missed_cog.bot.loop.create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_dm_messages(self, missed_cog):
        await missed_cog.on_message(self._message("Missed_Trades", guild=False))
        missed_cog.bot.loop.create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_fractal_trades_channels(self, missed_cog):
        """Taken-trade channels are handled by the Trades cog, not this one."""
        await missed_cog.on_message(self._message("Fractal_Trades"))
        missed_cog.bot.loop.create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_channels_without_category(self, missed_cog):
        await missed_cog.on_message(self._message(None))
        missed_cog.bot.loop.create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_schedules_sync_on_missed_message(self, missed_cog):
        await missed_cog.on_message(self._message("Missed_Trades"))
        missed_cog.bot.loop.create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_schedules_sync_on_missed_trades_2(self, missed_cog):
        await missed_cog.on_message(self._message("Missed_Trades2"))
        missed_cog.bot.loop.create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_debounce_cancels_previous_task(self, missed_cog):
        existing = MagicMock()
        missed_cog._pending_sync[1] = existing
        await missed_cog.on_message(self._message("Missed_Trades"))
        existing.cancel.assert_called_once()


# ──────────────────────────────────────────────
# Channel creation listener tests
# ──────────────────────────────────────────────

class TestMissedChannelCreate:

    @pytest.fixture
    def missed_cog(self):
        bot = MagicMock()
        bot.loop = MagicMock()
        return Missed(bot)

    def _channel(self, category_name, cls=None):
        import discord
        channel = MagicMock(spec=cls or discord.TextChannel)
        channel.name = "missed_1_gc"
        channel.send = AsyncMock()
        if category_name is None:
            channel.category = None
        else:
            channel.category = MagicMock()
            channel.category.name = category_name
        return channel

    @pytest.mark.asyncio
    async def test_posts_review_prompt(self, missed_cog):
        channel = self._channel("Missed_Trades")
        await missed_cog.on_guild_channel_create(channel)

        channel.send.assert_awaited_once()
        embed = channel.send.call_args.kwargs["embed"]
        assert "Missed Setup" in embed.title
        assert channel.send.call_args.kwargs["view"] is not None

    @pytest.mark.asyncio
    async def test_posts_no_checklist(self, missed_cog):
        """The entry checklist is a pre-entry gate — irrelevant to an untaken setup."""
        channel = self._channel("Missed_Trades")
        await missed_cog.on_guild_channel_create(channel)
        assert channel.send.await_count == 1

    @pytest.mark.asyncio
    async def test_ignores_fractal_trades_category(self, missed_cog):
        channel = self._channel("Fractal_Trades")
        await missed_cog.on_guild_channel_create(channel)
        channel.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ignores_voice_channels(self, missed_cog):
        import discord
        channel = self._channel("Missed_Trades", cls=discord.VoiceChannel)
        await missed_cog.on_guild_channel_create(channel)
        channel.send.assert_not_awaited()


# ──────────────────────────────────────────────
# Review persistence tests
# ──────────────────────────────────────────────

class TestMissedReview:

    @pytest.mark.asyncio
    async def test_modal_saves_review(self, tmp_path):
        modal = MissedReviewModal("missed_1_gc")
        modal.setup_seen._value = "London swept Asia high"
        modal.why_missed._value = "Was asleep"
        modal.what_happened._value = "Ran 4R"
        modal.what_changes._value = "Set an alert"

        interaction = AsyncMock()
        interaction.response = AsyncMock()
        interaction.original_response = AsyncMock(return_value=AsyncMock())

        with patch("cogs.missed.MISSED_DIR", tmp_path):
            await modal.on_submit(interaction)

        saved = json.loads((tmp_path / "missed_1_gc" / "review.json").read_text())
        assert saved["setup_seen"] == "London swept Asia high"
        assert saved["why_missed"] == "Was asleep"
        assert saved["what_happened"] == "Ran 4R"
        assert saved["what_changes"] == "Set an alert"

    @pytest.mark.asyncio
    async def test_reason_select_saves_category(self, tmp_path):
        select = MissReasonSelect()
        select._values = ["not_at_desk"]

        interaction = AsyncMock()
        interaction.channel.name = "missed_1_gc"

        with patch("cogs.missed.MISSED_DIR", tmp_path):
            await select.callback(interaction)

        saved = json.loads((tmp_path / "missed_1_gc" / "review.json").read_text())
        assert saved["reason_category"] == "not_at_desk"

    @pytest.mark.asyncio
    async def test_reason_select_preserves_existing_review(self, tmp_path):
        channel_dir = tmp_path / "missed_1_gc"
        channel_dir.mkdir()
        (channel_dir / "review.json").write_text(json.dumps({"why_missed": "Was asleep"}))

        select = MissReasonSelect()
        select._values = ["hesitated"]

        interaction = AsyncMock()
        interaction.channel.name = "missed_1_gc"

        with patch("cogs.missed.MISSED_DIR", tmp_path):
            await select.callback(interaction)

        saved = json.loads((channel_dir / "review.json").read_text())
        assert saved["why_missed"] == "Was asleep"
        assert saved["reason_category"] == "hesitated"

    def test_insight_keys_match_factory(self):
        assert set(empty_missed_insights()) == set(MISSED_INSIGHT_KEYS)

    def test_every_reason_has_a_label(self):
        assert len(MISS_REASON_LABELS) == len(MISS_REASONS)
        for value, label, emoji in MISS_REASONS:
            assert MISS_REASON_LABELS[value] == label
            assert emoji

    @pytest.mark.asyncio
    async def test_review_context_with_file(self, tmp_path):
        cog = Missed(MagicMock())
        channel_dir = tmp_path / "missed_1_gc"
        channel_dir.mkdir()
        (channel_dir / "review.json").write_text(json.dumps({
            "reason_category": "not_at_desk",
            "setup_seen": "Asia sweep",
            "why_missed": "Asleep",
            "what_happened": "Ran 4R",
            "what_changes": "Set an alert",
        }))

        context = await cog._get_review_context({"_folder_path": str(channel_dir)})
        assert "Wasn't at the desk" in context
        assert "Asia sweep" in context
        assert "Set an alert" in context

    @pytest.mark.asyncio
    async def test_review_context_no_file(self, tmp_path):
        cog = Missed(MagicMock())
        assert await cog._get_review_context({"_folder_path": str(tmp_path)}) == ""

    @pytest.mark.asyncio
    async def test_review_context_corrupt_file(self, tmp_path):
        cog = Missed(MagicMock())
        (tmp_path / "review.json").write_text("{not json")
        assert await cog._get_review_context({"_folder_path": str(tmp_path)}) == ""

    @pytest.mark.asyncio
    async def test_review_context_untagged(self, tmp_path):
        cog = Missed(MagicMock())
        (tmp_path / "review.json").write_text(json.dumps({"why_missed": "Asleep"}))
        context = await cog._get_review_context({"_folder_path": str(tmp_path)})
        assert "Not tagged" in context


# ──────────────────────────────────────────────
# Analysis loop tests
# ──────────────────────────────────────────────

class TestMissedAnalysisLoop:

    @pytest.fixture
    def missed_cog(self):
        bot = MagicMock()
        bot.loop = MagicMock()
        return Missed(bot)

    @pytest.fixture
    def interaction(self):
        interaction = AsyncMock()
        interaction.followup = AsyncMock()
        return interaction

    @pytest.mark.asyncio
    async def test_successful_analysis(self, missed_cog, interaction, tmp_path):
        insights = tmp_path / "missed_insights.json"
        tracker = tmp_path / "tracker.txt"

        entries = [{"message_id": "300", "content": "Missed the London short",
                    "images": [], "_folder_path": str(tmp_path)}]

        complete = AsyncMock(return_value='{"miss_reasons": ["Asleep at London open"], '
                        '"setup_patterns": ["Asia high sweep"], '
                        '"action_items": ["Set a price alert"]}')

        with patch("cogs.missed.MISSED_INSIGHTS_PATH", insights), \
             patch("cogs.missed.MISSED_TRACKER_PATH", tracker), \
             patch("cogs.missed.FAILED_MISSED_PATH", tmp_path / "failed.json"), \
             patch("cogs.missed.llm_service.complete", new=complete):
            await missed_cog.run_analysis_loop(interaction, entries, 0)

        saved = json.loads(insights.read_text())
        assert "Asleep at London open" in saved["miss_reasons"]
        assert "Asia high sweep" in saved["setup_patterns"]
        assert "Set a price alert" in saved["action_items"]
        assert tracker.read_text() == "300"

    @pytest.mark.asyncio
    async def test_api_failure_uses_fallback(self, missed_cog, interaction, tmp_path):
        insights = tmp_path / "missed_insights.json"
        tracker = tmp_path / "tracker.txt"

        entries = [{"message_id": "400", "content": "Missed it",
                    "images": [], "_folder_path": str(tmp_path)}]

        # The service raises once it has exhausted its own retries.
        complete = AsyncMock(side_effect=LLMError("DeepSeek HTTP 500"))

        with patch("cogs.missed.MISSED_INSIGHTS_PATH", insights), \
             patch("cogs.missed.MISSED_TRACKER_PATH", tracker), \
             patch("cogs.missed.FAILED_MISSED_PATH", tmp_path / "failed.json"), \
             patch("cogs.missed.RETRY_DELAY", 0), \
             patch("cogs.missed.llm_service.complete", new=complete):
            await missed_cog.run_analysis_loop(interaction, entries, 0)

        assert complete.await_args.kwargs["retries"] == MAX_RETRIES
        # Tracker still advances so a bad entry is never retried forever
        assert tracker.read_text() == "400"
        saved = json.loads(insights.read_text())
        assert saved["miss_reasons"] == []

    @pytest.mark.asyncio
    async def test_merges_into_existing_insights(self, missed_cog, interaction, tmp_path):
        insights = tmp_path / "missed_insights.json"
        insights.write_text(json.dumps({"miss_reasons": ["Old reason"],
                                        "setup_patterns": [], "action_items": []}))
        tracker = tmp_path / "tracker.txt"

        entries = [{"message_id": "500", "content": "Missed it",
                    "images": [], "_folder_path": str(tmp_path)}]

        complete = AsyncMock(return_value='{"miss_reasons": ["New reason"], "setup_patterns": [], "action_items": []}')

        with patch("cogs.missed.MISSED_INSIGHTS_PATH", insights), \
             patch("cogs.missed.MISSED_TRACKER_PATH", tracker), \
             patch("cogs.missed.FAILED_MISSED_PATH", tmp_path / "failed.json"), \
             patch("cogs.missed.llm_service.complete", new=complete):
            await missed_cog.run_analysis_loop(interaction, entries, 0)

        saved = json.loads(insights.read_text())
        assert set(saved["miss_reasons"]) == {"Old reason", "New reason"}

    @pytest.mark.asyncio
    async def test_tolerates_legacy_insights_schema(self, missed_cog, interaction, tmp_path):
        """An insights file missing newer keys must not crash the loop."""
        insights = tmp_path / "missed_insights.json"
        insights.write_text(json.dumps({"miss_reasons": ["Old reason"]}))
        tracker = tmp_path / "tracker.txt"

        entries = [{"message_id": "600", "content": "Missed it",
                    "images": [], "_folder_path": str(tmp_path)}]

        complete = AsyncMock(return_value='{"miss_reasons": [], "setup_patterns": ["p"], "action_items": ["a"]}')

        with patch("cogs.missed.MISSED_INSIGHTS_PATH", insights), \
             patch("cogs.missed.MISSED_TRACKER_PATH", tracker), \
             patch("cogs.missed.FAILED_MISSED_PATH", tmp_path / "failed.json"), \
             patch("cogs.missed.llm_service.complete", new=complete):
            await missed_cog.run_analysis_loop(interaction, entries, 0)

        saved = json.loads(insights.read_text())
        assert saved["setup_patterns"] == ["p"]
        assert saved["action_items"] == ["a"]

    def test_prompt_states_trade_was_not_taken(self):
        prompt = Missed._build_prompt({"content": "Saw it, skipped it"}, "")
        assert "NEVER TAKEN" in prompt
        assert "miss_reasons" in prompt
        # Must not ask for the taken-trade schema
        assert "good_habits" not in prompt
        assert "mistakes" not in prompt

    def test_prompt_includes_review_context(self):
        prompt = Missed._build_prompt({"content": "notes"}, "\nTrader's Missed-Trade Review:\n- Tagged reason: X\n")
        assert "Tagged reason: X" in prompt


# ──────────────────────────────────────────────
# Stats tests
# ──────────────────────────────────────────────

class TestMissedStats:

    @pytest.fixture
    def missed_cog(self):
        return Missed(MagicMock())

    @pytest.fixture
    def interaction(self):
        interaction = AsyncMock()
        interaction.response = AsyncMock()
        interaction.followup = AsyncMock()
        return interaction

    @pytest.mark.asyncio
    async def test_stats_empty(self, missed_cog, interaction, tmp_path):
        with patch("cogs.missed.MISSED_DIR", tmp_path):
            await missed_cog.missed_stats.callback(missed_cog, interaction)
        assert "No missed trades logged yet" in interaction.followup.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_stats_no_directory(self, missed_cog, interaction):
        with patch("cogs.missed.MISSED_DIR", Path("/nonexistent/path")):
            await missed_cog.missed_stats.callback(missed_cog, interaction)
        assert "No missed trades logged yet" in interaction.followup.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_stats_counts_reasons_and_assets(self, missed_cog, interaction, tmp_path):
        for name, reason in [("missed_1_gc", "not_at_desk"),
                             ("missed_2_gc", "not_at_desk"),
                             ("missed_3_es", "hesitated")]:
            d = tmp_path / name
            d.mkdir()
            (d / "review.json").write_text(json.dumps({"reason_category": reason}))

        with patch("cogs.missed.MISSED_DIR", tmp_path):
            await missed_cog.missed_stats.callback(missed_cog, interaction)

        embed = interaction.followup.send.call_args.kwargs["embed"]
        assert "**3** missed setup(s) logged." in embed.description

        fields = {f.name: f.value for f in embed.fields}
        assert "Wasn't at the desk" in fields["Why they were missed"]
        assert "**2** (66%)" in fields["Why they were missed"]
        assert "**GC** — 2" in fields["📊 Most missed assets"]
        assert "**ES** — 1" in fields["📊 Most missed assets"]

    @pytest.mark.asyncio
    async def test_stats_reports_untagged(self, missed_cog, interaction, tmp_path):
        (tmp_path / "missed_1_gc").mkdir()  # no review.json at all
        d = tmp_path / "missed_2_es"
        d.mkdir()
        (d / "review.json").write_text(json.dumps({"why_missed": "text only"}))

        with patch("cogs.missed.MISSED_DIR", tmp_path):
            await missed_cog.missed_stats.callback(missed_cog, interaction)

        embed = interaction.followup.send.call_args.kwargs["embed"]
        fields = {f.name: f.value for f in embed.fields}
        assert "2 missed trade(s) have no reason tagged yet." in fields["🏷️ Untagged"]

    @pytest.mark.asyncio
    async def test_stats_ignores_stray_files(self, missed_cog, interaction, tmp_path):
        (tmp_path / "missed_1_gc").mkdir()
        (tmp_path / "stray.json").write_text("{}")

        with patch("cogs.missed.MISSED_DIR", tmp_path):
            await missed_cog.missed_stats.callback(missed_cog, interaction)

        embed = interaction.followup.send.call_args.kwargs["embed"]
        assert "**1** missed setup(s) logged." in embed.description


# ──────────────────────────────────────────────
# Shull psychology integration
# ──────────────────────────────────────────────

class TestMissedShullIntegration:

    def test_every_reason_has_a_fear_mapping(self):
        from cogs.missed import MISS_REASON_FEARS
        for value, _, _ in MISS_REASONS:
            assert value in MISS_REASON_FEARS

    def test_operational_reasons_are_not_labelled_as_fear(self):
        """Being asleep isn't a fear response — mislabelling it would be noise."""
        from cogs.missed import MISS_REASON_FEARS
        assert MISS_REASON_FEARS["not_at_desk"] is None
        assert MISS_REASON_FEARS["no_fill"] is None
        assert MISS_REASON_FEARS["risk_rules"] is None

    def test_hesitation_maps_to_fear_of_being_wrong(self):
        from cogs.missed import MISS_REASON_FEARS
        assert MISS_REASON_FEARS["hesitated"] == "fear of being wrong"
        assert MISS_REASON_FEARS["waited_too_long"] == "fear of being wrong"

    def test_prompt_carries_shull_guidance(self):
        prompt = Missed._build_prompt({"content": "notes"}, "")
        assert "Emotions are DATA" in prompt
        assert "fear spectrum" in prompt
        # The prompt must explicitly forbid the framing Shull rejects.
        assert 'never write "be less emotional"' in prompt.lower()

    @pytest.mark.asyncio
    async def test_review_context_includes_fear_for_hesitation(self, tmp_path):
        cog = Missed(MagicMock())
        d = tmp_path / "missed_1_gc"
        d.mkdir()
        (d / "review.json").write_text(json.dumps({"reason_category": "hesitated"}))
        context = await cog._get_review_context({"_folder_path": str(d)})
        assert "fear of being wrong" in context

    @pytest.mark.asyncio
    async def test_review_context_omits_fear_for_operational_miss(self, tmp_path):
        cog = Missed(MagicMock())
        d = tmp_path / "missed_1_gc"
        d.mkdir()
        (d / "review.json").write_text(json.dumps({"reason_category": "not_at_desk"}))
        context = await cog._get_review_context({"_folder_path": str(d)})
        assert "Fear most often behind" not in context

    @pytest.mark.asyncio
    async def test_review_context_includes_psych_state(self, tmp_path):
        from services import psych_service as ps
        cog = Missed(MagicMock())
        d = tmp_path / "missed_1_gc"
        d.mkdir()
        (d / "review.json").write_text(json.dumps({"reason_category": "hesitated"}))
        (d / "psych.json").write_text(json.dumps(ps.build_state(fear=-4, emotions=["anxious"])))

        context = await cog._get_review_context({"_folder_path": str(d)})
        assert "Psychological State" in context
        assert "-4 on a -5" in context

    @pytest.mark.asyncio
    async def test_psych_context_absent_when_unlogged(self, tmp_path):
        cog = Missed(MagicMock())
        assert await cog._get_psych_context(tmp_path) == ""

    @pytest.mark.asyncio
    async def test_stats_reports_fear_driven_share(self, tmp_path):
        cog = Missed(MagicMock())
        interaction = AsyncMock()
        interaction.response = AsyncMock()
        interaction.followup = AsyncMock()

        for name, reason in [("missed_1_gc", "hesitated"),
                             ("missed_2_es", "waited_too_long"),
                             ("missed_3_nq", "not_at_desk")]:
            d = tmp_path / name
            d.mkdir()
            (d / "review.json").write_text(json.dumps({"reason_category": reason}))

        with patch("cogs.missed.MISSED_DIR", tmp_path):
            await cog.missed_stats.callback(cog, interaction)

        fields = {f.name: f.value for f in interaction.followup.send.call_args.kwargs["embed"].fields}
        assert "🧠 The Shull read" in fields
        assert "**2** of **3**" in fields["🧠 The Shull read"]

    @pytest.mark.asyncio
    async def test_stats_omits_shull_read_when_no_fear_driven_misses(self, tmp_path):
        cog = Missed(MagicMock())
        interaction = AsyncMock()
        interaction.response = AsyncMock()
        interaction.followup = AsyncMock()

        d = tmp_path / "missed_1_gc"
        d.mkdir()
        (d / "review.json").write_text(json.dumps({"reason_category": "not_at_desk"}))

        with patch("cogs.missed.MISSED_DIR", tmp_path):
            await cog.missed_stats.callback(cog, interaction)

        fields = {f.name for f in interaction.followup.send.call_args.kwargs["embed"].fields}
        assert "🧠 The Shull read" not in fields
