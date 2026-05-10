"""
Unit tests for cogs/general.py
"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from cogs.general import General


class TestGeneralCog:

    @pytest.fixture
    def bot(self):
        bot = MagicMock()
        bot.remove_command = MagicMock()
        return bot

    @pytest.fixture
    def cog(self, bot):
        return General(bot)

    @pytest.fixture
    def ctx(self):
        ctx = AsyncMock()
        ctx.guild = MagicMock()
        return ctx

    @pytest.mark.asyncio
    async def test_ping(self, cog, ctx):
        await cog.ping.callback(cog, ctx)
        ctx.send.assert_called_once_with("Pong! 🏓")

    @pytest.mark.asyncio
    async def test_list_channels(self, cog, ctx):
        ch1, ch2 = MagicMock(), MagicMock()
        ch1.name, ch2.name = "general", "trades"
        ctx.guild.text_channels = [ch1, ch2]
        await cog.list_channels.callback(cog, ctx)
        assert "2" in ctx.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_list_channels_empty(self, cog, ctx):
        ctx.guild.text_channels = []
        await cog.list_channels.callback(cog, ctx)
        assert "0" in ctx.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_help_builds_embed(self, cog, ctx):
        cmd = MagicMock(name="test", help="A test.", hidden=False)
        cmd.name = "test"
        mock_cog = MagicMock()
        mock_cog.get_commands.return_value = [cmd]
        cog.bot.cogs = {"TestCog": mock_cog}
        await cog.custom_help.callback(cog, ctx)
        embed = ctx.send.call_args.kwargs.get("embed") or ctx.send.call_args[1].get("embed")
        assert embed.title == "📖 Command Reference"

    @pytest.mark.asyncio
    async def test_help_skips_hidden(self, cog, ctx):
        vis = MagicMock(name="vis", help="Visible.", hidden=False)
        vis.name = "vis"
        hid = MagicMock(name="hid", help="Hidden.", hidden=True)
        hid.name = "hid"
        mock_cog = MagicMock()
        mock_cog.get_commands.return_value = [vis, hid]
        cog.bot.cogs = {"C": mock_cog}
        await cog.custom_help.callback(cog, ctx)
        embed = ctx.send.call_args.kwargs.get("embed") or ctx.send.call_args[1].get("embed")
        assert "vis" in embed.fields[0].value
        assert "hid" not in embed.fields[0].value

    @pytest.mark.asyncio
    async def test_help_no_docstring(self, cog, ctx):
        cmd = MagicMock(name="x", help=None, hidden=False)
        cmd.name = "x"
        mock_cog = MagicMock()
        mock_cog.get_commands.return_value = [cmd]
        cog.bot.cogs = {"C": mock_cog}
        await cog.custom_help.callback(cog, ctx)
        embed = ctx.send.call_args.kwargs.get("embed") or ctx.send.call_args[1].get("embed")
        assert "No description" in embed.fields[0].value

    def test_removes_default_help(self, bot):
        General(bot)
        bot.remove_command.assert_called_once_with("help")
