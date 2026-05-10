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
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from cogs.trades import extract_json, Trades, MAX_RETRIES


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
