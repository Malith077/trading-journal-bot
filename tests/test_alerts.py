"""
Unit tests for cogs/alerts.py
"""
import pytest
import discord
from unittest.mock import AsyncMock, MagicMock, patch, ANY
from cogs.alerts import Alerts
from services.couchdb_service import couchdb_service


SAMPLE_ALERT = {
    "message": "🟢 BULLISH C2 on ES [5]",
    "ticker": "ES",
    "timeframe": "5",
    "signal": "BULLISH C2",
    "time_utc": "2026-05-16 04:30:00",
    "candle_signal": {"o": 5800, "h": 5820, "l": 5790, "c": 5815}
}


class TestAlertsCog:

    @pytest.fixture
    def bot(self):
        bot = MagicMock()
        guild = MagicMock()
        guild.name = "Test Guild"
        guild.create_text_channel = AsyncMock()
        channel = AsyncMock()
        channel.name = "trading_alerts"
        guild.text_channels = [channel]
        bot.guilds = [guild]
        return bot

    @pytest.fixture
    def cog(self, bot):
        couchdb_service.save_alert = AsyncMock()
        # Mock default bias so baseline tests don't drop the alert and colors remain default
        couchdb_service.get_bias_by_date = AsyncMock(return_value={"biases": {"ES": "Bullish", "GC": "Bearish"}, "narrative_confirmed": {"ES": True, "GC": True}})
        return Alerts(bot)

    @pytest.fixture
    def alerts_channel(self, bot):
        return bot.guilds[0].text_channels[0]

    @pytest.mark.asyncio
    async def test_valid_alert_posts_embed(self, cog, alerts_channel):
        await cog.on_tradingview_alert(SAMPLE_ALERT)

        alerts_channel.send.assert_called_once()
        embed = alerts_channel.send.call_args.kwargs["embed"]
        assert "BULLISH C2" in embed.title
        assert embed.description == SAMPLE_ALERT["message"]

    @pytest.mark.asyncio
    async def test_bullish_signal_green_embed(self, cog, alerts_channel):
        await cog.on_tradingview_alert(SAMPLE_ALERT)

        embed = alerts_channel.send.call_args.kwargs["embed"]
        assert embed.color == discord.Color.green()
        assert "🟢" in embed.title

    @pytest.mark.asyncio
    async def test_bearish_signal_red_embed(self, cog, alerts_channel):
        payload = {**SAMPLE_ALERT, "ticker": "GC", "signal": "BEARISH CISD", "message": "🔴 BEARISH CISD on GC [15]"}
        await cog.on_tradingview_alert(payload)

        embed = alerts_channel.send.call_args.kwargs["embed"]
        assert embed.color == discord.Color.red()
        assert "🔴" in embed.title

    @pytest.mark.asyncio
    async def test_neutral_signal_greyple_embed(self, cog, alerts_channel):
        payload = {**SAMPLE_ALERT, "signal": "NEUTRAL FLOW", "message": "NEUTRAL FLOW on ES [5]"}
        await cog.on_tradingview_alert(payload)

        embed = alerts_channel.send.call_args.kwargs["embed"]
        # Neutral signal opposes Bullish bias, so it gets the warning color
        assert embed.color == discord.Color.orange()
        assert "⚪" in embed.title

    @pytest.mark.asyncio
    async def test_embed_has_ohlc_field(self, cog, alerts_channel):
        await cog.on_tradingview_alert(SAMPLE_ALERT)

        embed = alerts_channel.send.call_args.kwargs["embed"]
        ohlc_field = next(f for f in embed.fields if f.name == "📊 Candle")
        assert "5800" in ohlc_field.value
        assert "5820" in ohlc_field.value

    @pytest.mark.asyncio
    async def test_embed_has_ticker_field(self, cog, alerts_channel):
        await cog.on_tradingview_alert(SAMPLE_ALERT)

        embed = alerts_channel.send.call_args.kwargs["embed"]
        ticker_field = next(f for f in embed.fields if f.name == "Ticker")
        assert "ES" in ticker_field.value

    @pytest.mark.asyncio
    async def test_embed_has_timeframe_field(self, cog, alerts_channel):
        await cog.on_tradingview_alert(SAMPLE_ALERT)

        embed = alerts_channel.send.call_args.kwargs["embed"]
        tf_field = next(f for f in embed.fields if f.name == "Timeframe")
        assert "5" in tf_field.value

    @pytest.mark.asyncio
    async def test_embed_has_footer(self, cog, alerts_channel):
        await cog.on_tradingview_alert(SAMPLE_ALERT)

        embed = alerts_channel.send.call_args.kwargs["embed"]
        assert embed.footer.text == "TradingView Alert • Fractal Model"

    @pytest.mark.asyncio
    async def test_empty_candle_no_ohlc_field(self, cog, alerts_channel):
        payload = {**SAMPLE_ALERT, "candle_signal": {}}
        await cog.on_tradingview_alert(payload)

        embed = alerts_channel.send.call_args.kwargs["embed"]
        field_names = [f.name for f in embed.fields]
        assert "📊 Candle" not in field_names

    @pytest.mark.asyncio
    async def test_missing_candle_signal_no_ohlc(self, cog, alerts_channel):
        payload = {**SAMPLE_ALERT}
        del payload["candle_signal"]
        await cog.on_tradingview_alert(payload)

        embed = alerts_channel.send.call_args.kwargs["embed"]
        field_names = [f.name for f in embed.fields]
        assert "📊 Candle" not in field_names

    @pytest.mark.asyncio
    async def test_missing_fields_handled(self, cog, alerts_channel):
        minimal = {"signal": "BULLISH C2"}
        # Ticker becomes N/A, so we must mock N/A as Bullish
        with patch("cogs.alerts.couchdb_service.get_bias_by_date", AsyncMock(return_value={"biases": {"N/A": "Bullish"}})):
            await cog.on_tradingview_alert(minimal)

        alerts_channel.send.assert_called_once()
        embed = alerts_channel.send.call_args.kwargs["embed"]
        assert "BULLISH C2" in embed.title

    @pytest.mark.asyncio
    async def test_channel_not_found_no_error(self, cog, bot):
        bot.guilds[0].text_channels = []
        with patch("cogs.alerts.couchdb_service.get_bias_by_date", AsyncMock(return_value={"biases": {"N/A": "Bullish"}})):
            await cog.on_tradingview_alert(SAMPLE_ALERT)

    @pytest.mark.asyncio
    async def test_multiple_guilds_sends_to_all(self, cog, bot):
        guild2 = MagicMock()
        guild2.name = "Guild 2"
        ch2 = AsyncMock()
        ch2.name = "trading_alerts"
        guild2.text_channels = [ch2]
        bot.guilds = [bot.guilds[0], guild2]

        await cog.on_tradingview_alert(SAMPLE_ALERT)
        assert ch2.send.called

    @pytest.mark.asyncio
    async def test_send_forbidden_handled(self, cog, bot):
        bot.guilds[0].text_channels[0].send = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "no perms"))
        await cog.on_tradingview_alert(SAMPLE_ALERT)

    @pytest.mark.asyncio
    async def test_send_http_exception_handled(self, cog, bot):
        bot.guilds[0].text_channels[0].send = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "server error"))
        await cog.on_tradingview_alert(SAMPLE_ALERT)

    @pytest.mark.asyncio
    async def test_create_channel_forbidden_handled(self, cog, bot):
        bot.guilds[0].text_channels = []
        bot.guilds[0].create_text_channel = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "no perms"))
        await cog.on_tradingview_alert(SAMPLE_ALERT)

    @pytest.mark.asyncio
    async def test_persists_to_couchdb(self, cog, alerts_channel):
        couchdb_service.save_alert = AsyncMock()
        await cog.on_tradingview_alert(SAMPLE_ALERT)
        couchdb_service.save_alert.assert_called_once_with(SAMPLE_ALERT)

    @pytest.mark.asyncio
    async def test_context_narrative_confirmed(self, cog, alerts_channel):
        payload = {**SAMPLE_ALERT, "timeframe": "60", "signal": "BULLISH CISD"}
        bias_data = {"biases": {"ES": "Bullish"}, "narrative_confirmed": {"ES": False}}
        
        with patch("cogs.alerts.couchdb_service.get_bias_by_date", AsyncMock(return_value=bias_data)):
            with patch("cogs.alerts.couchdb_service.update_narrative_confirmation", AsyncMock()) as mock_update:
                await cog.on_tradingview_alert(payload)
                
                embed = alerts_channel.send.call_args.kwargs["embed"]
                context_field = next(f for f in embed.fields if f.name == "🧠 Contextual Analysis")
                assert "NARRATIVE CONFIRMED" in context_field.value
                assert "⭐⭐⭐" in context_field.value
                mock_update.assert_called_once_with(ANY, "ES", True)

    @pytest.mark.asyncio
    async def test_context_high_probability_entry(self, cog, alerts_channel):
        payload = {**SAMPLE_ALERT, "timeframe": "5", "signal": "BULLISH C2"}
        bias_data = {"biases": {"ES": "Bullish"}, "narrative_confirmed": {"ES": True}}
        
        with patch("cogs.alerts.couchdb_service.get_bias_by_date", AsyncMock(return_value=bias_data)):
            await cog.on_tradingview_alert(payload)
            
            embed = alerts_channel.send.call_args.kwargs["embed"]
            context_field = next(f for f in embed.fields if f.name == "🧠 Contextual Analysis")
            assert "HIGH PROBABILITY ENTRY" in context_field.value
            assert "⭐⭐⭐" in context_field.value

    @pytest.mark.asyncio
    async def test_context_caution_unconfirmed_narrative(self, cog, alerts_channel):
        payload = {**SAMPLE_ALERT, "timeframe": "5", "signal": "BULLISH C2"}
        bias_data = {"biases": {"ES": "Bullish"}, "narrative_confirmed": {"ES": False}}
        
        with patch("cogs.alerts.couchdb_service.get_bias_by_date", AsyncMock(return_value=bias_data)):
            await cog.on_tradingview_alert(payload)
            
            embed = alerts_channel.send.call_args.kwargs["embed"]
            assert embed.color == discord.Color.orange()
            context_field = next(f for f in embed.fields if f.name == "🧠 Contextual Analysis")
            assert "CAUTION" in context_field.value
            assert "⭐⭐" in context_field.value

    @pytest.mark.asyncio
    async def test_context_counter_trend(self, cog, alerts_channel):
        payload = {**SAMPLE_ALERT, "timeframe": "5", "signal": "BEARISH C2"}
        bias_data = {"biases": {"ES": "Bullish"}, "narrative_confirmed": {"ES": True}}
        
        with patch("cogs.alerts.couchdb_service.get_bias_by_date", AsyncMock(return_value=bias_data)):
            await cog.on_tradingview_alert(payload)
            
            embed = alerts_channel.send.call_args.kwargs["embed"]
            assert embed.color == discord.Color.orange()
            context_field = next(f for f in embed.fields if f.name == "🧠 Contextual Analysis")
            assert "COUNTER-TREND" in context_field.value
            assert "⭐" in context_field.value

    @pytest.mark.asyncio
    async def test_context_no_bias_set_drops_alert(self, cog, alerts_channel):
        payload = {**SAMPLE_ALERT}
        bias_data = {"biases": {"ES": "Neutral"}, "narrative_confirmed": {"ES": False}}
        
        with patch("cogs.alerts.couchdb_service.get_bias_by_date", AsyncMock(return_value=bias_data)):
            await cog.on_tradingview_alert(payload)
            
            alerts_channel.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_bias_data_drops_alert(self, cog, alerts_channel):
        payload = {**SAMPLE_ALERT}
        
        with patch("cogs.alerts.couchdb_service.get_bias_by_date", AsyncMock(return_value=None)):
            await cog.on_tradingview_alert(payload)
            
            alerts_channel.send.assert_not_called()
