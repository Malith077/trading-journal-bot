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
    def interaction(self):
        interaction = AsyncMock()
        interaction.guild = MagicMock()
        return interaction

    @pytest.mark.asyncio
    async def test_ping(self, cog, interaction):
        await cog.ping.callback(cog, interaction)
        interaction.response.send_message.assert_called_once_with("Pong! 🏓")

    @pytest.mark.asyncio
    async def test_list_channels(self, cog, interaction):
        ch1, ch2 = MagicMock(), MagicMock()
        ch1.name, ch2.name = "general", "trades"
        interaction.guild.text_channels = [ch1, ch2]
        await cog.list_channels.callback(cog, interaction)
        assert "2" in interaction.response.send_message.call_args[0][0]

    @pytest.mark.asyncio
    async def test_list_channels_empty(self, cog, interaction):
        interaction.guild.text_channels = []
        await cog.list_channels.callback(cog, interaction)
        assert "0" in interaction.response.send_message.call_args[0][0]

    @pytest.mark.asyncio
    async def test_help_builds_embed(self, cog, interaction):
        cmd = MagicMock()
        cmd.name = "test"
        cmd.description = "A test."
        cog.bot.tree.walk_commands.return_value = [cmd]
        
        await cog.custom_help.callback(cog, interaction)
        embed = interaction.response.send_message.call_args.kwargs.get("embed")
        assert embed.title == "📖 Command Reference"
        assert "A test." in embed.fields[0].value

    def test_removes_default_help(self, bot):
        General(bot)
        bot.remove_command.assert_called_once_with("help")
