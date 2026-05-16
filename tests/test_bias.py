"""
Unit tests for cogs/bias.py
"""
import pytest
import discord
from unittest.mock import AsyncMock, MagicMock, patch
from cogs.bias import Bias, BiasView


class TestBiasView:

    def test_bias_view_embed_generation_bullish_neutral(self):
        assets = ["ES", "NQ"]
        state = {"ES": "Bullish", "NQ": "Neutral"}
        view = BiasView(assets, state)
        embed = view.create_embed()
        assert "**ES:** 🟢 Bullish" in embed.fields[0].value
        assert "**NQ:** ⚪ Neutral" in embed.fields[0].value

    def test_bias_view_embed_generation_bearish(self):
        assets = ["GC"]
        state = {"GC": "Bearish"}
        view = BiasView(assets, state)
        embed = view.create_embed()
        assert "**GC:** 🔴 Bearish" in embed.fields[0].value

    def test_bias_view_embed_has_title_and_footer(self):
        assets = ["ES"]
        state = {"ES": "Neutral"}
        view = BiasView(assets, state)
        embed = view.create_embed()
        assert embed.title == "📊 Daily Trading Bias"
        assert embed.footer.text == "Bias resets daily at 7 AM AEST"

    @pytest.mark.asyncio
    async def test_bias_update_bullish(self):
        assets = ["ES"]
        state = {"ES": "Neutral"}
        view = BiasView(assets, state)
        interaction = AsyncMock()
        with patch("cogs.bias.couchdb_service.save_bias", new_callable=AsyncMock) as mock_save:
            await view._update_bias(interaction, "Bullish")
            assert state["ES"] == "Bullish"
            mock_save.assert_called_once()

    @pytest.mark.asyncio
    async def test_bias_update_bearish(self):
        assets = ["NQ"]
        state = {"NQ": "Bullish"}
        view = BiasView(assets, state)
        interaction = AsyncMock()
        with patch("cogs.bias.couchdb_service.save_bias", new_callable=AsyncMock) as mock_save:
            await view._update_bias(interaction, "Bearish")
            assert state["NQ"] == "Bearish"
            mock_save.assert_called_once()

    @pytest.mark.asyncio
    async def test_bias_update_neutral(self):
        assets = ["YM"]
        state = {"YM": "Bullish"}
        view = BiasView(assets, state)
        interaction = AsyncMock()
        with patch("cogs.bias.couchdb_service.save_bias", new_callable=AsyncMock) as mock_save:
            await view._update_bias(interaction, "Neutral")
            assert state["YM"] == "Neutral"
            mock_save.assert_called_once()

    @pytest.mark.asyncio
    async def test_bias_view_persists_correct_payload_structure(self):
        assets = ["ES"]
        state = {"ES": "Bullish"}
        view = BiasView(assets, state)
        interaction = AsyncMock()
        with patch("cogs.bias.couchdb_service.save_bias", new_callable=AsyncMock) as mock_save:
            await view._update_bias(interaction, "Bearish")
            payload = mock_save.call_args[0][0]
            assert payload["date"] is not None
            assert payload["biases"] == {"ES": "Bearish"}
            assert "updated_at" in payload

    def test_internal_asset_select_logic(self):
        """Test the select logic directly without discord.py callback wrapping."""
        assets = ["ES", "NQ", "YM"]
        state = {a: "Neutral" for a in assets}
        view = BiasView(assets, state)
        assert view.selected_asset == "ES"

        # Simulate what asset_select does: update selected_asset and rebuild options
        view.selected_asset = "NQ"
        view._update_select_options()
        assert view.selected_asset == "NQ"


class TestBiasCog:

    @pytest.fixture
    def bot(self):
        bot = MagicMock()
        bot.guilds = []
        return bot

    @pytest.fixture
    def cog(self, bot):
        with patch("cogs.bias.Bias.bias_reset_task"):
            return Bias(bot)

    @pytest.mark.asyncio
    async def test_bias_view_embed_generation(self):
        """Original smoke test preserved."""
        assets = ["ES", "NQ"]
        state = {"ES": "Bullish", "NQ": "Neutral"}
        view = BiasView(assets, state)
        embed = view.create_embed()
        assert "**ES:** 🟢 Bullish" in embed.fields[0].value
        assert "**NQ:** ⚪ Neutral" in embed.fields[0].value

    @pytest.mark.asyncio
    async def test_bias_update_logic(self):
        """Original smoke test preserved."""
        assets = ["ES"]
        state = {"ES": "Neutral"}
        view = BiasView(assets, state)
        interaction = AsyncMock()
        with patch("cogs.bias.couchdb_service.save_bias", new_callable=AsyncMock) as mock_save:
            await view._update_bias(interaction, "Bullish")
            assert state["ES"] == "Bullish"
            mock_save.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_ready_creates_channel(self, bot, cog):
        guild = MagicMock()
        guild.text_channels = []
        guild.create_text_channel = AsyncMock()
        bot.guilds = [guild]
        with patch("cogs.bias.couchdb_service.get_bias_by_date", AsyncMock(return_value=None)):
            await cog.on_ready()
            guild.create_text_channel.assert_called_once_with(name="trading_bias")

    @pytest.mark.asyncio
    async def test_on_ready_channel_already_exists(self, bot, cog):
        guild = MagicMock()
        guild.name = "Test Guild"
        channel = AsyncMock()
        channel.name = "trading_bias"
        guild.text_channels = [channel]
        guild.create_text_channel = AsyncMock()
        bot.guilds = [guild]
        with patch("cogs.bias.couchdb_service.get_bias_by_date", AsyncMock(return_value=None)):
            await cog.on_ready()
            guild.create_text_channel.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_ready_with_existing_couchdb_data(self, bot, cog):
        guild = MagicMock()
        channel = AsyncMock()
        channel.name = "trading_bias"
        guild.text_channels = [channel]
        bot.guilds = [guild]
        existing = {"date": "2026-05-16", "biases": {"ES": "Bullish", "NQ": "Bearish"}}
        with patch("cogs.bias.couchdb_service.get_bias_by_date", AsyncMock(return_value=existing)):
            await cog.on_ready()

    @pytest.mark.asyncio
    async def test_on_ready_forbidden_handled(self, bot, cog):
        guild = MagicMock()
        guild.name = "Test Guild"
        guild.text_channels = []
        guild.create_text_channel = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "no perms"))
        bot.guilds = [guild]
        with patch("cogs.bias.couchdb_service.get_bias_by_date", AsyncMock(return_value=None)):
            await cog.on_ready()
            guild.create_text_channel.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_new_bias_prompt(self, bot, cog):
        channel = AsyncMock()
        await cog.send_new_bias_prompt(channel)
        channel.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_bias_prefix_command(self, bot):
        """Test !bias prefix command via the raw callback."""
        cog = Bias.__new__(Bias)
        cog.bot = bot
        cog.bias_reset_task = MagicMock()

        ctx = MagicMock()
        ctx.guild.text_channels = []
        target_channel = AsyncMock()
        target_channel.name = "trading_bias"
        target_channel.id = 12345
        target_channel.mention = "#trading_bias"
        ctx.guild.text_channels = [target_channel]
        ctx.channel.name = "general"
        ctx.channel.id = 99999
        ctx.send = AsyncMock()

        with patch.object(cog, "send_new_bias_prompt", AsyncMock()) as mock_send:
            await cog.bias_prefix_cmd.callback(cog, ctx)
            mock_send.assert_called_once_with(target_channel)

    @pytest.mark.asyncio
    async def test_bias_prefix_command_creates_channel(self, bot):
        """Test !bias creates channel when missing."""
        cog = Bias.__new__(Bias)
        cog.bot = bot
        cog.bias_reset_task = MagicMock()

        ctx = MagicMock()
        ctx.guild.text_channels = []
        new_channel = AsyncMock()
        new_channel.name = "trading_bias"
        new_channel.id = 12345
        new_channel.mention = "#trading_bias"
        ctx.guild.create_text_channel = AsyncMock(return_value=new_channel)
        ctx.channel.name = "general"
        ctx.channel.id = 99999
        ctx.send = AsyncMock()

        with patch.object(cog, "send_new_bias_prompt", AsyncMock()) as mock_send:
            await cog.bias_prefix_cmd.callback(cog, ctx)
            ctx.guild.create_text_channel.assert_called_once_with(name="trading_bias")
            mock_send.assert_called_once_with(new_channel)

    @pytest.mark.asyncio
    async def test_bias_prefix_command_forbidden(self, bot):
        """Test !bias handles Forbidden when creating channel."""
        cog = Bias.__new__(Bias)
        cog.bot = bot
        cog.bias_reset_task = MagicMock()

        ctx = MagicMock()
        ctx.guild.text_channels = []
        ctx.guild.create_text_channel = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "no perms"))
        ctx.send = AsyncMock()

        await cog.bias_prefix_cmd.callback(cog, ctx)
        ctx.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_bias_command_redirects_to_correct_channel(self, bot):
        cog = Bias.__new__(Bias)
        cog.bot = bot
        cog.bias_reset_task = MagicMock()

        interaction = AsyncMock()
        interaction.guild = MagicMock()
        target_channel = AsyncMock()
        target_channel.name = "trading_bias"
        target_channel.mention = "#trading_bias"
        interaction.guild.text_channels = [target_channel]

        with patch.object(cog, "send_new_bias_prompt", AsyncMock()) as mock_send:
            await cog.bias_cmd.callback(cog, interaction)
            mock_send.assert_called_once_with(target_channel)

    @pytest.mark.asyncio
    async def test_bias_command_creates_channel_if_missing(self, bot):
        cog = Bias.__new__(Bias)
        cog.bot = bot
        cog.bias_reset_task = MagicMock()

        interaction = AsyncMock()
        interaction.guild = MagicMock()
        interaction.guild.text_channels = []
        new_channel = AsyncMock()
        new_channel.name = "trading_bias"
        new_channel.mention = "#trading_bias"
        interaction.guild.create_text_channel = AsyncMock(return_value=new_channel)

        with patch.object(cog, "send_new_bias_prompt", AsyncMock()) as mock_send:
            await cog.bias_cmd.callback(cog, interaction)
            interaction.guild.create_text_channel.assert_called_once_with(name="trading_bias")
            mock_send.assert_called_once_with(new_channel)

    @pytest.mark.asyncio
    async def test_bias_command_forbidden(self, bot):
        cog = Bias.__new__(Bias)
        cog.bot = bot
        cog.bias_reset_task = MagicMock()

        interaction = AsyncMock()
        interaction.guild = MagicMock()
        interaction.guild.text_channels = []
        interaction.guild.create_text_channel = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "no perms"))

        await cog.bias_cmd.callback(cog, interaction)
        interaction.response.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_cog_unload_cancels_task(self, bot):
        mock_task = MagicMock()
        cog = Bias.__new__(Bias)
        cog.bot = bot
        cog.bias_reset_task = mock_task
        cog.cog_unload()
        mock_task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_bias_reset_task_calls_send_prompt(self, bot):
        guild = MagicMock()
        channel = AsyncMock()
        channel.name = "trading_bias"
        guild.text_channels = [channel]
        bot.guilds = [guild]

        cog = Bias.__new__(Bias)
        cog.bot = bot
        cog.bias_reset_task = MagicMock()

        with patch.object(cog, "send_new_bias_prompt", new_callable=AsyncMock) as mock_send:
            await Bias.bias_reset_task.coro(cog)
            mock_send.assert_called_once_with(channel)

    @pytest.mark.asyncio
    async def test_bias_reset_task_no_channel_skips(self, bot):
        guild = MagicMock()
        guild.text_channels = []
        bot.guilds = [guild]

        cog = Bias.__new__(Bias)
        cog.bot = bot

        with patch.object(cog, "send_new_bias_prompt", new_callable=AsyncMock) as mock_send:
            await Bias.bias_reset_task.coro(cog)
            mock_send.assert_not_called()
