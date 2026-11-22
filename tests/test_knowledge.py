"""
Unit tests for cogs/knowledge.py
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path
import discord
from discord.ext import commands

from cogs.knowledge import Knowledge
from services.llm_service import LLMError


class TestKnowledgeCog:

    @pytest.fixture
    def bot(self):
        return MagicMock()

    @pytest.fixture
    def cog(self, bot):
        return Knowledge(bot)

    @pytest.fixture
    def interaction(self):
        interaction = AsyncMock()
        interaction.guild = MagicMock()
        interaction.channel = MagicMock()
        interaction.channel.name = "current_channel"
        interaction.response = AsyncMock()
        interaction.edit_original_response = AsyncMock()
        return interaction

    @pytest.mark.asyncio
    async def test_no_content(self, cog, interaction):
        target = MagicMock(spec=discord.TextChannel)
        target.name = "empty_channel"

        async def empty_history(**kwargs):
            return
            yield
        target.history = empty_history

        await cog.extract_knowledge.callback(cog, interaction, target_channel=target)
        interaction.edit_original_response.assert_called()
        
        # Check if any call has "No content"
        calls = str(interaction.edit_original_response.call_args_list)
        assert "No content" in calls

    @pytest.mark.asyncio
    async def test_successful_extraction(self, cog, interaction, tmp_path):
        target = MagicMock(spec=discord.TextChannel)
        target.name = "strategy_channel"

        msg = MagicMock()
        msg.content = "Use CISD for entries"
        msg.author.bot = False
        msg.author.name = "trader"
        msg.created_at.strftime.return_value = "2026-01-01"
        msg.attachments = []

        async def mock_history(**kwargs):
            yield msg
        target.history = mock_history

        with patch("cogs.knowledge.KB_DIR", tmp_path), \
             patch("cogs.knowledge.rag_service") as mock_rag, \
             patch("cogs.knowledge.llm_service.complete",
                   new=AsyncMock(return_value="# Strategy Article\nContent")):
            await cog.extract_knowledge.callback(cog, interaction, target_channel=target)

        article = tmp_path / "strategy_channel_article.md"
        assert article.exists()
        assert "Strategy Article" in article.read_text()
        mock_rag.index_markdown_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_llm_error_is_reported(self, cog, interaction):
        target = MagicMock(spec=discord.TextChannel)
        target.name = "test_ch"

        msg = MagicMock()
        msg.content = "Some content"
        msg.author.bot = False
        msg.author.name = "user"
        msg.created_at.strftime.return_value = "2026-01-01"
        msg.attachments = []

        async def mock_history(**kwargs):
            yield msg
        target.history = mock_history

        with patch("cogs.knowledge.llm_service.complete",
                   new=AsyncMock(side_effect=LLMError("DeepSeek HTTP 500"))):
            await cog.extract_knowledge.callback(cog, interaction, target_channel=target)

        assert any("AI Error" in str(c) for c in interaction.edit_original_response.call_args_list)
