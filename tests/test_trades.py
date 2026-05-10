"""
Unit tests for cogs/trades.py

Covers:
  - extract_json(): clean JSON, markdown-wrapped, regex fallback, total garbage
  - Trades.sync_fractals(): category found/not found, message + image sync
  - Trades.analyze_trades(): no directory, up-to-date, new trades found
  - Trades.run_analysis_loop(): successful parse, fallback on failure
"""
import json
import pytest
import asyncio
import discord
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from cogs.trades import extract_json, Trades, ReflectionModal, MAX_RETRIES


# ──────────────────────────────────────────────
# extract_json() tests
# ──────────────────────────────────────────────

class TestExtractJson:

    def test_clean_json(self):
        raw = '{"good_habits": ["habit1"], "mistakes": ["mistake1"]}'
        result = extract_json(raw)
        assert result["good_habits"] == ["habit1"]
        assert result["mistakes"] == ["mistake1"]

    def test_markdown_wrapped_json(self):
        raw = '```json\n{"good_habits": ["habit1"], "mistakes": []}\n```'
        result = extract_json(raw)
        assert result["good_habits"] == ["habit1"]
        assert result["mistakes"] == []

    def test_json_with_surrounding_text(self):
        raw = 'Here is my analysis:\n{"good_habits": ["waited for C2"], "mistakes": []}\nDone!'
        result = extract_json(raw)
        assert "waited for C2" in result["good_habits"]

    def test_totally_unparseable(self):
        raw = "This is not JSON at all, just random text."
        result = extract_json(raw)
        assert result == {"good_habits": [], "mistakes": []}

    def test_empty_string(self):
        result = extract_json("")
        assert result == {"good_habits": [], "mistakes": []}

    def test_json_with_extra_whitespace(self):
        raw = '  \n  {"good_habits": ["x"], "mistakes": ["y"]}  \n  '
        result = extract_json(raw)
        assert result["good_habits"] == ["x"]

    def test_json_with_unicode(self):
        raw = '{"good_habits": ["Waiting for C2 → C3 closure"], "mistakes": []}'
        result = extract_json(raw)
        assert "→" in result["good_habits"][0]


# ──────────────────────────────────────────────
# Trades cog command tests
# ──────────────────────────────────────────────

class TestTradesCog:

    @pytest.fixture
    def bot(self):
        bot = MagicMock()
        bot.loop = asyncio.get_event_loop()
        return bot

    @pytest.fixture
    def trades_cog(self, bot):
        return Trades(bot)

    @pytest.fixture
    def ctx(self):
        ctx = AsyncMock()
        ctx.guild = MagicMock()
        ctx.send = AsyncMock()
        return ctx

    # --- sync_fractals ---

    @pytest.mark.asyncio
    async def test_sync_fractals_no_category(self, trades_cog, ctx):
        ctx.guild.categories = []
        await trades_cog.sync_fractals.callback(trades_cog, ctx)
        ctx.send.assert_called_once()
        assert "Couldn't find category" in ctx.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_sync_fractals_with_category(self, trades_cog, ctx, tmp_path):
        mock_attachment = AsyncMock()
        mock_attachment.filename = "chart.png"
        mock_attachment.save = AsyncMock()

        mock_message = MagicMock()
        mock_message.id = 12345
        mock_message.content = "Trade notes"
        mock_message.created_at = MagicMock()
        mock_message.created_at.isoformat.return_value = "2026-01-01T00:00:00"
        mock_message.author.name = "trader"
        mock_message.attachments = [mock_attachment]

        mock_channel = MagicMock()
        mock_channel.name = "test_channel"

        async def mock_history(**kwargs):
            yield mock_message
        mock_channel.history = mock_history

        mock_category = MagicMock()
        mock_category.name = "Fractal_Trades"
        mock_category.text_channels = [mock_channel]
        ctx.guild.categories = [mock_category]

        with patch("cogs.trades.TRADES_DIR", tmp_path):
            await trades_cog.sync_fractals.callback(trades_cog, ctx)

        assert ctx.send.call_count >= 2
        assert "Sync complete" in ctx.send.call_args_list[-1][0][0]

    # --- analyze_trades ---

    @pytest.mark.asyncio
    async def test_analyze_trades_no_directory(self, trades_cog, ctx):
        with patch("cogs.trades.TRADES_DIR", Path("/nonexistent/path")):
            await trades_cog.analyze_trades.callback(trades_cog, ctx)
        ctx.send.assert_called_once()
        assert "No trades directory found" in ctx.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_analyze_trades_up_to_date(self, trades_cog, ctx, tmp_path):
        trades_dir = tmp_path / "trades"
        trades_dir.mkdir()
        channel_dir = trades_dir / "channel1"
        channel_dir.mkdir()

        history = [{"message_id": "100", "content": "old trade"}]
        (channel_dir / "history.json").write_text(json.dumps(history))

        tracker = tmp_path / "tracker.txt"
        tracker.write_text("100")
        failed_path = tmp_path / "failed.json"

        with patch("cogs.trades.TRADES_DIR", trades_dir), \
             patch("cogs.trades.TRACKER_PATH", tracker), \
             patch("cogs.trades.FAILED_TRADES_PATH", failed_path):
            await trades_cog.analyze_trades.callback(trades_cog, ctx)

        assert any("up to date" in str(c).lower() for c in ctx.send.call_args_list)

    @pytest.mark.asyncio
    async def test_analyze_trades_finds_new_trades(self, trades_cog, ctx, tmp_path):
        trades_dir = tmp_path / "trades"
        trades_dir.mkdir()
        channel_dir = trades_dir / "channel1"
        channel_dir.mkdir()

        history = [{"message_id": "200", "content": "new trade"}]
        (channel_dir / "history.json").write_text(json.dumps(history))

        tracker = tmp_path / "tracker.txt"
        tracker.write_text("100")
        failed_path = tmp_path / "failed.json"

        with patch("cogs.trades.TRADES_DIR", trades_dir), \
             patch("cogs.trades.TRACKER_PATH", tracker), \
             patch("cogs.trades.FAILED_TRADES_PATH", failed_path), \
             patch.object(trades_cog.bot.loop, "create_task"):
            await trades_cog.analyze_trades.callback(trades_cog, ctx)

        assert any("1" in str(c) and "trade" in str(c).lower()
                    for c in ctx.send.call_args_list)


# ──────────────────────────────────────────────
# run_analysis_loop tests
# ──────────────────────────────────────────────

class TestRunAnalysisLoop:

    @pytest.fixture
    def trades_cog(self):
        return Trades(MagicMock())

    @pytest.fixture
    def ctx(self):
        ctx = AsyncMock()
        status_msg = AsyncMock()
        ctx.send = AsyncMock(return_value=status_msg)
        return ctx

    @pytest.mark.asyncio
    async def test_successful_analysis(self, trades_cog, ctx, tmp_path):
        tracker = tmp_path / "tracker.txt"
        tracker.write_text("0")
        insights = tmp_path / "insights.json"
        failed = tmp_path / "failed.json"

        trades = [{
            "message_id": "100",
            "content": "Waited for C2 closure.",
            "images": [],
            "_folder_path": str(tmp_path)
        }]

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "response": '{"good_habits": ["Waited for C2"], "mistakes": []}'
        })

        with patch("cogs.trades.TRACKER_PATH", tracker), \
             patch("cogs.trades.INSIGHTS_PATH", insights), \
             patch("cogs.trades.FAILED_TRADES_PATH", failed), \
             patch("cogs.trades.RETRY_DELAY", 0), \
             patch("aiohttp.ClientSession") as mock_cls:

            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session.post = MagicMock(return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_resp),
                __aexit__=AsyncMock(return_value=False)
            ))
            mock_cls.return_value = mock_session

            await trades_cog.run_analysis_loop(ctx, trades, 0)

        assert insights.exists()
        data = json.loads(insights.read_text())
        assert "Waited for C2" in data["good_habits"]
        assert tracker.read_text() == "100"

    @pytest.mark.asyncio
    async def test_api_failure_uses_fallback(self, trades_cog, ctx, tmp_path):
        tracker = tmp_path / "tracker.txt"
        tracker.write_text("0")
        insights = tmp_path / "insights.json"
        failed = tmp_path / "failed.json"

        trades = [{
            "message_id": "100",
            "content": "Some trade.",
            "images": [],
            "_folder_path": str(tmp_path)
        }]

        mock_resp = AsyncMock()
        mock_resp.status = 500

        with patch("cogs.trades.TRACKER_PATH", tracker), \
             patch("cogs.trades.INSIGHTS_PATH", insights), \
             patch("cogs.trades.FAILED_TRADES_PATH", failed), \
             patch("cogs.trades.RETRY_DELAY", 0), \
             patch("aiohttp.ClientSession") as mock_cls:

            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session.post = MagicMock(return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_resp),
                __aexit__=AsyncMock(return_value=False)
            ))
            mock_cls.return_value = mock_session

            await trades_cog.run_analysis_loop(ctx, trades, 0)

        assert insights.exists()
        assert tracker.read_text() == "100"
        embed_call = ctx.send.call_args_list[-1]
        embed = embed_call.kwargs.get("embed") or embed_call[1].get("embed")
        assert embed is not None


# ──────────────────────────────────────────────
# Auto-sync listener tests
# ──────────────────────────────────────────────

class TestAutoSync:
    """Tests for the on_message auto-sync debounce feature."""

    @pytest.fixture
    def bot(self):
        bot = MagicMock()
        bot.loop = asyncio.get_event_loop()
        bot.loop.create_task = MagicMock()
        return bot

    @pytest.fixture
    def trades_cog(self, bot):
        return Trades(bot)

    def _make_message(self, *, is_bot=False, category_name="Fractal_Trades", guild_id=1):
        msg = MagicMock()
        msg.author.bot = is_bot
        msg.guild = MagicMock()
        msg.guild.id = guild_id
        msg.channel = MagicMock()
        msg.channel.category = MagicMock()
        msg.channel.category.name = category_name
        return msg

    @pytest.mark.asyncio
    async def test_ignores_bot_messages(self, trades_cog):
        """Bot messages should not trigger auto-sync."""
        msg = self._make_message(is_bot=True)
        await trades_cog.on_message(msg)
        trades_cog.bot.loop.create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_dm_messages(self, trades_cog):
        """DM messages (no guild) should not trigger auto-sync."""
        msg = self._make_message()
        msg.guild = None
        await trades_cog.on_message(msg)
        trades_cog.bot.loop.create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_non_fractal_channels(self, trades_cog):
        """Messages in other categories should not trigger auto-sync."""
        msg = self._make_message(category_name="General")
        await trades_cog.on_message(msg)
        trades_cog.bot.loop.create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_channels_without_category(self, trades_cog):
        """Messages in channels with no category should not trigger auto-sync."""
        msg = self._make_message()
        msg.channel.category = None
        await trades_cog.on_message(msg)
        trades_cog.bot.loop.create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_schedules_sync_on_fractal_message(self, trades_cog):
        """A message in Fractal_Trades schedules a delayed sync task."""
        msg = self._make_message()
        await trades_cog.on_message(msg)
        trades_cog.bot.loop.create_task.assert_called_once()
        assert msg.guild.id in trades_cog._pending_sync

    @pytest.mark.asyncio
    async def test_debounce_cancels_previous_task(self, trades_cog):
        """A second message cancels the first timer and starts a new one."""
        msg1 = self._make_message(guild_id=1)
        await trades_cog.on_message(msg1)

        first_task = trades_cog._pending_sync[1]

        msg2 = self._make_message(guild_id=1)
        await trades_cog.on_message(msg2)

        first_task.cancel.assert_called_once()
        assert trades_cog.bot.loop.create_task.call_count == 2

    @pytest.mark.asyncio
    async def test_make_auto_ctx(self, trades_cog):
        """_make_auto_ctx creates a context with guild, channel, and send."""
        channel = MagicMock()
        channel.guild = MagicMock()
        channel.send = AsyncMock()

        ctx = await trades_cog._make_auto_ctx(channel)

        assert ctx.guild == channel.guild
        assert ctx.channel == channel
        assert ctx.send == channel.send

    @pytest.mark.asyncio
    async def test_delayed_sync_cancelled(self, trades_cog):
        """_delayed_sync exits cleanly when cancelled (debounce reset)."""
        guild = MagicMock()
        channel = MagicMock()

        # Patch sleep to raise CancelledError (simulating debounce reset)
        with patch("cogs.trades.asyncio.sleep", side_effect=asyncio.CancelledError):
            await trades_cog._delayed_sync(guild, channel)

        # Should not crash — just return silently


# ──────────────────────────────────────────────
# Reflection feature tests
# ──────────────────────────────────────────────

class TestReflections:
    """Tests for the trade reflection feature."""

    @pytest.fixture
    def trades_cog(self):
        bot = MagicMock()
        bot.loop = MagicMock()
        return Trades(bot)

    # --- on_guild_channel_create ---

    @pytest.mark.asyncio
    async def test_channel_create_posts_reflection_prompt(self, trades_cog):
        """New channel under Fractal_Trades gets a reflection button message."""
        channel = AsyncMock(spec=discord.TextChannel)
        channel.name = "new_trade"
        channel.category = MagicMock()
        channel.category.name = "Fractal_Trades"

        await trades_cog.on_guild_channel_create(channel)

        channel.send.assert_called_once()
        call_kwargs = channel.send.call_args
        embed = call_kwargs.kwargs.get("embed") or call_kwargs[1].get("embed")
        view = call_kwargs.kwargs.get("view") or call_kwargs[1].get("view")
        assert embed is not None
        assert "New Trade Channel" in embed.title
        assert view is not None

    @pytest.mark.asyncio
    async def test_channel_create_ignores_other_categories(self, trades_cog):
        """Channels in other categories don't get reflection prompts."""
        channel = AsyncMock(spec=discord.TextChannel)
        channel.category = MagicMock()
        channel.category.name = "General"

        await trades_cog.on_guild_channel_create(channel)

        channel.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_channel_create_ignores_voice_channels(self, trades_cog):
        """Voice channels don't trigger reflections."""
        channel = MagicMock(spec=discord.VoiceChannel)
        channel.category = MagicMock()
        channel.category.name = "Fractal_Trades"

        await trades_cog.on_guild_channel_create(channel)

    # --- _get_reflections_context ---

    def test_reflections_context_with_file(self, trades_cog, tmp_path):
        """Returns formatted reflection text when reflections.json exists."""
        reflections = {
            "why_entered": "CISD confirmed on 5m",
            "how_felt": "Confident and patient",
            "what_learned": "Wait for C2 closure"
        }
        (tmp_path / "reflections.json").write_text(json.dumps(reflections))

        trade = {"_folder_path": str(tmp_path)}
        result = trades_cog._get_reflections_context(trade)

        assert "CISD confirmed on 5m" in result
        assert "Confident and patient" in result
        assert "Wait for C2 closure" in result

    def test_reflections_context_no_file(self, trades_cog, tmp_path):
        """Returns empty string when no reflections.json exists."""
        trade = {"_folder_path": str(tmp_path)}
        result = trades_cog._get_reflections_context(trade)
        assert result == ""

    def test_reflections_context_corrupt_file(self, trades_cog, tmp_path):
        """Returns empty string when reflections.json is corrupt."""
        (tmp_path / "reflections.json").write_text("not valid json{{{")
        trade = {"_folder_path": str(tmp_path)}
        result = trades_cog._get_reflections_context(trade)
        assert result == ""

    # --- ReflectionModal on_submit ---

    @pytest.mark.asyncio
    async def test_modal_saves_reflections(self, tmp_path):
        """ReflectionModal.on_submit saves responses to disk and sends embed."""
        modal = ReflectionModal(trades_dir=tmp_path, channel_name="test_trade")
        # Simulate filled-in form values
        modal.why_entered._value = "CISD confirmed"
        modal.how_felt._value = "Confident"
        modal.what_learned._value = "Be patient"

        interaction = AsyncMock()
        response_msg = AsyncMock()
        interaction.original_response = AsyncMock(return_value=response_msg)

        await modal.on_submit(interaction)

        # Check file was saved
        saved = tmp_path / "test_trade" / "reflections.json"
        assert saved.exists()
        data = json.loads(saved.read_text())
        assert data["why_entered"] == "CISD confirmed"
        assert data["how_felt"] == "Confident"
        assert data["what_learned"] == "Be patient"

        # Check embed was sent and pinned
        interaction.response.send_message.assert_called_once()
        response_msg.pin.assert_called_once()
